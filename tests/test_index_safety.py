"""Offline failure injection: never call a real provider."""
from dataclasses import replace
import json
import sqlite3
import threading

import httpx
import pytest

from serein.adapters.embedding import EmbeddingClient, EmbeddingTransientError
from serein.config import Settings
from serein.core import Store
from serein.recall.index import build_index
from serein.recall.index_safety import IndexBlocked, index_status, resume_index, guarded_index
from serein.recall.worker import update_pending
from serein.recall.vectors import fill_vectors


@pytest.fixture
def indexed(tmp_path):
    settings = Settings(tmp_path/'canonical.db', tmp_path/'index.db', writable=True)
    with Store(settings.database) as store:
        for i in range(18):
            store.create(f'e{i:02}', 'event', 'Synthetic event', f'Synthetic body {i}')
            store.conn.execute('INSERT INTO index_outbox(document_id) VALUES (?)', (f'e{i:02}',))
    build_index(settings.database, settings.index)
    profile = dict(model='synthetic', provider_host='synthetic.invalid', document_instruction='', query_instruction='', max_chars=6000)
    with sqlite3.connect(settings.index) as conn:
        conn.executemany('INSERT INTO settings VALUES (?,?)', [('embedding_profile', json.dumps(profile)), ('embedding_dimension', '2')])
    class Client:
        dimension = 2
        def __init__(self):
            self.profile = profile
            self.calls = []
            self.error = None
        def documents(self, texts):
            self.calls.append(list(texts))
            if self.error:
                raise self.error
            return [[1, 0] for _ in texts]
    return settings, Client()


def test_deterministic_pause_survives_new_writes_configuration_and_rebuild(indexed):
    settings, client = indexed
    client.error = ValueError('synthetic-key secret body https://private.invalid')
    with pytest.raises(IndexBlocked):
        update_pending(settings, client=client)
    before = index_status(settings.database)
    assert before['status'] == 'paused' and before['attempts'] == 1
    assert 'secret' not in json.dumps(before)
    with Store(settings.database) as store:
        store.create('new', 'event', 'New', 'Saved while paused')
        store.conn.execute('INSERT INTO index_outbox(document_id) VALUES (?)', ('new',))
    changed = replace(settings, index=settings.index.parent/'new-model.db')
    build_index(settings.database, changed.index)
    for call in (lambda: update_pending(changed, client=client), lambda: fill_vectors(settings, client=client)):
        with pytest.raises(IndexBlocked):
            call()
    assert len(client.calls) == 1
    assert index_status(settings.database)['pending'] == before['pending'] + 1
    assert index_status(settings.database)['attempts'] == 1


def test_transient_budget_backoff_retry_after_and_restart(indexed, monkeypatch):
    settings, client = indexed
    clock = [1000.0]
    monkeypatch.setattr('serein.recall.index_safety.time.time', lambda: clock[0])
    client.error = EmbeddingTransientError('private request', retry_after=30)
    for attempt in range(1, 4):
        with pytest.raises(IndexBlocked):
            update_pending(settings, client=client)
        state = index_status(settings.database)  # new connection, durable budget
        assert state['attempts'] == attempt
        assert len(client.calls) == attempt
        if attempt < 3:
            assert state['retry_at'] == clock[0] + 30
            with pytest.raises(IndexBlocked):
                update_pending(replace(settings), client=client)
            clock[0] = state['retry_at']
    for _ in range(3):
        with pytest.raises(IndexBlocked):
            update_pending(replace(settings), client=client)
    assert len(client.calls) == 3 and state['status'] == 'paused'
    assert state['pending'] > 0


