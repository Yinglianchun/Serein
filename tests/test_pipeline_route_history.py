"""Synthetic old-window recovery; no production data or real model calls."""
import asyncio
from copy import deepcopy
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from test_public_features import settings, ingest, synthetic_runner, output_for
from serein.core.store import Store, encode, Conflict
from serein.extensions import pipeline as p, pipeline_recovery as recovery
from serein.api.http import create_app
from serein import work_tasks


def freeze(settings):
    p.initialize(settings.database)
    batch = p.new_batch(settings.database, True)
    return batch, json.loads(batch['input_json'])


def history(settings, data, *, key='route:synthetic', legacy=False, runner=synthetic_runner):
    origin = {k: deepcopy(data[k]) for k in ('contract', 'routing_messages', 'tracks',
             'next_track_ordinal', 'scope', 'recent', 'day', 'input_policy') if k in data}
    batch = {'id': key, 'scope': data['scope'], 'input_json': encode(origin)}
    with Store(settings.database) as store:
        store.conn.execute("INSERT INTO pipeline_batches(id,scope,input_json,status) VALUES (?,?,?,'routing_only')",
                           (key, data['scope'], encode(origin)))
    routed = asyncio.run(p.route_batch(settings.database, batch, origin, runner))
    with Store(settings.database) as store, store.transaction(immediate=True):
        p.track_state.persist(store.conn, routed['track_state_updates'], data['scope'])
        if legacy:
            for a in routed['assignments']:
                store.conn.execute('INSERT OR REPLACE INTO pipeline_routes VALUES (?,?)',
                                   (a['source_message_id'], encode(a)))
        else:
            origin['routing_result'] = routed
            recovery.record_routes(store.conn, key, routed['assignments'])
        store.conn.execute("UPDATE pipeline_batches SET status='routed',input_json=? WHERE id=?", (encode(origin), key))
    return routed


def move_card(settings, routed, *, missing=False):
    card = deepcopy(routed['tracks'][0])
    card.update(last_session_id='later-window', recent_source_message_ids=[999],
                throughline='LATER WINDOW MUST NOT ENTER OLDER PLAN')
    with Store(settings.database) as store:
        if missing:
            store.conn.execute('DELETE FROM pipeline_tracks WHERE id=?', (card['track_id'],))
        else:
            store.conn.execute('UPDATE pipeline_tracks SET scope=?,card_json=? WHERE id=?',
                               ('later-window', encode(card), card['track_id']))
    return card


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('missing', [False, True])
def test_recover_producer_without_rerouting_or_overwriting_later_card(settings, legacy, missing):
    ingest(settings)
    batch, data = freeze(settings)
    original = history(settings, data, legacy=legacy)
    later = move_card(settings, original, missing=missing)
    calls = []
    async def runner(role, request):
        calls.append(role)
        assert role != 'track_router'
        assert 'LATER WINDOW' not in request['prompt']
        return output_for(role, request)
    result = asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))
    assert result['events'] == 1 and calls == ['event_curator', 'event_writer']
    with Store(settings.database, read_only=True) as store:
        frozen = json.loads(store.conn.execute('SELECT input_json FROM pipeline_batches WHERE id=?', (batch['id'],)).fetchone()[0])
        assert frozen['routing_result']['recovered_route_sources'][0]['batch_id'] == 'route:synthetic'
        assert frozen['routing_result']['assignments'] == original['assignments']
        card = json.loads(store.conn.execute('SELECT card_json FROM pipeline_tracks').fetchone()[0])
        assert card == (original['tracks'][0] if missing else later)
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 2
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))['status'] == 'current'


