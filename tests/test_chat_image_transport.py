"""Exercise the client route and real request_for/HTTP serialization offline."""
import base64
from copy import deepcopy
import json

import httpx
import pytest

from test_public_settings import deployment


PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII='
REMOTE = 'https://images.invalid/synthetic.png?token=synthetic-url-secret'
TEXT = 'Describe this image.<attachment type="message_insert_extra_bundle">Synthetic Operit context</attachment>'


def configure_transport(client, protocol, operit, eyes=False):
    client.patch('/v1/settings', json={
        'models': [{'id': 'vision', 'label': 'Synthetic vision', 'model': 'synthetic-vision',
                    'base_url': 'https://provider.invalid/v1', 'protocol': protocol,
                    'api_key': 'synthetic-key'}],
        'assignments': {'chat': 'vision', 'image_transcription': 'vision'},
        'upstream': {'operit_enabled': operit},
        'features': {'image_eyes': eyes},
    }).raise_for_status()


def install_transport(monkeypatch, handler):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    clients = []
    def client(**kwargs):
        value = original(**kwargs, transport=transport)
        clients.append(value)
        return value
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    return clients


def reply(protocol, stream, text='Synthetic answer'):
    if not stream:
        if protocol == 'anthropic':
            return httpx.Response(200, json={'id': 'synthetic', 'type': 'message',
                'role': 'assistant', 'content': [{'type': 'text', 'text': text}],
                'stop_reason': 'end_turn', 'usage': {}})
        return httpx.Response(200, json={'choices': [{'message': {
            'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}]})
    events = ([{'type': 'message_start', 'message': {'id': 'synthetic', 'usage': {}}},
               {'type': 'content_block_delta', 'index': 0,
                'delta': {'type': 'text_delta', 'text': text}},
               {'type': 'message_stop'}] if protocol == 'anthropic' else
              [{'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': text}}]}])
    content = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events)
    if protocol == 'openai':
        content += 'data: [DONE]\n\n'
    return httpx.Response(200, text=content, headers={'content-type': 'text/event-stream'})


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('operit', [False, True])
@pytest.mark.parametrize('url', [PNG, REMOTE], ids=['data-url', 'remote-url'])
def test_standard_user_image_reaches_http_transport(deployment, monkeypatch, protocol, stream, operit, url):
    _, client = deployment
    configure_transport(client, protocol, operit)
    seen = []
    def handler(request):
        assert request.method == 'POST'
        assert str(request.url) == 'https://provider.invalid/v1/' + (
            'messages' if protocol == 'anthropic' else 'chat/completions')
        seen.append(json.loads(request.content))
        return reply(protocol, stream)
    install_transport(monkeypatch, handler)
    # No image fetch is allowed while Eyes is off, including remote URLs.
    def no_download(*args):
        raise AssertionError('Unexpected remote image fetch')
    monkeypatch.setattr('serein.extensions.pipeline_images._remote_image_bytes', no_download)
    messages = [{'role': 'user', 'content': [{'type': 'text', 'text': TEXT},
        {'type': 'image_url', 'image_url': {'url': url, 'detail': 'low'}}]}]
    original = deepcopy(messages)
    response = client.post('/v1/chat/completions', json={'messages': messages,
        'stream': stream, 'thinking': {'type': 'low'}, 'serein': {'memory': False}})
    assert response.status_code == 200, response.text
    assert len(seen) == 1 and messages == original
    payload = seen[0]
    assert payload['model'] == 'synthetic-vision' and payload['stream'] is stream
    assert payload['thinking'] == {'type': 'low'}  # No provider guess or auto-disable.
    if protocol == 'openai':
        assert payload['messages'] == messages
    else:
        expected = ({'type': 'base64', 'media_type': 'image/png', 'data': PNG.split(',', 1)[1]}
                    if url == PNG else {'type': 'url', 'url': url})
        assert payload['messages'] == [{'role': 'user', 'content': [
            {'type': 'text', 'text': TEXT}, {'type': 'image', 'source': expected}]}]


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('operit', [False, True])
@pytest.mark.parametrize('url', [PNG, REMOTE], ids=['data-url', 'remote-url'])
def test_eyes_transcribes_over_http_before_text_only_chat(deployment, monkeypatch, protocol, stream, operit, url):
    _, client = deployment
    configure_transport(client, protocol, operit, eyes=True)
    downloads = []
    def synthetic_download(value):
        assert value == REMOTE
        downloads.append(value)
        return base64.b64decode(PNG.split(',', 1)[1])
    monkeypatch.setattr('serein.extensions.pipeline_images._remote_image_bytes', synthetic_download)
    seen = []
    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if len(seen) == 1:
            assert PNG.split(',', 1)[1] in json.dumps(payload)
            return reply(protocol, False, json.dumps({'image_transcriptions': [
                {'input_image': 1, 'text': 'Visible synthetic title', 'unreadable': False}]}))
        return reply(protocol, stream)
    install_transport(monkeypatch, handler)
    response = client.post('/v1/chat/completions', json={'messages': [{'role': 'user', 'content': [
        {'type': 'text', 'text': TEXT}, {'type': 'image_url', 'image_url': {'url': url}}]}],
        'stream': stream, 'serein': {'memory': False}})
    assert response.status_code == 200, response.text
    assert len(seen) == 2
    encoded = json.dumps(seen[1])
    assert 'Visible synthetic title' in encoded
    assert 'image_url' not in encoded and PNG not in encoded and REMOTE not in encoded
    assert ('message_insert_extra_bundle' not in encoded) is operit
    assert downloads == ([REMOTE] if url == REMOTE else [])
