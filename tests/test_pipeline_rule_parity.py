import asyncio
import copy

import pytest

from test_public_features import settings, ingest, output_for, raw_archive
from serein.extensions import pipeline as p
from serein.extensions.pipeline_admission import count_rounds
from serein.extensions.pipeline_audit import curator_receipt_errors
from serein.extensions.pipeline_images import MissingOriginalImage


@pytest.mark.parametrize('status', [404, 410, 503])
def test_download_only_classifies_gone_http_status_as_missing(monkeypatch, status):
    from urllib.parse import urlsplit
    from serein.extensions import pipeline_images as images
    monkeypatch.setattr(images, '_public_image_target', lambda url: (
        urlsplit(url), 80, [(None, None, None, None, ('93.184.216.34', 80))]))
    monkeypatch.setattr(images.socket, 'create_connection', lambda *args, **kwargs: object())
    class Connection:
        def __init__(self, *args, **kwargs): pass
        def request(self, *args): pass
        def getresponse(self): return type('Response', (), {'status': status})()
        def close(self): pass
    monkeypatch.setattr(images.http.client, 'HTTPConnection', Connection)
    expected = MissingOriginalImage if status in (404, 410) else ValueError
    with pytest.raises(expected) as caught:
        images.image_bytes('http://example.com/image.png')
    assert isinstance(caught.value, MissingOriginalImage) == (status in (404, 410))


def test_missing_image_does_not_drop_usable_sibling_and_is_not_refetched(settings, monkeypatch):
    from serein.extensions import pipeline_images as images
    uri = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII='
    calls = []
    def gone(url):
        calls.append(url)
        raise MissingOriginalImage('synthetic HTTP 410')
    monkeypatch.setattr(images, '_remote_image_bytes', gone)
    raw_archive(settings).ingest([
        {'source_event_id': 'u', 'session_id': 'one', 'role': 'user', 'text': 'Compare these covers',
         'created_at': '2025-01-01T00:00:00Z', 'metadata': {'attachments': [
             {'kind': 'image', 'url': 'https://example.com/gone.png'}, {'kind': 'image', 'url': uri}]}},
        {'source_event_id': 'a', 'session_id': 'one', 'role': 'assistant', 'text': 'The blue cover is useful',
         'created_at': '2025-01-01T00:01:00Z'}], source='test')
    async def runner(role, request):
        if request['execution']['task'] == 'image_transcription':
            assert request['images'][0]['position'] == 2
            return {'image_transcriptions': [{'input_image': 1, 'text': 'Blue cover', 'unreadable': False}]}
        return output_for(role, request)
    result = asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))
    assert result['events'] == 1 and len(result['missing_images']) == 1
    assert len(calls) == 1


@pytest.mark.parametrize('url', [None, 'missing-original', 'https://example.com/gone.png'])
def test_missing_original_skips_attachment_and_keeps_text(settings, monkeypatch, url):
    def gone(_):
        raise MissingOriginalImage('synthetic HTTP 404')
    monkeypatch.setattr('serein.extensions.pipeline_images.image_bytes', gone)
    raw_archive(settings).ingest([
        {'source_event_id': 'u', 'session_id': 'one', 'role': 'user', 'text': 'We chose the blue cover.',
         'created_at': '2025-01-01T00:00:00Z', 'metadata': {'attachments': [{'kind': 'image', 'url': url}]}},
        {'source_event_id': 'a', 'session_id': 'one', 'role': 'assistant', 'text': 'I will bind it.',
         'created_at': '2025-01-01T00:01:00Z'}], source='test')
    seen = []
    async def runner(role, request):
        seen.append(role)
        assert request['execution']['task'] != 'image_transcription'
        if role != 'track_router':
            assert request['images'] == []
            assert '原图缺失' in request['prompt']
        return output_for(role, request)
    result = asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))
    assert result['events'] == 1 and result['processed_originals'] == 2
    assert result['missing_images'][0]['reason'] == 'original_missing'
    assert seen == list(p.ROLES)
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))['status'] == 'current'


def test_temporary_download_failure_is_not_a_missing_original(settings, monkeypatch):
    raw_archive(settings).ingest([
        {'source_event_id': 'u', 'session_id': 'one', 'role': 'user', 'text': 'Read this picture',
         'created_at': '2025-01-01T00:00:00Z',
         'metadata': {'attachments': [{'kind': 'image', 'url': 'https://example.com/slow.png'}]}},
        {'source_event_id': 'a', 'session_id': 'one', 'role': 'assistant', 'text': 'Let us read it',
         'created_at': '2025-01-01T00:01:00Z'}], source='test')
    def timeout(_):
        raise TimeoutError('synthetic download timeout')
    monkeypatch.setattr('serein.extensions.pipeline_images.image_bytes', timeout)
    async def runner(role, request):
        return output_for(role, request)
    with pytest.raises(TimeoutError):
        asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))