def test_historical_recovery_never_overwrites_existing_card_without_anchor_metadata(settings):
    ingest(settings)
    batch, data = freeze(settings)
    routed = history(settings, data)
    later = deepcopy(routed['tracks'][0])
    later.pop('recent_source_message_ids', None)
    later.pop('recent_turns', None)
    later.update(last_session_id='later-window', throughline='LEGACY LATER CARD WITHOUT ANCHORS')
    with Store(settings.database) as store:
        store.conn.execute('UPDATE pipeline_tracks SET scope=?,card_json=? WHERE id=?',
                           ('later-window', encode(later), later['track_id']))
    async def runner(role, request):
        assert role != 'track_router'
        assert 'LEGACY LATER CARD WITHOUT ANCHORS' not in request['prompt']
        return output_for(role, request)
    result = asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))
    assert result['events'] == 1
    with Store(settings.database, read_only=True) as store:
        frozen = json.loads(store.conn.execute(
            'SELECT input_json FROM pipeline_batches WHERE id=?', (batch['id'],)).fetchone()[0])
        assert frozen['routing_result']['recovered_route_sources'][0]['batch_id'] == 'route:synthetic'
        assert json.loads(store.conn.execute(
            'SELECT card_json FROM pipeline_tracks WHERE id=?', (later['track_id'],)).fetchone()[0]) == later


def test_completed_downstream_jobs_reuse_frozen_plan_after_historical_recovery(settings, monkeypatch):
    ingest(settings)
    batch, data = freeze(settings)
    routed = history(settings, data, legacy=True)
    settle = p.settle
    monkeypatch.setattr(p, 'settle', lambda *a: (_ for _ in ()).throw(RuntimeError('interrupted')))
    with pytest.raises(RuntimeError, match='interrupted'):
        asyncio.run(p.advance(settings.database, include_recent=True, runner=synthetic_runner))
    monkeypatch.setattr(p, 'settle', settle)
    with Store(settings.database) as store:
        frozen = json.loads(store.conn.execute('SELECT input_json FROM pipeline_batches WHERE id=?', (batch['id'],)).fetchone()[0])
        frozen.pop('routing_result')  # old version saved component/jobs only
        store.conn.execute('UPDATE pipeline_batches SET input_json=? WHERE id=?', (encode(frozen), batch['id']))
        outputs = dict(store.conn.execute('SELECT id,output_json FROM pipeline_jobs WHERE batch_id=?', (batch['id'],)))
    later = move_card(settings, routed)
    async def forbidden(*a):
        pytest.fail('accepted work was repeated')
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=forbidden))['events'] == 1
    with Store(settings.database, read_only=True) as store:
        assert dict(store.conn.execute('SELECT id,output_json FROM pipeline_jobs WHERE batch_id=?', (batch['id'],))) == outputs
        assert json.loads(store.conn.execute('SELECT card_json FROM pipeline_tracks').fetchone()[0]) == later


def test_source_snapshot_without_jobs_is_self_contained(settings):
    ingest(settings)
    _, data = freeze(settings)
    routed = history(settings, data)
    move_card(settings, routed)
    with Store(settings.database) as store:
        store.conn.execute("DELETE FROM pipeline_jobs WHERE batch_id='route:synthetic'")
    assert p.cached_route_result(settings.database, data)['assignments'] == routed['assignments']


def test_legacy_chunk_replay_never_uses_end_of_day_card_for_early_batch(settings):
    ingest(settings)
    _, early = freeze(settings)
    ingest(settings, 2)
    with Store(settings.database, read_only=True) as store:
        messages = [p.message(r) for r in store.conn.execute('SELECT * FROM raw_events ORDER BY id')]
    source = {**early, 'routing_messages': messages, 'input_policy': {'max_input_chars': 40}}
    async def router(role, request):
        output = output_for(role, request)
        output['track_updates'][0]['throughline'] = 'EARLY' if max(m['id'] for m in request['messages']) == 2 else 'FUTURE'
        return output
    routed = history(settings, source, legacy=True, runner=router)
    move_card(settings, routed)
    recovered = p.cached_route_result(settings.database, early)
    assert recovered['tracks'][0]['throughline'] == 'EARLY'
    assert recovered['recovered_route_sources'][0]['source_message_ids'] == [1, 2]


