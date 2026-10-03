import asyncio
import json

import pytest

from test_public_features import settings, ingest, output_for, synthetic_runner
from test_protected_append_sources import case
from serein.core.store import Store, digest
from serein.extensions import pipeline as p
from serein.extensions import pipeline_latest as latest


@pytest.mark.parametrize('model_value', [None, False, True, 'ignored'])
def test_new_event_eligibility_is_generated_by_host(settings, model_value):
    ingest(settings)
    async def runner(role, request):
        result = output_for(role, request)
        if role == 'event_writer':
            if model_value is None:
                result.pop('recallable')
            else:
                result['recallable'] = model_value
        return result
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))['events'] == 1
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute("SELECT recallable FROM fact_events WHERE status='active'").fetchone()[0] == 1


def test_normal_extension_appends_and_preserves_existing_settings_and_sources(settings):
    ingest(settings)
    asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))
    with Store(settings.database,read_only=True) as store:
        old=dict(store.conn.execute("SELECT * FROM fact_events WHERE status='active'").fetchone())
    ingest(settings,2)
    async def runner(role,request):
        if role!='event_writer':return output_for(role,request)
        assert request['writer_mode']=='append'
        assert {m['id'] for m in request['messages']}=={3,4}
        assert old['body'] in request['prompt']
        assert 'Agreed to plan 1' not in request['prompt']
        result=output_for(role,request)
        result.update(title='A different title',recallable=False)
        return result
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))['events']==1
    with Store(settings.database,read_only=True) as store:
        current=dict(store.conn.execute("SELECT * FROM fact_events WHERE status='active'").fetchone())
        assert current['body']==old['body']+'\n\nWe agreed to Book club plan 2'
        assert current['title']==old['title'] and current['recallable']==old['recallable']
        assert store.conn.execute('SELECT count(*) FROM fact_event_sources WHERE item_id=?',(current['item_id'],)).fetchone()[0]==4


@pytest.mark.parametrize('manual', [False, None])
@pytest.mark.parametrize('action', ['extend', 'rewrite'])
def test_continuation_preserves_manual_closure_or_unreviewed_state(settings, manual, action):
    from serein.compat.events import Events
    ingest(settings)
    asyncio.run(p.advance(settings.database, include_recent=True, runner=synthetic_runner))
    with Store(settings.database, read_only=True) as store:
        key = store.conn.execute("SELECT item_id FROM fact_events WHERE status='active'").fetchone()[0]
    Events(settings.database).revise(key, recallable=manual)
    ingest(settings, 2)
    async def runner(role, request):
        result = output_for(role, request)
        if role == 'event_curator': result['events'][0]['action'] = action
        if role == 'event_writer': result['recallable'] = True
        return result
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))['events'] == 1
    with Store(settings.database, read_only=True) as store:
        row = store.conn.execute("SELECT item_id,recallable FROM fact_events WHERE status='active'").fetchone()
        assert row['recallable'] == (None if manual is None else 0)
        assert store.read(row['item_id'])['manual_surface'] == row['recallable']


