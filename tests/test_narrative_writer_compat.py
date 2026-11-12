"""Offline API Writer regressions; every provider call uses synthetic output."""
from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from test_public_features import settings
from serein.api.http import create_app
from serein.deployment import save_settings


SCHEMA = json.loads((Path(__file__).parents[1]
    / 'web/codex_agents/narrative_writer/output.schema.json').read_text())
OUTPUT = {'evidence_sufficient': True, 'body': 'A synthetic sourced sentence.',
    'issues': [], 'self_review': {
        key: True for key in SCHEMA['properties']['self_review']['required']}}


def client_for(settings, base_url='https://api.deepseek.com/v1', protocol='openai',
               model='deepseek-v4-pro'):
    save_settings(settings.database, {'models': [{
        'id': 'writer', 'model': model, 'base_url': base_url, 'protocol': protocol}],
        'assignments': {'writer': 'writer'}, 'upstream': {'writer_enabled': True}})
    return TestClient(create_app(settings, token='synthetic', live=True),
                      headers={'Authorization': 'Bearer synthetic'})


def task(schema=SCHEMA):
    return {'prompt': 'Bound synthetic text only.', 'output_schema': schema}


@pytest.mark.parametrize('base_url,protocol,model,json_mode', [
    ('https://api.deepseek.com/v1', 'openai', 'deepseek-v4-pro', True),
    ('https://api.deepseek.com', 'openai', 'renamed-model', True),
    ('https://API.DEEPSEEK.COM/v1', 'openai', 'deepseek-chat', True),
    ('https://api.deepseek.com/anthropic', 'anthropic', 'deepseek-v4-pro', False),
    ('https://api.openai.com/v1', 'openai', 'synthetic-writer', False),
    ('https://proxy.example/v1', 'openai', 'deepseek-v4-pro', False),
    ('https://api.deepseek.com.evil.example/v1', 'openai', 'deepseek-chat', False),
    ('https://proxy.example/api.deepseek.com', 'openai', 'synthetic-writer', False),
])
def test_writer_selects_json_mode_only_for_official_deepseek_chat(
        settings, monkeypatch, base_url, protocol, model, json_mode):
    client = client_for(settings, base_url, protocol, model)
    calls = []
    async def complete(selected, payload):
        calls.append((selected, payload))
        return {'choices': [{'finish_reason': 'stop',
            'message': {'content': json.dumps(OUTPUT)}}]}
    monkeypatch.setattr('serein.api.chat.complete', complete)
    response = client.post('/v1/models/writer', json=task())
    assert response.status_code == 200, response.text
    assert response.json() == {'result': OUTPUT}
    assert len(calls) == 1
    selected, payload = calls[0]
    assert selected['request_timeout_seconds'] == 300
    assert payload['messages'][-1] == {'role': 'user', 'content': task()['prompt']}
    if json_mode:
        assert payload['response_format'] == {'type': 'json_object'}
        instruction = payload['messages'][0]
        assert instruction['role'] == 'system' and 'JSON' in instruction['content']
        assert json.loads(instruction['content'].split('\n', 1)[1]) == SCHEMA
    else:
        assert payload['response_format'] == {'type': 'json_schema', 'json_schema': {
            'name': 'narrative_preview', 'strict': True, 'schema': SCHEMA}}
        assert payload['messages'] == [{'role': 'user', 'content': task()['prompt']}]


def test_writer_json_mode_reaches_chat_completions_transport(settings, monkeypatch):
    from serein import model_runtime
    calls = []
    response_content = json.dumps(OUTPUT)
    class Client:
        def __init__(self, **kwargs):
            assert kwargs['timeout'].read == 300
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, *, headers, json):
            calls.append((url, json))
            assert json['model'] == 'deepseek-v4-pro' and json['stream'] is False
            assert json['response_format'] == {'type': 'json_object'}
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                'message': {'content': response_content}}]})
    monkeypatch.setattr(model_runtime.httpx, 'AsyncClient', Client)
    response = client_for(settings).post('/v1/models/writer', json=task())
    assert response.status_code == 200, response.text
    assert len(calls) == 1 and calls[0][0] == 'https://api.deepseek.com/v1/chat/completions'


