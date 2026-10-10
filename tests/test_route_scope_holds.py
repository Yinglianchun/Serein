"""Offline scope-hold / 03:00 regression fixtures. No upstream connections."""
import asyncio
from datetime import datetime, timedelta, timezone
from importlib import import_module
import json

import pytest
from fastapi.testclient import TestClient
from test_public_features import settings, output_for, synthetic_runner, ingest
from serein.compat.raw_archive import raw_archive
from serein.core.store import Store, encode
from serein.deployment import save_settings
from serein.extensions import pipeline as p
from serein.extensions.pipeline_limits import routing_blocks
from serein.api.http import create_app


class Clock(datetime):
    value=datetime(2026,10,10,3,30,tzinfo=p.TZ)

    @classmethod
    def now(cls,tz=None):
        return cls.value.astimezone(tz or timezone.utc)


@pytest.fixture(autouse=True)
def reset_clock():
    Clock.value=datetime(2026,10,10,3,30,tzinfo=p.TZ)


def seed(settings, session='edge', start=None, count=7):
    start=start or Clock.value.replace(hour=2,minute=35)
    rows=[]
    for i in range(count):
        for role,offset in [('user',0),('assistant',1)]:
            rows.append({'source_event_id':f'{session}-{start.isoformat()}-{i}-{role}','session_id':session,
                         'role':role,'text':f'Book club plan {i} {role}',
                         'created_at':(start+timedelta(minutes=i*5+offset)).isoformat()})
    return raw_archive(settings).ingest(rows,source='test')