@pytest.mark.parametrize('drift',[False,True])
def test_already_bound_pending_sources_never_call_writer_or_replace_event(settings,drift):
    ingest(settings)
    asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))
    ingest(settings,2)
    with Store(settings.database) as store:
        event=store.conn.execute("SELECT item_id FROM fact_events WHERE status='active'").fetchone()[0]
        # Synthetic receipt/marker drift: sources are bound, but their processing
        # markers were never written. They must not create another paid request.
        for raw in store.conn.execute('SELECT * FROM raw_events WHERE id IN (3,4)').fetchall():
            store.conn.execute('INSERT INTO fact_event_sources(item_id,source_system,session_id,message_id,role,created_at,content,content_sha256,hash_algorithm,evidence_kind,binding_method) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (event,raw['source'],raw['session_id'],raw['source_event_id'],raw['role'],raw['created_at'],raw['text'],digest(raw['text']),'sha256','supporting','synthetic'))
    calls=[]
    async def runner(role,request):
        calls.append(role)
        assert role!='event_writer'
        result=output_for(role,request)
        if drift and role=='event_curator':
            with Store(settings.database) as store:
                store.conn.execute("UPDATE fact_events SET status='tombstoned' WHERE item_id=?",(event,))
        return result
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM fact_events').fetchone()[0]==1
        assert result['events']==0 and result['processed_originals']==0 and result['deferred']==2
        assert store.conn.execute('SELECT count(*) FROM raw_processing WHERE raw_id IN (3,4)').fetchone()[0]==0
    assert calls.count('event_writer')==0


@pytest.mark.parametrize('action',['rewrite','merge'])
def test_explicit_full_body_work_reads_only_selected_old_sources(monkeypatch,action):
    component,proposal=case(stable=(5,6),action=action)
    component['base_event_candidates'][0]['protected']=False
    component['base_event_candidates'].append({
        **component['base_event_candidates'][0],'event_id':'second','source_message_ids':[3,4]})
    proposal['events'][0]['base_event_ids']=['target','second'] if action=='merge' else ['target']
    event=latest.normalize_event_curator_output(proposal,component)['events'][0]
    assert not event.get('append_only')
    monkeypatch.setattr(p,'snapshot',lambda *args:{
        'identity':{'user_name':'User','ai_name':'AI'},'models':{},'revision':1,'policy':{'execution_mode':'agent'}})
    request=p.request_for(None,{'id':'synthetic','input_json':json.dumps({'day':'2026-01-01'})},
        'event_writer',event=event,component=component,messages=component['context_messages'])
    assert request['writer_mode']=='rewrite'
    expected={1,2,5,6} if action=='rewrite' else {1,2,3,4,5,6}
    assert {m['id'] for m in request['messages']}==expected
    assert '<previous_events_json>' in request['prompt']
    if action=='rewrite':
        assert 'Synthetic message 3' not in request['prompt'] and 'Synthetic message 4' not in request['prompt']


@pytest.mark.parametrize('context_read',[False,True])
def test_extension_has_only_explicit_bounded_context_and_no_reply_target_text(monkeypatch,context_read):
    component,proposal=case(stable=(3,4))
    component['base_event_candidates'][0]['protected']=False
    component['base_event_candidates'].append({
        **component['base_event_candidates'][0],'event_id':'other','source_message_ids':[5,6]})
    component['context_messages'] += [
        {'id':i,'role':'user','content':'Context '+str(i)+'x'*990,'created_at':'2026-01-01T00:00:00Z'}
        for i in range(10,30)]
    component['messages'][0]['metadata']={'reply_context':{
        'target_id':1,'target_text':'LEAKED_OLD_REPLY_TARGET '*2000},
        'original_message':{'reply_to':{'target_text':'LEAKED_OLD_REPLY_TARGET '*2000}}}
    component['context_receipt']={'read_source_ids':[1,2,5,6,*range(10,30)]}
    event=latest.normalize_event_curator_output(proposal,component)['events'][0]
    monkeypatch.setattr(p,'snapshot',lambda *args:{
        'identity':{'user_name':'User','ai_name':'AI'},'models':{},'revision':1,'policy':{'execution_mode':'agent'}})
    request=p.request_for(None,{'id':'synthetic','input_json':json.dumps({'day':'2026-01-01'})},
        'event_writer',event=event,component=component,messages=component['context_messages'],context_read=context_read)
    block=json.loads(request['prompt'].split('<event_reading_block_json>\n',1)[1].split('\n</event_reading_block_json>',1)[0])
    assert {m['source_message_id'] for m in block}.isdisjoint({1,2,5,6})
    context=[m for m in block if m['evidence_role']=='context_only']
    if context_read:
        assert 0<len(context)<=6 and sum(len(m['text']) for m in context)<=6000
        assert {m['source_message_id'] for m in context}.issubset(component['context_receipt']['read_source_ids'])
    else:assert context==[]
    assert 'LEAKED_OLD_REPLY_TARGET' not in request['prompt']


def test_prompt_builder_defends_append_against_mispassed_old_materials():
    old={'id':1,'content':'OLD_FULL_TEXT','role':'user','created_at':'2026-01-01T00:00:00Z'}
    new={'id':2,'content':'New progress','role':'user','created_at':'2026-01-02T00:00:00Z'}
    base={'event_id':'old','body':'Old Event body','title':'Old title','source_message_ids':[1]}
    prompt=latest.build_event_writer_prompt('2026-01-02','',[old,new],
        previous_events=[base],context_messages=[old],append_only=True,
        source_materials=[{'source_message_id':1,'reason':'OLD_MATERIAL_QUOTE'},
                          {'source_message_id':2,'reason':'New material'}])
    assert 'Old Event body' in prompt and 'New progress' in prompt
    assert 'OLD_FULL_TEXT' not in prompt and 'OLD_MATERIAL_QUOTE' not in prompt


def test_append_total_body_limit_remains_1500_and_preserves_old_body_on_overflow(settings):
    ingest(settings)
    with Store(settings.database) as store:
        store.conn.execute("UPDATE raw_events SET text=? WHERE source_event_id='u1'",('Synthetic grounded text '+'x'*800,))
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))['events']==1
    with Store(settings.database,read_only=True) as store:
        old=store.conn.execute("SELECT body FROM fact_events WHERE status='active'").fetchone()[0]
    ingest(settings,2)
    async def oversized(role,request):
        result=output_for(role,request)
        if role=='event_writer':
            assert request['append_remaining_chars']==1500-len(old)-2
            result['event_draft']='x'*(request['append_remaining_chars']+1)
        return result
    with pytest.raises(ValueError,match='总计不得超过 1500'):
        asyncio.run(p.advance(settings.database,include_recent=True,runner=oversized))
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute("SELECT body FROM fact_events WHERE status='active'").fetchone()[0]==old
        assert store.conn.execute('SELECT count(*) FROM raw_processing WHERE raw_id IN (3,4)').fetchone()[0]==0
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))['events']==1
    with Store(settings.database,read_only=True) as store:
        body=store.conn.execute("SELECT body FROM fact_events WHERE status='active'").fetchone()[0]
        assert len(body)<=1500 and body.startswith(old+'\n\n')


