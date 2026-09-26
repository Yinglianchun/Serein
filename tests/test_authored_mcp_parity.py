import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from serein.api.http import create_app
from serein.api.mcp import create_server
from serein.api.read_text import diary_text
from serein.application import Application
from serein.bootstrap import initialize
from serein.config import Settings
from serein.core import Store
from serein.deployment import save_settings


@pytest.fixture
def runtime(tmp_path):
    settings = Settings(tmp_path/'memory.db', writable=True)
    initialize(settings)
    save_settings(settings.database, {'pipeline': {'auto_enabled': False},
                                      'identity': {'ai_name': 'Synthetic Companion'}})
    return settings


def test_diary_text_includes_original_binding_and_timestamp():
    text = diary_text({'diaries': [{'id': 7, 'entry_type': 'diary', 'title': 'Rain',
        'date': '2026-09-17', 'created_at': '2026-09-17T01:02:03+08:00',
        'content': 'Body', 'comments': [], 'source_id': 'legacy-diary:7'}]})
    assert 'created_at: 2026-09-17T01:02:03+08:00' in text
    assert 'bound_sources: 1' in text and '[source 1] source_id=legacy-diary:7' in text


def call(server, name, **args):
    value = asyncio.run(server.call_tool(name, args))
    if isinstance(value, tuple):
        return value[1]
    text = value[0].text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def test_new_schema_has_self_use_required_fields_and_defaults(runtime):
    save_settings(runtime.database, {'features': {'favorites': True}})
    server = create_server(Application(runtime))
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    required = {'write_scene': {'content', 'cues'}, 'edit_scene': {'scene_id', 'expected_updated_at'},
                'set_scene_status': {'scene_id', 'expected_updated_at', 'status'},
                'read_diary': set(), 'write_diary': {'content'}, 'revise_diary': {'diary_id', 'content'},
                'comment_diary': {'diary_id', 'content'}, 'delete_diary': {'diary_id'},
                'annotate': {'memory_id', 'content'}}
    for name, fields in required.items():
        schema = tools[name].inputSchema
        assert set(schema.get('required', [])) == fields
        assert 'operation_id' not in schema['properties']
        assert 'expected_revision' not in schema['properties']
        assert schema['additionalProperties'] is False
    for name in ('write_scene', 'write_diary', 'comment_diary'):
        assert tools[name].annotations.idempotentHint is False
    favorites = tools['read_favorites'].inputSchema['properties']
    assert favorites['limit']['default'] == 5
    assert favorites['include_archived']['default'] is False
    assert tools['write_diary'].inputSchema['properties']['author']['default'] == 'ai'
    for name in ('read_memory', 'recall_memory', 'find_arc', 'read_arc_materials', 'read_diary', 'read_favorites'):
        assert tools[name].outputSchema is None


def test_scene_minimal_write_edit_status_annotations_and_legacy_retry(runtime):
    server = create_server(Application(runtime))
    reply = call(server, 'write_scene', content='Synthetic memory', cues=['synthetic'])
    key = re.search(r'\[scene_id:([^\]]+)\]', reply)[1]
    original_text = call(server, 'read_memory', identifier=key)
    assert f'id: scene:{key}' in original_text and 'body:\nSynthetic memory' in original_text
    with Store(runtime.database, read_only=True) as store:
        original = store.read(key)
    edit = call(server, 'edit_scene', scene_id=key, expected_updated_at=original['updated_at'], content='Edited memory')
    assert edit['status'] == 'updated'
    stale = call(server, 'edit_scene', scene_id=key, expected_updated_at=original['updated_at'], content='Stale overwrite')
    assert stale['status'] == 'conflict'
    annotation = call(server, 'annotate', memory_id=key, content='Separate annotation', annotation_id='note')
    assert annotation['value']['author'] == 'Synthetic Companion'
    assert call(server, 'annotate', memory_id=key, content='Separate annotation', annotation_id='note') == annotation
    archived = call(server, 'set_scene_status', scene_id=key, expected_updated_at=edit['updated_at'], status='archived')
    assert archived['scene']['metadata']['scene_status'] == 'archived'
    old_args = {'operation_id': 'old-client', 'title': 'Old call', 'content': 'Old contract', 'cues': ['old'], 'date': '2026-09-14'}
    old = call(server, 'write_scene', **old_args)
    assert call(server, 'write_scene', **old_args)['id'] == old['id']
    with pytest.raises(Exception, match='mix old'):
        call(server, 'write_scene', **old_args, domain='life')
    with Store(runtime.database, read_only=True) as store:
        assert store.read(key)['body_md'] == 'Edited memory'
        assert store.conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 2


