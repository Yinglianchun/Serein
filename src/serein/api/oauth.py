"""Small OAuth 2.1 authorization server for remote MCP clients.

The existing Gateway Key remains a static bearer credential. OAuth credentials
are opaque, short lived, audience-bound tokens stored only as hashes in a
separate runtime database.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse


SCOPE = "serein:mcp"
ACCESS_TTL = 60 * 60
REFRESH_TTL = 30 * 24 * 60 * 60
CODE_TTL = 5 * 60
CLIENT_LIMIT = 1024


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _opaque(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def _json_response(payload: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code,
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def _oauth_error(error: str, description: str, status_code: int = 400) -> JSONResponse:
    return _json_response({"error": error, "error_description": description}, status_code)


def _external_origin(request: Request) -> str:
    forwarded_host = request.headers.get("x-forwarded-host")
    host = forwarded_host or request.headers.get("host", "")
    forwarded_proto = request.headers.get("x-forwarded-proto")
    scheme = forwarded_proto if forwarded_proto in {"http", "https"} else request.url.scheme
    try:
        parsed = urlsplit(f"{scheme}://{host}")
    except ValueError as exc:
        raise ValueError("Invalid public host") from exc
    if (scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("Invalid public host")
    return urlunsplit((scheme, parsed.netloc, "", "", ""))


def _secure_origin(origin: str) -> bool:
    parsed = urlsplit(origin)
    return parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in {
        "localhost", "127.0.0.1", "::1", "testserver",
    })


def _normalize_resource(value: str, origin: str) -> str:
    try:
        parsed = urlsplit(value)
        expected = urlsplit(origin)
    except ValueError as exc:
        raise ValueError("Invalid resource") from exc
    path = parsed.path.rstrip("/") or "/"
    if (parsed.scheme.lower(), parsed.netloc.lower()) != (expected.scheme.lower(), expected.netloc.lower()):
        raise ValueError("Resource must use this Serein origin")
    if parsed.query or parsed.fragment or parsed.username or parsed.password or path not in {"/serein/mcp", "/mcp"}:
        raise ValueError("Resource must be a Serein MCP endpoint")
    return urlunsplit((expected.scheme, expected.netloc, path, "", ""))


def _redirect_uri(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("Invalid redirect_uri")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError("Invalid redirect_uri") from exc
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback)) or not parsed.hostname:
        raise ValueError("redirect_uri must use HTTPS or a loopback HTTP address")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Invalid redirect_uri")
    return value


def _append_query(uri: str, values: dict[str, str]) -> str:
    parsed = urlsplit(uri)
    query = parsed.query + ("&" if parsed.query else "") + urlencode(values)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


class OAuthStore:
    def __init__(self, database: Path):
        self.path = database.with_name(database.stem + ".oauth.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    client_id TEXT PRIMARY KEY,
                    client_name TEXT NOT NULL,
                    redirect_uris TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_codes (
                    code_hash TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_grants (
                    access_hash TEXT PRIMARY KEY,
                    access_expires_at INTEGER NOT NULL,
                    refresh_hash TEXT UNIQUE NOT NULL,
                    refresh_expires_at INTEGER NOT NULL,
                    client_id TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    family_id TEXT NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS oauth_access_expiry ON oauth_grants(access_expires_at);
                CREATE INDEX IF NOT EXISTS oauth_refresh_expiry ON oauth_grants(refresh_expires_at);
                CREATE INDEX IF NOT EXISTS oauth_family ON oauth_grants(family_id);
                CREATE TABLE IF NOT EXISTS oauth_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)

    def bind_gateway_key(self, gateway_key: str) -> None:
        fingerprint = _digest(gateway_key)
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM oauth_meta WHERE key='gateway_key_hash'").fetchone()
            if row and not hmac.compare_digest(row["value"], fingerprint):
                conn.execute("DELETE FROM oauth_codes")
                conn.execute("DELETE FROM oauth_grants")
            conn.execute("INSERT INTO oauth_meta(key,value) VALUES ('gateway_key_hash',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (fingerprint,))

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def register(self, client_name: str, redirect_uris: list[str]) -> tuple[str, int]:
        now = int(time.time())
        client_id = _opaque("scl_")
        with self._connect() as conn:
            conn.execute("DELETE FROM oauth_codes WHERE expires_at<=?", (now,))
            conn.execute("DELETE FROM oauth_grants WHERE refresh_expires_at<=?", (now,))
            # DCR is intentionally unauthenticated. Remove stale registrations that
            # never reached authorization so they cannot permanently exhaust the cap.
            conn.execute("""DELETE FROM oauth_clients WHERE created_at<?
                AND NOT EXISTS (SELECT 1 FROM oauth_codes c WHERE c.client_id=oauth_clients.client_id)
                AND NOT EXISTS (SELECT 1 FROM oauth_grants g WHERE g.client_id=oauth_clients.client_id)""",
                         (now - 24 * 60 * 60,))
            count = conn.execute("SELECT count(*) FROM oauth_clients").fetchone()[0]
            if count >= CLIENT_LIMIT:
                remove = count - CLIENT_LIMIT + 1
                conn.execute("""DELETE FROM oauth_clients WHERE client_id IN (
                    SELECT client_id FROM oauth_clients c
                    WHERE NOT EXISTS (SELECT 1 FROM oauth_codes WHERE client_id=c.client_id)
                      AND NOT EXISTS (SELECT 1 FROM oauth_grants WHERE client_id=c.client_id)
                    ORDER BY created_at LIMIT ?)""", (remove,))
                count = conn.execute("SELECT count(*) FROM oauth_clients").fetchone()[0]
                if count >= CLIENT_LIMIT:
                    raise ValueError("OAuth client registration limit reached")
            conn.execute("INSERT INTO oauth_clients VALUES (?,?,?,?)",
                         (client_id, client_name, json.dumps(redirect_uris), now))
        return client_id, now

    def client(self, client_id: str):
        with self._connect() as conn:
            return conn.execute("SELECT * FROM oauth_clients WHERE client_id=?", (client_id,)).fetchone()

    def create_code(self, *, client_id: str, redirect_uri: str, code_challenge: str,
                    resource: str, scope: str) -> str:
        code = _opaque("sac_")
        now = int(time.time())
        with self._connect() as conn:
            conn.execute("DELETE FROM oauth_codes WHERE expires_at<=?", (now,))
            conn.execute("INSERT INTO oauth_codes VALUES (?,?,?,?,?,?,?)", (
                _digest(code), client_id, redirect_uri, code_challenge, resource, scope, now + CODE_TTL,
            ))
        return code

    def exchange_code(self, *, code: str, client_id: str, redirect_uri: str,
                      verifier: str, resource: str | None) -> dict | None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM oauth_codes WHERE code_hash=?", (_digest(code),)).fetchone()
            if not row:
                return None
            # A code is single-use even when an exchange attempt has bad parameters.
            conn.execute("DELETE FROM oauth_codes WHERE code_hash=?", (_digest(code),))
            if (row["expires_at"] <= now or row["client_id"] != client_id
                    or row["redirect_uri"] != redirect_uri or (resource and row["resource"] != resource)
                    or not _valid_verifier(verifier, row["code_challenge"])):
                return None
            return self._issue(conn, client_id, row["resource"], row["scope"], _opaque("fam_"), now)

    def refresh(self, *, refresh_token: str, client_id: str, resource: str | None) -> dict | None:
        now = int(time.time())
        token_hash = _digest(refresh_token)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM oauth_grants WHERE refresh_hash=?", (token_hash,)).fetchone()
            if not row:
                return None
            if row["revoked"]:
                conn.execute("UPDATE oauth_grants SET revoked=1 WHERE family_id=?", (row["family_id"],))
                return None
            if (row["refresh_expires_at"] <= now or row["client_id"] != client_id
                    or (resource and row["resource"] != resource)):
                conn.execute("UPDATE oauth_grants SET revoked=1 WHERE access_hash=?", (row["access_hash"],))
                return None
            conn.execute("UPDATE oauth_grants SET revoked=1 WHERE access_hash=?", (row["access_hash"],))
            return self._issue(conn, client_id, row["resource"], row["scope"], row["family_id"], now)

    def _issue(self, conn, client_id: str, resource: str, scope: str, family_id: str, now: int) -> dict:
        access = _opaque("sat_")
        refresh = _opaque("srt_")
        conn.execute("INSERT INTO oauth_grants VALUES (?,?,?,?,?,?,?,?,?,?)", (
            _digest(access), now + ACCESS_TTL, _digest(refresh), now + REFRESH_TTL,
            client_id, resource, scope, family_id, 0, now,
        ))
        return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL,
                "refresh_token": refresh, "scope": scope}

    def valid_access_token(self, token: str, resource: str) -> bool:
        now = int(time.time())
        with self._connect() as conn:
            row = conn.execute("SELECT access_expires_at,resource,scope,revoked FROM oauth_grants "
                               "WHERE access_hash=?", (_digest(token),)).fetchone()
        return bool(row and not row["revoked"] and row["access_expires_at"] > now
                    and row["resource"] == resource and row["scope"] == SCOPE)


def _valid_verifier(verifier: str, challenge: str) -> bool:
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    if not (43 <= len(verifier) <= 128) or any(c not in allowed for c in verifier):
        return False
    actual = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return hmac.compare_digest(actual, challenge)


def _authorization_params(values: dict[str, str], store: OAuthStore, origin: str) -> dict[str, str]:
    required = ("client_id", "redirect_uri", "state", "code_challenge", "resource")
    if any(not values.get(name) or len(values[name]) > 2048 for name in required):
        raise ValueError("Missing or invalid authorization parameter")
    if values.get("response_type") != "code":
        raise ValueError("Only response_type=code is supported")
    challenge = values["code_challenge"]
    if (values.get("code_challenge_method") != "S256" or len(challenge) != 43
            or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in challenge)):
        raise ValueError("PKCE S256 is required")
    if len(values["state"]) > 1024:
        raise ValueError("Invalid state")
    client = store.client(values["client_id"])
    if not client:
        raise ValueError("Unknown OAuth client")
    redirect_uri = _redirect_uri(values["redirect_uri"])
    if redirect_uri not in json.loads(client["redirect_uris"]):
        raise ValueError("redirect_uri does not exactly match the registered URI")
    scope = values.get("scope") or SCOPE
    if scope != SCOPE:
        raise ValueError("Unsupported scope")
    resource = _normalize_resource(values["resource"], origin)
    return {**values, "redirect_uri": redirect_uri, "scope": scope, "resource": resource,
            "client_name": client["client_name"]}


async def _form(request: Request) -> dict[str, str]:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
        raise ValueError("Form encoded request required")
    body = await request.body()
    if len(body) > 16_384:
        raise ValueError("Request too large")
    parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True)
    if any(len(values) != 1 for values in parsed.values()):
        raise ValueError("Duplicate form parameter")
    return {key: values[0] for key, values in parsed.items()}


def routes(database: Path, gateway_key: str, *, current_gateway_key=None) -> tuple[APIRouter, OAuthStore]:
    router = APIRouter()
    store = OAuthStore(database)
    store.bind_gateway_key(gateway_key)
    read_key = current_gateway_key or (lambda: gateway_key)

    def metadata(request: Request, resource_path: str = "/serein/mcp"):
        try:
            origin = _external_origin(request)
        except ValueError as exc:
            return _oauth_error("invalid_request", str(exc))
        resource = origin + resource_path
        return _json_response({"resource": resource, "authorization_servers": [origin],
                               "scopes_supported": [SCOPE],
                               "bearer_methods_supported": ["header"]})

    @router.get("/.well-known/oauth-protected-resource")
    def protected_root(request: Request):
        return metadata(request)

    @router.get("/.well-known/oauth-protected-resource/serein/mcp")
    def protected_serein(request: Request):
        return metadata(request)

    @router.get("/.well-known/oauth-protected-resource/mcp")
    def protected_legacy(request: Request):
        return metadata(request, "/mcp")

    @router.get("/.well-known/oauth-authorization-server")
    def authorization_metadata(request: Request):
        try:
            origin = _external_origin(request)
        except ValueError as exc:
            return _oauth_error("invalid_request", str(exc))
        return _json_response({
            "issuer": origin,
            "authorization_endpoint": origin + "/authorize",
            "token_endpoint": origin + "/token",
            "registration_endpoint": origin + "/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": [SCOPE],
        })

    @router.post("/register")
    async def register(request: Request):
        try:
            origin = _external_origin(request)
            if not _secure_origin(origin):
                raise ValueError("OAuth registration requires HTTPS")
            if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise ValueError("JSON request required")
            body = await request.body()
            if len(body) > 16_384:
                raise ValueError("Request too large")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("JSON object required")
            redirects = payload.get("redirect_uris")
            if not isinstance(redirects, list) or not 1 <= len(redirects) <= 10:
                raise ValueError("redirect_uris must contain 1 to 10 entries")
            redirects = [_redirect_uri(value) for value in redirects]
            if len(set(redirects)) != len(redirects):
                raise ValueError("redirect_uris must be unique")
            if payload.get("token_endpoint_auth_method", "none") != "none":
                return _oauth_error("invalid_client_metadata", "Only public clients are supported")
            grants = payload.get("grant_types", ["authorization_code", "refresh_token"])
            responses = payload.get("response_types", ["code"])
            if grants != ["authorization_code", "refresh_token"] and set(grants) != {"authorization_code", "refresh_token"}:
                return _oauth_error("invalid_client_metadata", "authorization_code and refresh_token are required")
            if responses != ["code"]:
                return _oauth_error("invalid_client_metadata", "Only response_type code is supported")
            name = str(payload.get("client_name") or "MCP client").strip()[:120]
            client_id, issued_at = store.register(name, redirects)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return _oauth_error("invalid_client_metadata", str(exc))
        return _json_response({"client_id": client_id, "client_id_issued_at": issued_at,
                               "client_name": name, "redirect_uris": redirects,
                               "grant_types": ["authorization_code", "refresh_token"],
                               "response_types": ["code"], "token_endpoint_auth_method": "none"}, 201)

    @router.get("/authorize")
    def authorize_page(request: Request):
        try:
            origin = _external_origin(request)
            if not _secure_origin(origin):
                raise ValueError("OAuth authorization requires HTTPS")
            keys = [key for key, _value in request.query_params.multi_items()]
            if len(keys) != len(set(keys)):
                raise ValueError("Duplicate authorization parameter")
            params = _authorization_params(dict(request.query_params), store, origin)
        except ValueError as exc:
            return _oauth_error("invalid_request", str(exc))
        hidden = "".join(f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(value, quote=True)}">'
                         for key, value in params.items() if key != "client_name")
        page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width"><title>授权 Serein MCP</title></head>
<body><main><h1>授权 Serein MCP</h1><p><strong>{html.escape(params['client_name'])}</strong>
请求访问这台 Serein 实例。授权后可使用实例当前开放的 MCP 工具。</p>
<p>回调地址：<code>{html.escape(params['redirect_uri'])}</code></p>
<form method="post" action="/authorize">{hidden}
<label>Gateway Key <input name="gateway_key" type="password" required autocomplete="current-password"></label>
<button type="submit">确认授权</button></form></main></body></html>"""
        return HTMLResponse(page, headers={"Cache-Control": "no-store", "Pragma": "no-cache",
            "Content-Security-Policy": "default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"})

    @router.post("/authorize")
    async def authorize_submit(request: Request):
        try:
            origin = _external_origin(request)
            if not _secure_origin(origin):
                raise ValueError("OAuth authorization requires HTTPS")
            values = await _form(request)
            supplied_key = values.pop("gateway_key", "")
            params = _authorization_params(values, store, origin)
            if not hmac.compare_digest(supplied_key.encode(), read_key().encode()):
                return HTMLResponse("Gateway Key 不正确。", status_code=401,
                                    headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
            code = store.create_code(client_id=params["client_id"], redirect_uri=params["redirect_uri"],
                                     code_challenge=params["code_challenge"], resource=params["resource"],
                                     scope=params["scope"])
            location = _append_query(params["redirect_uri"], {"code": code, "state": params["state"]})
        except ValueError as exc:
            return _oauth_error("invalid_request", str(exc))
        return RedirectResponse(location, status_code=303, headers={"Cache-Control": "no-store"})

    @router.post("/token")
    async def token_endpoint(request: Request):
        if request.headers.get("authorization"):
            return _oauth_error("invalid_client", "Public clients must not send client credentials", 401)
        try:
            values = await _form(request)
            origin = _external_origin(request)
            client_id = values.get("client_id", "")
            resource_value = values.get("resource")
            resource = _normalize_resource(resource_value, origin) if resource_value else None
            if not store.client(client_id):
                return _oauth_error("invalid_client", "Unknown OAuth client", 401)
            read_key()  # Revoke old grants before exchanging or refreshing tokens.
            grant_type = values.get("grant_type")
            if grant_type == "authorization_code":
                redirect_uri = _redirect_uri(values.get("redirect_uri", ""))
                result = store.exchange_code(code=values.get("code", ""), client_id=client_id,
                    redirect_uri=redirect_uri, verifier=values.get("code_verifier", ""), resource=resource)
            elif grant_type == "refresh_token":
                result = store.refresh(refresh_token=values.get("refresh_token", ""),
                                       client_id=client_id, resource=resource)
            else:
                return _oauth_error("unsupported_grant_type", "Unsupported grant_type")
        except ValueError as exc:
            return _oauth_error("invalid_request", str(exc))
        if not result:
            return _oauth_error("invalid_grant", "Grant is invalid, expired, or already used")
        return _json_response(result)

    return router, store