@pytest.mark.parametrize('base_url', ['https://api.deepseek.com/v1', 'https://other.example/v1'])
@pytest.mark.parametrize('invalid', [
    'synthetic-private-invalid-json', '[]', 'null', '{}',
    json.dumps({**OUTPUT, 'extra': 'synthetic-private-extra'}),
    json.dumps({**OUTPUT, 'evidence_sufficient': 'true'}),
    json.dumps({**OUTPUT, 'issues': [3]}),
    json.dumps({**OUTPUT, 'self_review': {**OUTPUT['self_review'], 'source_bound': 1}}),
    json.dumps({**OUTPUT, 'self_review': {}}),
])
def test_writer_rejects_invalid_output_without_exposing_contents(
        settings, monkeypatch, base_url, invalid):
    calls = []
    async def complete(*args):
        calls.append(1)
        return {'choices': [{'message': {'content': invalid}}]}
    monkeypatch.setattr('serein.api.chat.complete', complete)
    response = client_for(settings, base_url).post('/v1/models/writer', json=task())
    assert response.status_code == 502
    assert response.json() == {'detail': 'Writer returned an invalid result'}
    assert calls == [1]  # No silent downgrade/retry after an invalid response.


@pytest.mark.parametrize('finish_reason', ['length', 'content_filter'])
def test_writer_rejects_incomplete_result_even_if_json_is_valid(settings, monkeypatch, finish_reason):
    async def complete(*args):
        return {'choices': [{'finish_reason': finish_reason,
            'message': {'content': json.dumps(OUTPUT)}}]}
    monkeypatch.setattr('serein.api.chat.complete', complete)
    response = client_for(settings).post('/v1/models/writer', json=task())
    assert response.status_code == 502
    assert response.json() == {'detail': 'Writer returned an invalid result'}


@pytest.mark.parametrize('choices', [[], [None], ['synthetic-private-invalid-choice']])
def test_writer_rejects_invalid_choice_without_server_error(settings, monkeypatch, choices):
    async def complete(*args):
        return {'choices': choices}
    monkeypatch.setattr('serein.api.chat.complete', complete)
    response = client_for(settings).post('/v1/models/writer', json=task())
    assert response.status_code == 502
    assert response.json() == {'detail': 'Writer returned an invalid result'}


@pytest.mark.parametrize('schema', [
    {'type': 'private-invalid-type'}, {'$schema': 3}, {'$schema': None},
    {'$schema': 'https://private.example/unsupported-dialect'},
])
def test_writer_validates_schema_before_calling_provider(settings, monkeypatch, schema):
    async def unexpected(*args):
        pytest.fail('Invalid schema must not call the provider')
    monkeypatch.setattr('serein.api.chat.complete', unexpected)
    response = client_for(settings).post('/v1/models/writer', json=task(schema))
    assert response.status_code == 400
    assert response.json() == {'detail': 'Writer requires a valid output_schema'}


def test_writer_resolves_local_schema_refs_but_never_fetches_remote_refs(settings, monkeypatch):
    async def complete(*args):
        return {'choices': [{'message': {'content': json.dumps(OUTPUT)}}]}
    def unexpected_fetch(*args, **kwargs):
        pytest.fail('Writer schema validation must not fetch external resources')
    monkeypatch.setattr('serein.api.chat.complete', complete)
    monkeypatch.setattr('urllib.request.urlopen', unexpected_fetch)
    client = client_for(settings)
    local = {'$defs': {'result': SCHEMA}, '$ref': '#/$defs/result'}
    response = client.post('/v1/models/writer', json=task(local))
    assert response.status_code == 200, response.text
    response = client.post('/v1/models/writer', json=task({
        '$ref': 'https://private.example/never-fetch-schema'}))
    assert response.status_code == 502
    assert response.json() == {'detail': 'Writer returned an invalid result'}


def test_writer_preserves_valid_insufficient_evidence_result(settings, monkeypatch):
    output = deepcopy(OUTPUT)
    output.update(evidence_sufficient=False, body='', issues=['More source text is needed.'])
    output['self_review']['source_bound'] = False
    async def complete(*args):
        return {'choices': [{'message': {'content': json.dumps(output)}}]}
    monkeypatch.setattr('serein.api.chat.complete', complete)
    response = client_for(settings).post('/v1/models/writer', json=task())
    assert response.status_code == 200
    assert response.json() == {'result': output}
