from uuid import uuid4

import asyncio
import json
import httpx
import pytest

from test_live_clients import live
from test_live_narratives import seed
from serein.api.narratives import endpoints
from serein.compat.narratives import narrative_transaction
from serein.compat.narrative_theme import material_rows, source_receipt
from serein.deployment import save_settings


def configure_scout(settings):
    save_settings(settings.database, {'models': [{'id': 'scout', 'model': 'synthetic-scout',
        'base_url': 'https://model.example.invalid/v1', 'api_key': 'synthetic', 'protocol': 'openai'}],
        'assignments': {'narrative_scout': 'scout'}})


def selection(settings, event, scene, diary):
    with narrative_transaction(settings.database) as rolls:
        rows = material_rows(endpoints(settings, rolls).materialize({
            'linked_event_ids': [event], 'linked_scene_ids': [scene], 'linked_diary_ids': [diary]}))
    return [dict(row, receipt=source_receipt(row)) for row in rows]


def test_create_theme_line_retry_conflict_and_writer_focus(live):
    settings, client = live
    event, scene = seed(client, settings)
    diary = client.post('/diaries', json={'title': '最早的雨声', 'content': '一起听雨', 'date': '2025-01-01'}).json()['id']
    sources = selection(settings, event, scene, diary)
    body = {'theme': '一起听雨的日子', 'title': '雨声', 'request_id': str(uuid4()), 'sources': sources}
    response = client.post('/api/narrative-rolls/create-line', json=body)
    assert response.status_code == 200, response.text
    key = response.json()['narrative_id']
    assert client.post('/api/narrative-rolls/create-line', json=body).json()['status'] == 'idempotent'
    line = client.get('/api/narrative-rolls', params={'narrative_id': key}).json()
    assert line['body'] == '' and line['publication_status'] == 'collecting'
    assert line['linked_diary_ids'] == [diary] and line['time_start'] == '2025-01-01'
    preview = client.post('/api/narrative-rolls/preview-input', json={
        'narrative_id': key, 'mode': 'rewrite', 'expected_revision': line['revision'],
        'expected_document_sha256': line['document_sha256']}).json()
    assert preview['writing_focus'] == body['theme']
    assert preview['materials']['diaries'][0]['content'] == '一起听雨'
    assert client.post('/api/narrative-rolls/create-line', json={**body, 'title': '另一条线'}).status_code == 409
    assert client.post('/api/narrative-rolls/create-line', json={**body, 'title': '雨' * 17}).status_code == 400
    client.put(f'/diaries/{diary}', json={'content': '这条材料改过了'})
    stale = client.post('/api/narrative-rolls/create-line', json={**body, 'request_id': str(uuid4())})
    assert stale.status_code == 409 and '材料已变化' in stale.text


def test_discover_mixed_sources_excludes_locked_and_does_not_write(live, monkeypatch):
    settings, client = live
    event, scene = seed(client, settings)
    diary = client.post('/diaries', json={'title': '雨声日记', 'content': '一起听雨', 'date': '2025-01-01'}).json()['id']
    locked = client.post('/diaries', json={'title': '雨声秘密', 'content': '不能外发的秘密',
        'date': '2025-01-02', 'unlock_at': '2099-01-01T00:00:00+08:00'}).json()['id']
    configure_scout(settings)
    calls = []
    async def model(client, name, system, payload):
        assert name['model'] == 'synthetic-scout' and name['api_key'] == 'synthetic'
        calls.append(payload)
        if len(calls) == 1:
            return {'terms': ['雨声', '听雨']}
        candidates = payload['candidates']
        keys = {row['key'] for row in candidates}
        assert f'diary:{locked}' not in keys
        assert f'diary:{diary}' in keys and f'event:{event}' in keys and f'scene:{scene}' in keys
        assert '不能外发的秘密' not in str(candidates)
        return {'title': '雨声', 'outline': '从日记到一起听雨。',
            'sources': [{'key': row['key'], 'reason': '一起听雨'} for row in candidates]}
    monkeypatch.setattr('serein.compat.narrative_theme.model_json', model)
    before = client.get('/api/narrative-rolls').json()
    response = client.post('/api/narrative-rolls/discover-theme', json={'theme': '一起听雨'})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['sources'][0]['source_id'] == str(diary)
    assert len(result['sources']) == 3
    assert client.get('/api/narrative-rolls').json() == before
    body = {'theme': '一起听雨', 'title': result['title'], 'request_id': str(uuid4()), 'sources': result['sources']}
    assert client.post('/api/narrative-rolls/create-line', json=body).status_code == 200