def test_reply_to_other_user_does_not_count_as_an_owned_round():
    messages = [{'id': 1, 'session_id': 'one', 'role': 'user'},
                {'id': 2, 'session_id': 'one', 'role': 'assistant',
                 'metadata': {'reply_to_user_message_id': 99}}]
    assert count_rounds([1, 2], messages) == 0
    messages[1]['metadata']['reply_to_user_message_id'] = 1
    assert count_rounds([1, 2], messages) == 1
    messages[1]['metadata'] = {}
    assert count_rounds([1, 2], messages) == 1


def test_boundary_quote_recovery_remains_in_exclusive_cited_original():
    component = {'context_messages': [{'id': 1, 'content': 'We picked the blue shelf.'},
                                      {'id': 2, 'content': 'The lamp needs a new switch.'}],
                 'memberships': [], 'parked_context_source_ids': []}
    plan = {'events': [{'primary_track_id': 'home', 'source_bindings': [{'source_message_id': 1}]},
                       {'primary_track_id': 'home', 'source_bindings': [{'source_message_id': 2}]}],
            'skip_source_message_ids': [], 'defer_source_message_ids': []}
    review = {'events': [{'event_index': 0, 'reason': 'Shelf choice'}, {'event_index': 1, 'reason': 'Lamp repair'}],
              'boundaries': [{'left_event_index': 0, 'right_event_index': 1, 'reason': 'Different activities',
                              'evidence': [{'source_message_id': 1, 'quote': 'We picked blue shelf.'},
                                           {'source_message_id': 2, 'quote': 'new switch'}]}],
              'dispositions': []}
    assert curator_receipt_errors(review, plan, component) == []
    assert review['boundaries'][0]['evidence'][0]['quote'] == component['context_messages'][0]['content']
    wrong = copy.deepcopy(review)
    wrong['boundaries'][0]['evidence'][0]['quote'] = 'Unrelated mountains and oceans'
    assert curator_receipt_errors(wrong, plan, component)
    wrong = copy.deepcopy(review)
    wrong['boundaries'][0]['evidence'][1]['source_message_id'] = 1
    assert curator_receipt_errors(wrong, plan, component)


def test_failed_writer_resume_reuses_successful_router_and_curator(settings):
    ingest(settings)
    seen = []
    async def fails(role, request):
        seen.append(role)
        if role == 'event_writer':
            raise ValueError('synthetic Writer failure')
        return output_for(role, request)
    with pytest.raises(ValueError):
        asyncio.run(p.advance(settings.database, include_recent=True, runner=fails))
    resumed = []
    async def succeeds(role, request):
        resumed.append(role)
        return output_for(role, request)
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=succeeds))['events'] == 1
    assert resumed == ['event_writer']


def test_paused_writer_holds_its_chat_but_allows_another_and_resumes_frozen_step(settings):
    from serein.core.store import Store
    ingest(settings)
    async def fails(role, request):
        if role == 'event_writer':raise ValueError('synthetic broken writer')
        return output_for(role, request)
    for _ in range(2):
        with pytest.raises(ValueError):
            asyncio.run(p.advance(settings.database, include_recent=True, runner=fails))
    paused = asyncio.run(p.advance(settings.database, include_recent=True, runner=fails))
    assert paused['status'] == 'paused' and paused['failures'] == 3
    ingest(settings, 2)
    raw_archive(settings).ingest([
        {'source_event_id': 'other-u', 'session_id': 'other', 'role': 'user', 'text': 'Pick a notebook',
         'created_at': '2025-01-01T00:00:00Z'},
        {'source_event_id': 'other-a', 'session_id': 'other', 'role': 'assistant', 'text': 'Use the blue one',
         'created_at': '2025-01-01T00:01:00Z'}], source='test')
    async def succeeds(role, request):return output_for(role, request)
    result = asyncio.run(p.advance(settings.database, include_recent=True, runner=succeeds))
    assert result['events'] == 1 and result['batch_id'] != paused['batch_id']
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=succeeds))['status'] == 'current'
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_processing WHERE raw_id IN (1,2,3,4)').fetchone()[0] == 0
        assert store.conn.execute('SELECT count(*) FROM pipeline_jobs WHERE batch_id=? AND output_json IS NOT NULL',
                                  (paused['batch_id'],)).fetchone()[0] == 2
    p.retry_batch(settings.database, paused['batch_id'])
    resumed = []
    async def resume(role, request):
        resumed.append(role)
        return output_for(role, request)
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=resume))['events'] == 1
    assert resumed == ['event_writer']


