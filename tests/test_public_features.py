import asyncio
import io
import json
import zipfile
import pytest
from dataclasses import replace
from fastapi.testclient import TestClient
from serein.application import Application
from serein.api.http import create_app
from serein.api.mcp import create_server
from serein.bootstrap import initialize
from serein.config import Settings
from serein.core.store import Store
from serein.deployment import save_settings, read_settings
from serein.api.export import markdown_archive
from serein.compat.raw_archive import raw_archive
from serein.extensions.pipeline import advance, submit, ROLES

@pytest.fixture
def settings(tmp_path):
    value=Settings(tmp_path/'memory.db',tmp_path/'index.db',writable=True,extensions={'handoff':{'enabled':True}})
    initialize(value)
    return value

def test_switch_removes_mcp_and_http_tools_and_preserves_content(settings):
    application=Application(settings);server=create_server(application)
    names=lambda:{tool.name for tool in asyncio.run(server.list_tools())}
    assert not {'memo_create','window_shadow_write'}&names()
    save_settings(settings.database,{'features':{'memos':True,'window_shadows':True}})
    assert {'memo_create','window_shadow_write'}<=names()
    asyncio.run(server.call_tool('memo_create',{'title':'Book','content':'Bring the book','memo_id':'m1'}))
    app=create_app(settings,token='test',live=True)
    client=TestClient(app,headers={'Authorization':'Bearer test'})
    result=client.post('/v1/extensions/memo_update',json={'memo_id':'m1','content':'Bring two books'})
    assert result.status_code==200,result.text
    with Store(settings.database) as store:
        store.create('reading-scene','scene','Library afternoon','We read at the library.')
    shadow=client.post('/v1/extensions/window_shadow_write',json={'window_id':'w1','title':'Reading','content':'A quiet reading session.','scene_ids':['reading-scene']})
    assert shadow.status_code==200,shadow.text
    assert client.get('/api/window-shadows').json()['windows'][0]['content']=='A quiet reading session.'
    projected=client.get('/api/window-shadows').json()['windows'][0]
    assert projected['title']=='Reading' and projected['scenes']==[{'id':'reading-scene','title':'Library afternoon'}]
    save_settings(settings.database,{'features':{'memos':False,'window_shadows':False}})
    assert not {'memo_create','window_shadow_write'}&names()
    with pytest.raises(Exception):asyncio.run(server.call_tool('memo_list',{}))
    assert client.post('/v1/extensions/memo_list',json={}).status_code==404
    assert client.get('/api/window-shadows').status_code==404
    save_settings(settings.database,{'features':{'memos':True,'window_shadows':True}})
    assert client.post('/v1/extensions/memo_list',json={}).json()['items'][0]['content']=='Bring two books'
    assert client.get('/api/window-shadows').json()['windows'][0]['revision']==1
    restricted=create_server(Application(replace(settings,mcp_tools=['memo_list'])))
    assert {tool.name for tool in asyncio.run(restricted.list_tools())}=={'memo_list'}
    save_settings(settings.database,{'features':{'memos':False}})
    assert asyncio.run(restricted.list_tools())==[]

def test_relation_auto_accept_only_new_valid_proposals_and_removal(settings):
    from test_live_relations import seed, create
    client=TestClient(create_app(settings,token='test',live=True),headers={'Authorization':'Bearer test'})
    source,target,old=seed(client)
    save_settings(settings.database,{'features':{'relations_auto_accept':True}})
    assert client.get('/api/scene-edges').json()['edges']==[]
    second=create(client,'Third book','A new book arrived.')
    result=client.post('/api/scene-edge-proposals/manual',json={'source_scene_id':target,'target_scene_id':second,
        'relation_type':'echoes','source_evidence':'后来又下雨了，我想起那天窗边的雨声。',
        'target_evidence':'A new book arrived.','reason':'Shared reading theme','confirm':'CREATE_SCENE_EDGE_PROPOSAL'})
    assert result.status_code==200,result.text
    assert result.json()['status']=='accepted',result.text
    edge=client.get('/api/scene-edges').json()['edges'][0]
    assert client.request('DELETE','/api/scene-edges/'+edge['edge_id'],json={'scene_id':target,'confirm':'DELETE_SCENE_EDGE'}).status_code==200
    assert client.get('/api/scene-edges').json()['edges']==[]

