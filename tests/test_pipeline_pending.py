"""Synthetic fixtures for retained work, explicit adoption and atomic closure."""
import asyncio
import json

import pytest

from test_public_features import settings,ingest,output_for,synthetic_runner
from serein.core.store import Store
from serein.deployment import save_settings
from serein.extensions import pipeline as p
from serein.extensions import pipeline_pending as pending


async def insufficient(role,request):
    return {'evidence_sufficient':False,'self_review':{}} if role=='event_writer' else output_for(role,request)


def test_curator_unknown_field_is_retained_without_writing(settings):
    ingest(settings)
    calls=[]
    async def extra_field(role,request):
        calls.append(role)
        output=output_for(role,request)
        if role=='event_curator':output['events'][0]['primary_activity_details']='Synthetic extra summary'
        return output
    for _ in range(2):
        with pytest.raises(ValueError,match="unexpected=\\['primary_activity_details'\\]"):
            asyncio.run(p.advance(settings.database,include_recent=True,runner=extra_field))
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=extra_field))['status']=='retained'
    before=list(calls)
    asyncio.run(p.advance(settings.database,include_recent=True,runner=extra_field))
    assert calls==before and 'event_writer' not in calls
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT count(*) FROM fact_events').fetchone()[0]==0
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0
        item=pending.entries(store.conn)[0]
    assert 'Synthetic extra summary' in pending.detail(settings.database,item['id'])['last_error']['output_text']


def retained(settings):
    ingest(settings)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=insufficient))
    assert result['events']==0 and result['pending']==2
    with Store(settings.database) as store:return pending.entries(store.conn)[0]


def adopting(*,background=False,writer_fails=False):
    async def run(role,request):
        if role=='event_curator':
            component=request['component']
            if not request.get('context_read'):
                assert 'Book club plan 1' not in request['prompt']
                return {'context_request':{'pending_material_id':component['pending_materials'][0]['pending_material_id'],'reason':'continue_pending'}}
            output=output_for(role,request)
            if not background:
                output['adopt_pending_material_ids']=[component['pending_material_read']['id']]
                output['events'][0]['owned_unit_roots']=[u['unit_root_message_id'] for u in component['memberships']]
            return output
        if role=='event_writer' and writer_fails:return {'evidence_sufficient':False,'self_review':{}}
        return output_for(role,request)
    return run


def test_insufficient_is_reserved_and_resume_reuses_the_cut(settings):
    item=retained(settings)
    assert item['kind']=='writing'
    async def forbidden(*args):pytest.fail('Unchanged retained work was called automatically')
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=forbidden))['status']=='current'
    p.initialize(settings.database)
    with Store(settings.database) as store:
        data=json.loads(store.conn.execute('SELECT input_json FROM pipeline_batches').fetchone()[0])
        assert 'components' in data
        assert not json.loads(store.conn.execute("SELECT request_json FROM pipeline_jobs WHERE role LIKE 'event_writer:%'").fetchone()[0]).get('task_snapshot_compacted')
    pending.resume(settings.database,item['id'])
    calls=[]
    async def good(role,request):calls.append(role);return output_for(role,request)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=good))['events']==1
    assert calls==['event_writer']
    with Store(settings.database) as store:assert pending.entries(store.conn)[0]['status']=='resolved'


def test_prompt_limit_retains_once_without_model_calls(settings):
    ingest(settings);save_settings(settings.database,{'pipeline':{'max_prompt_chars':1}})
    async def forbidden(*args):pytest.fail('Oversized prompt reached model')
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=forbidden))
    assert result['status']=='retained'
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=forbidden))['status']=='current'
    with Store(settings.database) as store:
        item=pending.entries(store.conn)[0]
        assert item['last_attempt']['output_chars']==0 and item['kind']=='unrouted'
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0


def test_resume_keeps_old_cut_even_after_an_independent_new_event(settings):
    item=retained(settings);ingest(settings,2)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))['events']==1
    pending.resume(settings.database,item['id']);calls=[]
    async def good(role,request):calls.append(role);return output_for(role,request)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=good))['events']==1
    assert calls==['event_writer']


def test_retained_originals_are_short_references_in_router_prompt(settings):
    item=retained(settings);ingest(settings,2)
    calls=[]
    async def good(role,request):
        if role=='track_router':
            calls.append(role)
            assert 'Book club plan 1' not in request['prompt']
            assert 'Agreed to plan 1' not in request['prompt']
            assert 'pending_material_id' in request['prompt']
        return output_for(role,request)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=good))['events']==1
    assert calls==['track_router']


