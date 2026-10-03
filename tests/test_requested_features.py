import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from test_public_features import settings, ingest, output_for
from test_live_clients import live
from test_live_narratives import seed as seed_narrative
from serein.application import Application
from serein.api.http import create_app
from serein.chat_features import prepare, delivered, after_reply
from serein.compat.dreams import Dreams
from serein.core.store import Store, encode
from serein.deployment import save_settings, read_settings, task_model
from serein import work_tasks as work
from serein.extensions import pipeline


def configure(settings,features):
    save_settings(settings.database,{'models':[
        {'id':'shared','model':'shared-model','base_url':'http://127.0.0.1:9/v1'},
        {'id':'obsolete','model':'obsolete-model','base_url':'http://127.0.0.1:9/v1'}],
        'assignments':{'persona':'shared','anti_retreat':'obsolete','dreams':'shared'},'features':features})


def test_retreat_after_reply_next_turn_only_and_durable_cooldown(settings,monkeypatch):
    configure(settings,{'anti_retreat':True})
    calls=[]
    async def complete(model,payload):
        calls.append((model,payload))
        return {'choices':[{'message':{'content':json.dumps({'signal':True,'kind':'conflict','confidence':.99})}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    assert task_model(settings.database,'anti_retreat')['id']=='shared'
    text,receipt=asyncio.run(prepare(settings.database,'w','A disagreement',[]))
    assert text=='' and not calls
    delivered(settings.database,'w',receipt)
    asyncio.run(after_reply(settings.database,'w','A disagreement','The delivered answer',[],round_id=1))
    assert len(calls)==1 and calls[0][0]['id']=='shared'
    assert 'The delivered answer' in calls[0][1]['messages'][1]['content']
    assert not asyncio.run(prepare(settings.database,'other','next',[]))[0]
    text,receipt=asyncio.run(prepare(settings.database,'w','next',[]))
    assert '只是提醒' in text
    delivered(settings.database,'w',receipt)
    for n in range(3,10):
        text,receipt=asyncio.run(prepare(settings.database,'w','next',[]))
        assert text==''
        delivered(settings.database,'w',receipt)
        asyncio.run(after_reply(settings.database,'w','next','answer',[],round_id=receipt['round']))
    assert len(calls)==1  # Ten-minute cooldown still holds despite enough rounds.
    with Store(settings.database) as store:
        store.conn.execute("UPDATE background_state SET value_json=json_set(value_json,'$.last_at',?) WHERE name='anti_retreat:w'",(time.time()-601,))
    asyncio.run(after_reply(settings.database,'w','next','answer',[],round_id=9))
    assert len(calls)==2
    save_settings(settings.database,{'features':{'anti_retreat':False}})
    assert not asyncio.run(prepare(settings.database,'w','next',[]))[0]


def test_slow_detector_does_not_gate_response_body(settings,monkeypatch):
    configure(settings,{'anti_retreat':True})
    from serein.api import chat
    save_settings(settings.database,{'assignments':{'chat':'shared'}})
    async def answer(*args,**kwargs):return {'choices':[{'message':{'role':'assistant','content':'Delivered now'}}]}
    monkeypatch.setattr(chat,'complete',answer)
    async def check():
        detecting=asyncio.Event();release=asyncio.Event();sent=[]
        async def detector(*args,**kwargs):
            detecting.set();await release.wait();return {'triggered':False}
        monkeypatch.setattr('serein.chat_features.persona_engine',lambda *args:SimpleNamespace(detect_conflict_nudge=detector))
        app=create_app(settings,token='synthetic',live=True)
        raw=json.dumps({'messages':[{'role':'user','content':'Question'}]}).encode()
        async def receive():return {'type':'http.request','body':raw,'more_body':False}
        async def send(message):sent.append(message)
        scope={'type':'http','http_version':'1.1','method':'POST','scheme':'http','path':'/v1/chat/completions',
               'raw_path':b'/v1/chat/completions','query_string':b'', 'root_path':'',
               'headers':[(b'authorization',b'Bearer synthetic'),(b'content-type',b'application/json')],
               'server':('test',80),'client':('test',123)}
        request=asyncio.create_task(app(scope,receive,send))
        await asyncio.wait_for(detecting.wait(),5)
        assert any(m['type']=='http.response.body' and b'Delivered now' in m['body'] for m in sent)
        assert not request.done()
        release.set();await request
    asyncio.run(check())


def test_dream_prompt_saved_and_used_without_engine_restart(settings,monkeypatch):
    configure(settings,{})
    client=TestClient(create_app(settings,token='synthetic',live=True),headers={'Authorization':'Bearer synthetic'})
    assert client.patch('/v1/settings',json={'dream':{'main_prompt':'Synthetic main identity'}}).status_code==200
    engine=Dreams(settings);requests=[]
    async def complete(model,payload):
        requests.append(payload)
        return {'choices':[{'message':{'content':'A synthetic dream.'}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    assert asyncio.run(engine._call_dream_model({}))=='A synthetic dream.'
    prompt=requests[-1]['messages'][0]['content']
    assert prompt.startswith('Synthetic main identity') and '80 到 220' in prompt
    client.patch('/v1/settings',json={'dream':{'main_prompt':'Changed main identity'}}).raise_for_status()
    asyncio.run(engine._call_dream_model({}))
    assert requests[-1]['messages'][0]['content'].startswith('Changed main identity')
    assert read_settings(settings.database)['dream']['main_prompt']=='Changed main identity'
    assert client.patch('/v1/settings',json={'dream':{'main_prompt':'x'*40001}}).status_code==422


def test_narrative_main_model_preview_save_cas_and_writer_interlock(live):
    settings,client=live;seed_narrative(client,settings)
    save_settings(settings.database,{'upstream':{'writer_enabled':True},'features':{'narrative_tools':True}})
    app=Application(settings);tool=app.contributions.tools['narrative_volume']
    assert read_settings(settings.database)['upstream']['writer_enabled'] is False
    assert client.post('/v1/models/writer',json={}).status_code==409
    save_settings(settings.database,{'upstream':{'writer_enabled':True}})
    assert read_settings(settings.database)['upstream']['writer_enabled'] is False
    result=asyncio.run(tool('read',narrative_id='narrative_test'))
    assert result['status']=='ok' and result['receipt']['materials']['events']
    draft=asyncio.run(tool('preview',narrative_id='narrative_test',body='Main model authored prose.',receipt=result['receipt']))
    assert draft['writes_performed']==[]
    assert asyncio.run(tool('read',narrative_id='narrative_test'))['narrative']['revision']==1
    saved=asyncio.run(tool('save',narrative_id='narrative_test',receipt=draft['save_arguments']))
    assert saved['status']=='updated' and saved['model_called'] is False
    assert asyncio.run(tool('read',narrative_id='narrative_test'))['narrative']['body']=='Main model authored prose.'
    assert asyncio.run(tool('save',narrative_id='narrative_test',receipt=draft['save_arguments']))['status']=='conflict'
    save_settings(settings.database,{'features':{'narrative_tools':False}});app.refresh_optional()
    assert 'narrative_volume' not in app.contributions.tools
    assert read_settings(settings.database)['upstream']['writer_enabled'] is False
    with pytest.raises(ValueError):asyncio.run(tool('read',narrative_id='narrative_test'))


def test_narrative_api_writer_uses_text_only(settings, monkeypatch):
    save_settings(settings.database, {'models':[
        {'id':'writer','model':'synthetic-writer','base_url':'http://127.0.0.1:9/v1'}],
        'assignments':{'writer':'writer'}, 'upstream':{'writer_enabled':True}})
    client=TestClient(create_app(settings,token='synthetic',live=True),headers={'Authorization':'Bearer synthetic'})
    calls=[]
    async def complete(model,payload):
        calls.append((model,payload))
        return {'choices':[{'message':{'content':'{"body":"A sourced sentence."}'}}]}
    monkeypatch.setattr('serein.api.chat.complete',complete)
    task={'prompt':'Bound text only','output_schema':{'type':'object'}}
    assert client.post('/v1/models/writer',json={**task,'image_inputs':['https://example.org/original.png']}).status_code==400
    assert calls==[]
    response=client.post('/v1/models/writer',json=task)
    assert response.status_code==200,response.text
    assert calls[0][0]['request_timeout_seconds']==300
    assert calls[0][1]['messages'][0]['content']=='Bound text only'
    import httpx
    async def timeout(model,payload):
        raise httpx.ReadTimeout('synthetic timeout')
    monkeypatch.setattr('serein.api.chat.complete',timeout)
    assert client.post('/v1/models/writer',json=task).status_code==504
    async def total_timeout(model,payload):
        raise TimeoutError('synthetic total deadline')
    monkeypatch.setattr('serein.api.chat.complete',total_timeout)
    assert client.post('/v1/models/writer',json=task).status_code==504
    cancelled=[]
    original_wait_for=asyncio.wait_for
    async def bounded_wait_for(awaitable,timeout):
        assert timeout==300
        return await original_wait_for(awaitable,timeout=0.01)
    async def hung(model,payload):
        try:await asyncio.Event().wait()
        finally:cancelled.append(True)
    monkeypatch.setattr('serein.api.chat.asyncio.wait_for',bounded_wait_for)
    monkeypatch.setattr('serein.api.chat.complete',hung)
    assert client.post('/v1/models/writer',json=task).status_code==504
    assert cancelled==[True]


def test_background_task_claim_progress_failure_resume_and_single_execution(settings):
    async def check():
        started=asyncio.Event();release=asyncio.Event();calls=[]
        queued=work.enqueue(settings.database,'pipeline')
        assert work.enqueue(settings.database,'pipeline')['run_id']==queued['run_id']
        async def operation():
            calls.append(1);work.progress(stage='event_writer',completed=3)
            started.set();await release.wait();raise ValueError('Synthetic schema validation failed')
        task=asyncio.create_task(work.execute(settings.database,'pipeline',operation,queued_id=queued['run_id']))
        await started.wait()
        assert (await work.execute(settings.database,'pipeline',operation))['status']=='busy'
        state=work.status(settings.database,'pipeline')
        assert state['stage']=='event_writer' and state['completed']==3
        assert work.enqueue(settings.database,'pipeline')['run_id']==queued['run_id']
        release.set()
        with pytest.raises(ValueError):await task
        assert calls==[1]
        assert work.status(settings.database,'pipeline')['error']=='Synthetic schema validation failed'
        retried=work.enqueue(settings.database,'pipeline')
        assert retried['run_id']!=queued['run_id']
        async def success():return {'status':'processed','events':1}
        await work.execute(settings.database,'pipeline',success,queued_id=retried['run_id'])
        assert work.status(settings.database,'pipeline')['status']=='completed'
    asyncio.run(check())


def test_actual_worker_continues_import_without_browser_and_restores_pipeline(settings):
    from serein.imports import stage,list_imports
    from test_file_imports import messages
    upload=stage(settings.database,json.dumps(messages(52)),'synthetic.json','auto',False)
    client=TestClient(create_app(settings,token='synthetic',live=True),headers={'Authorization':'Bearer synthetic'})
    queued=client.post('/v1/imports/'+upload['id']+'/continue').json()
    assert queued['status']=='queued'
    async def run_worker():
        worker=asyncio.create_task(work.run(settings))
        try:
            async with asyncio.timeout(10):
                while work.status(settings.database,queued['id'])['status']!='completed':await asyncio.sleep(.05)
        finally:
            worker.cancel();await asyncio.gather(worker,return_exceptions=True)
    asyncio.run(run_worker())
    other=TestClient(create_app(settings,token='synthetic',live=True),headers={'Authorization':'Bearer synthetic'})
    result=other.get('/v1/imports').json()['items'][0]
    assert result['processed']==52 and result['inserted']==52 and result['task']['status']=='completed'
    ingest(settings)
    task=asyncio.run(pipeline.advance(settings.database,include_recent=True))
    state=other.get('/v1/pipeline/status').json()
    assert state['status']=='awaiting_agent' and state['result']['job_id']==task['job_id']
    pipeline.submit(settings.database,task['job_id'],output_for(task['role'],task['request']))
    next_task=asyncio.run(pipeline.advance(settings.database,include_recent=True))
    assert next_task['job_id']!=task['job_id']
    with Store(settings.database) as store:
        store.conn.execute("UPDATE background_state SET value_json=json_set(value_json,'$.status','running','$.lease_until',0) WHERE name='work:pipeline'")
    assert other.get('/v1/pipeline/status').json()['status']=='interrupted'
    assert other.post('/v1/pipeline/next',json={'include_recent':True}).json()['status']=='queued'