@pytest.mark.parametrize('legacy',[False,True])
def test_semicolon_cues_on_mcp_write_and_edit(runtime,legacy):
    server = create_server(Application(runtime))
    first,second = 'rain'*12,'window'*8
    args = {'content':'Synthetic scene','cues':[f' {first};；{second};{first}； ']}
    if legacy:
        args.update(operation_id='semicolon-write',title='Synthetic',date='2026-09-26')
    reply = call(server,'write_scene',**args)
    key = reply['id'] if legacy else re.search(r'\[scene_id:([^\]]+)\]',reply)[1]
    with Store(runtime.database,read_only=True) as store:
        doc = store.read(key)
    assert doc['metadata']['scene_cues'] == [first,second]
    edit = {'scene_id':key,'cues':[' rain；window;rain； ']}
    if legacy:
        edit.update(operation_id='semicolon-edit',expected_revision=doc['revision'])
    else:
        edit['expected_updated_at'] = doc['updated_at']
    call(server,'edit_scene',**edit)
    with Store(runtime.database,read_only=True) as store:
        assert store.read(key)['metadata']['scene_cues'] == ['rain','window']


def test_proposed_scene_splits_cues_before_length_validation():
    from serein.api.mcp import Draft
    first,second = 'rain'*12,'window'*8
    draft = Draft(title='Synthetic',body_md='Synthetic scene',date='2026-09-26',
                  cues=[f'{first};{second}；{first}'])
    assert draft.cues == [first,second]


@pytest.mark.parametrize('favorite', [False, True])
def test_scene_domain_evidence_and_favorite_are_atomic(runtime, favorite):
    save_settings(runtime.database, {'features': {'favorites': favorite}})
    server = create_server(Application(runtime))
    args = {'content': 'Authored scene', 'cues': ['quote'], 'domain': 'life', 'favorite': True,
            'evidence_refs': [{'source_system': 'synthetic', 'session_id': 's', 'message_id': 'm',
                               'role': 'user', 'content': 'Exact synthetic quotation',
                               'created_at': '2026-09-14T12:00:00+08:00', 'binding_method': 'explicit'}]}
    if not favorite:
        with pytest.raises(Exception, match='disabled'):
            call(server, 'write_scene', **args)
        with Store(runtime.database, read_only=True) as store:
            for table in ('documents', 'sources', 'evidence_bindings', 'write_receipts'):
                assert store.conn.execute('SELECT count(*) FROM '+table).fetchone()[0] == 0
        return
    reply = call(server, 'write_scene', **args)
    key = re.search(r'\[scene_id:([^\]]+)\]', reply)[1]
    saved = call(server, 'read_memory', identifier=key, with_evidence=True)
    assert 'body:\nAuthored scene' in saved and 'text:\nExact synthetic quotation' in saved
    from serein.compat.scenes import Scenes
    assert Scenes(runtime.database).evidence(key)['evidence_refs'][0]['content'] == 'Exact synthetic quotation'
    with Store(runtime.database, read_only=True) as store:
        current = store.read(key)
        assert current['metadata']['canonical_domain'] == 'life'
    call(server, 'edit_scene', scene_id=key, expected_updated_at=current['updated_at'], content='Edited')
    assert 'text:\nExact synthetic quotation' in call(server, 'read_memory', identifier=key, with_evidence=True)
    favorites = call(server, 'read_favorites')
    assert '[favorites]' in favorites and f'id: scene:{key}' in favorites