def test_pause_budget_persists_api_attempts_and_retry_route_is_authenticated(settings, monkeypatch):
    from fastapi.testclient import TestClient
    from serein.api.http import create_app
    from serein.core.store import Store
    from serein.deployment import save_settings
    ingest(settings)
    save_settings(settings.database, {'pipeline': {'execution_mode': 'api'},
        'models': [{'id': 'local', 'model': 'synthetic', 'base_url': 'http://127.0.0.1:9/v1'}],
        'assignments': {role: 'local' for role in p.ROLES}})
    calls = []
    async def broken(model, payload):
        calls.append(1)
        return {'choices': [{'message': {'content': '{}'}}]}
    monkeypatch.setattr('serein.model_runtime.complete', broken)
    paused = asyncio.run(p.advance(settings.database, include_recent=True))
    assert paused['status'] == 'paused' and len(calls) == 3
    assert asyncio.run(p.advance(settings.database, include_recent=True))['status'] == 'current'
    assert len(calls) == 3
    with Store(settings.database, read_only=True) as store:
        attempts_before_retry = [dict(row) for row in store.conn.execute('SELECT * FROM pipeline_attempts ORDER BY id')]
    assert len(attempts_before_retry) == 3
    app = create_app(settings, token='test', live=True)
    assert TestClient(app).post('/v1/pipeline/retry-batch', json={'batch_id': paused['batch_id']}).status_code == 401
    client = TestClient(app, headers={'Authorization': 'Bearer test'})
    assert client.get('/v1/pipeline/status').json()['paused_batches'][0]['batch_id'] == paused['batch_id']
    assert client.post('/v1/pipeline/retry-batch', json={'batch_id': paused['batch_id']}).json()['status'] == 'resumed'
    assert len(calls) == 3  # Resuming resets the budget without calling the model.
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM pipeline_job_failures').fetchone()[0] == 0
        attempts = [dict(row) for row in store.conn.execute('SELECT * FROM pipeline_attempts ORDER BY id')]
    resets = [row for row in attempts if row['error'] == 'curator_omission_retry_reset']
    model_attempts = [row for row in attempts if row['error'] != 'curator_omission_retry_reset']
    assert len(model_attempts) == 3
    assert model_attempts == attempts_before_retry  # Preserve the original attempt history.
    assert len(resets) == 1
    assert resets[0]['output_text'] == ''
    assert resets[0]['job_id'] == attempts_before_retry[-1]['job_id']
    assert resets[0]['id'] > attempts_before_retry[-1]['id']


def test_continue_worker_moves_to_independent_chat_after_pausing_failed_batch(settings, monkeypatch):
    from serein.core.store import Store
    from serein.work_tasks import work
    ingest(settings)
    async def fails(role, request):
        if role == 'event_writer':raise ValueError('synthetic broken writer')
        return output_for(role, request)
    for _ in range(2):
        with pytest.raises(ValueError):
            asyncio.run(p.advance(settings.database, include_recent=True, runner=fails))
    with Store(settings.database, read_only=True) as store:
        bad = store.conn.execute('SELECT id FROM pipeline_batches').fetchone()[0]
    raw_archive(settings).ingest([
        {'source_event_id': 'other-u', 'session_id': 'other', 'role': 'user', 'text': 'Choose a notebook',
         'created_at': '2025-01-01T00:00:00Z'},
        {'source_event_id': 'other-a', 'session_id': 'other', 'role': 'assistant', 'text': 'Use the blue cover',
         'created_at': '2025-01-01T00:01:00Z'}], source='test')
    advance = p._advance
    results = []
    async def runner(role, request):
        if request['batch_id'] == bad and role == 'event_writer':raise ValueError('synthetic broken writer')
        return output_for(role, request)
    async def controlled(database, **kwargs):
        result = await advance(database, runner=runner, **kwargs)
        results.append(result['status'])
        return result
    monkeypatch.setattr(p, '_advance', controlled)
    result = asyncio.run(work(settings, 'pipeline', {'include_recent': True}))
    assert results == ['paused', 'processed', 'current']
    assert result['events'] == 1