def test_partial_batch_resume_never_rewrites_the_successful_event(settings):
    ingest(settings);ingest(settings,2)
    async def run(role,request):
        out=output_for(role,request)
        if role=='track_router':
            for index,row in enumerate(out['message_assignments']):row['primary_track_ref']='new:1' if index<2 else 'new:2'
            out['track_updates'].append({**out['track_updates'][0],'track_ref':'new:2','subject':'Independent synthetic plan'})
        if role=='event_writer' and min(m['id'] for m in request['messages'])==1:return {'evidence_sufficient':False,'self_review':{}}
        return out
    # Keep the two pairs within a single transport window.
    with Store(settings.database) as store:
        store.conn.execute("UPDATE raw_events SET created_at='2025-01-01T00:02:00Z' WHERE id=3")
        store.conn.execute("UPDATE raw_events SET created_at='2025-01-01T00:03:00Z' WHERE id=4")
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=run))['events']==1
    with Store(settings.database) as store:
        item=pending.entries(store.conn)[0]
        assert item['source_ids']==[1,2]
        existing=dict(store.conn.execute('SELECT item_id,body FROM fact_events').fetchone())
    pending.resume(settings.database,item['id']);calls=[]
    async def good(role,request):calls.append(role);return output_for(role,request)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=good))['events']==1
    assert calls==['event_writer']
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT count(*) FROM fact_events').fetchone()[0]==2
        assert store.conn.execute('SELECT body FROM fact_events WHERE item_id=?',(existing['item_id'],)).fetchone()[0]==existing['body']


@pytest.mark.parametrize('background',[False,True])
def test_future_event_closes_only_explicit_whole_adoption(settings,background):
    item=retained(settings);ingest(settings,2)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=adopting(background=background)))
    assert result['events']==1 and result['pending']==0
    with Store(settings.database) as store:
        saved=next(i for i in pending.entries(store.conn) if i['id']==item['id'])
        assert saved['status']==('open' if background else 'resolved')
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==(2 if background else 4)
        if not background:assert saved['resolved_event_ids']


def test_failed_adopting_writer_leaves_old_pending_open(settings):
    item=retained(settings);ingest(settings,2)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=adopting(writer_fails=True)))
    assert result['events']==0
    with Store(settings.database) as store:
        assert next(i for i in pending.entries(store.conn) if i['id']==item['id'])['status']=='open'
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0
        latest=pending.entries(store.conn)[0]['id']
    pending.resume(settings.database,latest)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))
    assert result['events']==1
    with Store(settings.database) as store:
        assert all(i['status']=='resolved' for i in pending.entries(store.conn))


def test_partial_or_fabricated_adoption_is_rejected(settings):
    item=retained(settings);ingest(settings,2)
    p.initialize(settings.database);batch=p.new_batch(settings.database,True)
    data=json.loads(batch['input_json']);routed=asyncio.run(p.route_batch(settings.database,batch,data,synthetic_runner))
    data=p.save_routing_snapshot(settings.database,batch,data,routed)
    component=data['components'][0];component['pending_materials']=pending.catalog(settings.database,component)
    with pytest.raises(ValueError):pending.adopted_component(component,{'adopt_pending_material_ids':[item['id']]})
    component=pending.read_material(settings.database,component,item['id'])
    adopted=pending.adopted_component(component,{'adopt_pending_material_ids':[item['id']]})
    with pytest.raises(ValueError,match='整份承接'):
        pending.check_adoption(adopted,{'events':[{'source_message_ids':[1,3,4]}],'skip_source_message_ids':[],'defer_source_message_ids':[2]})


def test_final_save_failure_does_not_consume_pending(settings,monkeypatch):
    item=retained(settings);ingest(settings,2)
    from serein.compat.events import Events
    def fail(*args,**kwargs):raise RuntimeError('synthetic storage failure')
    monkeypatch.setattr(Events,'settle',fail)
    with pytest.raises(RuntimeError):asyncio.run(p.advance(settings.database,include_recent=True,runner=adopting()))
    with Store(settings.database) as store:
        assert next(i for i in pending.entries(store.conn) if i['id']==item['id'])['status']=='open'
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0