@pytest.mark.parametrize('damage', ['scope', 'message', 'bridge', 'ordinal', 'producer', 'incomplete'])
def test_incompatible_producer_never_bypasses_repair(settings, damage):
    ingest(settings)
    _, data = freeze(settings)
    routed = history(settings, data)
    move_card(settings, routed)
    with Store(settings.database) as store:
        row = store.conn.execute("SELECT * FROM pipeline_jobs WHERE batch_id='route:synthetic'").fetchone()
        request, output = json.loads(row['request_json']), json.loads(row['output_json'])
        if damage == 'scope':
            store.conn.execute("UPDATE pipeline_batches SET scope='foreign' WHERE id='route:synthetic'")
        elif damage == 'message':
            request['messages'][0]['content'] = 'CHANGED EVIDENCE'
        elif damage == 'bridge':
            output['message_assignments'][0]['routing_role'] = 'bridge'
        elif damage == 'ordinal':
            request['next_track_ordinal'] = 99
        elif damage == 'producer':
            request['batch_id'] = 'route:unrelated'
        else:
            output = None
        store.conn.execute('UPDATE pipeline_jobs SET request_json=?,output_json=? WHERE id=?',
                           (encode(request), encode(output) if output else None, row['id']))
    async def forbidden(*a):
        pytest.fail('unverifiable routes reached model')
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=forbidden))['status'] == 'needs_repair'
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 0


def test_ambiguous_legacy_history_is_not_guessed(settings):
    ingest(settings)
    _, data = freeze(settings)
    history(settings, data, legacy=True)
    async def other(role, request):
        output = output_for(role, request)
        output['track_updates'][0]['throughline'] = 'different interpretation'
        return output
    routed = history(settings, data, key='route:other', legacy=True, runner=other)
    move_card(settings, routed)
    with pytest.raises(p.RoutingRecoveryError, match='ambiguous'):
        p.cached_route_result(settings.database, data)


def hold_bad_cache(settings):
    batch, data = freeze(settings)
    with Store(settings.database) as store:
        for m in data['routing_messages']:
            store.conn.execute('INSERT INTO pipeline_routes VALUES (?,?)', (m['id'], encode({
                'source_message_id': m['id'], 'primary_track_id': 'session_'+data['scope']+'_track_0005',
                'context_track_ids': [], 'routing_role': 'primary_activity'})))
    result = asyncio.run(p.advance(settings.database, include_recent=True, runner=synthetic_runner))
    assert result['status'] == 'needs_repair'
    return batch, data


def test_confirmed_rebuild_exits_hold_preserves_records_and_avoids_bad_cache(settings):
    ingest(settings)
    batch, data = hold_bad_cache(settings)
    with Store(settings.database) as store:
        store.conn.execute('INSERT INTO pipeline_jobs VALUES (?,?,?,?,?)',
                           (batch['id']+':event_writer:0:0', batch['id'], 'event_writer:0:0',
                            encode({'role': 'event_writer'}), encode({'event_draft': 'OLD DRAFT MUST SURVIVE'})))
    with pytest.raises(ValueError):
        asyncio.run(recovery.rebuild(settings.database, batch['id'], ''))
    result = asyncio.run(recovery.rebuild(settings.database, batch['id'], 'REBUILD_PIPELINE_BATCH'))
    assert result['status'] == 'rebuilt'
    with Store(settings.database, read_only=True) as store:
        old = store.conn.execute('SELECT * FROM pipeline_batches WHERE id=?', (batch['id'],)).fetchone()
        assert old['input_json'] == batch['input_json'] and old['status'] == 'superseded_repair'
        assert len(json.loads(old['result_json'])['discarded_route_cache']) == 2
        assert 'OLD DRAFT MUST SURVIVE' in store.conn.execute('SELECT output_json FROM pipeline_jobs').fetchone()[0]
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 0
        assert store.conn.execute('SELECT count(*) FROM pipeline_routes').fetchone()[0] == 0
    with pytest.raises(ValueError, match='任务输入已更新'):
        p.submit(settings.database, batch['id']+':event_writer:0:0', {'event_draft': 'changed'})
    calls = []
    async def runner(role, request):
        calls.append(role)
        assert 'OLD DRAFT' not in request['prompt']
        return output_for(role, request)
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))['events'] == 1
    assert calls == ['track_router', 'event_curator', 'event_writer']
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT id FROM pipeline_tracks').fetchone()[0].endswith('_0006')
    with pytest.raises(Conflict):
        asyncio.run(recovery.rebuild(settings.database, batch['id'], 'REBUILD_PIPELINE_BATCH'))


