"""Explicit Scene restore uses only temporary canonical stores and model stubs."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from serein.api.http import create_app
from serein.api.mcp import create_server
from serein.application import Application
from serein.bootstrap import initialize
from serein.compat.scenes import Scenes
from serein.config import Settings
from serein.core import Store
from serein.core.writer import Writer
from serein.deployment import save_settings
from serein.legacy_migration.scan import scan
from serein.legacy_migration.workflow import Migration


@pytest.fixture
def runtime(tmp_path):
    settings = Settings(tmp_path/'memory.db', tmp_path/'index.db', writable=True)
    initialize(settings)
    save_settings(settings.database, {'pipeline': {'auto_enabled': False}})
    return settings


def seed(settings, manual=False, lifecycle='archived', kind='scene', metadata=None):
    with Store(settings.database) as store:
        return store.create('synthetic', kind, 'Synthetic Scene', 'Exact authored body',
                            lifecycle=lifecycle, manual_surface=manual,
                            metadata=metadata or {'scene_status': lifecycle, 'active': lifecycle == 'active',
                                                 'type': 'archived' if lifecycle == 'archived' else 'scene',
                                                 'scene_cues': ['synthetic'], 'domain': ['general']})


def read(settings):
    with Store(settings.database, read_only=True) as store:
        return store.read('synthetic'), store.surface_state('synthetic')


@pytest.mark.parametrize('manual', [False, None, True])
def test_explicit_restore_is_atomic_versioned_and_idempotent(runtime, manual):
    before = seed(runtime, manual)
    scenes = Scenes(runtime.database)
    result = scenes.edit('synthetic', before['updated_at'], status='active', restore_surface=True)
    after, surface = read(runtime)
    assert result['status'] == 'updated'
    assert after['lifecycle'] == 'active' and after['manual_surface'] == 1
    assert surface['can_surface'] and not surface['reasons']
    assert after['revision'] == before['revision'] + 1
    assert after['body_md'] == before['body_md'] and after['title'] == before['title']
    assert after['metadata']['scene_cues'] == before['metadata']['scene_cues']
    assert result['updated_at'] == result['scene']['metadata']['updated_at'] == after['updated_at']
    assert scenes.edit('synthetic', before['updated_at'], status='active', restore_surface=True)['status'] == 'conflict'
    repeated = scenes.edit('synthetic', after['updated_at'], status='active', restore_surface=True)
    assert repeated['status'] == 'unchanged' and read(runtime)[0] == after


@pytest.mark.parametrize('manual', [False, None, True])
def test_lifecycle_only_restore_and_archive_preserve_manual_switch(runtime, manual):
    before = seed(runtime, manual)
    scenes = Scenes(runtime.database)
    active = scenes.edit('synthetic', before['updated_at'], status='active')
    assert read(runtime)[0]['manual_surface'] == manual
    scenes.edit('synthetic', active['updated_at'], status='archived')
    after, _ = read(runtime)
    assert after['lifecycle'] == 'archived' and after['manual_surface'] == manual


@pytest.mark.parametrize('manual', [False, None])
def test_explicit_restore_of_already_active_scene_does_not_short_circuit(runtime, manual):
    before = seed(runtime, manual, lifecycle='active')
    result = Scenes(runtime.database).edit('synthetic', before['updated_at'], status='active', restore_surface=True)
    assert result['status'] == 'updated'
    assert read(runtime)[0]['manual_surface'] == 1 and read(runtime)[1]['can_surface']


def test_official_legacy_migration_archive_can_be_explicitly_restored(runtime, tmp_path):
    folder = tmp_path/'old'/'buckets'/'archive'
    folder.mkdir(parents=True)
    (folder/'old.md').write_text('---\nid: old\nname: Synthetic archive\n---\nExact migrated body', encoding='utf-8')
    migration = Migration(runtime, scan(tmp_path/'old'), {'generate_cues': False})
    try:
        migration.import_bodies()
        key = migration.ids['old']
        with Store(runtime.database, read_only=True) as store:
            before = store.read(key)
        assert before['lifecycle'] == 'archived' and before['manual_surface'] == 0
        result = Scenes(runtime.database).edit(key, before['updated_at'], status='active', restore_surface=True)
        with Store(runtime.database, read_only=True) as store:
            after = store.read(key)
            assert store.surface_state(key)['can_surface']
        assert result['status'] == 'updated' and after['manual_surface'] == 1
        assert after['body_md'] == before['body_md']
    finally:
        migration.close()


def test_restore_rolls_back_lifecycle_manual_revision_and_outbox_on_failure(runtime, monkeypatch):
    before = seed(runtime)
    with Store(runtime.database, read_only=True) as store:
        outbox = [tuple(row) for row in store.conn.execute('SELECT * FROM index_outbox')]
    def fail(_writer, _key):
        raise RuntimeError('synthetic index failure')
    monkeypatch.setattr(Writer, '_dirty', fail)
    with pytest.raises(RuntimeError, match='synthetic index failure'):
        Scenes(runtime.database).edit('synthetic', before['updated_at'], status='active', restore_surface=True)
    assert read(runtime)[0] == before
    with Store(runtime.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM revisions').fetchone()[0] == 1
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM index_outbox')] == outbox


def test_concurrent_restore_and_edit_accept_only_one_version(runtime):
    before = seed(runtime)
    barrier = Barrier(2)
    def update(restore):
        barrier.wait()
        return Scenes(runtime.database).edit('synthetic', before['updated_at'],
            **({'status': 'active', 'restore_surface': True} if restore else {'content': 'New authored body'}))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, [True, False]))
    assert sorted(r['status'] for r in results) == ['conflict', 'updated']
    after, _ = read(runtime)
    assert after['revision'] == before['revision'] + 1
    if results[0]['status'] == 'updated':
        assert after['lifecycle'] == 'active' and after['manual_surface'] == 1
        assert after['body_md'] == before['body_md']
    else:
        assert after['lifecycle'] == 'archived' and after['manual_surface'] == 0
        assert after['body_md'] == 'New authored body'


@pytest.mark.parametrize('kind,lifecycle,immutable', [('event', 'archived', False), ('scene', 'deleted', False),
                                                   ('scene', 'archived', True)])
def test_invalid_or_immutable_targets_remain_untouched(runtime, kind, lifecycle, immutable):
    before = seed(runtime, kind=kind, lifecycle=lifecycle, metadata={'source_record_immutable': immutable})
    scenes = Scenes(runtime.database)
    if immutable:
        with pytest.raises(ValueError, match='Immutable'):
            scenes.edit('synthetic', before['updated_at'], status='active', restore_surface=True)
    else:
        assert scenes.edit('synthetic', before['updated_at'], status='active', restore_surface=True)['status'] == 'not_found'
    assert read(runtime)[0] == before
    assert scenes.edit('missing', before['updated_at'], status='active', restore_surface=True)['status'] == 'not_found'


@pytest.mark.parametrize('flag,status', [('true', 'active'), (1, 'active'), (None, 'active'),
                                       (True, 'archived'), (True, 'deleted'), (True, None)])
def test_restore_validation_never_mutates(runtime, flag, status):
    before = seed(runtime)
    with pytest.raises(ValueError, match='restore_surface'):
        Scenes(runtime.database).edit('synthetic', before['updated_at'], status=status, restore_surface=flag)
    assert read(runtime)[0] == before


def test_http_restore_requires_auth_and_read_only_has_no_write_route(runtime):
    before = seed(runtime)
    body = {'name': 'set_scene_status', 'arguments': {'scene_id': 'synthetic',
            'expected_updated_at': before['updated_at'], 'status': 'active', 'restore_surface': True}}
    # No lifespan/background service is started by these request-only clients.
    client = TestClient(create_app(runtime, token='synthetic-token', live=True))
    assert client.post('/v1/tools/call', json=body).status_code == 401
    assert read(runtime)[0] == before
    response = client.post('/v1/tools/call', json=body, headers={'Authorization': 'Bearer synthetic-token'})
    assert response.status_code == 200 and response.json()['result']['status'] == 'updated'
    assert read(runtime)[1]['can_surface']
    readonly = replace(runtime, writable=False)
    client = TestClient(create_app(readonly, token='synthetic-token'))
    assert client.post('/v1/tools/call', json=body, headers={'Authorization': 'Bearer synthetic-token'}).status_code == 404
    server = create_server(Application(readonly))
    assert 'set_scene_status' not in {t.name for t in asyncio.run(server.list_tools())}


def test_mcp_explicit_restore_and_lifecycle_only_contract(runtime, monkeypatch):
    before = seed(runtime)
    app = Application(runtime)
    synced = []
    monkeypatch.setattr(app.services, 'sync_index', lambda **kw: synced.append(kw))
    server = create_server(app)
    tool = next(t for t in asyncio.run(server.list_tools()) if t.name == 'set_scene_status')
    assert tool.inputSchema['properties']['restore_surface']['default'] is False
    args = {'scene_id': 'synthetic', 'expected_updated_at': before['updated_at'], 'status': 'active'}
    def call(arguments):
        value = asyncio.run(server.call_tool('set_scene_status', arguments))
        assert not getattr(value, 'isError', False)
    call(args)
    active, _ = read(runtime)
    assert active['manual_surface'] == 0
    call({**args, 'expected_updated_at': active['updated_at'], 'restore_surface': True})
    assert read(runtime)[1]['can_surface']
    assert synced == [{'document_ids': ['synthetic']}, {'document_ids': ['synthetic']}]


@pytest.mark.parametrize('flag,status', [('true', 'active'), (1, 'active'), (None, 'active'),
                                       (True, 'archived'), (True, 'deleted')])
def test_mcp_rejects_invalid_restore_requests(runtime, flag, status):
    before = seed(runtime)
    server = create_server(Application(runtime))
    with pytest.raises(Exception):
        asyncio.run(server.call_tool('set_scene_status', {'scene_id': 'synthetic',
            'expected_updated_at': before['updated_at'], 'status': status, 'restore_surface': flag}))
    assert read(runtime)[0] == before
