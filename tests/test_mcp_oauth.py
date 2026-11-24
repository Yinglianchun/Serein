import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from serein.api.http import create_app
from serein.config import Settings
from serein.core import Store


def _challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()


def _authorize(client, registration, *, state='exact-state', resource='https://memory.example/serein/mcp'):
    verifier = 'synthetic-verifier-' + 'x' * 43
    params = {
        'response_type':'code', 'client_id':registration['client_id'],
        'redirect_uri':registration['redirect_uris'][0], 'state':state,
        'code_challenge':_challenge(verifier), 'code_challenge_method':'S256',
        'resource':resource, 'scope':'serein:mcp',
    }
    page = client.get('/authorize', params=params)
    assert page.status_code == 200
    assert 'synthetic-gateway-key' not in page.text
    consent = client.post('/authorize', data={**params, 'gateway_key':'synthetic-gateway-key'},
                          follow_redirects=False)
    assert consent.status_code == 303
    callback = urlsplit(consent.headers['location'])
    values = parse_qs(callback.query)
    assert values['state'] == [state]
    return values['code'][0], verifier, params


def test_oauth_discovery_pkce_refresh_rotation_and_static_bearer(tmp_path):
    token_file = tmp_path/'gateway-key'
    token_file.write_text('synthetic-gateway-key', encoding='utf-8')
    settings = Settings(tmp_path/'memory.db')
    with Store(settings.database) as store:
        store.create('scene_oauth', 'scene', 'OAuth fixture', 'Protected body')
    app = create_app(settings, token='synthetic-gateway-key', token_file=token_file)
    with TestClient(app, base_url='https://memory.example') as client:
        protected = client.get('/.well-known/oauth-protected-resource').json()
        assert protected == {
            'resource':'https://memory.example/serein/mcp',
            'authorization_servers':['https://memory.example'],
            'scopes_supported':['serein:mcp'], 'bearer_methods_supported':['header'],
        }
        metadata = client.get('/.well-known/oauth-authorization-server').json()
        assert metadata['authorization_endpoint'] == 'https://memory.example/authorize'
        assert metadata['token_endpoint'] == 'https://memory.example/token'
        assert metadata['registration_endpoint'] == 'https://memory.example/register'
        assert metadata['code_challenge_methods_supported'] == ['S256']
        assert metadata['token_endpoint_auth_methods_supported'] == ['none']

        registered = client.post('/register', json={
            'client_name':'ChatGPT',
            'redirect_uris':['https://chat.openai.com/aip/callback'],
            'grant_types':['authorization_code', 'refresh_token'],
            'response_types':['code'], 'token_endpoint_auth_method':'none',
        })
        assert registered.status_code == 201, registered.text
        registration = registered.json()

        code, verifier, params = _authorize(client, registration)
        token = client.post('/token', data={
            'grant_type':'authorization_code', 'client_id':registration['client_id'],
            'code':code, 'redirect_uri':params['redirect_uri'], 'code_verifier':verifier,
            'resource':params['resource'],
        })
        assert token.status_code == 200, token.text
        grant = token.json()
        assert grant['token_type'] == 'Bearer' and grant['expires_in'] == 3600
        assert grant['access_token'].startswith('sat_') and grant['refresh_token'].startswith('srt_')

        initialize = {'jsonrpc':'2.0', 'id':1, 'method':'initialize', 'params':{
            'protocolVersion':'2025-03-26', 'capabilities':{},
            'clientInfo':{'name':'oauth-test', 'version':'1'},
        }}
        oauth_headers = {'Authorization':'Bearer '+grant['access_token'],
                         'Accept':'application/json, text/event-stream'}
        assert client.post('/serein/mcp', headers=oauth_headers, json=initialize).status_code == 200
        # Access tokens are audience-bound to the exact MCP resource.
        denied_alias = client.post('/mcp', headers=oauth_headers, json=initialize)
        assert denied_alias.status_code == 401
        assert 'resource_metadata="https://memory.example/.well-known/oauth-protected-resource/mcp"' in denied_alias.headers['www-authenticate']
        # The original static Gateway Key remains compatible.
        static = client.post('/mcp', headers={'Authorization':'Bearer synthetic-gateway-key',
                                              'Accept':'application/json, text/event-stream'}, json=initialize)
        assert static.status_code == 200

        refreshed = client.post('/token', data={
            'grant_type':'refresh_token', 'client_id':registration['client_id'],
            'refresh_token':grant['refresh_token'],
        })
        assert refreshed.status_code == 200
        rotated = refreshed.json()
        assert rotated['refresh_token'] != grant['refresh_token']
        assert client.post('/serein/mcp', headers=oauth_headers, json=initialize).status_code == 401
        rotated_headers = {'Authorization':'Bearer '+rotated['access_token'],
                           'Accept':'application/json, text/event-stream'}
        assert client.post('/serein/mcp', headers=rotated_headers, json=initialize).status_code == 200
        # Replaying a rotated refresh token revokes the entire token family.
        replay = client.post('/token', data={
            'grant_type':'refresh_token', 'client_id':registration['client_id'],
            'refresh_token':grant['refresh_token'], 'resource':params['resource'],
        })
        assert replay.status_code == 400 and replay.json()['error'] == 'invalid_grant'
        assert client.post('/serein/mcp', headers=rotated_headers, json=initialize).status_code == 401

        code2, verifier2, params2 = _authorize(client, registration, state='key-rotation')
        surviving = client.post('/token', data={
            'grant_type':'authorization_code', 'client_id':registration['client_id'],
            'code':code2, 'redirect_uri':params2['redirect_uri'], 'code_verifier':verifier2,
        }).json()

        # Rotation takes effect in the running service before an old grant can be refreshed.
        token_file.write_text('replacement-gateway-key', encoding='utf-8')
        refresh = client.post('/token', data={'grant_type':'refresh_token', 'client_id':registration['client_id'],
                                             'refresh_token':surviving['refresh_token']})
        assert refresh.status_code == 400 and refresh.json()['error'] == 'invalid_grant'
        headers = {'Accept':'application/json, text/event-stream'}
        for key in (surviving['access_token'], 'synthetic-gateway-key'):
            assert client.post('/serein/mcp', headers={**headers,'Authorization':'Bearer '+key}, json=initialize).status_code == 401
        headers['Authorization'] = 'Bearer replacement-gateway-key'
        assert client.post('/serein/mcp', headers=headers, json=initialize).status_code == 200


