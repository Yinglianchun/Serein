import asyncio
import os
from pathlib import Path
import sys

import pytest

pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from serein.application import Application
from serein.config import Settings
from serein.core import Store
from serein.core.search import build_index
from serein.api.mcp import create_server


@pytest.mark.parametrize('restricted', [False, True])
def test_legacy_authoring_tools_stay_unavailable_with_old_config(tmp_path, restricted):
    from serein.deployment import save_settings

    database = tmp_path / 'memory.db'
    with Store(database):
        pass
    retired = {'handoff', 'narrative_revision_inbox', 'review_narrative_revision', 'publish_narrative'}
    settings = Settings(database, writable=True,
                        extensions={'handoff': {'enabled': True}, 'narrative_authoring': {'enabled': True}},
                        mcp_tools=[*sorted(retired), 'read_memory', 'narrative_volume', 'window_shadow_write'] if restricted else None)
    save_settings(database, {'features': {'narrative_tools': True, 'window_shadows': True}})
    server = create_server(Application(settings))

    async def exercise():
        for _ in range(2):
            names = {tool.name for tool in await server.list_tools()}
            assert not retired & names
            assert {'read_memory', 'narrative_volume', 'window_shadow_write'} <= names
        for name in retired:
            with pytest.raises(Exception, match='Unknown tool'):
                await server.call_tool(name, {})

    asyncio.run(exercise())


