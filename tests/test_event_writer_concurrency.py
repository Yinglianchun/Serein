import asyncio
import json

import pytest

from test_public_features import settings, output_for
from serein.compat.raw_archive import raw_archive
from serein.core.store import Store
from serein.deployment import read_settings, save_settings
from serein.extensions import pipeline as p


def split_curator_output(request):
    component=request['component']
    stable={m['id'] for m in component['messages']}
    roots=[u['unit_root_message_id'] for u in component['memberships']
           if u['unit_root_message_id'] in stable]
    assert len(roots)==4
    track=component['track_ids'][0]
    return {'events':[
        {'action':'create','base_event_ids':[],'primary_track_id':track,
         'owned_unit_roots':roots[:2]},
        {'action':'create','base_event_ids':[],'primary_track_id':track,
         'owned_unit_roots':roots[2:]},
    ],'skip_unit_roots':[],'defer_unit_roots':[],
       'decision_review':{'events':[{'event_index':0,'reason':'First plan and reply'},
                                    {'event_index':1,'reason':'Second plan and reply'}],
                          'boundaries':[{'left_event_index':0,'right_event_index':1,
                                         'reason':'Second plan starts another activity',
                                         'evidence':[{'source_message_id':roots[0],'quote':'First plan'},
                                                     {'source_message_id':roots[2],'quote':'Second plan'}]}],
                          'dispositions':[]}}


def base_output(role,request):
    if role=='track_router':
        result=output_for(role,request)
        result['track_updates'][0]['event_policy']='default'
        return result
    if role=='event_curator':
        return split_curator_output(request)
    return output_for(role,request)


def two_event_dialogue(settings):
    raw_archive(settings).ingest([
        {'source_event_id':'u1','session_id':'parallel','role':'user',
         'text':'First plan','created_at':'2025-01-01T00:00:00Z'},
        {'source_event_id':'a1','session_id':'parallel','role':'assistant',
         'text':'First reply','created_at':'2025-01-01T00:01:00Z'},
        {'source_event_id':'u2','session_id':'parallel','role':'user',
         'text':'Second plan','created_at':'2025-01-01T00:02:00Z'},
        {'source_event_id':'a2','session_id':'parallel','role':'assistant',
         'text':'Second reply','created_at':'2025-01-01T00:03:00Z'},
    ],source='test')


def test_writer_concurrency_setting_defaults_validates_and_only_enables_inline_execution(settings):
    assert read_settings(settings.database)['pipeline']['event_writer_concurrency']==1
    for bad in (0,9,1.5,True,'2'):
        with pytest.raises(ValueError,match='concurrency'):
            save_settings(settings.database,{'pipeline':{'event_writer_concurrency':bad}})
    save_settings(settings.database,{'pipeline':{'event_writer_concurrency':4}})
    two_event_dialogue(settings)
    p.initialize(settings.database)
    batch=p.new_batch(settings.database,True)
    # Agent mode still exposes exactly one task at a time.
    assert p.event_writer_concurrency(settings.database,batch,None)==1
    # A caller-provided inline runner is safe to parallelize even in Agent mode.
    assert p.event_writer_concurrency(settings.database,batch,base_output)==4
    save_settings(settings.database,{
        'models':[{'id':'local','model':'synthetic','base_url':'http://127.0.0.1:9/v1'}],
        'assignments':{role:'local' for role in p.ROLES},
        'pipeline':{'execution_mode':'api','event_writer_concurrency':4},
    })
    assert p.event_writer_concurrency(settings.database,batch,None)==4


def test_default_writer_concurrency_keeps_first_pass_serial(settings):
    two_event_dialogue(settings)
    active=0
    maximum=0
    async def runner(role,request):
        nonlocal active,maximum
        if role!='event_writer':
            return base_output(role,request)
        active+=1
        maximum=max(maximum,active)
        await asyncio.sleep(.02)
        active-=1
        return output_for(role,request)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))
    assert result['events']==2
    assert maximum==1