def test_http_diary_self_use_flow_and_locked_rejections(runtime):
    headers = {'Authorization': 'Bearer synthetic', 'Accept': 'application/json, text/event-stream'}
    with TestClient(create_app(runtime, token='synthetic', live=True), headers=headers) as client:
        def rpc(name, args, error=False):
            result = client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                              'params': {'name': name, 'arguments': args}}).json()['result']
            assert bool(result.get('isError')) is error, result
            if error or result.get('structuredContent'):
                return result if error else result['structuredContent']
            text = result['content'][0]['text']
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        entry = rpc('write_diary', {'content': 'Synthetic diary'})
        key = entry['id']
        assert entry['author'] == 'ai'
        assert 'body:\nSynthetic diary' in rpc('read_diary', {'diary_id': key})
        diary_text = rpc('read_diary', {'diary_id': key})
        assert 'created_at:' in diary_text and 'bound_sources: 0' in diary_text
        assert 'count: 1' in rpc('read_diary', {'date': entry['date']})
        directory = rpc('read_diary', {'query': 'Synthetic', 'date': entry['date']})
        assert directory.startswith('Diary directory\n') and f'diary_id: {key}' in directory
        assert 'body:' not in directory and 'excerpt: Synthetic diary' in directory
        assert rpc('revise_diary', {'diary_id': key, 'content': 'Revised'})['revision'] == 2
        rpc('comment_diary', {'diary_id': key, 'content': 'Comment'})
        assert '[comment 1] author=ai' in rpc('read_diary', {'diary_id': key})
        rpc('write_diary', {'content': 'Must fail', 'kind': 'diary'}, error=True)
        locked = rpc('write_diary', {'content': 'Sealed private synthetic text',
                                     'unlock_at': (datetime.now(timezone.utc)+timedelta(days=1)).isoformat()})
        locked_key = locked['id']
        assert locked['content'] == '' and locked['body_available'] is False
        for name, args in [('revise_diary', {'content': 'Forbidden'}), ('comment_diary', {'content': 'Forbidden'}), ('delete_diary', {})]:
            rpc(name, {'diary_id': locked_key, **args}, error=True)
        rpc('delete_diary', {'diary_id': key})
        assert 'count: 0' in rpc('read_diary', {'diary_id': key})
        rpc('comment_diary', {'diary_id': key, 'content': 'After deletion'}, error=True)
    with Store(runtime.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM diary_revisions WHERE diary_id=?', (key,)).fetchone()[0] == 2
        assert store.conn.execute('SELECT visibility FROM diary_entries WHERE id=?', (key,)).fetchone()[0] == 'deleted'
        assert store.conn.execute('SELECT count(*) FROM diary_projection_pending').fetchone()[0] == 0


def test_diary_projection_failure_rolls_back_new_contract(runtime, monkeypatch):
    import serein.api.authored_tools as module
    def fail(_conn):
        raise RuntimeError('synthetic projection failure')
    monkeypatch.setattr(module, 'project_diaries', fail)
    with pytest.raises(Exception, match='projection failure'):
        call(create_server(Application(runtime)), 'write_diary', content='Rollback me')
    with Store(runtime.database, read_only=True) as store:
        for table in ('diaries', 'diary_entries', 'diary_projection_pending'):
            assert store.conn.execute('SELECT count(*) FROM '+table).fetchone()[0] == 0


def test_readonly_and_allowlist_cannot_reach_hidden_old_writers(runtime):
    readonly = create_server(Application(Settings(runtime.database, writable=False)))
    assert 'count: 0' in call(readonly, 'read_diary')
    for name in ('write_scene', 'set_scene_status', 'write_diary', 'revise_diary', 'annotate'):
        with pytest.raises(Exception, match='Unknown tool'):
            call(readonly, name, operation_id='old')
    restricted = create_server(Application(Settings(runtime.database, writable=True, mcp_tools=['read_diary'])))
    with pytest.raises(Exception, match='Unknown tool'):
        call(restricted, 'write_scene', operation_id='old', title='T', content='C', cues=['c'], date='2026-09-14')


def test_core_only_notebook_uses_same_calls_without_initializing_legacy_tables(tmp_path):
    settings = Settings(tmp_path/'core.db', writable=True)
    with Store(settings.database):
        pass
    server = create_server(Application(settings))
    assert 'count: 0' in call(server, 'read_diary')
    entry = call(server, 'write_diary', content='Core synthetic diary')
    key = entry['id']
    assert entry['author'] == 'ai' and entry['title'] == ''
    revised = call(server, 'revise_diary', diary_id=key, content='Core revised')
    assert revised['revision'] == 2
    call(server, 'comment_diary', diary_id=key, content='Core comment')
    read = call(server, 'read_diary', diary_id=key)
    assert 'body:\nCore revised' in read and 'Core comment' in read
    locked = call(server, 'write_diary', content='Core locked', unlock_at=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat())
    assert locked['content'] == '' and locked['body_available'] is False
    with pytest.raises(Exception, match='readable'):
        call(server, 'delete_diary', diary_id=locked['id'])
    call(server, 'delete_diary', diary_id=key)
    assert 'count: 0' in call(server, 'read_diary', diary_id=key)
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='diaries'").fetchone() is None