def api(settings,monkeypatch,callback):
    # These modules bind transports on import. Load them before the temporary
    # stub so later HTTP tests cannot retain this fixture's complete function.
    for module in ('serein.image_transcription', 'serein.api.chat'):
        import_module(module)
    save_settings(settings.database,{'models':[{'id':'stub','model':'synthetic','base_url':'http://127.0.0.1:9/v1'}],
                                     'assignments':{role:'stub' for role in p.ROLES}})
    monkeypatch.setattr(p,'datetime',Clock)
    calls=[]
    async def complete(model,payload):
        with Store(settings.database,read_only=True) as store:
            request=json.loads(store.conn.execute('SELECT request_json FROM pipeline_jobs WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        calls.append(request)
        result=callback(request)
        return {'choices':[{'message':{'content':result if isinstance(result,str) else encode(result)}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    return calls


def test_0300_producer_and_settlement_share_whole_envelopes(settings,monkeypatch):
    seed(settings)
    calls=api(settings,monkeypatch,lambda r:output_for(r['role'],r))
    asyncio.run(p.flush_routes(settings.database))
    assert [[m['id'] for m in r['messages']] for r in calls]==[list(range(1,11)),list(range(11,15))]
    with Store(settings.database,read_only=True) as store:
        proof=[tuple(r) for r in store.conn.execute('SELECT * FROM pipeline_route_provenance')]
        jobs=[tuple(r) for r in store.conn.execute('SELECT id,request_json,output_json FROM pipeline_jobs')]
    batch=p.new_batch(settings.database,False,Clock.value)
    data=json.loads(batch['input_json'])
    assert [m['id'] for m in data['routing_messages']]==list(range(1,11))
    assert [m['id'] for m in data['messages']]==[1,2]
    recovered=p.cached_route_result(settings.database,data)
    assert recovered['recovered_route_sources'][0]['source_message_ids']==list(range(1,11))
    result=asyncio.run(p.scheduled_advance(settings.database))
    assert result['status']=='processed'
    assert len([r for r in calls if r['role']=='track_router'])==2
    p.initialize(settings.database)
    assert asyncio.run(p.scheduled_advance(settings.database))['status']=='current'
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==2
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0]==14
        assert store.conn.execute('SELECT completed FROM pipeline_schedule').fetchone()[0]==1
        assert [tuple(r) for r in store.conn.execute('SELECT * FROM pipeline_route_provenance')][:14]==proof
        assert [tuple(r) for r in store.conn.execute('SELECT id,request_json,output_json FROM pipeline_jobs')][:2]==jobs


@pytest.mark.parametrize('limit',[1,10000])
def test_watermark_never_splits_pair_even_with_budget_change(settings,limit):
    seed(settings,start=Clock.value.replace(hour=2,minute=59),count=2)
    p.initialize(settings.database)
    batch=p.new_batch(settings.database,False,Clock.value)
    assert batch is None  # end at 03:00 is complete, but not stable before 02:40
    with Store(settings.database,read_only=True) as store:
        messages=[p.task_message(r) for r in store.conn.execute('SELECT * FROM raw_events ORDER BY id')]
    assert [[m['id'] for m in block] for block in routing_blocks(messages,limit)]==[[1,2],[3,4]]
    # A reply arriving beyond the watermark leaves BOTH halves parked.
    with Store(settings.database) as store:
        store.conn.execute("UPDATE raw_events SET created_at='2026-10-10T03:01:00+08:00' WHERE id=2")
    assert p.new_batch(settings.database,False,Clock.value) is None


@pytest.mark.parametrize('bad',['json','bridge','transport'])
def test_route_failure_budget_holds_scope_across_restart_new_raw_and_budget(settings,monkeypatch,bad):
    seed(settings)
    def broken(r):
        if bad=='json':return '{broken'
        if bad=='transport':raise OSError('synthetic network failure')
        result=output_for(r['role'],r)
        result['message_assignments'][0]['routing_role']='bridge'
        return result
    calls=api(settings,monkeypatch,broken)
    asyncio.run(p.flush_routes(settings.database))
    if bad=='transport':
        for i in range(2):
            p.initialize(settings.database)
            assert p.held_result(settings.database)['blocked_scopes'][0]['next_retry_at']
            before=len(calls)
            asyncio.run(p.flush_routes(settings.database))
            assert len(calls)==before
            Clock.value+=timedelta(minutes=11)
            asyncio.run(p.flush_routes(settings.database))
    assert len(calls)==3
    p.initialize(settings.database)
    seed(settings,start=Clock.value.replace(hour=4,minute=0),count=7,session='edge')
    save_settings(settings.database,{'pipeline':{'max_input_chars':10}})
    for _ in range(2):asyncio.run(p.flush_routes(settings.database))
    assert len(calls)==3
    with Store(settings.database,read_only=True) as store:
        paused=store.conn.execute("SELECT * FROM pipeline_batches WHERE status='paused_failure'").fetchone()
        assert paused and store.conn.execute('SELECT failures FROM pipeline_job_failures').fetchone()[0]==3
        assert store.conn.execute('SELECT count(*) FROM pipeline_attempts').fetchone()[0]==3
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0
    seed(settings,session='other',start=Clock.value-timedelta(hours=2),count=1)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))['events']==1
    p.retry_batch(settings.database,paused['id'])
    # Manual recovery uses exactly the old producer request, despite smaller budget.
    calls=api(settings,monkeypatch,lambda r:output_for(r['role'],r))
    asyncio.run(p.flush_routes(settings.database))
    assert len(calls)==2
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT status FROM pipeline_batches WHERE id=?',(paused['id'],)).fetchone()[0]=='routed'


def test_repair_is_scope_hold_and_schedule_never_reports_completion(settings,monkeypatch):
    seed(settings)
    calls=api(settings,monkeypatch,lambda r:output_for(r['role'],r))
    p.initialize(settings.database)
    batch=p.new_batch(settings.database,False,Clock.value)
    p.mark_needs_repair(settings.database,batch,p.RoutingRecoveryError('missing proof'))
    seed(settings,session='other',start=Clock.value-timedelta(hours=2),count=1)
    assert asyncio.run(p.scheduled_advance(settings.database))['events']==1
    assert asyncio.run(p.scheduled_advance(settings.database))['status']=='needs_repair'
    assert all(r['role']!='track_router' or r['messages'][0]['original_session_id']=='other' for r in calls)
    p.initialize(settings.database)
    client=TestClient(create_app(settings,token='test',live=True),headers={'Authorization':'Bearer test'})
    hold=client.get('/v1/pipeline/status').json()['blocked_scopes'][0]
    assert hold['batch_id']==batch['id'] and hold['hold_status']=='needs_repair'
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT completed FROM pipeline_schedule').fetchone()[0]==0
        assert store.conn.execute('SELECT count(*) FROM raw_processing WHERE raw_id<=14').fetchone()[0]==0
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner,retry_repair=True))
    assert result['events']==1


def test_successful_router_prefix_is_reused_after_pause(settings,monkeypatch):
    seed(settings)
    def fail_suffix(r):
        if r['messages'][0]['id']>10:return '{broken'
        return output_for(r['role'],r)
    calls=api(settings,monkeypatch,fail_suffix)
    asyncio.run(p.flush_routes(settings.database))
    assert len(calls)==4
    with Store(settings.database,read_only=True) as store:
        row=store.conn.execute("SELECT * FROM pipeline_batches WHERE status='paused_failure'").fetchone()
        successful=store.conn.execute('SELECT output_json FROM pipeline_jobs WHERE output_json IS NOT NULL').fetchone()[0]
    p.retry_batch(settings.database,row['id'])
    calls=api(settings,monkeypatch,lambda r:output_for(r['role'],r))
    asyncio.run(p.flush_routes(settings.database))
    assert len(calls)==1 and calls[0]['messages'][0]['id']==11
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT output_json FROM pipeline_jobs ORDER BY rowid LIMIT 1').fetchone()[0]==successful
        assert store.conn.execute('SELECT count(*) FROM pipeline_routes').fetchone()[0]==14