def test_transaction_rolls_back_adoption_and_event_together(settings,monkeypatch):
    item=retained(settings);ingest(settings,2)
    original=pending.finish_adoptions
    def fail(conn,*args):original(conn,*args);raise RuntimeError('synthetic before-commit failure')
    monkeypatch.setattr(pending,'finish_adoptions',fail)
    with pytest.raises(RuntimeError):asyncio.run(p.advance(settings.database,include_recent=True,runner=adopting()))
    with Store(settings.database) as store:
        assert next(i for i in pending.entries(store.conn) if i['id']==item['id'])['status']=='open'
        assert store.conn.execute('SELECT count(*) FROM fact_events').fetchone()[0]==0
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0


def test_latest_bad_output_is_accessible_without_becoming_an_event(settings):
    ingest(settings)
    async def malformed(role,request):
        return {'bad_output':'SYNTHETIC_UNACCEPTED'} if role=='event_curator' else output_for(role,request)
    for _ in range(2):
        with pytest.raises(ValueError):asyncio.run(p.advance(settings.database,include_recent=True,runner=malformed))
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=malformed))['status']=='retained'
    with Store(settings.database) as store:item=pending.entries(store.conn)[0]
    result=pending.detail(settings.database,item['id'])
    assert 'SYNTHETIC_UNACCEPTED' in result['last_attempt']['output_text']
    assert len(result['messages'])==2


def test_http_pending_auth_resume_and_busy_lease(settings):
    from fastapi.testclient import TestClient
    from serein.api.http import create_app
    item=retained(settings)
    client=TestClient(create_app(settings,token='synthetic',live=True))
    assert client.get('/v1/pipeline/pending/'+str(item['id'])).status_code==401
    client.headers['Authorization']='Bearer synthetic'
    assert client.get('/v1/pipeline/status').json()['pending_materials'][0]['id']==item['id']
    assert client.get('/v1/pipeline/pending/'+str(item['id'])).json()['jobs']
    assert client.post('/v1/pipeline/resume-pending',json={'pending_id':True}).status_code==400
    assert client.post('/v1/pipeline/resume-pending',json={'pending_id':item['id']}).json()['status']=='resumed'


def test_lowering_input_target_cannot_retire_an_explicitly_resumed_frame(settings):
    ingest(settings);ingest(settings,2)
    with Store(settings.database) as store:
        store.conn.execute("UPDATE raw_events SET text=text||?,created_at='2025-01-01T00:02:00Z'",('x'*800,))
    async def fail(role,request):
        if role=='event_writer':raise ValueError('synthetic writer failure')
        return output_for(role,request)
    for _ in range(2):
        with pytest.raises(ValueError):asyncio.run(p.advance(settings.database,include_recent=True,runner=fail))
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=fail))['status']=='retained'
    with Store(settings.database) as store:item=pending.entries(store.conn)[0]
    save_settings(settings.database,{'pipeline':{'max_input_chars':2000}})
    pending.resume(settings.database,item['id']);calls=[]
    async def good(role,request):calls.append(role);return output_for(role,request)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=good))
    assert result['batch_id']==item['batch_id'] and result['processed_originals']==4
    assert calls==['event_writer']


def test_replanned_pending_keeps_previous_failed_output_visible(settings):
    ingest(settings)
    async def malformed(role,request):
        return {'bad_output':'SYNTHETIC_UNACCEPTED'} if role=='event_curator' else output_for(role,request)
    for _ in range(2):
        with pytest.raises(ValueError):asyncio.run(p.advance(settings.database,include_recent=True,runner=malformed))
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=malformed))['status']=='retained'
    with Store(settings.database) as store:item=pending.entries(store.conn)[0]
    resumed=pending.resume(settings.database,item['id'],replan=True)
    assert resumed['batch_id']!=item['batch_id']
    assert 'SYNTHETIC_UNACCEPTED' in pending.detail(settings.database,item['id'])['last_attempt']['output_text']
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))['events']==1
    p.initialize(settings.database)
    assert 'SYNTHETIC_UNACCEPTED' in pending.detail(settings.database,item['id'])['last_error']['output_text']