def test_full_old_body_blocks_append_before_any_writer_call(settings):
    ingest(settings)
    with Store(settings.database) as store:
        store.conn.execute("UPDATE raw_events SET text=? WHERE source_event_id='u1'",('x'*1487,))
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))['events']==1
    ingest(settings,2)
    calls=[]
    async def without_writer(role,request):
        calls.append(role)
        assert role!='event_writer'
        return output_for(role,request)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=without_writer))
    assert result['status']=='needs_repair' and '1500' in result['reason']
    assert calls==['track_router','event_curator']
    with Store(settings.database,read_only=True) as store:
        assert len(store.conn.execute("SELECT body FROM fact_events WHERE status='active'").fetchone()[0])==1500
        assert store.conn.execute('SELECT count(*) FROM raw_processing WHERE raw_id IN (3,4)').fetchone()[0]==0


def test_completed_legacy_writer_is_not_replayed_or_appended_twice(settings):
    ingest(settings)
    asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))
    ingest(settings,2)
    task=asyncio.run(p.advance(settings.database,include_recent=True))
    while task['role']!='event_writer':
        p.submit(settings.database,task['job_id'],output_for(task['role'],task['request']))
        task=asyncio.run(p.advance(settings.database,include_recent=True))
    with Store(settings.database,read_only=True) as store:
        request=json.loads(store.conn.execute('SELECT request_json FROM pipeline_jobs WHERE id=?',(task['job_id'],)).fetchone()[0])
    request['event'].pop('append_only')
    request.pop('writer_mode')
    request['writer_previous_body_only']=1
    output=output_for('event_writer',request)
    output['event_draft']='Legacy completed whole Event'
    with Store(settings.database) as store:
        store.conn.execute('UPDATE pipeline_jobs SET request_json=?,output_json=? WHERE id=?',
            (json.dumps(request),json.dumps(output),task['job_id']))
    async def forbidden(*args):
        raise AssertionError('completed stages must not call the model again')
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=forbidden))['events']==1
    with Store(settings.database,read_only=True) as store:
        body=store.conn.execute("SELECT body FROM fact_events WHERE status='active'").fetchone()[0]
        assert body=='Legacy completed whole Event'