def test_resume_recent_ten_pending_and_export_readable_only(settings):
    save_settings(settings.database,{'features':{'resume':True}})
    with Store(settings.database) as store:
        store.create('favorite','scene','Book','Original scene body')
        store.create('hidden','scene','Hidden','Deleted body',lifecycle='deleted')
        for i in range(12):store.create(f'event-{i:02}','event','Event',f'Event {i}',created_at=f'2025-01-{i+1:02}T00:00:00Z')
    from serein.core.personal import Personal
    Personal(settings.database).save('favorite','favorite',{'favorite':True},document_id='favorite')
    raw_archive(settings).ingest([{'source_event_id':'m1','session_id':'a','role':'user','text':'Unfinished original','created_at':'2025-02-01T00:00:00Z'}],source='test')
    result=Application(settings).contributions.tools['resume']('new')
    assert result['total_recent_events']==10 and result['event_ids']==[f'event-{i:02}' for i in range(2,12)]
    assert result['total_pending_originals']==1 and result['items'][-1]['body_md']=='Unfinished original'
    with zipfile.ZipFile(io.BytesIO(markdown_archive(settings.database))) as archive:
        assert {'scene/','event/'}<=set(archive.namelist())
        texts=[archive.read(name).decode() for name in archive.namelist() if name.endswith('.md')]
        assert len(texts)==13 and not any('Deleted body' in text for text in texts)
        assert any(text.endswith('Original scene body') for text in texts)

def ingest(settings,number=1):
    return raw_archive(settings).ingest([
        {'source_event_id':f'u{number}','session_id':'books','role':'user','text':f'Book club plan {number}','created_at':f'2025-01-0{number}T00:00:00Z'},
        {'source_event_id':f'a{number}','session_id':'books','role':'assistant','text':f'Agreed to plan {number}','created_at':f'2025-01-0{number}T00:01:00Z'}],source='test')

def output_for(role,request):
    if role=='track_router':
        key=request['active_tracks'][0]['track_id'] if request['active_tracks'] else 'new:1'
        return {'message_assignments':[{'source_message_id':row['id'],'primary_track_ref':key,'context_track_refs':[],
            'routing_role':'primary_activity'} for row in request['messages']],
            'track_updates':[{'track_ref':key,'subject':'Book club','throughline':'Plan book club','event_policy':'rolling_engineering','status':'active'}]}
    if role=='event_curator':
        component=request['component'];bases=[item['event_id'] for item in component['base_event_candidates']]
        return {'events':[{'action':'merge' if len(bases)>1 else 'extend' if bases else 'create',
            'owned_unit_roots':[u['unit_root_message_id'] for u in component['memberships'] if u['unit_root_message_id'] in {m['id'] for m in component['messages']}], 'base_event_ids':bases,
            'primary_track_id':component['track_ids'][0]}],'skip_unit_roots':[],'defer_unit_roots':[], **({'image_transcriptions':[{'input_image':i,'text':'Visible book title','unreadable':False} for i,_ in enumerate(request['images'],1)]} if request.get('images') else {})}
    if role=='event_writer':
        from serein.extensions.pipeline_latest import _SELF_REVIEW_KEYS
        return {'title':'Book club','event_draft':'We agreed to '+request['messages'][0]['content'],
            'recallable':True,'evidence_sufficient':True,'kept_details':['Book club plan'],'discarded_details':[],
            'self_review':{key:True for key in _SELF_REVIEW_KEYS}}
    raise AssertionError('Unexpected or retired pipeline stage: '+role)

async def synthetic_runner(role,request):return output_for(role,request)

def test_pipeline_does_not_truncate_a_long_dialogue_unit(settings):
    raw_archive(settings).ingest([{'source_event_id':str(i),'session_id':'long','role':'assistant' if i==201 else 'user',
        'text':f'part {i}','created_at':'2025-01-01T00:00:00Z'} for i in range(202)],source='test')
    with pytest.raises(ValueError,match='提示词'):
        asyncio.run(advance(settings.database,include_recent=True))
    save_settings(settings.database,{'pipeline':{'max_prompt_chars':80000}})
    task=asyncio.run(advance(settings.database,include_recent=True))
    assert task['status']=='awaiting_agent' and len(task['request']['messages'])==202