def test_rebuild_http_auth_confirm_target_and_busy_lease(settings):
    ingest(settings)
    batch, _ = hold_bad_cache(settings)
    app = create_app(settings, token='synthetic', live=True)
    client = TestClient(app)
    payload = {'batch_id': batch['id'], 'confirm': 'REBUILD_PIPELINE_BATCH'}
    assert client.post('/v1/pipeline/rebuild', json=payload).status_code == 401
    client.headers['Authorization'] = 'Bearer synthetic'
    assert client.post('/v1/pipeline/rebuild', json={**payload, 'confirm': ''}).status_code == 400
    # A real queued/running pipeline must retain exclusive ownership.
    work_tasks.enqueue(settings.database, 'pipeline')
    assert client.post('/v1/pipeline/rebuild', json=payload).json()['status'] == 'busy'
    work_tasks.pause(settings.database, 'pipeline')
    response = client.post('/v1/pipeline/rebuild', json=payload)
    assert response.status_code == 200 and response.json()['status'] == 'rebuilt'


@pytest.mark.parametrize('committed', [True, False])
def test_rebuild_refuses_processed_or_changed_originals_atomically(settings, committed):
    ingest(settings)
    batch, _ = hold_bad_cache(settings)
    with Store(settings.database) as store:
        if committed:
            store.conn.execute("INSERT INTO raw_processing VALUES (1,'another-operation','settled')")
        else:
            store.conn.execute("UPDATE raw_events SET text='changed' WHERE id=2")
    with pytest.raises(Conflict):
        asyncio.run(recovery.rebuild(settings.database, batch['id'], 'REBUILD_PIPELINE_BATCH'))
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT status FROM pipeline_batches WHERE id=?', (batch['id'],)).fetchone()[0] == 'needs_repair'
        assert store.conn.execute('SELECT count(*) FROM pipeline_routes').fetchone()[0] == 2


def test_snapshot_and_provenance_publish_rollback_together(settings):
    ingest(settings)
    batch, data = freeze(settings)
    routed = asyncio.run(p.route_batch(settings.database, batch, data, synthetic_runner))
    with Store(settings.database) as store:
        with pytest.raises(RuntimeError), store.transaction(immediate=True):
            store.conn.execute('UPDATE pipeline_batches SET input_json=? WHERE id=?', (encode({**data, 'routing_result': routed}), batch['id']))
            recovery.record_routes(store.conn, batch['id'], routed['assignments'])
            raise RuntimeError('interrupted publication')
        assert store.conn.execute('SELECT count(*) FROM pipeline_route_provenance').fetchone()[0] == 0
        assert store.conn.execute('SELECT count(*) FROM pipeline_routes').fetchone()[0] == 0
        assert 'routing_result' not in json.loads(store.conn.execute('SELECT input_json FROM pipeline_batches WHERE id=?', (batch['id'],)).fetchone()[0])


