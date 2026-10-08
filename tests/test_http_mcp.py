from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from serein.api.http import create_app
from serein.config import Settings
from serein.core import Store
from serein.core.search import build_index
from serein.deployment import save_settings


@pytest.mark.parametrize('writable', [False, True])
def test_event_scene_switch_persists_and_refreshes_http_mcp(tmp_path, writable):
    from serein.application import Application
    settings = Settings(tmp_path/'promotion.db', writable=True)
    with Store(settings.database):
        pass
    services = Application(settings).services
    event = services.write('seed', 'save', {'kind':'event', 'title':'Trip', 'body_md':'A trip',
        'sources':[{'source_key':'synthetic:trip', 'content':'Keep the ticket.'}]})
    request = {'operation_id':'promote', 'event_id':event['id'], 'expected_revision':1,
               'title':'The ticket', 'body_md':'I kept our ticket.'}
    from dataclasses import replace
    settings = replace(settings, writable=writable)
    headers = {'Authorization':'Bearer test', 'Accept':'application/json, text/event-stream'}
    with TestClient(create_app(settings, token='test', live=writable), headers=headers) as client:
        def rpc(method, params=None):
            result = client.post('/serein/mcp', json={'jsonrpc':'2.0', 'id':1, 'method':method, 'params':params or {}})
            assert result.status_code == 200, result.text
            return result.json()['result']
        def names():return {t['name'] for t in rpc('tools/list')['tools']}
        def call():return rpc('tools/call', {'name':'promote_event_to_scene', 'arguments':request})
        from serein.deployment import read_settings
        assert read_settings(settings.database)['features']['event_to_scene'] is False
        if writable:
            assert client.get('/v1/settings').json()['features']['event_to_scene'] is False
        assert 'promote_event_to_scene' not in names()
        assert call()['isError']
        assert client.post('/v1/extensions/promote_event_to_scene', json=request).status_code == 404
        # A read-only deployment cannot mutate settings, but must still hide the tool
        # when opening an existing database with the feature enabled.
        if writable:
            response = client.patch('/v1/settings', json={'features':{'event_to_scene':True}})
            assert response.status_code == 200, response.text
        else:
            save_settings(settings.database, {'features':{'event_to_scene':True}})
        assert ('promote_event_to_scene' in names()) is writable
        if not writable:
            assert call()['isError']
            return
        from serein.deployment import read_settings
        assert read_settings(settings.database)['features']['event_to_scene'] is True
        promoted = call()
        assert not promoted.get('isError')
        scene_id = promoted['structuredContent']['id']
        assert call()['structuredContent']['id'] == scene_id
        assert client.post('/v1/extensions/promote_event_to_scene', json=request).json()['id'] == scene_id
        assert client.patch('/v1/settings', json={'features':{'event_to_scene':False}}).status_code == 200
        # Call before listing, as a client with stale cached tool definitions would.
        assert call()['isError']
        assert client.post('/v1/extensions/promote_event_to_scene', json=request).status_code == 404
        assert 'promote_event_to_scene' not in names()
        assert services.read(scene_id)['document']['body_md'] == request['body_md']
        assert services.read(event['id'])['document']['body_md'] == 'A trip'


def test_retired_index_sync_setting_does_not_restore_http_or_mcp_tool(tmp_path):
    database = tmp_path/'retired-index-sync.db'
    with Store(database):
        pass
    save_settings(database, {'features':{'index_sync_tool':True}})
    settings = Settings(database, writable=True)
    headers = {'Authorization':'Bearer test', 'Accept':'application/json, text/event-stream'}
    with TestClient(create_app(settings, token='test', live=True), headers=headers) as client:
        def rpc(method, params=None):
            response = client.post('/serein/mcp', json={'jsonrpc':'2.0', 'id':1, 'method':method,
                                                        'params':params or {}})
            assert response.status_code == 200, response.text
            return response.json()['result']

        def names():
            return {tool['name'] for tool in rpc('tools/list')['tools']}

        assert 'index_sync' not in names()
        assert rpc('tools/call', {'name':'index_sync', 'arguments':{}})['isError']
        assert client.post('/v1/extensions/index_sync', json={}).status_code == 404
        response = client.patch('/v1/settings', json={'features':{'index_sync_tool':True}})
        assert response.status_code == 422
        assert 'index_sync_tool' not in client.get('/v1/settings').json()['features']