def test_pipeline_agent_jobs_retry_ownership_and_rolling_extension(settings):
    ingest(settings)
    first=asyncio.run(advance(settings.database,include_recent=True))
    assert first['status']=='awaiting_agent' and first['role']=='track_router'
    assert asyncio.run(advance(settings.database,include_recent=True))['job_id']==first['job_id']
    bad=output_for(first['role'],first['request']);bad['message_assignments'].pop()
    with pytest.raises(ValueError):submit(settings.database,first['job_id'],bad)
    for role in ROLES:
        task=asyncio.run(advance(settings.database,include_recent=True))
        assert task['role']==role
        submit(settings.database,task['job_id'],output_for(role,task['request']))
    result=asyncio.run(advance(settings.database,include_recent=True))
    assert result['events']==1 and result['pending']==0
    assert asyncio.run(advance(settings.database,include_recent=True))['status']=='current'
    ingest(settings,2)
    assert asyncio.run(advance(settings.database,include_recent=True,runner=synthetic_runner))['events']==1
    with Store(settings.database) as store:
        assert store.conn.execute("SELECT COUNT(*) FROM documents WHERE kind='event' AND lifecycle='active'").fetchone()[0]==1
        assert store.conn.execute("SELECT COUNT(*) FROM documents WHERE kind='event' AND lifecycle='superseded'").fetchone()[0]==1
        key=store.conn.execute("SELECT id FROM documents WHERE kind='event' AND lifecycle='active'").fetchone()[0]
    assert len(Application(settings).services.read(key)['evidence'])==4

def test_pipeline_model_api_selection_uses_names_and_insufficient_writer_stays_pending(settings,monkeypatch):
    ingest(settings)
    save_settings(settings.database,{'identity':{'user_name':'Nori','ai_name':'Atlas'},
        'models':[{'id':'local','model':'synthetic','base_url':'http://127.0.0.1:9/v1'}],
        'assignments':{role:'local' for role in ROLES}})
    calls=[]
    async def complete(model,payload):
        with Store(settings.database,read_only=True) as store:
            request=json.loads(store.conn.execute('SELECT request_json FROM pipeline_jobs WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        role=request['role'];calls.append(role)
        assert request['identity']['ai_name']=='Atlas'
        assert payload['messages'][1]['content']==request['prompt']
        assert 'max_tokens' not in payload and 'max_completion_tokens' not in payload and 'max_output_tokens' not in payload
        result=output_for(role,request)
        if role=='event_writer':
            result.update(evidence_sufficient=False,recallable=False,title='',event_draft='',kept_details=[])
            result['self_review']['owned_evidence_sufficient']=False
        return {'choices':[{'message':{'content':json.dumps(result)}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    result=asyncio.run(advance(settings.database,include_recent=True))
    assert calls==list(ROLES) and result['events']==0 and result['pending']==2
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT COUNT(*) FROM raw_processing').fetchone()[0]==0

def test_persona_and_anti_retreat_independent_and_memo_ack(settings,monkeypatch):
    from serein.chat_features import prepare, delivered, after_reply
    calls=[]
    async def complete(model,payload):
        calls.append(payload)
        return {'choices':[{'message':{'content':json.dumps({'signal':True,'kind':'conflict','confidence':.99})}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    save_settings(settings.database,{'models':[{'id':'local','model':'synthetic','base_url':'http://127.0.0.1:9/v1'}],
        'assignments':{'persona':'local','anti_retreat':'local'}})
    assert asyncio.run(prepare(settings.database,'window','text',[]))[0]=='' and calls==[]
    save_settings(settings.database,{'features':{'anti_retreat':True}})
    text,receipt=asyncio.run(prepare(settings.database,'window','text',[]))
    assert text=='' and calls==[]
    delivered(settings.database,'window',receipt)
    asyncio.run(after_reply(settings.database,'window','text','response',[],round_id=receipt['round']));assert len(calls)==1
    text,_=asyncio.run(prepare(settings.database,'window','next',[]))
    assert '只是提醒' in text and 'Current Persona State' not in text
    save_settings(settings.database,{'features':{'anti_retreat':False,'persona':True,'memos':True}})
    application=Application(settings);application.refresh_optional()
    application.contributions.tools['memo_create']('Book','Bring it',memo_id='m1',repeat_rule='once')
    text,receipt=asyncio.run(prepare(settings.database,'window','text',[]))
    assert 'Current Persona State' not in text and 'Bring it' in text and len(calls)==1
    assert application.contributions.tools['memo_list']()['items'][0]['reminder_count']==0
    delivered(settings.database,'window',receipt)
    assert application.contributions.tools['memo_list']()['items']==[]
