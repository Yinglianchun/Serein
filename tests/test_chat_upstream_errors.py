"""Safe classification of rejected requests through the actual HTTP transport."""
import json

import httpx
import pytest

from serein.core.store import Store
from serein.model_runtime import safe_upstream_http_error
from test_public_settings import deployment
from test_chat_image_transport import PNG, REMOTE, configure_transport, install_transport


@pytest.mark.parametrize('status,gateway,category', [
    (400, 400, 'upstream_invalid_request'),
    (422, 422, 'upstream_validation_error'),
    (401, 502, 'upstream_authentication_error'),
    (403, 502, 'upstream_permission_error'),
    (404, 502, 'upstream_endpoint_not_found'),
    (413, 502, 'upstream_request_too_large'),
    (429, 502, 'upstream_rate_limit'),
    (500, 502, 'upstream_service_error'),
    (503, 502, 'upstream_service_error'),
    (307, 502, 'upstream_http_error'),
    (418, 502, 'upstream_http_error'),
])
def test_classification_never_accesses_upstream_body_or_headers(status, gateway, category):
    class StatusOnly:
        status_code = status
        def __getattr__(self, name):
            raise AssertionError('Classifier accessed private response material: ' + name)
    actual_status, detail = safe_upstream_http_error(StatusOnly())
    assert actual_status == gateway
    assert detail['code'] == category and detail['upstream_status'] == status
    assert set(detail) == {'code', 'message', 'upstream_status'}


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('status,category', [
    (400, 'upstream_invalid_request'), (422, 'upstream_validation_error'),
    (503, 'upstream_service_error')])
@pytest.mark.parametrize('body_kind', ['json', 'html'])
def test_rejected_image_request_returns_only_safe_classification(
        deployment, monkeypatch, caplog, protocol, stream, status, category, body_kind):
    settings, client = deployment
    configure_transport(client, protocol, operit=True)
    secrets = ['PRIVATE_PROMPT', PNG, REMOTE, 'Bearer synthetic-secret-token',
               'arbitrary-provider-code', 'private-request-id']
    provider_text = '|'.join(secrets)
    upstream_responses = []
    def handler(request):
        payload = json.loads(request.content)
        # Reject exactly what the caller sent, without an image/thinking retry.
        assert payload['thinking'] == {'type': 'low'}
        assert PNG.split(',', 1)[1] in json.dumps(payload)
        options = ({'json': {'error': {'message': provider_text, 'code': secrets[4]}}}
                   if body_kind == 'json' else {'text': '<html>' + provider_text + '</html>'})
        response = httpx.Response(status, headers={'x-request-id': secrets[5],
            'location': REMOTE}, **options)
        upstream_responses.append(response)
        return response
    clients = install_transport(monkeypatch, handler)
    response = client.post('/v1/chat/completions', json={'messages': [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'Synthetic question'}, {'type': 'image_url', 'image_url': {'url': PNG}}]}],
        'thinking': {'type': 'low'}, 'stream': stream, 'serein': {'memory': False}})
    assert response.status_code == (status if status in (400, 422) else 502)
    detail = response.json()['detail']
    assert set(detail) == {'code', 'message', 'upstream_status'}
    assert detail['code'] == category and detail['upstream_status'] == status
    assert 'text/event-stream' not in response.headers.get('content-type', '')
    assert len(upstream_responses) == 1
    assert all(item.is_closed for item in clients + upstream_responses)
    with Store(settings.database, read_only=True) as store:
        observation = json.loads(store.conn.execute(
            'SELECT payload_json FROM injection_debug ORDER BY id DESC LIMIT 1').fetchone()[0])
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 0
    assert observation['request_status'] == 'failed' and observation['injected_bucket_ids'] == []
    exposed = response.text + json.dumps(dict(response.headers)) + observation['failure_reason'] + caplog.text
    assert all(secret not in exposed for secret in secrets)


@pytest.mark.parametrize('stream', [False, True])
def test_network_error_remains_generic_and_closes_client(deployment, monkeypatch, stream):
    _, client = deployment
    configure_transport(client, 'openai', operit=False)
    def handler(request):
        raise httpx.ConnectError('PRIVATE_NETWORK_ERROR ' + REMOTE, request=request)
    clients = install_transport(monkeypatch, handler)
    response = client.post('/v1/chat/completions', json={
        'messages': [{'role': 'user', 'content': 'Synthetic question'}],
        'stream': stream, 'serein': {'memory': False}})
    assert response.status_code == 502
    assert 'PRIVATE_NETWORK_ERROR' not in response.text and REMOTE not in response.text
    assert all(item.is_closed for item in clients)
