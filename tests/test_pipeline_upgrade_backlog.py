"""Upgrade recovery never needs the old queued request to fit a new budget."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from test_public_features import settings, output_for
from test_pipeline_batch_recovery import short_runner, rows
from serein.api.http import create_app
from serein.compat.raw_archive import raw_archive
from serein.core.store import Store, encode, Conflict
from serein.deployment import save_settings
from serein.extensions import pipeline as p, pipeline_recovery as recovery


def legacy_backlog(settings, *, contract=None, failed=False):
    save_settings(settings.database, {'pipeline': {'max_input_chars': 100000}})
    raw_archive(settings).ingest([
        {'source_event_id':str(i),'session_id':'upgrade-backlog','role':'user' if i%2==0 else 'assistant',
         'text':str(i)*4987,'created_at':f'2025-01-01T00:{i:02d}:00Z',
         'metadata':{'timestamp_source':'import_time','runtime':'synthetic'}} for i in range(10)
    ],source='synthetic')
    p.initialize(settings.database)
    batch=p.new_batch(settings.database,True)
    if failed:
        async def fail(role,request):
            if role=='event_curator':raise OSError('Old frozen request failed')
            return output_for(role,request)
        with pytest.raises(OSError):asyncio.run(p.advance(settings.database,include_recent=True,runner=fail))
    with Store(settings.database) as store:
        data=json.loads(store.conn.execute('SELECT input_json FROM pipeline_batches WHERE id=?',(batch['id'],)).fetchone()[0])
        data.update(runtime_revision='previous-release',contract=contract or p.CONTRACT)
        store.conn.execute('UPDATE pipeline_batches SET input_json=? WHERE id=?',(encode(data),batch['id']))
    save_settings(settings.database,{'pipeline':{'max_input_chars':12000}})
    return batch['id']


def test_status_exposes_legacy_backlog_before_any_model_call(settings):
    batch_id=legacy_backlog(settings)
    client=TestClient(create_app(settings,token='synthetic',live=True),headers={'Authorization':'Bearer synthetic'})
    result=client.get('/v1/pipeline/status')
    assert result.status_code==200,result.text
    item=next(item for item in result.json()['pending_materials'] if item['batch_id']==batch_id)
    assert item['originals']==10 and item['original_chars']==49870
    assert result.json()['status']!='awaiting_agent'
    assert rows(settings,'raw_processing')==[]


def test_pending_reroute_does_not_replay_the_recorded_routing_error(settings):
    from serein.extensions import pipeline_pending as pending
    batch_id=legacy_backlog(settings)
    with Store(settings.database) as store:
        data=json.loads(store.conn.execute('SELECT input_json FROM pipeline_batches WHERE id=?',(batch_id,)).fetchone()[0])
        data['routing_evidence_error']='No bounded historical pre-routing Track frame: synthetic-old-track'
        store.conn.execute('UPDATE pipeline_batches SET input_json=? WHERE id=?',(encode(data),batch_id))
    p.initialize(settings.database)
    with Store(settings.database) as store:item=pending.entries(store.conn)[0]
    assert item['kind']=='unrouted'
    pending.resume(settings.database,item['id'],replan=True)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=short_runner))
    assert result['status']=='processed',result
    assert len(rows(settings,'raw_processing'))==10
    with Store(settings.database) as store:
        old=json.loads(store.conn.execute('SELECT input_json FROM pipeline_batches WHERE id=?',(batch_id,)).fetchone()[0])
        assert old['routing_evidence_error']==data['routing_evidence_error']
        assert store.conn.execute('SELECT status FROM pipeline_pending_materials WHERE id=?',(item['id'],)).fetchone()[0]=='resolved'


def test_pending_reroute_retires_unverifiable_cache(settings):
    from serein.extensions import pipeline_pending as pending
    from test_public_features import ingest,synthetic_runner
    from test_pipeline_route_history import hold_bad_cache
    ingest(settings)
    batch,_=hold_bad_cache(settings)
    originals=rows(settings,'raw_events')
    with Store(settings.database) as store:item=pending.entries(store.conn)[0]
    assert item['kind']=='unrouted'
    result=pending.resume(settings.database,item['id'],replan=True)
    assert result['status']=='resumed'
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))['events']==1
    assert rows(settings,'raw_events')==originals
    with Store(settings.database) as store:
        audit=json.loads(store.conn.execute('SELECT result_json FROM pipeline_batches WHERE id=?',(batch['id'],)).fetchone()[0])
        assert len(audit['discarded_route_cache'])==2


def test_legacy_direct_rebuild_without_runtime_revision_is_available(settings):
    batch_id=legacy_backlog(settings,contract='public-event-message-tracks-v7',failed=True)
    with Store(settings.database) as store:
        data=json.loads(store.conn.execute('SELECT input_json FROM pipeline_batches WHERE id=?',(batch_id,)).fetchone()[0])
        data.pop('runtime_revision')
        store.conn.execute('UPDATE pipeline_batches SET input_json=? WHERE id=?',(encode(data),batch_id))
    p.initialize(settings.database)
    result=asyncio.run(recovery.rebuild(settings.database,batch_id,'REBUILD_PIPELINE_BATCH'))
    assert result['status']=='rebuilt'
    for _ in range(5):
        assert asyncio.run(p.advance(settings.database,include_recent=True,runner=short_runner))['status']=='processed'
    assert len(rows(settings,'raw_processing'))==10


def test_old_agent_request_is_not_advertised_again_after_upgrade(settings):
    batch_id=legacy_backlog(settings)
    with Store(settings.database) as store:
        store.conn.execute('INSERT INTO pipeline_jobs VALUES (?,?,?,?,NULL)',
            ('old-agent',batch_id,'track_router:0',encode({'role':'track_router','prompt':'original frozen request'})))
        store.conn.execute("INSERT OR REPLACE INTO background_state(name,value_json) VALUES ('work:pipeline',?)",
            (encode({'id':'pipeline','status':'awaiting_agent','stage':'track_router',
                     'job_id':'old-agent','result':{'job_id':'old-agent'},'updated_at':0}),))
    client=TestClient(create_app(settings,token='synthetic',live=True),headers={'Authorization':'Bearer synthetic'})
    for _ in range(2):
        result=client.get('/v1/pipeline/status').json()
        assert result['status']!='awaiting_agent' and result['job_id']==''
        assert len(result['pending_materials'])==1
    assert rows(settings,'pipeline_jobs')[0][-1] is None


def test_replanned_legacy_batch_keeps_its_place_before_later_queued_material(settings):
    from serein.extensions import pipeline_pending as pending
    batch_id=legacy_backlog(settings)
    raw_archive(settings).ingest([
        {'source_event_id':str(i),'session_id':'upgrade-backlog','role':role,'text':'later material',
         'created_at':f'2025-01-01T00:{i}:00Z','metadata':{'timestamp_source':'import_time','runtime':'synthetic'}}
        for i,role in ((10,'user'),(11,'assistant'))],source='synthetic')
    p.initialize(settings.database)
    with Store(settings.database) as store:
        item=pending.entries(store.conn)[0]
        data=json.loads(store.conn.execute('SELECT input_json FROM pipeline_batches WHERE id=?',(batch_id,)).fetchone()[0])
        new=[p.task_message(row) for row in store.conn.execute('SELECT * FROM raw_events WHERE id>10 ORDER BY id')]
        data.update(contract=p.CONTRACT,runtime_revision=p.runtime_revision(),messages=new,routing_messages=new,parked=[])
        store.conn.execute("INSERT INTO pipeline_batches VALUES ('later',?,?,'pending',NULL)",(data['scope'],encode(data)))
    result=pending.resume(settings.database,item['id'],replan=True)
    assert p.new_batch(settings.database,True)['id']==result['batch_id']
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT status FROM pipeline_batches WHERE id=?',(batch_id,)).fetchone()[0]=='superseded_repair'


@pytest.mark.parametrize('old_contract',[None,'public-event-message-tracks-v7'])
def test_49870_chars_are_rebuilt_as_ordered_12k_batches_after_upgrade(settings,old_contract):
    batch_id=legacy_backlog(settings,contract=old_contract,failed=True)
    originals=rows(settings,'raw_events'); jobs=rows(settings,'pipeline_jobs');attempts=rows(settings,'pipeline_attempts')
    p.initialize(settings.database)
    preview=recovery.recovery_preview(settings.database,batch_id,12000)
    assert preview['original_chars']==49870 and preview['batch_chars']==[9974]*5
    result=asyncio.run(recovery.recover_batch(settings.database,batch_id,'rebatch','REBATCH_PIPELINE_BATCH',12000))
    assert result['batches']==5
    assert rows(settings,'pipeline_jobs')==jobs and rows(settings,'pipeline_attempts')==attempts
    for key in result['batch_ids']:
        selected=p.new_batch(settings.database,True)
        assert selected['id']==key
        data=json.loads(selected['input_json'])
        assert data['contract']==p.CONTRACT and data['runtime_revision']==p.runtime_revision()
        assert sum(len(m['content']) for m in data['routing_messages'])==9974
        assert asyncio.run(p.advance(settings.database,include_recent=True,runner=short_runner))['status']=='processed'
    assert rows(settings,'raw_events')==originals
    assert len(rows(settings,'raw_processing'))==10
    assert {job[0] for job in jobs}.issubset(job[0] for job in rows(settings,'pipeline_jobs'))


def test_upgraded_backlog_still_refuses_edited_originals(settings):
    batch_id=legacy_backlog(settings,contract='public-event-message-tracks-v7')
    with Store(settings.database) as store:store.conn.execute("UPDATE raw_events SET text='edited' WHERE id=1")
    p.initialize(settings.database)
    with pytest.raises(Conflict,match='原话已变化'):
        asyncio.run(recovery.recover_batch(settings.database,batch_id,'rebatch','REBATCH_PIPELINE_BATCH',12000))
    assert rows(settings,'raw_processing')==[]


def test_single_49870_char_exchange_can_replan_without_truncation(settings):
    from serein.extensions import pipeline_pending as pending
    save_settings(settings.database,{'pipeline':{'max_input_chars':100000}})
    raw_archive(settings).ingest([
        {'source_event_id':str(i),'session_id':'one-long-exchange','role':role,'text':'x'*24935,
         'created_at':f'2025-01-01T00:0{i}:00Z','metadata':{'timestamp_source':'import_time'}}
        for i,role in enumerate(('user','assistant'))],source='synthetic')
    p.initialize(settings.database); batch=p.new_batch(settings.database,True)
    with Store(settings.database) as store:
        data=json.loads(batch['input_json']);data.update(contract='public-event-message-tracks-v7',runtime_revision='previous-release')
        store.conn.execute('UPDATE pipeline_batches SET input_json=? WHERE id=?',(encode(data),batch['id']))
    save_settings(settings.database,{'pipeline':{'max_input_chars':12000}})
    p.initialize(settings.database)
    with Store(settings.database) as store:item=pending.entries(store.conn)[0]
    assert not recovery.recovery_preview(settings.database,batch['id'],12000)['can_rebatch']
    pending.resume(settings.database,item['id'],replan=True)
    seen=[]
    async def run(role,request):
        if role=='track_router':seen.append(sum(len(m['content']) for m in request['messages']))
        return await short_runner(role,request)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=run))
    assert result.get('processed_originals')==2 and result.get('events')==1,result
    assert seen==[49870]
    assert len(rows(settings,'raw_processing'))==2
