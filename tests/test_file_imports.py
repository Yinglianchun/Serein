import asyncio
import json
import pytest
from fastapi.testclient import TestClient
from serein.config import Settings
from serein.bootstrap import initialize
from serein.api.http import create_app
from serein.imports import parse_file,stage,advance_import
from serein.core.store import Store
from serein.deployment import save_settings
from serein.extensions.pipeline import advance
from serein.import_tagging import process

NAMES={'user_name':'Nori','ai_name':'Atlas'}

@pytest.fixture
def settings(tmp_path):
    value=Settings(tmp_path/'memory.db',tmp_path/'index.db',writable=True)
    initialize(value);return value

def messages(count=2):
    return {'id':'original-session','messages':[{'id':f'm{i}','role':'user' if i%2==0 else 'assistant',
        'content':f'  original {i}\n','timestamp':1700000000+i} for i in range(count)]}

def test_chatgpt_current_branch_and_claude_sessions():
    nodes={key:{'parent':parent,'message':{'id':key,'author':{'role':role},'content':{'parts':[body]},'create_time':1700000000}}
           for key,parent,role,body in [('u',None,'user','Question'),('old','u','assistant','Unselected'),('new','u','assistant','Selected')]}
    parsed=parse_file(json.dumps({'id':'thread','mapping':nodes,'current_node':'new'}),'chat.json',NAMES)
    assert [row['text'] for row in parsed['entries']]==['Question','Selected']
    with pytest.raises(ValueError,match='current_node'):
        parse_file(json.dumps({'mapping':nodes}),'chat.json',NAMES)
    claude=[{'uuid':f'c{i}','chat_messages':[{'uuid':'reused','sender':'human','text':f'conversation {i}',
        'created_at':'2025-01-01T00:00:00Z'}]} for i in range(2)]
    parsed=parse_file(json.dumps(claude),'claude.json',NAMES)
    assert len({item['session_id'] for item in parsed['entries']})==2
    assert len({item['source_event_id'] for item in parsed['entries']})==2

def test_generic_jsonl_markdown_and_invalid_json():
    parsed=parse_file('\n'.join(json.dumps(row) for row in messages()['messages']),'chat.jsonl',NAMES)
    assert len(parsed['entries'])==2
    parsed=parse_file('Nori: hello\nAtlas: welcome\n```\nuser: quoted code\n```','chat.md',NAMES)
    assert [row['role'] for row in parsed['entries']]==['user','assistant']
    assert 'user: quoted code' in parsed['entries'][1]['text']
    with pytest.raises(ValueError,match='JSON 格式'):
        parse_file('{broken','chat.json',NAMES)
    with pytest.raises(ValueError,match='无法识别'):
        parse_file('{"secret":"not a transcript"}','chat.json',NAMES)

def test_upload_preview_resume_and_no_processing_partial_file(settings):
    client=TestClient(create_app(settings,token='test',live=True),headers={'Authorization':'Bearer test'})
    body=json.dumps(messages(28))
    preview=client.post('/v1/imports/preview',json={'filename':'chat.json','content':body}).json()
    assert preview['total']==28 and preview['processed']==0
    with Store(settings.database) as store:assert store.conn.execute('SELECT COUNT(*) FROM raw_events').fetchone()[0]==0
    queued=client.post('/v1/imports/'+preview['id']+'/continue').json()
    assert queued['status']=='queued'
    assert client.post('/v1/imports/'+preview['id']+'/continue').json()['run_id']==queued['run_id']
    part=advance_import(settings,preview['id'])
    assert part['processed']==25
    assert asyncio.run(advance(settings.database,include_recent=True))['status']=='current'
    other=TestClient(create_app(settings,token='test',live=True),headers={'Authorization':'Bearer test'})
    assert other.get('/v1/imports').json()['items'][0]['processed']==25
    assert other.post('/v1/imports/'+preview['id']+'/continue').json()['run_id']==queued['run_id']
    from serein.work_tasks import execute, work
    result=asyncio.run(execute(settings.database,queued['id'],lambda:work(settings,queued['id'],{}),queued_id=queued['run_id']))
    assert result['inserted']==28 and result['status']=='completed'
    assert asyncio.run(advance(settings.database,include_recent=True))['status']=='current'
    same=stage(settings.database,body,'renamed.json','auto',True)
    assert same['id']==preview['id'] and same['inserted']==28
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT text FROM raw_events ORDER BY id LIMIT 1').fetchone()[0]=='  original 0\n'
    client.headers.clear();assert client.get('/v1/imports').status_code==401

