"""Eyes failures keep source evidence and permit explicit text-only chat."""
import json

import httpx
import pytest
from fastapi import HTTPException

from serein.core.store import Store
from test_event_handoff import PNG
from test_public_settings import deployment, configure


def setup(client):
    configure(client, operit_enabled=True).raise_for_status()
    client.patch('/v1/settings', json={'assignments': {'image_transcription': 'model-a'},
        'features': {'image_eyes': True}}).raise_for_status()


def send(client, urls, *, stream=False, text='Synthetic text question'):
    parts = ([{'type': 'text', 'text': text}] if text else []) + [
        {'type': 'image_url', 'image_url': {'url': url}} for url in urls]
    return client.post('/v1/chat/completions', json={'messages': [{'role': 'user', 'content': parts}],
        'stream': stream, 'serein': {'memory': False, 'window_id': 'synthetic-fallback'}})


def main_transport(monkeypatch, stream, seen):
    original = httpx.AsyncClient
    def handle(request):
        seen.append(json.loads(request.content))
        if stream:
            return httpx.Response(200, text='data: {"choices":[{"index":0,"delta":{"content":"Synthetic reply"}}]}\n\ndata: [DONE]\n\n')
        return httpx.Response(200, json={'choices': [{'message': {
            'role': 'assistant', 'content': 'Synthetic reply'}}]})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(
        **kw, transport=httpx.MockTransport(handle)))


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('cause', ['timeout', 'invalid_json', 'invalid_image'])
def test_read_failure_retains_original_and_sends_safe_text_only(deployment, monkeypatch, stream, cause):
    settings, client = deployment
    setup(client)
    async def transcribe(*args, **kw):
        if cause == 'timeout':
            raise httpx.ReadTimeout('PRIVATE_PROVIDER_ERROR')
        return {'choices': [{'message': {'content': 'PRIVATE_PROVIDER_ERROR'}}]}
    monkeypatch.setattr('serein.image_transcription.complete', transcribe)
    seen = []
    main_transport(monkeypatch, stream, seen)
    url = 'data:image/png;base64,!!!' if cause == 'invalid_image' else PNG
    response = send(client, [url], stream=stream)
    assert response.status_code == 200, response.text
    assert len(seen) == 1
    payload = json.dumps(seen[0])
    assert 'Synthetic text question' in payload and 'could not read' in payload
    assert 'PRIVATE_PROVIDER_ERROR' not in payload and url not in payload and 'image_url' not in payload
    with Store(settings.database, read_only=True) as store:
        raw = store.conn.execute("SELECT metadata_json,image_transcription_status FROM raw_events WHERE role='user'").fetchone()
        assert json.loads(raw['metadata_json'])['attachments'][0]['url'] == url
        assert raw['image_transcription_status'] == 'failed'
        observation = json.loads(store.conn.execute('SELECT payload_json FROM injection_debug ORDER BY id DESC LIMIT 1').fetchone()[0])
    assert observation['request_status'] == 'completed'
    assert observation['image_transcription'] == {'status': 'failed', 'fallback': 'text_only',
        'reason': 'image_read_failed', 'mode': 'eyes'}


def test_image_only_failure_never_claims_transcription_success(deployment, monkeypatch):
    _, client = deployment
    setup(client)
    seen = []
    main_transport(monkeypatch, False, seen)
    assert send(client, ['data:image/png;base64,!!!'], text='').status_code == 200
    payload = json.dumps(seen[0])
    assert 'No image transcription is available' in payload
    assert 'Eyes transcribed' not in payload and 'image_url' not in payload


def test_missing_image_model_falls_back_but_source_save_failure_does_not(deployment, monkeypatch):
    _, client = deployment
    setup(client)
    seen = []
    main_transport(monkeypatch, False, seen)
    async def missing(*args):
        raise HTTPException(409, 'Select an image transcription model in Settings')
    monkeypatch.setattr('serein.api.chat.transcribe_image_turn', missing)
    assert send(client, [PNG]).status_code == 200 and len(seen) == 1
    async def failed_archive(*args):
        raise HTTPException(500, 'Could not archive the source image message')
    monkeypatch.setattr('serein.api.chat.transcribe_image_turn', failed_archive)
    assert send(client, [PNG]).status_code == 500 and len(seen) == 1


def test_fallback_and_successful_rerolls_do_not_replay_each_others_context(deployment, monkeypatch):
    _, client = deployment
    setup(client)
    seen = []
    main_transport(monkeypatch, False, seen)
    fail = True
    async def transcribe(*args):
        if fail:
            raise HTTPException(502, 'Synthetic read failure')
        return 'KNOWN_SYNTHETIC_TRANSCRIPTION', {'status': 'complete', 'message_id': 1, 'images': 1}
    monkeypatch.setattr('serein.api.chat.transcribe_image_turn', transcribe)
    assert send(client, [PNG]).status_code == 200
    fail = False
    assert send(client, [PNG]).status_code == 200
    fail = True
    assert send(client, [PNG]).status_code == 200
    assert 'could not read' in json.dumps(seen[0]) and 'KNOWN_SYNTHETIC_TRANSCRIPTION' not in json.dumps(seen[0])
    assert 'KNOWN_SYNTHETIC_TRANSCRIPTION' in json.dumps(seen[1]) and 'could not read' not in json.dumps(seen[1])
    assert 'could not read' in json.dumps(seen[2]) and 'KNOWN_SYNTHETIC_TRANSCRIPTION' not in json.dumps(seen[2])


def test_partial_image_failure_keeps_success_for_a_later_retry(deployment, monkeypatch):
    settings, client = deployment
    setup(client)
    calls = []
    async def transcribe(*args, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise httpx.ReadTimeout('Synthetic second image failure')
        return {'choices': [{'message': {'content': json.dumps({'image_transcriptions': [
            {'input_image': 1, 'text': 'Synthetic first image', 'unreadable': False}]})}}]}
    monkeypatch.setattr('serein.image_transcription.complete', transcribe)
    seen = []
    main_transport(monkeypatch, False, seen)
    assert send(client, [PNG, PNG]).status_code == 200
    assert 'Synthetic first image' not in json.dumps(seen[0])
    with Store(settings.database, read_only=True) as store:
        raw = store.conn.execute("SELECT image_transcription_status,image_transcription_json FROM raw_events WHERE role='user'").fetchone()
    assert raw['image_transcription_status'] == 'failed'
    assert json.loads(raw['image_transcription_json'])['items'][0]['text'] == 'Synthetic first image'