@pytest.mark.parametrize('writable', [False, True])
@pytest.mark.parametrize('entry_path', ['/serein/mcp', '/mcp'])
def test_http_mcp_auth_tools_and_single_lifecycle(tmp_path, monkeypatch, writable, entry_path):
    settings = Settings(tmp_path/'memory.db', tmp_path/'index.db', writable=writable)
    with Store(settings.database) as store:
        store.create('scene_test', 'scene', 'A synthetic memory', 'Original body')
    build_index(settings.database, settings.index)
    calls = []

    @asynccontextmanager
    async def lifecycle(app):
        calls.append('start')
        yield
        calls.append('stop')

    monkeypatch.setattr('serein.lifecycle.lifespan', lifecycle)
    headers = {'Authorization':'Bearer synthetic-key', 'Accept':'application/json, text/event-stream'}
    initialize = {'jsonrpc':'2.0', 'id':1, 'method':'initialize',
                  'params':{'protocolVersion':'2025-03-26', 'capabilities':{},
                            'clientInfo':{'name':'test', 'version':'1'}}}
    with TestClient(create_app(settings, token='synthetic-key', live=writable)) as client:
        for path in ('/serein/mcp', '/serein/mcp/', '/mcp', '/mcp/'):
            for auth in ('', 'Bearer wrong', 'Basic synthetic-key'):
                denied = client.post(path, headers={**headers, 'Authorization':auth}, json=initialize)
                assert denied.status_code == 401
                challenge = denied.headers['www-authenticate']
                assert challenge.startswith('Bearer resource_metadata="http://testserver/.well-known/oauth-protected-resource/')
                assert 'scope="serein:mcp"' in challenge
            result = client.post(path, headers=headers, json=initialize)
            assert result.status_code == 200, result.text
            assert not result.history
            assert result.json()['result']['serverInfo']['name'] == 'Serein'
        assert client.post(entry_path, headers={**headers, 'Origin':'https://foreign.invalid'}, json=initialize).status_code == 403
        assert client.post(entry_path, headers={**headers, 'Origin':'http://testserver'}, json=initialize).status_code == 200
        assert client.get(entry_path, headers={'Authorization':'Bearer wrong'}).status_code == 401
        assert client.delete(entry_path, headers={'Authorization':'Bearer wrong'}).status_code == 401

        def rpc(method, params=None):
            response = client.post(entry_path, headers=headers, json={'jsonrpc':'2.0', 'id':2, 'method':method, 'params':params or {}})
            assert response.status_code == 200, response.text
            return response.json()['result']

        names = {t['name'] for t in rpc('tools/list')['tools']}
        assert 'read_memory' in names
        assert ('write_scene' in names) is writable
        assert ('edit_scene' in names) is writable
        assert 'save_memory' not in names
        assert 'resume' not in names
        for name in ('list_source_messages', 'read_source_messages'):
            assert name not in names
            assert rpc('tools/call', {'name':name, 'arguments':{}})['isError']
        read = rpc('tools/call', {'name':'read_memory', 'arguments':{'identifier':'scene_test'}})
        assert not read['isError'] and 'structuredContent' not in read
        assert 'body:\nOriginal body' in read['content'][0]['text']
        if writable:
            save_settings(settings.database, {'features':{'resume':True}})
            assert 'resume' not in {t['name'] for t in rpc('tools/list')['tools']}
            for arguments in ({}, {'window_id':''}, {'window_id':'  '}):
                assert rpc('tools/call', {'name':'resume', 'arguments':arguments})['isError']
                assert client.post('/v1/extensions/resume', headers=headers, json=arguments).json()['window_id'] == 'main'
            assert rpc('tools/call', {'name':'resume', 'arguments':{'window_id':'synthetic-window'}})['isError']
            save_settings(settings.database, {'features':{'resume':False}})
            assert 'resume' not in {t['name'] for t in rpc('tools/list')['tools']}
            assert rpc('tools/call', {'name':'resume', 'arguments':{'window_id':'synthetic-window'}})['isError']
            arguments = {'operation_id':'http-mcp-save', 'title':'Synthetic', 'content':'Saved over MCP', 'cues':['synthetic'], 'date':'2026-09-14'}
            saved = rpc('tools/call', {'name':'write_scene', 'arguments':arguments})['structuredContent']
            retry = rpc('tools/call', {'name':'write_scene', 'arguments':arguments})['structuredContent']
            assert saved['id'] == retry['id']
            reread = rpc('tools/call', {'name':'read_memory', 'arguments':{'identifier':saved['id']}})
            assert 'structuredContent' not in reread
            assert 'body:\nSaved over MCP' in reread['content'][0]['text']
            assert 'date: 2026-09-14' in reread['content'][0]['text']
            assert 'bound_sources: 0' in reread['content'][0]['text']
            edit_args = {'operation_id':'http-mcp-edit', 'scene_id':saved['id'], 'expected_revision':1,
                         'content':'Edited over MCP'}
            edited = rpc('tools/call', {'name':'edit_scene', 'arguments':edit_args})['structuredContent']
            assert edited['revision'] == 2
            assert rpc('tools/call', {'name':'edit_scene', 'arguments':edit_args})['structuredContent']['revision'] == 2
            assert rpc('tools/call', {'name':'edit_scene', 'arguments':{**edit_args,'operation_id':'http-stale'}})['isError']
            current_text = rpc('tools/call', {'name':'read_memory', 'arguments':{'identifier':saved['id']}})['content'][0]['text']
            assert 'title: Synthetic' in current_text and 'body:\nEdited over MCP' in current_text
            with Store(settings.database, read_only=True) as store:
                current = store.read(saved['id'])
                assert current['metadata']['scene_cues'] == ['synthetic'] and current['metadata']['date'] == '2026-09-14'
            for kind in ('event', 'narrative'):
                rejected = rpc('tools/call', {'name':'write_scene', 'arguments':{
                    **arguments, 'operation_id':'reject-'+kind, 'kind':kind}})
                assert rejected['isError']
        assert calls == ['start']
    assert calls == ['start', 'stop']