def operit(body='\nOriginal body.\n',uuid='stable-uuid'):
    return {'exportDate':1700000000000,'memories':[{'uuid':uuid,'title':'Original title','content':body,
        'contentType':'text/plain','createdAt':1700000000000,'updatedAt':1700000001000,
        'tagNames':['original tag'],'folderPath':'Books','importance':.9}], 'links':[{'source':uuid,'target':'other'}]}


def test_first_import_stores_passages_without_models_and_duplicate_reuses_them(settings,monkeypatch):
    import sqlite3
    from serein.recall.passage_layouts import layout_path
    save_settings(settings.database,{'recall':{'passages_enabled':True}})
    data=operit('原文。'*200)
    preview=stage(settings.database,json.dumps(data),'long.json','operit',False)
    assert advance_import(settings,preview['id'])['inserted']==1
    with sqlite3.connect(layout_path(settings.database)) as db:
        row=db.execute('SELECT * FROM layouts').fetchone()
        assert row[2]==500 and len(json.loads(row[3]))>1
    monkeypatch.setattr('serein.recall.passages.slices',lambda *a,**kw:pytest.fail('Repeated import cut existing memory'))
    data['exportDate']+=1
    again=stage(settings.database,json.dumps(data),'again.json','operit',False)
    assert advance_import(settings,again['id'])['duplicate']==1

def test_operit_originals_metadata_duplicates_and_conflict(settings):
    data=operit();preview=stage(settings.database,json.dumps(data),'operit.json','operit',True)
    result=advance_import(settings,preview['id']);assert result['inserted']==1
    with Store(settings.database) as store:
        key=store.conn.execute("SELECT id FROM documents WHERE kind='scene'").fetchone()[0]
        doc=store.read(key)
        assert doc['body_md']=='\nOriginal body.\n'
        assert doc['metadata']['operit_original']['folderPath']=='Books'
        assert doc['metadata']['date']=='2023-11-15'  # Preserve the old import's UTC+8 calendar date.
        assert doc['metadata']['scene_cues']==[]
        assert store.conn.execute('SELECT COUNT(*) FROM raw_events').fetchone()[0]==0
        assert store.conn.execute('SELECT COUNT(*) FROM index_outbox').fetchone()[0]>0
        assert store.conn.execute('SELECT COUNT(*) FROM scene_relations').fetchone()[0]==0
    data['exportDate']+=1
    again=stage(settings.database,json.dumps(data),'second.json','auto',True)
    assert advance_import(settings,again['id'])['duplicate']==1
    data['memories'][0]['content']='conflicting body'
    changed=stage(settings.database,json.dumps(data),'changed.json','auto',True)
    result=advance_import(settings,changed['id']);assert result['failed']==1
    with Store(settings.database) as store:assert store.read(key)['body_md']=='\nOriginal body.\n'

