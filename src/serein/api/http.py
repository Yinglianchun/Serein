"""Authenticated read-only host for an independently deployed memory service."""

import hmac
import asyncio
import os
import sqlite3
import logging
from pathlib import Path
from threading import Lock
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..application import Application
from ..core.reader import Reader


class RecallRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=12000)
    mode: Literal['surface', 'lookup'] = 'surface'
    method: Literal['semantic', 'lexical'] = 'semantic'
    min_cosine: float = Field(default=.5, ge=-1, le=1)
    limit: int = Field(default=5, ge=1, le=100)
    intent: Literal['direct', 'latest', 'progress', 'timeline', 'narrative', 'exact'] = 'direct'
    topic: str | None = None
    with_evidence: bool = False
    exclude_ids: list[str] = Field(default_factory=list, max_length=500)
    delivered_ids: list[str] = Field(default_factory=list, max_length=500)
    use_passages: bool | None = None


class ArcSearch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=12000)
    limit: int = Field(default=5, ge=1, le=100)


def create_app(settings, *, token: str, live: bool=False, token_file: Path | None=None):
    if settings.writable and not live:
        raise ValueError('The HTTP rehearsal requires runtime.writable=false')
    if live and not settings.writable:
        raise ValueError('The live HTTP service requires runtime.writable=true')
    if not token:
        raise ValueError('Set the HTTP bearer credential before starting the service')
    application = Application(settings)
    services = application.services
    from .mcp import create_server
    from starlette.requests import Request
    from starlette.routing import Route
    from .oauth import SCOPE, _external_origin, routes as oauth_routes
    mcp_server = create_server(application, http=True)
    mcp_transport = mcp_server.streamable_http_app()
    @asynccontextmanager
    async def lifespan(_app):
        from ..lifecycle import lifespan as application_lifespan
        async with application_lifespan(application):
            async with mcp_server.session_manager.run():
                yield

    app = FastAPI(title='Serein', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    active_token = token
    token_lock = Lock()

    def current_token():
        nonlocal active_token
        if token_file is None:
            return active_token
        with token_lock:
            try:
                latest = token_file.read_text('utf-8').strip()
            except (OSError, UnicodeError):
                latest = ''
            if not latest:
                raise HTTPException(503, 'Gateway Key is unavailable')
            if latest != active_token:
                oauth_store.bind_gateway_key(latest)
                active_token = latest
            return active_token

    oauth_router, oauth_store = oauth_routes(settings.database, token, current_gateway_key=current_token)
    app.include_router(oauth_router)

    class AuthenticatedMCP:
        async def __call__(self, scope, receive, send):
            from starlette.datastructures import Headers
            from urllib.parse import urlsplit
            headers = Headers(scope=scope)
            path = scope.get('path', '').rstrip('/')
            try:
                origin_url = _external_origin(Request(scope))
                resource = origin_url + path
                metadata_url = origin_url + '/.well-known/oauth-protected-resource' + path
            except ValueError:
                await JSONResponse({'detail':'Invalid public host'}, status_code=400)(scope, receive, send)
                return
            authorization = headers.get('authorization', '')
            supplied = authorization[7:] if authorization.startswith('Bearer ') else ''
            try:
                expected = 'Bearer ' + current_token()
            except HTTPException as error:
                await JSONResponse({'detail':error.detail}, status_code=error.status_code)(scope, receive, send)
                return
            static_ok = hmac.compare_digest(authorization.encode(), expected.encode())
            oauth_ok = bool(supplied) and oauth_store.valid_access_token(supplied, resource)
            if not static_ok and not oauth_ok:
                response = JSONResponse({'detail':'Authentication required'}, status_code=401,
                    headers={'WWW-Authenticate':f'Bearer resource_metadata="{metadata_url}", scope="{SCOPE}"'})
                await response(scope, receive, send)
                return
            # Gateway overwrites X-Forwarded-Host; direct core clients use Host.
            origin = headers.get('origin')
            try:
                origin_ok = not origin or urlsplit(origin).netloc == headers.get('x-forwarded-host', headers.get('host'))
            except ValueError:
                origin_ok = False
            if not origin_ok:
                await JSONResponse({'detail':'Invalid origin'}, status_code=403)(scope, receive, send)
                return
            await mcp_transport({**scope, 'path':'/mcp', 'raw_path':b'/mcp'}, receive, send)

    # Both public addresses share one transport and application lifecycle.
    for path in ('/serein/mcp', '/serein/mcp/', '/mcp', '/mcp/'):
        app.router.routes.append(Route(path, endpoint=AuthenticatedMCP()))

    def authorize(authorization: str = Header(default='')):
        expected = 'Bearer ' + current_token()
        if not hmac.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(401, 'Authentication required', headers={'WWW-Authenticate':'Bearer'})

    @app.exception_handler(ValueError)
    async def invalid_request(_request, exc):
        return JSONResponse(status_code=400, content={'detail':str(exc)})

    @app.exception_handler(sqlite3.Error)
    @app.exception_handler(OSError)
    async def storage_unavailable(request, exc):
        code = getattr(exc, 'sqlite_errorcode', 0) & 255
        reason, message = 'storage_unavailable', '记忆存储暂不可用，请稍后重试。'
        if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            reason, message = 'storage_busy', '记忆库正被其他写入占用，本次操作未完成，请稍后重试。'
        elif code == sqlite3.SQLITE_FULL or getattr(exc, 'errno', None) == 28:
            reason, message = 'storage_full', '服务器磁盘已满，无法保存，需要先清理服务器空间。'
        elif code in (sqlite3.SQLITE_READONLY, sqlite3.SQLITE_PERM) or isinstance(exc, PermissionError):
            reason, message = 'storage_read_only', '记忆库或目录没有写入权限，需要修复服务器权限。'
        elif code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
            reason, message = 'storage_corrupt', '记忆库无法正常读取，需要检查数据库。'
        elif code == sqlite3.SQLITE_CANTOPEN:
            reason, message = 'storage_cannot_open', '无法打开记忆库文件，需要检查服务器路径或权限。'
        elif code == sqlite3.SQLITE_IOERR:
            reason, message = 'storage_io_error', '记忆库读写发生磁盘错误，需要检查服务器存储。'
        logging.getLogger(__name__).error('Storage failure path=%s reason=%s sqlite=%s errno=%s',
            request.url.path, reason, getattr(exc, 'sqlite_errorname', ''), getattr(exc, 'errno', None))
        return JSONResponse(status_code=503, content={'status': 'error', 'reason': reason,
            'message': message, 'detail': 'Memory storage unavailable'})

    @app.get('/health')
    def health():
        with Reader(settings.database) as reader:
            schema = reader.store.conn.execute('PRAGMA user_version').fetchone()[0]
        return {'status':'ok', 'read_only':not live, 'schema':schema,
                'release':os.environ.get('SEREIN_RELEASE',''),
                'snapshot':os.environ.get('SEREIN_SNAPSHOT_ID','')}

    startup_lock = Lock()
    storage_checked = False

    @app.get('/live')
    def liveness():
        nonlocal storage_checked
        with startup_lock:
            if not storage_checked:
                health()
                storage_checked = True
        return {'status':'ok'}

    auth = [Depends(authorize)]
    from .appearance import routes as appearance_routes
    app.include_router(appearance_routes(settings, auth))
    from .export import routes as export_routes
    app.include_router(export_routes(settings, auth))
    from .extensions import routes as extension_routes
    app.include_router(extension_routes(application, auth))
    if live:
        from .companion import routes as companion_routes
        app.include_router(companion_routes(settings, auth))
        from .imports import routes as import_routes
        app.include_router(import_routes(settings,auth))
        from .migration import routes as migration_routes
        app.include_router(migration_routes(settings,auth))
        from .settings import routes as settings_routes
        app.include_router(settings_routes(settings, auth))
        from .chat import routes as model_routes
        app.include_router(model_routes(settings, services, auth))
        from .host import routes as host_routes
        app.include_router(host_routes(settings, auth))
        from .event_mailbox import routes as mailbox_routes
        app.include_router(mailbox_routes(settings, services, auth))
        from .personal import routes as personal_routes
        app.include_router(personal_routes(settings, auth))
        from .live import routes
        app.include_router(routes(settings, services, auth))
        from .raw_archive import routes as raw_routes
        app.include_router(raw_routes(settings, auth))
        from .pipeline_progress import routes as pipeline_progress_routes
        app.include_router(pipeline_progress_routes(settings, auth))
        from .notebook import routes as notebook_routes
        from .scenes import routes as scene_routes
        from .gateway import routes as gateway_routes
        from ..compat.germany.diary_store import DiaryLockedError, DiaryNotFoundError
        app.include_router(notebook_routes(settings, auth))
        app.include_router(scene_routes(settings, services, auth))
        app.include_router(gateway_routes(services, auth))
        from .narratives import routes as narrative_routes
        app.include_router(narrative_routes(settings, auth))
        from .history import routes as history_routes
        app.include_router(history_routes(settings, auth))
        from .relations import routes as relation_routes
        app.include_router(relation_routes(settings, auth))
        from ..compat.publication import publication_configured
        if any(publication_configured(settings,kind) for kind in ('semantic_recall_router','domain_recall_policy')):
            from .publication import routes as publication_routes
            app.include_router(publication_routes(settings,auth))

        @app.exception_handler(DiaryNotFoundError)
        async def diary_not_found(_request, _exc):
            return JSONResponse(status_code=404,content={'detail':'Diary not found'})

        @app.exception_handler(DiaryLockedError)
        async def diary_locked(_request, exc):
            return JSONResponse(status_code=423,content={'detail':'Diary is locked','unlock_at':exc.unlock_at})

    @app.get('/v1/capabilities', dependencies=auth)
    def capabilities():
        return {'read_only':not live, **application.capabilities(),
                'semantic_configured':bool(settings.embedding and settings.reranker),
                'records_injections':False}

    @app.get('/v1/memories/{identifier}', dependencies=auth)
    def read_memory(identifier: str, revision: int | None = Query(default=None, ge=1), with_evidence: bool = True):
        return services.read(identifier, revision=revision, with_evidence=with_evidence)

    @app.get('/v1/arcs/{identifier}/materials', dependencies=auth)
    def materials(identifier: str, offset: int = Query(default=0, ge=0), limit: int = Query(default=20, ge=1, le=100),
                  with_evidence: bool = False):
        return services.materials(identifier, offset=offset, limit=limit, with_evidence=with_evidence)

    @app.post('/v1/recall', dependencies=auth)
    def recall(request: RecallRequest):
        return services.recall(**request.model_dump())

    @app.post('/v1/arcs/search', dependencies=auth)
    def find_arc(request: ArcSearch):
        return services.find_arc(request.query, request.limit)

    return app