def test_future_producer_job_is_not_shortened_into_a_fake_old_card(settings):
    ingest(settings)
    _, early = freeze(settings)
    ingest(settings, 2)
    with Store(settings.database, read_only=True) as store:
        messages = [p.message(r) for r in store.conn.execute('SELECT * FROM raw_events ORDER BY id')]
    # Unknown timestamps put both pairs in one real Router job.
    messages = [{**m, 'metadata': {**m['metadata'], 'timestamp_source': 'import_time'}} for m in messages]
    source = {**early, 'routing_messages': messages, 'input_policy': {'max_input_chars': 5000}}
    routed = history(settings, source)
    move_card(settings, routed)
    with pytest.raises(p.RoutingRecoveryError, match='frozen input range'):
        p.cached_route_result(settings.database, early)


def test_future_anchor_in_jobless_snapshot_is_rejected(settings):
    ingest(settings)
    _, data = freeze(settings)
    routed = history(settings, data)
    move_card(settings, routed)
    with Store(settings.database) as store:
        store.conn.execute("DELETE FROM pipeline_jobs WHERE batch_id='route:synthetic'")
        source = json.loads(store.conn.execute("SELECT input_json FROM pipeline_batches WHERE id='route:synthetic'").fetchone()[0])
        source['routing_result']['tracks'][0]['recent_source_message_ids'] = [999]
        store.conn.execute("UPDATE pipeline_batches SET input_json=? WHERE id='route:synthetic'", (encode(source),))
    with pytest.raises(p.RoutingRecoveryError, match='future Track'):
        p.cached_route_result(settings.database, data)


def test_downstream_input_drift_cannot_attach_to_recovered_routes(settings, monkeypatch):
    ingest(settings)
    batch, data = freeze(settings)
    history(settings, data, legacy=True)
    monkeypatch.setattr(p, 'settle', lambda *a: (_ for _ in ()).throw(RuntimeError('interrupted')))
    with pytest.raises(RuntimeError):
        asyncio.run(p.advance(settings.database, include_recent=True, runner=synthetic_runner))
    with Store(settings.database) as store:
        row = store.conn.execute("SELECT * FROM pipeline_jobs WHERE batch_id=? AND role='event_curator:0'", (batch['id'],)).fetchone()
        request = json.loads(row['request_json'])
        request['component']['memberships'][0]['routing_role'] = 'bridge'
        store.conn.execute('UPDATE pipeline_jobs SET request_json=? WHERE id=?', (encode(request), row['id']))
    async def forbidden(*a):
        pytest.fail('drifted ownership reached a model')
    result = asyncio.run(p.advance(settings.database, include_recent=True, runner=forbidden))
    assert result['status'] == 'needs_repair' and 'downstream job' in result['reason']


def test_rebuild_preserves_published_events_other_caches_and_queue_priority(settings):
    ingest(settings)
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=synthetic_runner))['events'] == 1
    with Store(settings.database, read_only=True) as store:
        events = [tuple(r) for r in store.conn.execute('SELECT * FROM fact_events')]
        sources = [tuple(r) for r in store.conn.execute('SELECT * FROM fact_event_sources')]
        routes = [tuple(r) for r in store.conn.execute('SELECT * FROM pipeline_routes')]
    ingest(settings, 2)
    batch, data = hold_bad_cache(settings)
    with Store(settings.database) as store:
        store.conn.execute("INSERT INTO pipeline_batches(id,scope,input_json,status) VALUES ('later-held',?,?,'needs_repair')", (data['scope'], encode(data)))
    result = asyncio.run(recovery.rebuild(settings.database, batch['id'], 'REBUILD_PIPELINE_BATCH'))
    assert p.new_batch(settings.database, True)['id'] == result['batch_id']
    with Store(settings.database, read_only=True) as store:
        assert [tuple(r) for r in store.conn.execute('SELECT * FROM fact_events')] == events
        assert [tuple(r) for r in store.conn.execute('SELECT * FROM fact_event_sources')] == sources
        assert [tuple(r) for r in store.conn.execute('SELECT * FROM pipeline_routes')] == routes