@pytest.mark.parametrize('entry_path', ['/serein/mcp', '/serein/mcp/', '/mcp', '/mcp/'])
def test_official_streamable_http_client(tmp_path, entry_path):
    import asyncio
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    settings = Settings(tmp_path/'memory.db', tmp_path/'index.db')
    with Store(settings.database) as store:
        store.create('scene_sdk', 'scene', 'SDK fixture', 'Read by the official client')
    build_index(settings.database, settings.index)
    app = create_app(settings, token='sdk-test-key')

    async def exercise():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         headers={'Authorization':'Bearer sdk-test-key'}) as http:
                async with streamable_http_client('http://testserver'+entry_path, http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        assert (await session.initialize()).serverInfo.name == 'Serein'
                        assert 'read_memory' in {t.name for t in (await session.list_tools()).tools}
                        result = await session.call_tool('read_memory', {'identifier':'scene_sdk'})
                        assert not result.isError
                        assert result.structuredContent is None
                        assert 'body:\nRead by the official client' in result.content[0].text
    asyncio.run(exercise())


def test_window_shadow_returns_each_section_once_over_http_and_mcp(tmp_path):
    import asyncio
    import json
    from serein.api.mcp import create_server
    from serein.application import Application
    from serein.compat.window_shadows import WindowShadows

    settings = Settings(tmp_path/'memory.db', tmp_path/'index.db', writable=True)
    with Store(settings.database):
        pass
    build_index(settings.database, settings.index)
    save_settings(settings.database, {'features':{'window_shadows':True}})
    shadows = WindowShadows(settings.database)
    sections = {'user_view':'Unique user view', 'self_view':'Unique self view', 'recent_events':'Unique recent events'}
    shadows.write('test-window', 'Synthetic shadow', **sections)
    with Store(settings.database, read_only=True) as store:
        original = tuple(store.conn.execute('select * from historical_works').fetchone())
    expected = shadows.read()
    assert 'sections' not in expected
    assert all(expected['content'].count(value)==1 for value in sections.values())
    assert all(heading in expected['content'] for heading in ('我眼中的你', '我眼中的自己', '这一窗发生的事'))
    assert shadows.read('test-window') == expected
    assert shadows.read('missing')['status'] == 'not_found'
    with TestClient(create_app(settings, token='shadow-test', live=True),
                    headers={'Authorization':'Bearer shadow-test'}) as client:
        text = client.post('/v1/extensions/window_shadow_read', json={}).json()
        assert 'index: 1' in text and 'test-window' not in text
        assert all(text.count(value)==1 for value in sections.values())
        def rpc(method, params=None):
            return client.post('/mcp', headers={'Accept':'application/json, text/event-stream'}, json={
                'jsonrpc':'2.0', 'id':1, 'method':method, 'params':params or {}}).json()['result']
        names = {t['name'] for t in rpc('tools/list')['tools']}
        assert {'window_shadow_write','window_shadow_read'} <= names
        result = rpc('tools/call', {'name':'window_shadow_read', 'arguments':{}})
        assert not result.get('isError') and result['content'][0]['text']==text
    with Store(settings.database, read_only=True) as store:
        assert tuple(store.conn.execute('select * from historical_works').fetchone()) == original


def test_shadow_reader_and_writer_follow_feature_refresh(tmp_path):
    import asyncio
    from serein.api.mcp import create_server
    from serein.application import Application
    from serein.compat.window_shadows import WindowShadows
    settings = Settings(tmp_path/'shadow.db', writable=True,
                        mcp_tools=['resume', 'window_shadow_read', 'window_shadow_write'])
    with Store(settings.database):
        pass
    save_settings(settings.database, {'features':{'window_shadows':True, 'resume':True}})
    server = create_server(Application(settings))

    async def exercise():
        assert {t.name for t in await server.list_tools()} == {'window_shadow_write','window_shadow_read'}
        await server.call_tool('window_shadow_write', {'window_id':'synthetic', 'title':'Test', 'content':'Full authored shadow'})
        assert WindowShadows(settings.database).read('synthetic')['content'] == 'Full authored shadow'
        save_settings(settings.database, {'features':{'window_shadows':False}})
        assert not await server.list_tools()
        save_settings(settings.database, {'features':{'window_shadows':True}})
        assert {t.name for t in await server.list_tools()} == {'window_shadow_write','window_shadow_read'}
    asyncio.run(exercise())