# Additional resume paths found in independent review.


def test_route_only_manual_repair_never_settles_raw(settings,monkeypatch):
    seed(settings)
    calls=api(settings,monkeypatch,lambda r:output_for(r['role'],r))
    asyncio.run(p.flush_routes(settings.database))
    with Store(settings.database,read_only=True) as store:
        batch=dict(store.conn.execute("SELECT * FROM pipeline_batches WHERE id LIKE 'route:%'").fetchone())
        saved=store.conn.execute('SELECT request_json,output_json FROM pipeline_jobs ORDER BY rowid').fetchall()
        jobs=[tuple(r) for r in saved]
    p.mark_needs_repair(settings.database,batch,p.RoutingRecoveryError('synthetic interruption'))
    async def forbidden(*a):pytest.fail('accepted route output charged twice')
    result=asyncio.run(p.advance(settings.database,include_recent=True,retry_repair=True,runner=forbidden))
    assert result['status']=='routed'
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0
        assert [tuple(r) for r in store.conn.execute('SELECT request_json,output_json FROM pipeline_jobs ORDER BY rowid')]==jobs


def test_route_only_explicit_rebuild_preserves_watermark_and_old_jobs(settings,monkeypatch):
    from serein.extensions import pipeline_recovery as recovery
    seed(settings)
    calls=api(settings,monkeypatch,lambda r:output_for(r['role'],r))
    asyncio.run(p.flush_routes(settings.database))
    with Store(settings.database,read_only=True) as store:
        batch=dict(store.conn.execute("SELECT * FROM pipeline_batches WHERE id LIKE 'route:%'").fetchone())
        jobs=[tuple(r) for r in store.conn.execute('SELECT id,request_json,output_json FROM pipeline_jobs ORDER BY rowid')]
    p.mark_needs_repair(settings.database,batch,p.RoutingRecoveryError('cannot prove old range'))
    with pytest.raises(ValueError):asyncio.run(recovery.rebuild(settings.database,batch['id'],''))
    replacement=asyncio.run(recovery.rebuild(settings.database,batch['id'],'REBUILD_PIPELINE_BATCH'))
    assert replacement['batch_id'].startswith('route:')
    assert asyncio.run(p.scheduled_advance(settings.database))['processed_originals']==2
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_processing WHERE raw_id>=3').fetchone()[0]==0
        assert [tuple(r) for r in store.conn.execute('SELECT id,request_json,output_json FROM pipeline_jobs ORDER BY rowid')][:2]==jobs
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0]==14


def test_upgrade_cannot_charge_or_reset_backoff_budget(settings,monkeypatch):
    seed(settings)
    def broken(r):raise OSError('synthetic failure')
    calls=api(settings,monkeypatch,broken)
    asyncio.run(p.flush_routes(settings.database))
    with Store(settings.database,read_only=True) as store:
        snapshot=store.conn.execute("SELECT input_json FROM pipeline_batches WHERE status='retry_wait'").fetchone()[0]
    monkeypatch.setattr(p,'runtime_revision',lambda:'upgraded')
    Clock.value+=timedelta(hours=1)
    asyncio.run(p.flush_routes(settings.database))
    assert len(calls)==1
    with Store(settings.database,read_only=True) as store:
        row=store.conn.execute("SELECT * FROM pipeline_batches WHERE status='needs_repair'").fetchone()
        assert row['input_json']==snapshot
        assert store.conn.execute('SELECT failures FROM pipeline_job_failures').fetchone()[0]==1
        assert store.conn.execute('SELECT count(*) FROM pipeline_attempts').fetchone()[0]==1