def test_operit_tagging_waits_for_model_and_never_rewrites_body(settings,monkeypatch):
    preview=stage(settings.database,json.dumps(operit()),'operit.json','auto',True)
    advance_import(settings,preview['id'])
    asyncio.run(process(settings.database))
    with Store(settings.database) as store:assert store.conn.execute('SELECT status FROM import_tag_jobs').fetchone()[0]=='pending'
    save_settings(settings.database,{'models':[{'id':'local','model':'deepseek-flash','base_url':'https://api.deepseek.com'}],
        'assignments':{'operit_tagging':'local'},'identity':NAMES})
    async def complete(model,payload):
        request=json.loads(payload['messages'][1]['content']);assert request['identity']['ai_name']=='Atlas'
        assert '额外返回 cues 数组' in payload['messages'][0]['content']
        assert 'Atlas' in request['forbidden_names']
        assert payload['thinking']=={'type':'disabled'}
        return {'choices':[{'message':{'content':json.dumps({'entities':[],'cues':['Original body'],'tags':['reading'],'domain':'life','body':'Invented text ignored'})}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    asyncio.run(process(settings.database))
    with Store(settings.database) as store:
        key=store.conn.execute('SELECT document_id FROM import_tag_jobs').fetchone()[0]
        assert store.conn.execute('SELECT status FROM import_tag_jobs').fetchone()[0]=='done'
        doc=store.read(key);assert doc['body_md']=='\nOriginal body.\n' and 'tags' not in doc['metadata']
        assert doc['metadata']['canonical_domain']=='life'
        assert doc['metadata']['operit_original']['tagNames']==['original tag'] and doc['metadata']['scene_cues']==['Original body']
        assert doc['metadata']['operit_cues_generated'] is True
        from serein.recall.scene import cue_matches
        from serein.recall.query import Query
        assert cue_matches(doc,Query('Original body'))


def test_custom_tagging_domains_persist_and_drive_boundaries(settings):
    from serein.deployment import read_settings
    from serein.recall.service import Recall
    client=TestClient(create_app(settings,token='test',live=True),headers={'Authorization':'Bearer test'})
    before=client.get('/v1/settings').json()
    assert len(before['tagging']['domains'])==7
    custom={'key':'reading','label':'阅读','description':'阅读经历','policy':'excluded'}
    result=client.patch('/v1/settings',json={'tagging':{'domains':[custom]}})
    assert result.status_code==200,result.text
    assert read_settings(settings.database)['tagging']['domains']==[custom]
    assert Recall(settings).policy.domains['reading']=='excluded'
    published=client.get('/api/semantic-recall/domain-policies').json()
    assert published['policies']==[custom]
    assert client.post('/api/semantic-recall/domain-policies/publish',json={
        'confirm':'PUBLISH_DOMAIN_RECALL_POLICIES','expected_dataset_version':published['dataset_version'],
        'policies':[{'key':'reading','policy':'normal'}]}).status_code==200
    assert Recall(settings).policy.domains['reading']=='normal'
    assert client.patch('/v1/settings',json={'tagging':{'domains':[custom,custom]}}).status_code==422


def test_domain_editor_updates_next_tagging_request_without_restart(settings, monkeypatch):
    from serein.compat.scenes import Scenes
    from serein.deployment import read_settings
    client=TestClient(create_app(settings,token='test',live=True),headers={'Authorization':'Bearer test'})
    save_settings(settings.database,{'models':[{'id':'tagger','model':'synthetic','base_url':'http://127.0.0.1:9/v1'}],
        'assignments':{'operit_tagging':'tagger'}})
    endpoint='/api/semantic-recall/domain-policies'
    baseline=client.get(endpoint).json()
    custom={'key':'reading','label':'阅读','description':'书籍与阅读经历，不包含项目文档','policy':'normal'}
    scenes=Scenes(settings.database)
    scenes.write('A book borrowed on Sunday.',['Sunday book'],title='Pending before domain change')
    calls=[]
    async def complete(model,payload):
        calls.append(json.loads(payload['messages'][1]['content']))
        return {'choices':[{'message':{'content':'{"domain":"reading","entities":[]}'}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)

    def publish(domains,version):
        return client.post(endpoint+'/publish',json={'confirm':'PUBLISH_DOMAIN_RECALL_POLICIES',
            'expected_dataset_version':version,'domains':domains})

    catalog=[*baseline['policies'],custom]
    added=publish(catalog,baseline['dataset_version'])
    assert added.status_code==200,added.text
    assert client.get(endpoint).json()['policies']==catalog
    asyncio.run(process(settings.database))
    assert calls[-1]['domains']==catalog
    updated={**custom,'label':'阅读与书评','description':'阅读体验、书评与借阅；排除工作文档'}
    catalog[-1]=updated
    changed=publish(catalog,added.json()['dataset_version'])
    assert changed.status_code==200,changed.text
    assert publish(catalog,baseline['dataset_version']).status_code==409
    assert publish([updated,updated],changed.json()['dataset_version']).status_code==400
    scenes.write('Another reading experience.',['another book'],title='After description update')
    asyncio.run(process(settings.database))
    assert len(calls)==2  # Existing tagged memories are not tagged again.
    assert calls[-1]['domains']==catalog
    assert read_settings(settings.database)['tagging']['domains']==catalog
    assert publish(baseline['policies'],changed.json()['dataset_version']).status_code==200
    assert client.get(endpoint).json()['policies']==baseline['policies']


def test_selected_tagging_binds_existing_cue_to_exact_passage(settings,monkeypatch):
    from serein.recall.legacy_indexes import cue_index
    save_settings(settings.database,{'models':[{'id':'tagger','model':'deepseek-flash','base_url':'https://api.deepseek.com/anthropic','protocol':'anthropic'}],
        'assignments':{'operit_tagging':'tagger'}})
    seen=[]
    async def complete(model,payload):
        assert payload['reasoning']=={'effort':'none'}
        seen.append((model,payload))
        return {'choices':[{'message':{'content':json.dumps({'bindings':[
            {'cue':'借书','passage_ordinal':0,'evidence':'借了一本书','confidence':.9},
            {'cue':'旅行','passage_ordinal':None,'evidence':'','confidence':0}]},ensure_ascii=False)}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    index=cue_index(settings,profile={'model':'synthetic-embedding','document_instruction':''})
    passages=[{'ordinal':0,'start_offset':0,'end_offset':9,'text':'今天借了一本书。'}]
    output=asyncio.run(index.binder.bind(title='图书馆',cues=['借书','旅行'],passages=passages))
    valid,invalid=index._validated_bindings(output,cues=['借书','旅行'],passages=passages)
    assert seen[0][0]['id']=='tagger' and seen[0][0]['protocol']=='anthropic'
    assert 'one exact continuous substring' in seen[0][1]['messages'][0]['content']
    assert len(valid)==1 and valid[0]['evidence_text']=='借了一本书'
    output['bindings'][0]['evidence']='虚构证据'
    valid,invalid=index._validated_bindings(output,cues=['借书','旅行'],passages=passages)
    assert not valid and invalid


def test_tagging_regular_scene_preserves_authored_cues_and_domain(settings,monkeypatch):
    from serein.compat.scenes import Scenes
    scenes=Scenes(settings.database)
    scenes.write('A book borrowed on Sunday.',['Sunday book'],title='New reading')
    scenes.write('A user classified memory.',['authored cue'],title='Already classified',domain='inner')
    save_settings(settings.database,{'models':[{'id':'tagger','model':'synthetic','base_url':'http://127.0.0.1:9/v1'}],
        'assignments':{'operit_tagging':'tagger'},'tagging':{'domains':[
            {'key':'reading','label':'阅读','description':'书籍与阅读经历','policy':'normal'}]}})
    calls=[]
    async def complete(model,payload):
        request=json.loads(payload['messages'][1]['content']);calls.append(request)
        assert [item['key'] for item in request['domains']]==['reading']
        return {'choices':[{'message':{'content':'{"domain":"reading","entities":[],"tags":["invented small tag"]}'}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    asyncio.run(process(settings.database))
    asyncio.run(process(settings.database))
    assert len(calls)==2
    with Store(settings.database) as store:
        docs={store.read(row[0])['title']:store.read(row[0]) for row in store.conn.execute("SELECT id FROM documents WHERE kind='scene'")}
    assert docs['New reading']['metadata']['canonical_domain']=='reading'
    assert docs['New reading']['metadata']['scene_cues']==['Sunday book']
    assert 'tags' not in docs['New reading']['metadata']
    assert docs['Already classified']['metadata']['canonical_domain']=='inner'

def test_conflicting_chat_message_and_unknown_timestamps_are_not_overwritten(settings):
    data=messages();data['messages'][0].pop('timestamp')
    first=stage(settings.database,json.dumps(data),'first.json','auto',False);advance_import(settings,first['id'])
    data['messages'].append({'id':'m2','role':'user','content':'new message'})
    second=stage(settings.database,json.dumps(data),'second.json','auto',False)
    result=advance_import(settings,second['id']);assert result['duplicate']==2 and result['inserted']==1
    data['messages'][0]['content']='changed original'
    third=stage(settings.database,json.dumps(data),'third.json','auto',False)
    assert advance_import(settings,third['id'])['failed']==1

@pytest.mark.parametrize('cues',[[],['Original body']])
def test_operit_completed_tagging_backfills_cues_once_and_respects_opt_out(settings,monkeypatch,cues):
    from serein.tagging_entities import snapshot,VERSION
    data=operit()
    upload=stage(settings.database,json.dumps(data),'old-operit.json','auto',True)
    advance_import(settings,upload['id'])
    with Store(settings.database) as store:
        key=store.conn.execute('SELECT document_id FROM import_tag_jobs').fetchone()[0]
        doc=store.read(key);_,stamp=snapshot(store,doc)
        store.revise(key,expected_revision=doc['revision'],title=doc['title'],body_md=doc['body_md'],
            metadata={**doc['metadata'],'entity_extraction_version':VERSION,'entity_input_hash':stamp,'tagged_entities':[],
                      'canonical_domain':'inner','operit_tagging_status':'done'})
        store.conn.execute("UPDATE import_tag_jobs SET body_hash=?,status='done'",(stamp,))
    data['memories'][0]['uuid']='opted-out'
    skipped=stage(settings.database,json.dumps(data),'opted-out.json','auto',False)
    advance_import(settings,skipped['id'])
    save_settings(settings.database,{'models':[{'id':'local','model':'synthetic','base_url':'http://127.0.0.1:9/v1'}],
        'assignments':{'operit_tagging':'local'}})
    calls=[]
    async def complete(model,payload):
        calls.append(payload)
        return {'choices':[{'message':{'content':json.dumps({'domain':'life','entities':[],'cues':cues})}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    for _ in range(3):asyncio.run(process(settings.database))
    assert len(calls)==1
    with Store(settings.database) as store:
        doc=store.read(key)
        assert doc['metadata']['scene_cues']==cues and doc['metadata']['operit_cues_generated'] is True
        assert doc['metadata']['canonical_domain']=='inner' and doc['body_md']=='\nOriginal body.\n'
        assert store.conn.execute('SELECT count(*) FROM import_tag_jobs').fetchone()[0]==1


def test_operit_named_cue_is_dropped_without_retry(settings,monkeypatch):
    upload=stage(settings.database,json.dumps(operit()),'operit.json','auto',True)
    advance_import(settings,upload['id'])
    save_settings(settings.database,{'models':[{'id':'local','model':'synthetic','base_url':'http://127.0.0.1:9/v1'}],
        'assignments':{'operit_tagging':'local'},'identity':NAMES})
    calls=[]
    async def complete(model,payload):
        request=json.loads(payload['messages'][1]['content']);calls.append(request)
        return {'choices':[{'message':{'content':json.dumps({'domain':'life','entities':[],'cues':['Atlas remembers this']})}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    asyncio.run(process(settings.database))
    with Store(settings.database) as store:
        row=store.conn.execute('SELECT * FROM import_tag_jobs').fetchone()
        assert row['status']=='done' and row['attempts']==1 and row['error']==''
        assert store.read(row['document_id'])['metadata']['scene_cues']==[]
        assert store.read(row['document_id'])['metadata']['operit_cues_generated'] is True
    asyncio.run(process(settings.database));assert len(calls)==1


def test_retry_one_preserves_success_and_other_failures(settings):
    from serein.imports import initialize_imports
    from serein.api.imports import routes
    from fastapi import FastAPI
    initialize_imports(settings.database)
    with Store(settings.database) as store:
        for key,state in [('a','failed'),('b','failed'),('c','done')]:
            store.conn.execute("INSERT INTO import_tag_jobs(document_id,body_hash,upload_id,status,attempts,error) VALUES (?,?,'',?,3,'prior reason')",(key,'hash',state))
    app=FastAPI();app.include_router(routes(settings,[]))
    client=TestClient(app)
    assert client.post('/v1/imports/retry-tagging',json={'limit':1}).json()=={'queued':1}
    with Store(settings.database) as store:
        rows=store.conn.execute('SELECT document_id,status,attempts,error FROM import_tag_jobs ORDER BY document_id').fetchall()
        assert [tuple(row) for row in rows]==[('a','pending',3,'prior reason'),('b','failed',3,'prior reason'),('c','done',3,'prior reason')]

def test_import_history_uses_compact_boundary_without_consuming_live_chat(settings):
    from serein.compat.raw_archive import raw_archive
    preview=stage(settings.database,json.dumps(messages(2)),'history.json','auto',False)
    assert advance_import(settings,preview['id'])['inserted']==2
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM pipeline_import_boundaries').fetchone()[0]==1
        assert store.conn.execute("SELECT count(*) FROM raw_processing WHERE outcome='archived_only'").fetchone()[0]==0
    raw_archive(settings).ingest([
        {'source_event_id':'live-u','session_id':'live','role':'user','text':'new question',
         'created_at':'2026-09-26T12:00:00Z'},
        {'source_event_id':'live-a','session_id':'live','role':'assistant','text':'new answer',
         'created_at':'2026-09-26T12:01:00Z'}],source='live')
    task=asyncio.run(advance(settings.database,include_recent=True))
    assert task['role']=='track_router'
    assert {item['content'] for item in task['request']['messages']}=={'new question','new answer'}