def test_stdio_mcp_write_read_recall_and_retry(tmp_path):
    database, index, config = tmp_path / "runtime.db", tmp_path / "index.db", tmp_path / "config.toml"
    with Store(database) as store:
        store.create('event_stdio', 'event', '归航', '雨天归航')
        source = store.add_source('message/1', '原文')
        store.bind('event_stdio', source)
    build_index(database, index)
    config.write_text('[storage]\ndatabase="runtime.db"\nindex="index.db"\n[runtime]\nwritable=true\n', encoding="utf-8")

    async def exercise():
        parameters = StdioServerParameters(command=sys.executable, args=["-m", "serein", "--config", str(config), "mcp"],
                                          env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listing = await session.list_tools()
                names = {tool.name for tool in listing.tools}
                assert {"read_memory", "recall_memory", "write_scene", "edit_scene", "write_diary", "propose_memory"} <= names
                for name in ('read_memory', 'recall_memory', 'find_arc', 'read_arc_materials', 'read_diary'):
                    assert next(tool for tool in listing.tools if tool.name == name).outputSchema is None
                assert 'update_evidence' not in names
                assert not {"handoff", "breath", "resume_context"} & names
                from serein.deployment import save_settings
                save_settings(database,{'features':{'memos':True}})
                assert 'memo_list' in {tool.name for tool in (await session.list_tools()).tools}
                memo=await session.call_tool('memo_create',{'title':'Book','content':'Bring it','memo_id':'stdio-memo'})
                assert not memo.isError
                save_settings(database,{'features':{'memos':False}})
                assert 'memo_list' not in {tool.name for tool in (await session.list_tools()).tools}
                assert (await session.call_tool('memo_list',{})).isError
                args = {"operation_id": "mcp-save", "title": "归航", "content": "雨天归航",
                        "cues": ["归航"], "date": "2026-09-14"}
                saved = await session.call_tool("write_scene", args)
                assert not saved.isError
                document_id = saved.structuredContent["id"]
                again = await session.call_tool("write_scene", args)
                assert again.structuredContent["id"] == document_id
                readback = await session.call_tool("read_memory", {"identifier": document_id})
                assert readback.structuredContent is None
                assert f'id: scene:{document_id}' in readback.content[0].text
                assert 'date: 2026-09-14' in readback.content[0].text
                assert 'body:\n雨天归航' in readback.content[0].text
                recalled = await session.call_tool("recall_memory", {"query": "归航"})
                assert recalled.structuredContent is None
                assert f'[typed_memory ref=scene:{document_id}]' in recalled.content[0].text
                assert 'promote_event_to_scene' not in names
                save_settings(database, {'features': {'event_to_scene': True}})
                assert 'promote_event_to_scene' in {t.name for t in (await session.list_tools()).tools}
                promoted = await session.call_tool('promote_event_to_scene', {
                    'operation_id':'mcp-promote', 'event_id':'event_stdio', 'expected_revision':1,
                    'title':'主窗口的归航', 'body_md':'我记得那天的雨。'})
                assert not promoted.isError
                assert promoted.structuredContent['event_surface']['reasons'] == ['promoted_to_scene', 'covered_by_scene']
                scene = await session.call_tool('read_memory', {'identifier':promoted.structuredContent['id']})
                assert scene.structuredContent is None and 'bound_sources: 1' in scene.content[0].text
                assert '原文' not in scene.content[0].text
                scene = await session.call_tool('read_memory', {'identifier':promoted.structuredContent['id'], 'with_evidence':True})
                assert scene.structuredContent is None and 'text:\n原文' in scene.content[0].text
                error = await session.call_tool("write_scene", {**args, "content": "changed"})
                assert error.isError
                save_settings(database,{'features':{'favorites':True}})
                favorite_args={**args,'operation_id':'favorite-new','favorite':True}
                favored=await session.call_tool('write_scene',favorite_args)
                assert not favored.isError and favored.structuredContent['favorite'] is True
                state={'operation_id':'favorite-existing','document_id':document_id,'expected_revision':1,'favorite':True}
                assert (await session.call_tool('set_memory_state',state)).structuredContent['favorite'] is True
                assert (await session.call_tool('set_memory_state',{**state,'operation_id':'invalid-bool','favorite':'yes'})).isError
                save_settings(database,{'features':{'favorites':False}})
                assert (await session.call_tool('set_memory_state',{**state,'operation_id':'disabled-favorite','favorite':False})).isError
                assert (await session.call_tool('write_scene',{**favorite_args,'operation_id':'disabled-new'})).isError
                # Adding an omitted favorite field does not change prior receipt arguments.
                assert (await session.call_tool('write_scene',args)).structuredContent['id']==document_id
    asyncio.run(exercise())


def test_read_only_mcp_has_no_writing_tools(tmp_path):
    server = create_server(Application(Settings(tmp_path / "not-opened.db")))
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == {"read_memory", "recall_memory", "find_arc", "read_arc_materials", "read_diary"}


@pytest.mark.parametrize('selected', [['save_memory'], ['write_scene'], ['edit_scene']])
def test_scene_tool_allowlist_upgrade(tmp_path, selected):
    database = tmp_path/'allowlist-upgrade.db'
    with Store(database):
        pass
    server = create_server(Application(Settings(database, writable=True, mcp_tools=selected)))
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == ({'write_scene','edit_scene'} if selected == ['save_memory'] else set(selected))
    with pytest.raises(Exception, match='Unknown tool'):
        asyncio.run(server.call_tool('save_memory', {}))


@pytest.mark.parametrize('kind', ['event','narrative'])
def test_edit_scene_rejects_other_memory_kinds(tmp_path, kind):
    database = tmp_path/'other-kind.db'
    with Store(database) as store:
        store.create('target', kind, 'Original title', 'Original body')
    server = create_server(Application(Settings(database, writable=True)))
    with pytest.raises(Exception, match='editable Scenes'):
        asyncio.run(server.call_tool('edit_scene', {'operation_id':'wrong-kind', 'scene_id':'target',
            'expected_revision':1, 'content':'Changed body'}))
    with Store(database, read_only=True) as store:
        assert store.read('target')['body_md'] == 'Original body'
        assert store.read('target')['revision'] == 1
        assert store.conn.execute('SELECT count(*) FROM write_receipts').fetchone()[0] == 0


def test_scene_only_schema_edit_preservation_and_proposal_boundary(tmp_path):
    database = tmp_path/'scene-contract.db'
    with Store(database) as store:
        store.create('scene_bound', 'scene', 'Before', 'Bound body',
                     metadata={'scene_cues':['old cue'], 'date':'2026-09-01', 'canonical_domain':'life',
                               'promoted_from_event':{'id':'event_original'}, 'custom':{'keep':True}})
        source = store.add_source('original:1', 'Exact original text')
        store.bind('scene_bound', source)
        store.create('scene_immutable', 'scene', 'Imported original', 'Immutable body',
                     metadata={'source_record_immutable':True})
    app = Application(Settings(database, writable=True))
    server = create_server(app)
    draft = {'title':'Updated Scene', 'body_md':'My authored prose', 'cues':['new cue'], 'date':'2026-09-14'}

    async def call(name, arguments):
        return (await server.call_tool(name, arguments))[1]

    async def exercise():
        for tool in await server.list_tools():
            assert tool.name != 'save_memory'
            if tool.name == 'propose_memory':
                schema = tool.inputSchema['$defs']['Draft']
                assert set(schema['required']) == {'title','body_md','cues','date'}
                assert set(schema['properties']) == {'title','body_md','cues','date','document_id','expected_revision','favorite'}
                assert schema['additionalProperties'] is False
            if tool.name == 'write_scene':
                assert set(tool.inputSchema['required']) == {'content','cues'}
                assert set(tool.inputSchema['properties']) == {'title','content','cues','date','domain','evidence_refs','favorite'}
                assert tool.inputSchema['additionalProperties'] is False
            if tool.name == 'edit_scene':
                assert set(tool.inputSchema['required']) == {'scene_id','expected_updated_at'}
                assert set(tool.inputSchema['properties']) == {'scene_id','expected_updated_at','title','content','cues'}
                assert tool.inputSchema['additionalProperties'] is False
        edit = {'scene_id':'scene_bound', 'expected_revision':1, 'content':draft['body_md']}
        saved = await call('edit_scene', {'operation_id':'edit', **edit})
        assert saved['revision'] == 2
        assert (await call('edit_scene', {'operation_id':'edit', **edit}))['revision'] == 2
        reread = app.services.read('scene_bound')
        assert reread['document']['body_md'] == draft['body_md']
        assert reread['document']['title'] == 'Before'
        assert reread['document']['metadata'] == {
            'scene_cues':['old cue'], 'date':'2026-09-01', 'canonical_domain':'life',
            'promoted_from_event':{'id':'event_original'}, 'custom':{'keep':True}}
        assert reread['evidence'][0]['content'] == 'Exact original text'
        with pytest.raises(Exception, match='revision'):
            await call('edit_scene', {'operation_id':'stale', **edit})
        with pytest.raises(Exception, match='editable Scenes'):
            await call('edit_scene', {'operation_id':'immutable', **edit, 'scene_id':'scene_immutable'})
        await call('edit_scene', {'operation_id':'cue-date', 'scene_id':'scene_bound', 'expected_revision':2,
                                 'cues':draft['cues'], 'date':draft['date']})
        current = app.services.read('scene_bound')['document']
        assert current['revision'] == 3 and current['body_md'] == draft['body_md'] and current['title'] == 'Before'
        assert current['metadata']['scene_cues'] == ['new cue'] and current['metadata']['date'] == '2026-09-14'
        # A retry after another successful edit must not restore old content.
        assert (await call('edit_scene', {'operation_id':'edit', **edit}))['revision'] == 2
        assert app.services.read('scene_bound')['document']['revision'] == 3
        for i, patch in enumerate([{}, {'scene_id':'missing','title':'x'}, {'content':''}, {'content':' '},
                                   {'cues':[]}, {'cues':[' ']}, {'date':'2026-02-30'}, {'kind':'event'},
                                   {'content':None}, {'favorite':True}, {'expected_revision':0,'title':'x'}]):
            with pytest.raises(Exception):
                await call('edit_scene', {'operation_id':f'invalid-edit-{i}', 'scene_id':'scene_bound', 'expected_revision':3, **patch})
        for i, invalid in enumerate([
            {**draft,'kind':'event'}, {**draft,'kind':'narrative'}, {**draft,'sources':[]},
            {**draft,'source_message_ids':[1]}, {**draft,'metadata':{}},
            {**draft,'document_id':'scene_bound'}, {**draft,'expected_revision':1},
            {**draft,'date':'2026-02-30'}, {**draft,'date':'20260914'},
            {**draft,'cues':[]}, {**draft,'cues':[' ']}, {**draft,'cues':['x'*81]},
        ]):
            with pytest.raises(Exception):
                await call('propose_memory', {'operation_id':f'invalid-propose-{i}', 'draft':invalid})
            flat = {('content' if key == 'body_md' else key):value for key,value in invalid.items()}
            with pytest.raises(Exception):
                await call('write_scene', {'operation_id':f'invalid-write-{i}', **flat})
        with pytest.raises(Exception, match='Unknown tool'):
            await call('save_memory', {'operation_id':'retired', 'draft':draft})

        proposal = await call('propose_memory', {'operation_id':'propose-scene', 'draft':draft})
        with Store(database, read_only=True) as store:
            assert store.conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 2
        review = {'candidate_id':proposal['id'], 'decision':'accept'}
        accepted = await call('review_memory', {'operation_id':'accept-scene', **review})
        assert accepted['status'] == 'accepted'
        assert (await call('review_memory', {'operation_id':'accept-scene', **review}))['document']['id'] == accepted['document']['id']
        result = app.services.read(accepted['document']['id'])
        assert result['document']['metadata']['scene_cues'] == ['new cue']
        assert result['document']['metadata']['date'] == '2026-09-14'
        assert result['evidence'] == []

        # Legacy proposals cannot bypass the new main-model boundary; internal
        # Event and Narrative writers retain their original generic contract.
        for kind in ('event','narrative'):
            old = app.services.write('old-'+kind, 'propose', {'kind':kind, 'title':'Old', 'body_md':'Old draft',
                'sources':[{'source_key':'old:'+kind, 'content':'Original'}]})
            with pytest.raises(Exception, match='only accepts Scene'):
                await call('review_memory', {'operation_id':'accept-'+kind, 'candidate_id':old['id'], 'decision':'accept'})
            assert any(item['id'] == old['id'] for item in app.services.candidates()['items'])
            dismissed = await call('review_memory', {'operation_id':'dismiss-'+kind, 'candidate_id':old['id'], 'decision':'dismiss'})
            assert dismissed['status'] == 'dismissed'
        with Store(database, read_only=True) as store:
            assert store.conn.execute("SELECT count(*) FROM documents WHERE kind!='scene'").fetchone()[0] == 0
            assert store.conn.execute("SELECT count(*) FROM write_receipts WHERE operation_id LIKE 'invalid-%' OR operation_id IN ('stale','immutable','accept-event','accept-narrative')").fetchone()[0] == 0

    asyncio.run(exercise())


@pytest.mark.parametrize('selected', [False, True])
def test_promotion_optional_allowlist_can_start_disabled(tmp_path, selected):
    from serein.deployment import save_settings
    database = tmp_path/'allowlist.db'
    with Store(database):
        pass
    server = create_server(Application(Settings(database, writable=True,
        mcp_tools=['read_memory', *(['promote_event_to_scene'] if selected else [])])))
    async def exercise():
        assert {t.name for t in await server.list_tools()} == {'read_memory'}
        save_settings(database, {'features':{'event_to_scene':True}})
        assert ('promote_event_to_scene' in {t.name for t in await server.list_tools()}) is selected
        save_settings(database, {'features':{'event_to_scene':False}})
        assert {t.name for t in await server.list_tools()} == {'read_memory'}
    asyncio.run(exercise())


def test_retired_index_sync_is_rejected_from_allowlist(tmp_path):
    database = tmp_path/'retired-index-sync-allowlist.db'
    with Store(database):
        pass
    with pytest.raises(ValueError,match='unavailable'):
        create_server(Application(Settings(database, writable=True,mcp_tools=['read_memory','index_sync'])))


@pytest.mark.parametrize('configured,method', [(False,'lexical'), (True,'semantic')])
def test_mcp_omitted_recall_arguments_follow_provider_configuration(tmp_path, configured, method, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv('SEREIN_SNAPSHOT_ID', 'test-snapshot')
    database = tmp_path / 'memory.db'
    with Store(database):
        pass
    app = Application(Settings(database, embedding={'endpoint':'https://example.test'} if configured else {}))
    calls = []

    class FakeRecall:
        policy = SimpleNamespace(direct_threshold=.65)

        def __init__(self, settings):
            self.settings = settings

        def run(self, query, **options):
            calls.append((self.settings, query, options))
            return {'status':'matched', 'selected_refs':['scene:synthetic']}

    monkeypatch.setattr('serein.application.Recall', FakeRecall)
    server = create_server(app)

    async def exercise():
        tool = next(t for t in await server.list_tools() if t.name == 'recall_memory')
        assert tool.inputSchema['properties']['method']['default'] is None
        for arguments in ({'query':'a memory'}, {'query':'a memory', 'method':None, 'min_cosine':None}):
            await server.call_tool('recall_memory', arguments)
            assert calls[-1][2]['method'] == method
            assert calls[-1][2]['min_cosine'] == .5
        await server.call_tool('recall_memory', {'query':'a memory','method':'lexical'})
        assert calls[-1][2]['method'] == 'lexical'
        await server.call_tool('recall_memory', {'query':'a memory','method':'semantic','min_cosine':None})
        assert calls[-1][2]['method'] == 'semantic'
        assert calls[-1][2]['min_cosine'] == .5
        await server.call_tool('recall_memory', {'query':'a memory','method':'semantic','min_cosine':.7})
        assert calls[-1][2]['min_cosine'] == .7
    asyncio.run(exercise())
    assert 'read-only' in server.instructions and 'test-snapshot' in server.instructions


def test_mcp_automatic_method_tracks_saved_ui_embedding_without_restart(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from serein.configured_models import model_index, recall_resource_version
    from serein.deployment import save_settings, task_model

    database = tmp_path / 'memory.db'
    with Store(database):
        pass
    app = Application(Settings(database))
    calls = []

    class FakeRecall:
        policy = SimpleNamespace(direct_threshold=.65)

        def __init__(self, settings):
            self.settings = settings

        def run(self, query, **options):
            calls.append((self.settings, options))
            return {'status':'matched', 'selected_refs':['scene:synthetic']}

    # Keep the real Services.recall/effective_settings path; never call a provider.
    monkeypatch.setattr('serein.application.Recall', FakeRecall)
    server = create_server(app)
    save_settings(database, {'models':[
        {'id':name, 'label':name, 'model':name, 'protocol':'openai',
         'base_url':f'https://{name}.example/v1'} for name in ('embedding-a','embedding-b')
    ]})

    def prepare_fixture(name):
        save_settings(database, {'assignments':{'embedding':name}})
        root = model_index(app.settings, task_model(database, 'embedding'))
        root.mkdir(parents=True, exist_ok=True)
        # These only satisfy the configuration readiness guard. FakeRecall
        # deliberately avoids opening indexes, routing data or model endpoints.
        (root / 'scope.sqlite').touch()
        (root / 'policy.json').write_text('{}', encoding='utf-8')
        (root / 'ready.json').write_text(json.dumps({
            'recall_resources':recall_resource_version()}), encoding='utf-8')
        return root

    async def exercise():
        await server.call_tool('recall_memory', {'query':'synthetic'})
        assert calls[-1][1]['method'] == 'lexical'
        for name in ('embedding-a','embedding-b'):
            root = prepare_fixture(name)
            # Same server, no list_tools refresh: call-time selection is required.
            await server.call_tool('recall_memory', {'query':'synthetic'})
            effective, options = calls[-1]
            assert options['method'] == 'semantic'
            assert effective.index == root / 'index.sqlite'
            assert effective.embedding['endpoint'] == f'https://{name}.example/v1/embeddings'
            assert app.settings.embedding == {}
            await server.call_tool('recall_memory', {'query':'synthetic', 'method':'lexical'})
            assert calls[-1][1]['method'] == 'lexical'
        # A newly selected but unprepared model must not silently fall back.
        (root / 'ready.json').unlink()
        previous = len(calls)
        with pytest.raises(Exception, match='Prepare the selected embedding model'):
            await server.call_tool('recall_memory', {'query':'synthetic'})
        assert len(calls) == previous
        save_settings(database, {'assignments':{'embedding':''}})
        await server.call_tool('recall_memory', {'query':'synthetic'})
        assert calls[-1][1]['method'] == 'lexical'

    asyncio.run(exercise())