def test_discovery_rejects_invented_material(live, monkeypatch):
    settings, client = live
    seed(client, settings)
    configure_scout(settings)
    async def model(client, name, system, payload):
        if 'candidates' not in payload:
            return {'terms': ['雨声']}
        return {'sources': [{'key': 'diary:999999', 'reason': 'imagined'}]}
    monkeypatch.setattr('serein.compat.narrative_theme.model_json', model)
    response = client.post('/api/narrative-rolls/discover-theme', json={'theme': '雨声'})
    assert response.status_code == 400 and '清单外' in response.text


def test_discovery_requires_explicit_scout_assignment(live, monkeypatch):
    settings, client = live
    async def unexpected(*args, **kwargs):
        pytest.fail('Unconfigured discovery must not call a model')
    monkeypatch.setattr('serein.compat.narrative_theme.model_json', unexpected)
    response = client.post('/api/narrative-rolls/discover-theme', json={'theme': '一起听雨'})
    assert response.status_code == 400 and '叙事卷找材料' in response.text


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_configured_discovery_transport_has_no_task_specific_output_budget(protocol):
    from serein.compat.narrative_theme import model_json
    model = {'model': 'gpt-synthetic' if protocol == 'openai' else 'synthetic-scout',
        'protocol': protocol, 'base_url': 'https://model.example.invalid/v1', 'api_key': 'synthetic'}
    calls = []
    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body['model'] == model['model']
        assert body['stream'] is False
        if protocol == 'openai':
            assert request.url.path == '/v1/chat/completions'
            assert request.headers['Authorization'] == 'Bearer synthetic'
            assert body['reasoning_effort'] == 'low'
            result = {'choices': [{'message': {'content': '{"terms":["雨声"]}'}}]}
        else:
            assert request.url.path == '/v1/messages'
            assert request.headers['x-api-key'] == 'synthetic'
            result = {'id': 'test', 'type': 'message', 'role': 'assistant',
                'content': [{'type': 'text', 'text': '{"terms":["雨声"]}'}], 'stop_reason': 'end_turn'}
        return httpx.Response(200, json=result)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            assert await model_json(client, model, 'Select materials', {'theme': '雨声'}) == {'terms': ['雨声']}
            await model_json(client, model, 'Select materials', {'theme': '雨声', 'candidates': []})
    asyncio.run(run())
    if protocol == 'openai':
        assert all(
            not {'max_tokens', 'max_completion_tokens', 'max_output_tokens'}.intersection(call)
            for call in calls
        )
    else:
        # Anthropic Messages requires max_tokens at the transport layer. The
        # generic adapter supplies one stable fallback; Narrative discovery no
        # longer applies its old stage-specific 1000/1800 budgets.
        assert [call['max_tokens'] for call in calls] == [1024, 1024]


def test_discovery_http_errors_do_not_expose_provider_body(live, monkeypatch):
    settings, client = live
    configure_scout(settings)
    async def fail(*args, **kwargs):
        response = httpx.Response(429, text='private-provider-detail',
            request=httpx.Request('POST', 'https://model.example.invalid/v1/chat/completions'))
        response.raise_for_status()
    monkeypatch.setattr('serein.compat.narrative_theme.model_json', fail)
    response = client.post('/api/narrative-rolls/discover-theme', json={'theme': '雨声'})
    assert response.status_code == 502
    assert 'private-provider-detail' not in response.text


def test_discovery_caps_material_input_and_selection(live, monkeypatch):
    settings, client = live
    configure_scout(settings)
    for number in range(20):
        response = client.post('/diaries', json={'title': f'雨声 {number}',
            'content': '一起听雨。' * 1500, 'date': f'2025-01-{number + 1:02}'} )
        assert response.status_code == 200
    async def model(client, name, system, payload):
        if 'candidates' not in payload:
            return {'terms': ['雨声']}
        candidates = payload['candidates']
        assert len(candidates) == 16
        assert sum(len(row['material_excerpt']) for row in candidates) <= 8000
        return {'title': '雨' * 30, 'sources': [{'key': row['key']} for row in candidates]}
    monkeypatch.setattr('serein.compat.narrative_theme.model_json', model)
    response = client.post('/api/narrative-rolls/discover-theme', json={'theme': '雨声'})
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result['title']) == 16 and len(result['sources']) == 10
    assert client.get('/api/narrative-rolls').json()['items'] == []