def test_successful_batches_reused_after_explicit_recovery(indexed):
    settings, client = indexed
    original = client.documents
    def fail_second(texts):
        if client.calls:
            raise ValueError('bad index positions')
        return original(texts)
    client.documents = fail_second
    with pytest.raises(IndexBlocked):
        update_pending(settings, client=client)
    assert len(client.calls[0]) == 16
    pending = index_status(settings.database)['pending']
    token = index_status(settings.database)['recovery_token']
    resume_index(settings.database, token)
    with pytest.raises(IndexBlocked):
        resume_index(settings.database, token)
    assert index_status(settings.database)['pending'] == pending
    client.documents = original
    assert update_pending(settings, client=client)['updated'] == 18
    assert len(client.calls) == 2 and len(client.calls[1]) == 2
    assert index_status(settings.database)['pending'] == 0


def test_concurrent_worker_and_recovery_cannot_duplicate_requests(indexed):
    settings, client = indexed
    entered, release = threading.Event(), threading.Event()
    original = client.documents
    def blocked(texts):
        entered.set()
        assert release.wait(10)
        return original(texts)
    client.documents = blocked
    errors = []
    def run():
        try:
            update_pending(settings, client=client)
        except Exception as error:
            errors.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(10)
        state = index_status(settings.database)
        with pytest.raises(IndexBlocked):
            update_pending(settings, client=client)
        with pytest.raises(IndexBlocked):
            resume_index(settings.database, state['recovery_token'])
        assert index_status(settings.database)['attempts'] == 1
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and not errors
    assert len(client.calls) == 2  # two distinct batches, only one worker


def test_interrupted_request_is_not_replayed(indexed):
    settings, client = indexed
    @guarded_index
    def interrupted(settings):
        raise KeyboardInterrupt()  # death after reservation, before acknowledgement
    with pytest.raises(KeyboardInterrupt):
        interrupted(settings)
    assert index_status(settings.database)['status'] == 'running'
    with pytest.raises(IndexBlocked):
        update_pending(settings, client=client)
    state = index_status(settings.database)
    assert state['status'] == 'paused' and state['reason'] == 'interrupted_operation'
    assert state['attempts'] == 1 and not client.calls
    resume_index(settings.database, state['recovery_token'])
    assert update_pending(settings, client=client)['updated'] == 18


@pytest.mark.parametrize('code,transient', [(400,False),(401,False),(403,False),(404,False),(429,True),(500,True),(502,True),(503,True),(504,True),(501,False)])
def test_http_errors_classified_without_body_or_url(indexed, code, transient):
    settings, _ = indexed
    client = EmbeddingClient(settings.database, settings.index, 'https://synthetic.invalid/embeddings', api_key='synthetic-key')
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(code, text='secret body', headers={'Retry-After':'40'}))) as transport:
        with pytest.raises(ValueError) as caught:
            client.documents(['one', 'two'], client=transport)
    assert isinstance(caught.value, EmbeddingTransientError) is transient
    assert 'secret' not in str(caught.value) and 'synthetic.invalid' not in str(caught.value)
    if transient:
        assert caught.value.retry_after == 40


@pytest.mark.parametrize('payload', [{'model':'synthetic','data':[{'index':0,'embedding':[1,0]}]*2}, {'model':'synthetic','data':[{'embedding':[1,0]}]*2}, [], {'model':'synthetic','data':[None,None]}])
def test_strict_200_contract_failure_is_permanent(indexed, payload):
    settings, _ = indexed
    client = EmbeddingClient(settings.database, settings.index, 'https://synthetic.invalid/embeddings', api_key='')
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200,json=payload))) as transport:
        with pytest.raises(ValueError) as caught:
            client.documents(['one', 'two'], client=transport)
    assert not isinstance(caught.value, EmbeddingTransientError)


