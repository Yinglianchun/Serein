"""One durable, fail-closed budget for indexing and explicit preparation.

The OS lock covers request AND persistence, across threads/processes. Reserve an
attempt before work; an interrupted operation requires human recovery rather than
replaying a possibly billed request. Nested fills share the outer operation.
"""
from contextvars import ContextVar
from functools import wraps
import json
import time
from uuid import uuid4

from ..core.store import Store, encode
from ..file_lock import exclusive_lock

MAX_ATTEMPTS = 3
NAME = 'embedding_index_safety'
_active = ContextVar('embedding_index_operation', default=None)


class IndexBlocked(ValueError):
    pass


def _initial():
    return dict(status='ready', attempts=0, max_attempts=MAX_ATTEMPTS,
                retry_at=None, reason=None, recovery_token=None)


def _read(store):
    row = store.conn.execute('SELECT value_json FROM background_state WHERE name=?', (NAME,)).fetchone()
    return json.loads(row[0]) if row else _initial()


def _write(store, state):
    store.conn.execute('INSERT OR REPLACE INTO background_state VALUES (?,?)', (NAME, encode(state)))


def index_status(database):
    with Store(database, read_only=True) as store:
        state = _read(store)
        pending = store.conn.execute('SELECT count(*) FROM index_outbox').fetchone()[0]
    return {**state, 'pending': pending}


def _lock(database):
    return exclusive_lock(str(database.resolve()) + '.embedding-index.lock')


def resume_index(database, expected_token):
    try:
        with _lock(database), Store(database) as store:
            with store.transaction(immediate=True):
                state = _read(store)
                if state['recovery_token'] != expected_token or state['status'] not in ('paused', 'running'):
                    raise IndexBlocked('Index recovery state changed; refresh status')
                # Acquiring the OS lock proves a running marker is abandoned.
                state = _initial()
                _write(store, state)
                return state
    except RuntimeError:
        raise IndexBlocked('Index operation is busy; recovery did not change its budget') from None


def _target(settings):
    """Hash only non-secret identity; config changes cannot reset a failed budget."""
    import hashlib
    import sqlite3
    from ..deployment import read_settings
    public = read_settings(settings.database, public=True)
    assigned = public.get('assignments', {}).get('embedding')
    selected = []
    for upstream in public.get('upstreams', []):
        for entry in upstream.get('models', []):
            model = {'id': entry, 'upstream_model': entry} if isinstance(entry, str) else entry
            if model.get('id') == assigned:
                selected.append({
                    'base_url': upstream.get('base_url'),
                    'protocol': upstream.get('protocol'),
                    **{key: model.get(key) for key in ('id', 'upstream_model', 'dimension', 'query_instruction', 'document_instruction', 'tokenizer')},
                })
    profile = {}
    if settings.index and settings.index.is_file():
        try:
            with sqlite3.connect(settings.index.resolve().as_uri() + '?mode=ro', uri=True) as conn:
                profile = dict(conn.execute("SELECT key,value FROM settings WHERE key LIKE 'embedding_%'"))
        except sqlite3.Error:
            profile = {'invalid': True}
    return hashlib.sha256(encode([str(settings.index), settings.embedding.get('endpoint'), assigned, selected, profile]).encode()).hexdigest()


def guarded_index(function):
    @wraps(function)
    def run(settings, *args, **kwargs):
        database = settings.database.resolve()
        if _active.get() == database:
            return function(settings, *args, **kwargs)
        try:
            with _lock(database):
                with Store(database) as store:
                    state = _read(store)
                    if state['status'] == 'running':
                        state.update(status='paused', reason='interrupted_operation', retry_at=None)
                        _write(store, state)
                    if state['status'] == 'paused':
                        raise IndexBlocked('Indexing paused; inspect status and explicitly recover')
                    target = _target(settings)
                    if state['status'] == 'backoff' and (state.get('operation') != function.__module__ + '.' + function.__name__ or state.get('target') != target):
                        state.update(status='paused', reason='index_configuration_changed', retry_at=None)
                        _write(store, state)
                        raise IndexBlocked('A failed index operation requires recovery before another entry point')
                    if state['retry_at'] and time.time() < state['retry_at']:
                        raise IndexBlocked('Index retry backoff is active')
                    if state['attempts'] >= MAX_ATTEMPTS:
                        state.update(status='paused', reason='attempt_limit', retry_at=None)
                        _write(store, state)
                        raise IndexBlocked('Index attempt limit reached')
                    state.update(status='running', attempts=state['attempts'] + 1,
                                 operation=function.__module__ + '.' + function.__name__, target=target,
                                 recovery_token=uuid4().hex, retry_at=None)
                    _write(store, state)
                token = _active.set(database)
                try:
                    result = function(settings, *args, **kwargs)
                except Exception as error:
                    from ..adapters.embedding import EmbeddingTransientError
                    transient = isinstance(error, EmbeddingTransientError)
                    try:
                        import httpx
                        transient = transient or isinstance(error, (httpx.TimeoutException, httpx.NetworkError))
                    except ImportError:
                        pass
                    retry = transient and state['attempts'] < MAX_ATTEMPTS
                    delay = max(5 * 2 ** (state['attempts'] - 1), getattr(error, "retry_after", None) or 0) if transient else 0
                    state.update(status='backoff' if retry else 'paused',
                                 retry_at=time.time() + delay if retry else None,
                                 reason='transient_provider_error' if transient else 'index_configuration_or_contract_error')
                    with Store(database) as store:
                        _write(store, state)
                    # Never persist/log exception text, URLs, response bodies or inputs.
                    raise IndexBlocked('Index operation failed; ' + state['reason']) from None
                else:
                    with Store(database) as store:
                        _write(store, _initial())
                    return result
                finally:
                    _active.reset(token)
        except RuntimeError:
            raise IndexBlocked('Another index operation is active') from None
    return run