def test_prompt_limit_parks_even_after_an_earlier_model_failure(settings):
    ingest(settings)
    async def failed(role,request):
        if role=='event_writer':raise ValueError('synthetic failed writer')
        return output_for(role,request)
    with pytest.raises(ValueError):asyncio.run(p.advance(settings.database,include_recent=True,runner=failed))
    save_settings(settings.database,{'pipeline':{'max_prompt_chars':1}})
    async def forbidden(*args):pytest.fail('Prompt over limit reached model')
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=forbidden))['status']=='retained'
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=forbidden))['status']=='current'


def test_mixed_batch_is_not_labeled_fully_cut_when_one_component_failed_curator(settings):
    ingest(settings);ingest(settings,2)
    with Store(settings.database) as store:
        store.conn.execute("UPDATE raw_events SET created_at='2025-01-01T00:02:00Z' WHERE id=3")
        store.conn.execute("UPDATE raw_events SET created_at='2025-01-01T00:03:00Z' WHERE id=4")
    async def run(role,request):
        out=output_for(role,request)
        if role=='track_router':
            for index,row in enumerate(out['message_assignments']):row['primary_track_ref']='new:1' if index<2 else 'new:2'
            out['track_updates'].append({**out['track_updates'][0],'track_ref':'new:2','subject':'Independent synthetic plan'})
        if role=='event_curator' and min(m['id'] for m in request['component']['messages'])==3:
            raise ValueError('synthetic invalid cut')
        return out
    for _ in range(2):
        with pytest.raises(ValueError):asyncio.run(p.advance(settings.database,include_recent=True,runner=run))
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=run))['status']=='retained'
    with Store(settings.database) as store:assert pending.entries(store.conn)[0]['kind']=='routing'


def large_retained(settings):
    ingest(settings)
    with Store(settings.database) as store:
        store.conn.execute("UPDATE raw_events SET text=text||? WHERE id=2",('PENDING_LARGE_'+'x'*45000,))
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=insufficient))['pending']==2
    with Store(settings.database) as store:return pending.entries(store.conn)[0]


def test_pending_read_over_30k_uses_prompt_room_not_the_batch_input_target(settings):
    item=large_retained(settings)
    save_settings(settings.database,{'pipeline':{'max_input_chars':2000}})
    ingest(settings,2)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=adopting()))
    assert result['events']==1 and result['processed_originals']==4
    with Store(settings.database) as store:assert pending.entries(store.conn)[0]['status']=='resolved'


def test_oversized_optional_pending_is_omitted_without_blocking_new_material(settings):
    item=large_retained(settings);ingest(settings,2)
    save_settings(settings.database,{'pipeline':{'max_prompt_chars':40000}})
    called=[]
    async def run(role,request):
        if role=='event_curator':
            called.append(request.get('context_read',False))
            if not request.get('context_read'):
                return {'context_request':{'pending_material_id':item['id'],'reason':'continue_pending'}}
            assert request['component']['pending_material_read_omitted']==item['id']
            assert 'pending_material_read' not in request['component']
            assert 'PENDING_LARGE_' not in request['prompt']
        return output_for(role,request)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=run))
    assert result['events']==1 and result['processed_originals']==2 and called==[False,True]
    with Store(settings.database) as store:
        assert pending.entries(store.conn)[0]['status']=='open'
        assert [row[0] for row in store.conn.execute('SELECT raw_id FROM raw_processing ORDER BY raw_id')]==[3,4]
    async def forbidden(*args):pytest.fail('Unchanged over-budget pending was called again')
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=forbidden))['status']=='current'


def test_optional_pending_checks_agent_delivery_size(settings):
    item=retained(settings);ingest(settings,2)
    p.initialize(settings.database);batch=p.new_batch(settings.database,True)
    data=json.loads(batch['input_json']);routed=asyncio.run(p.route_batch(settings.database,batch,data,synthetic_runner))
    data=p.save_routing_snapshot(settings.database,batch,data,routed)
    component=data['components'][0];component['pending_materials']=pending.catalog(settings.database,component)
    component=pending.read_material(settings.database,component,item['id'])
    request=p.request_for(settings.database,batch,'event_curator',component=component,context_read=True)
    limit=len(request['prompt'])+len(request['rules'])
    save_settings(settings.database,{'pipeline':{'max_prompt_chars':limit}})
    assert p.context_request_fits(settings.database,batch,request,'event_curator:0:context',synthetic_runner)
    assert not p.context_request_fits(settings.database,batch,request,'event_curator:0:context',None)