def test_status_and_recovery_are_authenticated_and_queue_safe(indexed):
    from fastapi.testclient import TestClient
    from serein.api.http import create_app
    settings, fake = indexed
    fake.error = ValueError('private body synthetic-key')
    with pytest.raises(IndexBlocked):
        update_pending(settings, client=fake)
    api = TestClient(create_app(settings, token='synthetic-auth', live=True))
    path = '/v1/settings/index-status'
    assert api.get(path).status_code == 401
    assert api.post('/v1/settings/resume-index', json={}).status_code == 401
    headers = {'Authorization':'Bearer synthetic-auth'}
    response = api.get(path, headers=headers)
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    assert 'synthetic-key' not in response.text and 'private body' not in response.text
    state = response.json()
    body = {'confirm':'RESUME_INDEX_EMBEDDING', 'recovery_token':state['recovery_token']}
    assert api.post('/v1/settings/resume-index', json={'recovery_token':state['recovery_token']}, headers=headers).status_code == 400
    assert api.post('/v1/settings/resume-index', json=body, headers=headers).status_code == 200
    assert api.post('/v1/settings/resume-index', json=body, headers=headers).status_code == 409
    assert index_status(settings.database)['pending'] == state['pending']
    assert len(fake.calls) == 1  # recovery itself makes no provider call
    from fastapi import FastAPI
    from serein.api.settings import routes
    readonly_app = FastAPI()
    readonly_app.include_router(routes(replace(settings, writable=False), []))
    readonly = TestClient(readonly_app)
    assert readonly.post('/v1/settings/resume-index', json=body).status_code == 403


def test_sync_index_does_not_acknowledge_paused_queue_without_embedding(indexed):
    from serein.application import Services
    settings, fake = indexed
    fake.error = ValueError('bad response')
    with pytest.raises(IndexBlocked):
        update_pending(settings, client=fake)
    pending = index_status(settings.database)['pending']
    result = Services(settings).sync_index()
    assert result['status'] == 'queued'
    assert index_status(settings.database)['pending'] == pending


@pytest.mark.parametrize('error_type', [httpx.ReadTimeout, httpx.ConnectError])
def test_timeout_and_network_errors_are_transient(indexed, error_type):
    settings, _ = indexed
    client = EmbeddingClient(settings.database, settings.index, 'https://synthetic.invalid/embeddings', api_key='')
    def handle(request):
        raise error_type('synthetic secret URL', request=request)
    with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
        with pytest.raises(EmbeddingTransientError) as caught:
            client.documents(['one'], client=transport)
    assert 'secret' not in str(caught.value)


def test_changing_entry_point_during_backoff_requires_recovery(indexed):
    settings, client = indexed
    client.error = EmbeddingTransientError('temporary')
    with pytest.raises(IndexBlocked):
        update_pending(settings, client=client)
    with pytest.raises(IndexBlocked):
        fill_vectors(settings, client=client)
    state = index_status(settings.database)
    assert state['status'] == 'paused' and state['attempts'] == 1
    assert state['reason'] == 'index_configuration_changed'
    assert len(client.calls) == 1
    resume_index(settings.database, state['recovery_token'])


def test_same_assignment_with_changed_model_cannot_clear_failed_budget(indexed):
    from serein.deployment import save_settings
    settings, _ = indexed
    model = dict(id='embedding', label='Synthetic', model='model-one', base_url='https://synthetic.invalid', api_key='')
    save_settings(settings.database, {'models':[model], 'assignments':{'embedding':'embedding'}})
    calls = []
    @guarded_index
    def operation(settings):
        calls.append(1)
        raise EmbeddingTransientError('temporary')
    with pytest.raises(IndexBlocked):
        operation(settings)
    save_settings(settings.database, {'models':[{**model, 'model':'model-two'}]})
    with pytest.raises(IndexBlocked):
        operation(settings)
    assert calls == [1]
    assert index_status(settings.database)['reason'] == 'index_configuration_changed'


def test_uninitialized_status_is_stable_and_read_only(indexed):
    settings, _ = indexed
    assert index_status(settings.database) == index_status(settings.database)
    with Store(settings.database, read_only=True) as store:
        assert not store.conn.execute("SELECT 1 FROM background_state WHERE name='embedding_index_safety'").fetchone()