def test_incremental_start_uses_exact_producer_frame_not_newer_card(settings,monkeypatch):
    seed(settings)
    calls=api(settings,monkeypatch,lambda r:output_for(r['role'],r))
    asyncio.run(p.flush_routes(settings.database))
    with Store(settings.database) as store:
        store.conn.execute("INSERT INTO raw_processing VALUES (1,'earlier','settled')")
        store.conn.execute("INSERT INTO raw_processing VALUES (2,'earlier','settled')")
    batch=p.new_batch(settings.database,True,Clock.value.replace(hour=3,minute=0))
    data=json.loads(batch['input_json'])
    assert [m['id'] for m in data['routing_messages']]==list(range(3,11))
    result=p.cached_route_result(settings.database,data)
    assert result['tracks'][0]['recent_source_message_ids']==[10]
    assert result['recovered_route_sources'][0]['source_message_ids']==list(range(1,11))
    assert len(calls)==2


@pytest.mark.parametrize('missing_anchors',[False,True])
def test_gap_start_never_reanchors_future_prose_or_reroutes_on_continue(settings,monkeypatch,missing_anchors):
    seed(settings)
    calls=api(settings,monkeypatch,lambda r:output_for(r['role'],r))
    asyncio.run(p.flush_routes(settings.database))
    with Store(settings.database) as store:
        store.conn.execute('DELETE FROM pipeline_routes WHERE raw_id IN (3,4)')
        store.conn.execute('DELETE FROM pipeline_route_provenance WHERE raw_id IN (3,4)')
        row=store.conn.execute('SELECT * FROM pipeline_tracks').fetchone()
        card=json.loads(row['card_json']);card['throughline']='FUTURE MUST NOT BE REANCHORED'
        if missing_anchors:card.pop('recent_source_message_ids')
        encoded=encode(card)
        store.conn.execute('UPDATE pipeline_tracks SET card_json=?',(encoded,))
    before=len(calls)
    asyncio.run(p.flush_routes(settings.database))
    assert len(calls)==before
    hold=p.scope_holds(settings.database)[0]
    assert hold['hold_status']=='needs_repair'
    assert 'historical pre-routing' in hold['reason']
    assert asyncio.run(p.advance(settings.database,include_recent=True,retry_repair=True))['status']=='needs_repair'
    assert len(calls)==before
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT card_json FROM pipeline_tracks').fetchone()[0]==encoded
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0


def test_partial_unit_continue_cannot_create_model_jobs_without_rebuild(settings,monkeypatch):
    seed(settings)
    calls=api(settings,monkeypatch,lambda r:output_for(r['role'],r))
    asyncio.run(p.flush_routes(settings.database))
    with Store(settings.database) as store:
        store.conn.execute('DELETE FROM pipeline_routes WHERE raw_id=1')
        store.conn.execute('DELETE FROM pipeline_route_provenance WHERE raw_id=1')
    before=len(calls)
    asyncio.run(p.flush_routes(settings.database))
    hold=p.scope_holds(settings.database)[0]
    assert hold['hold_status']=='needs_repair'
    assert asyncio.run(p.advance(settings.database,include_recent=True,retry_repair=True))['status']=='needs_repair'
    assert len(calls)==before
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM pipeline_jobs WHERE batch_id=?',(hold['batch_id'],)).fetchone()[0]==0


def test_router_frame_hash_and_predecessor_state_are_verified(settings,monkeypatch):
    from serein.extensions import pipeline_recovery as recovery
    seed(settings)
    calls=api(settings,monkeypatch,lambda r:output_for(r['role'],r))
    asyncio.run(p.flush_routes(settings.database))
    with Store(settings.database) as store:
        batch=dict(store.conn.execute("SELECT * FROM pipeline_batches WHERE status='routed'").fetchone())
        job=store.conn.execute('SELECT * FROM pipeline_jobs ORDER BY rowid DESC LIMIT 1').fetchone()
        request=json.loads(job['request_json'])
        assert request['routing_frame']['source_message_ids']==[11,12,13,14]
        request['active_tracks'][0]['throughline']='corrupted prior state'
        store.conn.execute('UPDATE pipeline_jobs SET request_json=? WHERE id=?',(encode(request),job['id']))
    with pytest.raises(p.RoutingRecoveryError,match='evidence hash'):
        recovery._frames(settings.database,batch)
    assert len(calls)==2