def test_first_writer_pass_runs_concurrently_and_settles_in_curator_order(settings):
    save_settings(settings.database,{'pipeline':{'event_writer_concurrency':2}})
    two_event_dialogue(settings)
    active=0
    maximum=0
    both_started=asyncio.Event()
    async def runner(role,request):
        nonlocal active,maximum
        if role!='event_writer':
            return base_output(role,request)
        active+=1
        maximum=max(maximum,active)
        if active==2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(),1)
        await asyncio.sleep(.01)
        active-=1
        return output_for(role,request)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))
    assert result['events']==2 and maximum==2
    with Store(settings.database,read_only=True) as store:
        rows=store.conn.execute(
            "SELECT origin_id,body FROM fact_events WHERE status='active' ORDER BY origin_id"
        ).fetchall()
        assert len(rows)==2
        assert rows[0]['origin_id'].endswith(':0') and 'First plan' in rows[0]['body']
        assert rows[1]['origin_id'].endswith(':1') and 'Second plan' in rows[1]['body']


def test_writer_context_followup_stays_serial_after_parallel_first_pass(settings):
    save_settings(settings.database,{'pipeline':{'event_writer_concurrency':2}})
    two_event_dialogue(settings)
    first_started=[]
    first_finished=[]
    context_started=[]
    both_started=asyncio.Event()
    async def runner(role,request):
        if role!='event_writer':
            return base_output(role,request)
        source=request['messages'][0]['id']
        if request.get('context_read'):
            context_started.append(source)
            assert len(first_finished)==2
            return output_for(role,request)
        first_started.append(source)
        if len(first_started)==2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(),1)
        first_finished.append(source)
        if source==min(first_started):
            component=request['component']
            return {'context_request':{
                'track_id':component['track_ids'][0],
                'before_message_id':min(m['id'] for m in component['messages']),
                'reason':'missing_subject',
            }}
        return output_for(role,request)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))
    assert result['events']==2
    assert len(first_started)==len(first_finished)==2
    assert len(context_started)==1


def test_agent_mode_with_high_setting_still_materializes_one_writer_task(settings):
    save_settings(settings.database,{'pipeline':{'execution_mode':'agent','event_writer_concurrency':8}})
    two_event_dialogue(settings)
    task=asyncio.run(p.advance(settings.database,include_recent=True))
    while task['role']=='track_router':
        output=base_output(task['role'],task['request'])
        p.submit(settings.database,task['job_id'],output)
        task=asyncio.run(p.advance(settings.database,include_recent=True))
    assert task['role']=='event_curator'
    p.submit(settings.database,task['job_id'],split_curator_output(task['request']))
    writer=asyncio.run(p.advance(settings.database,include_recent=True))
    assert writer['role']=='event_writer'
    with Store(settings.database,read_only=True) as store:
        rows=store.conn.execute(
            "SELECT id,output_json FROM pipeline_jobs WHERE role LIKE 'event_writer:%' ORDER BY rowid"
        ).fetchall()
        assert len(rows)==1 and rows[0]['output_json'] is None


def test_parallel_writer_failure_keeps_completed_sibling_for_resume(settings):
    save_settings(settings.database,{'pipeline':{'event_writer_concurrency':2}})
    two_event_dialogue(settings)
    started=asyncio.Event()
    first_source=None
    async def failing(role,request):
        nonlocal first_source
        if role!='event_writer':
            return base_output(role,request)
        source=request['messages'][0]['id']
        if first_source is None:
            first_source=source
            await started.wait()
            return output_for(role,request)
        started.set()
        await asyncio.sleep(.05)
        raise RuntimeError('synthetic second Writer failure')
    with pytest.raises(RuntimeError,match='second Writer failure'):
        asyncio.run(p.advance(settings.database,include_recent=True,runner=failing))
    with Store(settings.database,read_only=True) as store:
        writers=store.conn.execute(
            "SELECT id,output_json FROM pipeline_jobs WHERE role LIKE 'event_writer:%' ORDER BY role"
        ).fetchall()
        assert len(writers)==2
        assert sum(row['output_json'] is not None for row in writers)==1
    calls=[]
    async def resume(role,request):
        calls.append((role,request['messages'][0]['id'] if role=='event_writer' else None))
        return base_output(role,request)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=resume))
    assert result['events']==2
    # The already accepted sibling is read from its durable job and not repeated.
    assert sum(role=='event_writer' for role,_ in calls)==1