def test_oauth_rejects_open_redirect_bad_key_and_replayed_code(tmp_path):
    settings = Settings(tmp_path/'memory.db')
    with Store(settings.database):
        pass
    with TestClient(create_app(settings, token='synthetic-gateway-key'),
                    base_url='https://memory.example') as client:
        bad = client.post('/register', json={'redirect_uris':['http://foreign.example/callback']})
        assert bad.status_code == 400 and bad.json()['error'] == 'invalid_client_metadata'
        registration = client.post('/register', json={
            'client_name':'ChatGPT', 'redirect_uris':['https://chat.openai.com/aip/callback'],
        }).json()
        verifier = 'v' * 43
        params = {'response_type':'code', 'client_id':registration['client_id'],
            'redirect_uri':'https://evil.example/callback', 'state':'state',
            'code_challenge':_challenge(verifier), 'code_challenge_method':'S256',
            'resource':'https://memory.example/serein/mcp'}
        mismatch = client.get('/authorize', params=params)
        assert mismatch.status_code == 400 and 'redirect_uri' in mismatch.text

        params['redirect_uri'] = registration['redirect_uris'][0]
        wrong_key = client.post('/authorize', data={**params, 'gateway_key':'wrong'},
                                follow_redirects=False)
        assert wrong_key.status_code == 401 and 'synthetic-gateway-key' not in wrong_key.text

        code, correct_verifier, valid = _authorize(client, registration)
        wrong_pkce = client.post('/token', data={
            'grant_type':'authorization_code', 'client_id':registration['client_id'],
            'code':code, 'redirect_uri':valid['redirect_uri'], 'code_verifier':'z'*43,
            'resource':valid['resource'],
        })
        assert wrong_pkce.status_code == 400 and wrong_pkce.json()['error'] == 'invalid_grant'
        replay = client.post('/token', data={
            'grant_type':'authorization_code', 'client_id':registration['client_id'],
            'code':code, 'redirect_uri':valid['redirect_uri'], 'code_verifier':correct_verifier,
            'resource':valid['resource'],
        })
        assert replay.status_code == 400 and replay.json()['error'] == 'invalid_grant'


def test_oauth_registration_cap_evicts_unused_clients_and_public_http_is_refused(tmp_path, monkeypatch):
    import serein.api.oauth as oauth
    monkeypatch.setattr(oauth, 'CLIENT_LIMIT', 2)
    settings = Settings(tmp_path/'memory.db')
    with Store(settings.database):
        pass
    app = create_app(settings, token='synthetic-gateway-key')
    with TestClient(app, base_url='https://memory.example') as client:
        registrations = [client.post('/register', json={
            'client_name':f'client-{index}',
            'redirect_uris':[f'https://client.example/callback/{index}'],
        }) for index in range(3)]
        assert [response.status_code for response in registrations] == [201, 201, 201]
        first = registrations[0].json()
        params = {'response_type':'code', 'client_id':first['client_id'],
            'redirect_uri':first['redirect_uris'][0], 'state':'state',
            'code_challenge':_challenge('v'*43), 'code_challenge_method':'S256',
            'resource':'https://memory.example/serein/mcp'}
        assert client.get('/authorize', params=params).status_code == 400
    with TestClient(create_app(settings, token='synthetic-gateway-key'),
                    base_url='http://public.example') as client:
        response = client.post('/register', json={
            'redirect_uris':['https://client.example/callback'],
        })
        assert response.status_code == 400
        assert 'requires HTTPS' in response.text