def test_scope_head_gate_applies_to_recheck_resume_and_daytime(settings,monkeypatch):
    seed(settings)
    calls=api(settings,monkeypatch,lambda r:output_for(r['role'],r))
    p.initialize(settings.database)
    first=p.new_batch(settings.database,True,Clock.value)
    data=json.loads(first['input_json']);data['queue_order']=10000
    with Store(settings.database) as store:
        for key,state in [('route:later','routing_only'),('held-later','needs_repair')]:
            store.conn.execute('INSERT INTO pipeline_batches(id,scope,input_json,status,result_json) VALUES (?,?,?,?,?)',
                               (key,data['scope'],encode(data),state,encode({'status':'needs_repair','batch_id':key})))
    assert p.new_batch(settings.database,True,Clock.value,retry_repair=True)['id']==first['id']
    asyncio.run(p.flush_routes(settings.database))
    assert not calls
    with Store(settings.database) as store:
        detail={'status':'retry_wait','next_retry_at':(Clock.value+timedelta(hours=1)).isoformat()}
        store.conn.execute("UPDATE pipeline_batches SET status='retry_wait',result_json=? WHERE id=?",(encode(detail),first['id']))
    assert p.new_batch(settings.database,True,Clock.value,retry_repair=True) is None
    asyncio.run(p.flush_routes(settings.database))
    assert not calls


def test_backfilled_older_timestamp_never_reads_future_track_card(settings,monkeypatch):
    seed(settings)
    calls=api(settings,monkeypatch,lambda r:output_for(r['role'],r))
    asyncio.run(p.flush_routes(settings.database))
    before=len(calls)
    seed(settings,start=Clock.value.replace(hour=2,minute=42),count=5)
    asyncio.run(p.flush_routes(settings.database))
    assert len(calls)==before
    hold=p.scope_holds(settings.database)[0]
    assert hold['hold_status']=='needs_repair' and 'historical pre-routing' in hold['reason']
    assert asyncio.run(p.scheduled_advance(settings.database))['status']=='needs_repair'


def test_candidate_selection_replay_keeps_complete_predecessor_state(settings,monkeypatch):
    from serein.extensions import pipeline_recovery as recovery
    from serein.extensions import pipeline_track_candidates as candidates
    seed(settings,count=12)
    calls=api(settings,monkeypatch,lambda r:output_for(r['role'],r))
    save_settings(settings.database,{'pipeline':{'max_input_chars':180}})
    def alternating(cards,messages,recent,policy):
        # Candidate A is absent from one request and may return in a later frame.
        return [] if 5<=messages[0]['id']<=8 else list(cards)
    monkeypatch.setattr(candidates,'select',alternating)
    Clock.value+=timedelta(hours=1)
    asyncio.run(p.flush_routes(settings.database))
    with Store(settings.database,read_only=True) as store:
        batch=dict(store.conn.execute("SELECT * FROM pipeline_batches WHERE status='routed'").fetchone())
    frames=recovery._frames(settings.database,batch)
    assert len(frames)>2
    assert sum(len(frame['messages']) for frame in frames)==24
    assert len(calls)==len(frames)


def test_backfill_recent_context_cannot_read_later_source_time(settings,monkeypatch):
    seed(settings)
    calls=api(settings,monkeypatch,lambda r:output_for(r['role'],r))
    asyncio.run(p.flush_routes(settings.database))
    before=len(calls)
    seed(settings,start=Clock.value.replace(hour=1,minute=0),count=5)
    asyncio.run(p.flush_routes(settings.database))
    assert len(calls)>before
    for request in calls[before:]:
        assert all(m['id']<request['messages'][0]['id'] and
                   datetime.fromisoformat(m['created_at'])<=datetime.fromisoformat(request['messages'][0]['created_at'])
                   for m in request['recent_context'])
    assert calls[before]['recent_context']==[]


@pytest.mark.parametrize('previous',['idle','completed'])
def test_status_cannot_reuse_old_completion_over_scope_hold(settings,monkeypatch,previous):
    from serein.work_tasks import status,_save
    seed(settings)
    calls=api(settings,monkeypatch,lambda r:output_for(r['role'],r))
    p.initialize(settings.database)
    batch=p.new_batch(settings.database,False,Clock.value)
    p.mark_needs_repair(settings.database,batch,p.RoutingRecoveryError('missing frozen proof'))
    value=status(settings.database,'pipeline')
    value.update(status=previous,stage='settled_today',result={'status':'settled_today'})
    with Store(settings.database) as store:
        _save(store,'pipeline',value)
        store.conn.execute('INSERT INTO pipeline_schedule VALUES (?,1)',(Clock.value.date().isoformat(),))
    client=TestClient(create_app(settings,token='test',live=True),headers={'Authorization':'Bearer test'})
    visible=client.get('/v1/pipeline/status').json()
    assert visible['status']=='blocked' and visible['stage']=='blocked'
    assert visible['blocked_scopes'][0]['batch_id']==batch['id']
    assert asyncio.run(p.scheduled_advance(settings.database))['status']=='needs_repair'
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT completed FROM pipeline_schedule').fetchone()[0]==0
    assert not calls