def test_daily_flush_saves_producer_snapshot_and_route_provenance(settings, monkeypatch):
    from test_pipeline_limits import pairs
    from serein.compat.raw_archive import raw_archive
    from serein.deployment import save_settings
    raw_archive(settings).ingest([{'source_event_id': str(i), 'session_id': 'daytime',
        'role': m['role'], 'text': m['content'], 'created_at': m['created_at']}
        for i, m in enumerate(pairs(5, True))], source='test')
    save_settings(settings.database, {'models': [{'id': 'local', 'model': 'synthetic', 'base_url': 'http://127.0.0.1:9/v1'}],
                                     'assignments': {'track_router': 'local'}})
    original = p.route_batch
    async def routed(database, batch, data, runner):
        return await original(database, batch, data, synthetic_runner)
    monkeypatch.setattr(p, 'route_batch', routed)
    asyncio.run(p.flush_routes(settings.database))
    with Store(settings.database, read_only=True) as store:
        source = store.conn.execute("SELECT * FROM pipeline_batches WHERE status='routed'").fetchone()
        snapshot = json.loads(source['input_json'])['routing_result']
        p.validate_routing_result(json.loads(source['input_json']), snapshot)
        links = list(store.conn.execute('SELECT * FROM pipeline_route_provenance'))
        assert len(links) == 10 and all(row['batch_id'] == source['id'] for row in links)
        assert [json.loads(row['route_json']) for row in links] == snapshot['assignments']


def test_missing_cache_row_can_recover_from_persisted_provenance(settings):
    ingest(settings)
    _, data = freeze(settings)
    routed = history(settings, data)
    move_card(settings, routed)
    with Store(settings.database) as store:
        store.conn.execute('DELETE FROM pipeline_routes WHERE raw_id=1')
    assert p.cached_route_result(settings.database, data)['assignments'] == routed['assignments']


def test_missing_cache_never_reroutes_under_frozen_downstream_work(settings, monkeypatch):
    ingest(settings)
    batch, data = freeze(settings)
    history(settings, data, legacy=True)
    monkeypatch.setattr(p, 'settle', lambda *a: (_ for _ in ()).throw(RuntimeError('interrupted')))
    with pytest.raises(RuntimeError):
        asyncio.run(p.advance(settings.database, include_recent=True, runner=synthetic_runner))
    with Store(settings.database) as store:
        saved = json.loads(store.conn.execute('SELECT input_json FROM pipeline_batches WHERE id=?', (batch['id'],)).fetchone()[0])
        saved.pop('routing_result')
        store.conn.execute('UPDATE pipeline_batches SET input_json=? WHERE id=?', (encode(saved), batch['id']))
        store.conn.execute('DELETE FROM pipeline_routes')
    async def forbidden(*a):
        pytest.fail('cannot reroute while old ownership is still frozen')
    result = asyncio.run(p.advance(settings.database, include_recent=True, runner=forbidden))
    assert result['status'] == 'needs_repair' and 'route proof' in result['reason']


def test_rebuild_audits_orphan_provenance_before_invalidating_it(settings):
    ingest(settings)
    batch, data = hold_bad_cache(settings)
    with Store(settings.database) as store:
        row = store.conn.execute('SELECT route_json FROM pipeline_routes WHERE raw_id=1').fetchone()
        store.conn.execute('INSERT INTO pipeline_route_provenance VALUES (?,?,?)',
                           (1, 'route:missing', row[0]))
        store.conn.execute('DELETE FROM pipeline_routes WHERE raw_id=1')
    asyncio.run(recovery.rebuild(settings.database, batch['id'], 'REBUILD_PIPELINE_BATCH'))
    with Store(settings.database, read_only=True) as store:
        audit = json.loads(store.conn.execute('SELECT result_json FROM pipeline_batches WHERE id=?',
                                             (batch['id'],)).fetchone()[0])
        archived = next(item for item in audit['discarded_route_cache'] if item['raw_id'] == 1)
        assert archived['route_json'] is None
        assert archived['provenance']['batch_id'] == 'route:missing'
        assert store.conn.execute('SELECT count(*) FROM pipeline_route_provenance').fetchone()[0] == 0
