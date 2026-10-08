"""Explicit embedding calls using the preserved index profile and env credentials."""

import json
import os
from urllib.parse import urlparse

from serein.recall.index import Search, unit_vector


class EmbeddingPermanentError(ValueError):
    """Configuration, authentication or strict response contract failure."""


class EmbeddingTransientError(ValueError):
    def __init__(self, message, *, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def check_response(response):
    """No response body or request URL is included in errors."""
    if response.status_code == 200:
        return
    if response.status_code in (429, 500, 502, 503, 504):
        import math
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime
        delay = None
        raw = response.headers.get('Retry-After', '')
        try:
            delay = float(raw)
        except ValueError:
            try:
                delay = (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                pass
        if delay is not None and (not math.isfinite(delay) or delay < 0):
            delay = None
        raise EmbeddingTransientError(f'Embedding provider returned HTTP {response.status_code}', retry_after=delay)
    raise EmbeddingPermanentError(f'Embedding provider returned HTTP {response.status_code}; response body omitted')


class EmbeddingClient:
    def __init__(self, database, index, endpoint, api_key_env=None, api_key=None, tokenizer=None):
        with Search(database, index) as search:
            settings = dict(search.conn.execute("SELECT key,value FROM settings WHERE key LIKE 'embedding_%'"))
        if "embedding_profile" not in settings or "embedding_dimension" not in settings:
            raise EmbeddingPermanentError("Semantic query requires a configured embedding profile in the index")
        self.profile = json.loads(settings["embedding_profile"])
        self.dimension = json.loads(settings["embedding_dimension"])
        url = urlparse(endpoint)
        local_http = url.scheme == 'http' and url.hostname in ('localhost','127.0.0.1','::1')
        if (url.scheme != "https" and not local_http) or url.hostname != self.profile["provider_host"] or url.username or url.password:
            raise EmbeddingPermanentError("Embedding endpoint must use HTTPS and match the cached provider host")
        self.endpoint, self.api_key_env = endpoint, api_key_env
        self.api_key = api_key
        from .token_window import TokenWindow
        self.window = TokenWindow(tokenizer) if tokenizer else None

    def query(self, text, *, client=None):
        prepared = self.prepare(text, self.profile["query_instruction"])
        vector = self._request(prepared, 1, client=client)[0]
        return {"query": text, "profile": self.profile, "embedding": vector}

    def prepare(self, text, instruction, *, kind="Query"):
        if not text.strip() or not self.dimension:
            raise EmbeddingPermanentError("Text and a cached embedding dimension are required")
        prepared = f"Instruct: {instruction}\n{kind}: {text}" if instruction else text
        if self.window and not self.window.fits(prepared):
            raise EmbeddingPermanentError('Embedding input exceeds token window; prepare source passages first')
        if self.window and len(prepared) > self.profile['max_chars']:
            raise EmbeddingPermanentError('Embedding input exceeds character budget; prepare source passages first')
        return prepared[:self.profile['max_chars']]

    def documents(self, texts, *, client=None):
        """Embed body inputs in batch; response positions, not array order, own IDs."""
        if not texts:
            return []
        prepared = [self.prepare(text, self.profile["document_instruction"], kind="Document") for text in texts]
        return self._request(prepared, len(texts), client=client)

    def _request(self, inputs, count, *, client=None):
        import httpx
        key = self.api_key if self.api_key is not None else os.environ.get(self.api_key_env or '')
        if self.api_key is None and not key:
            raise EmbeddingPermanentError(f"Set the configured embedding credential environment variable: {self.api_key_env}")
        payload = {"model": self.profile["model"], "input": inputs, "encoding_format": "float"}
        owned = client is None
        client = client or httpx.Client(timeout=20, follow_redirects=False)
        try:
            response = client.post(self.endpoint, json=payload, headers={"Authorization": f"Bearer {key}"} if key else {})
            check_response(response)
            result = response.json()
            if result.get("model") != self.profile["model"] or len(result.get("data", [])) != count:
                raise EmbeddingPermanentError("Embedding provider returned a different model or unexpected number of vectors")
            vectors = {}
            for row in result["data"]:
                position = row.get("index", 0 if count == 1 else None)
                if type(position) is not int or not 0 <= position < count or position in vectors:
                    raise EmbeddingPermanentError("Embedding provider returned invalid or duplicate input positions")
                vectors[position] = unit_vector(row["embedding"], self.dimension)
            return [vectors[position] for position in range(count)]
        except (httpx.TimeoutException, httpx.NetworkError):
            raise EmbeddingTransientError("Embedding provider request timed out or connection failed") from None
        except httpx.HTTPError:
            raise EmbeddingPermanentError("Embedding provider request failed") from None
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
            if isinstance(error, (EmbeddingPermanentError, EmbeddingTransientError)):
                raise
            raise EmbeddingPermanentError("Embedding provider returned an invalid vector response") from None
        finally:
            if owned:
                client.close()
