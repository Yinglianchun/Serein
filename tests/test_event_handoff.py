import asyncio
import copy
import json
import pytest
from test_public_features import settings, ingest, output_for
from serein.core.store import Store, Conflict
from serein.deployment import save_settings, read_settings
from serein.extensions import pipeline as p, pipeline_latest as latest
from serein.extensions.pipeline_images import freeze_images, bind_transcriptions, verify_images, verify_transcriptions

PNG='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII='


@pytest.mark.parametrize('mode',['agent','api'])
def test_three_stage_image_chain_preserves_bytes_transcription_and_raw_sources(settings,monkeypatch,mode):
    import base64
    import hashlib
    from serein.compat.raw_archive import raw_archive
    raw_archive(settings).ingest([
        {'source_event_id':'image','session_id':'one','role':'user','text':'The book title','created_at':'2025-01-01T00:00:00Z',
         'metadata':{'attachments':[{'kind':'image','url':PNG}]}},
        {'source_event_id':'reply','session_id':'one','role':'assistant','text':'Agreed','created_at':'2025-01-01T00:01:00Z'}],source='synthetic')
    save_settings(settings.database,{'models':[{'id':'local','model':'synthetic','base_url':'http://127.0.0.1:9/v1'}],
        'assignments':{role:'local' for role in p.ROLES},'pipeline':{'execution_mode':mode}})
    seen=[];sha=hashlib.sha256(base64.b64decode(PNG.split(',')[1])).hexdigest()
    def check(request):
        role=request['role'];seen.append(role)
        assert role in ('track_router','event_curator','event_writer')
        if role=='event_curator':
            assert request['images'][0]['url']==PNG and request['images'][0]['sha256']==sha
            assert request['images'][0]['source_message_id']==1
            assert PNG not in request['prompt']
        if role=='event_writer':
            assert request['images']==[] and request['image_input_mode']=='transcriptions_only'
            assert request['curator_image_transcriptions']==[{'source_message_id':1,'position':1,'sha256':sha,
                'evidence_role':'owned','text':'Visible book title','unreadable':False}]
        return output_for(role,request)
    async def complete(model,payload):
        assert mode=='api','Agent mode must not call the API'
        with Store(settings.database,read_only=True) as store:
            request=json.loads(store.conn.execute('SELECT request_json FROM pipeline_jobs WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        if request.get('images'):
            assert request['images'][0]['url']=='[frozen task image]'
            from serein.extensions.pipeline_images import hydrate_request_images
            request=hydrate_request_images(settings.database,request['batch_id'],request)
        if request['role']=='event_curator':assert payload['messages'][1]['content'][1]['image_url']['url']==PNG
        if request['role']=='event_writer':
            assert isinstance(payload['messages'][1]['content'],str) and PNG not in payload['messages'][1]['content']
        result=check(request)
        if request['role']=='event_writer' and seen.count('event_writer')==1:
            result['self_review']['result_preserved']=False
        return {'choices':[{'message':{'content':json.dumps(result)}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    result=asyncio.run(p.advance(settings.database,include_recent=True))
    if mode=='agent':
        for role in p.ROLES:
            assert result['role']==role
            output=check(result['request'])
            p.submit(settings.database,result['job_id'],output)
            result=asyncio.run(p.advance(settings.database,include_recent=True))
    assert result['events']==1 and result['processed_originals']==2
    assert seen==list(p.ROLES)+(['event_writer'] if mode=='api' else [])
    with Store(settings.database,read_only=True) as store:
        details=json.loads(store.conn.execute('SELECT details_json FROM pipeline_event_details').fetchone()[0])
        assert 'evidence' not in details and details['curator_image_transcriptions'][0]['sha256']==sha
        if mode=='api':
            assert details['writer']['event_draft']=='We agreed to The book title'
            assert store.conn.execute("SELECT count(*) FROM pipeline_attempts WHERE job_id LIKE '%:event_writer:%'").fetchone()[0]==2
        assert PNG in store.conn.execute('SELECT metadata_json FROM raw_events WHERE id=1').fetchone()[0]
        refs=[dict(r) for r in store.conn.execute('SELECT * FROM fact_event_sources ORDER BY id')]
        assert [r['message_id'] for r in refs]==['image','reply']
        assert all(r['content_sha256'] for r in refs)
        assert store.conn.execute("SELECT count(*) FROM documents WHERE kind='event'").fetchone()[0]==1


def shared_proposals():
    messages=[{'id':i,'session_id':1,'content':'Synthetic activity','role':'user','created_at':'2025-01-01T00:00:00Z'} for i in range(1,8)]
    units=[{'unit_root_message_id':i,'source_message_ids':[i],'track_id':t,'session_id':1,'routing_role':'bridge' if i in (2,4) else 'primary_activity'} for i,t in enumerate(['a','a','b','b','c','d','d'],1)]
    component={'messages':messages,'context_messages':messages,'track_ids':list('abcd'),
               'memberships':units,'context_edges':[{'unit_root_message_id':2,'track_id':'b'},{'unit_root_message_id':4,'track_id':'c'}],
               'base_event_candidates':[{'event_id':'protected','primary_track_id':'a','source_message_ids':[90],'session_ids':[1],'manual':True}]}
    proposals={'events':[{'action':'extend' if t=='a' else 'create','base_event_ids':['protected'] if t=='a' else [],'primary_track_id':t,'owned_unit_roots':ids} for t,ids in zip('abcd',([1,2],[2,3,4],[4,5],[6,7]))],'skip_unit_roots':[],'defer_unit_roots':[],
               'decision_review':{'events':[{'event_index':i,'reason':f'Synthetic Track {i} activity'} for i in range(4)],
                                  'boundaries':[],'dispositions':[]}}
    return component,proposals


def test_protected_bridge_transitive_order_independent_and_independent_event():
    component,output=shared_proposals()
    for reverse in (False,True):
        value=copy.deepcopy(output)
        if reverse:value['events'].reverse()
        plan=latest.normalize_event_curator_output(value,component)
        assert [e['primary_track_id'] for e in plan['events']]==['d']
        assert plan['defer_source_message_ids']==[1,2,3,4,5]
        assert len(plan['hard_skips'])==3
        assert all(h['active_base_event_ids']==['protected'] for h in plan['hard_skips'])


def test_protection_does_not_hide_invalid_model_accounting_or_bridge():
    component,output=shared_proposals();output['skip_unit_roots']=[2]
    with pytest.raises(ValueError,match='disjoint'):latest.normalize_event_curator_output(output,component)
    output['skip_unit_roots']=[];component['memberships'][1]['routing_role']='primary_activity'
    with pytest.raises(ValueError,match='bridge'):latest.normalize_event_curator_output(output,component)


def test_images_are_frozen_and_transcriptions_cannot_claim_provenance():
    images=freeze_images([{'source_message_id':7,'position':1,'url':PNG,'evidence_role':'stable'}])
    output={'image_transcriptions':[{'input_image':1,'text':'Visible original','unreadable':False}]}
    bound=bind_transcriptions(output,images)
    assert bound[0]['source_message_id']==7 and len(bound[0]['sha256'])==64
    assert bound[0]['text']=='Visible original'
    verify_transcriptions(bound,images)
    for invalid in ([],bound*2,[{**bound[0],'sha256':'0'*64}],[{**bound[0],'evidence_role':'owned'}]):
        with pytest.raises(ValueError):verify_transcriptions(invalid,images)
    for entries in ([],output['image_transcriptions']*2,[{**output['image_transcriptions'][0],'source_message_id':99}]):
        with pytest.raises(ValueError):bind_transcriptions({'image_transcriptions':entries},images)
    images[0]['sha256']='0'*64
    with pytest.raises(ValueError):verify_images(images)


def test_writer_structure_without_lexical_style_rejection():
    value=output_for('event_writer',{'messages':[{'content':'Synthetic'}]})
    value['event_draft']='我笑称这是一场小小的试验。她说不确定，我说可以再试，她提醒我先记录条件。'
    value['sentence_evidence'][0]['sentence']=value['event_draft']
    assert 'result_or_unfinished' not in value
    assert latest.validate_event_writer_result(value)==[]
    value['self_review']['result_preserved']=False
    assert latest.validate_event_writer_result(value)


def test_api_configuration_conflicts_and_execution_freezes_all_stage_models(settings,monkeypatch):
    model={'id':'local','model':'first','base_url':'http://127.0.0.1:9/v1'}
    state=save_settings(settings.database,{'models':[model],'assignments':{role:'local' for role in p.ROLES},'pipeline':{'execution_mode':'api'}})
    with pytest.raises(Conflict):save_settings(settings.database,{'expected_version':state['settings_version']-1,'pipeline':{'execution_mode':'agent'}})
    calls=[]
    async def complete(selected,payload):
        calls.append(selected['model'])
        if len(calls)==1:save_settings(settings.database,{'models':[{**model,'model':'second'}]})
        with Store(settings.database,read_only=True) as store:
            request=json.loads(store.conn.execute('SELECT request_json FROM pipeline_jobs WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        return {'choices':[{'message':{'content':json.dumps(output_for(request['role'],request))}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    ingest(settings);assert asyncio.run(p.advance(settings.database,include_recent=True))['events']==1
    assert calls==['first']*3
    ingest(settings,2);assert asyncio.run(p.advance(settings.database,include_recent=True))['events']==1
    assert calls[3:]==['second']*3
    assert 'api_key' not in json.dumps(read_settings(settings.database,public=True)).replace('api_key_configured','').replace('api_key_env','')


def test_explicit_api_requires_all_models_and_agent_never_calls_api(settings,monkeypatch):
    with pytest.raises(ValueError):save_settings(settings.database,{'pipeline':{'execution_mode':'api'}})
    save_settings(settings.database,{'models':[{'id':'local','model':'example','base_url':'http://127.0.0.1:9/v1'}],
        'assignments':{role:'local' for role in p.ROLES},'pipeline':{'execution_mode':'agent'}})
    async def fail(*args):raise AssertionError('Agent must not call API')
    monkeypatch.setattr('serein.model_runtime.complete',fail)
    ingest(settings);task=asyncio.run(p.advance(settings.database,include_recent=True))
    assert task['status']=='awaiting_agent' and task['request']['execution']['model']=='example'


def test_writer_bounded_reread_gets_new_context_images_without_owning_them(settings,monkeypatch):
    ingest(settings);seen=[];context_ids=[]
    def reread(database,component,query):
        from serein.compat.raw_archive import raw_archive
        result=raw_archive(settings).ingest([{
            'source_event_id':'earlier-image','session_id':'earlier','role':'user','text':'An earlier title',
            'created_at':'2024-12-31T00:00:00Z',
            'metadata':{'attachments':[{'kind':'image','url':PNG}]}}],source='synthetic-context')
        context_id=result['items'][0]['id'];context_ids.append(context_id)
        context=copy.deepcopy(component)
        context['context_messages'].append({**context['messages'][0],'id':context_id,'content':'An earlier title',
            'metadata':{'attachments':[{'kind':'image','url':PNG}]}})
        return context
    monkeypatch.setattr(p,'extend_context',reread)
    async def runner(role,request):
        if request.get('transcription_only'):
            seen.append('transcribed')
            return {'image_transcriptions':[{'input_image':1,'text':'Earlier title','unreadable':False}]}
        if role=='event_writer' and not request.get('context_read'):
            return {'context_request':{'track_id':request['component']['track_ids'][0],
                'before_message_id':request['component']['messages'][0]['id'],'reason':'missing_subject'}}
        if role=='event_writer':
            seen.append('writer')
            assert context_ids[0] not in request['event']['source_message_ids']
            assert request['images']==[]
            assert request['curator_image_transcriptions'][0]['evidence_role']=='context_only'
            assert request['curator_image_transcriptions'][0]['text']=='Earlier title'
            with pytest.raises(ValueError):p.validate(request,{'context_request':{'track_id':request['component']['track_ids'][0],'before_message_id':1,'reason':'missing_subject'}})
        return output_for(role,request)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))['events']==1
    assert seen==['transcribed','writer']
    with Store(settings.database,read_only=True) as store:
        metadata=json.loads(store.conn.execute('SELECT details_json FROM pipeline_event_details').fetchone()[0])
        assert metadata['curator_image_transcriptions'][0]['source_message_id']==context_ids[0]


def test_completed_task_media_expires_after_seven_days_but_raw_archive_and_text_remain(settings):
    from serein.compat.raw_archive import raw_archive
    raw_archive(settings).ingest([
        {'source_event_id':'image','session_id':'one','role':'user','text':'The book title','created_at':'2025-01-01T00:00:00Z','metadata':{'attachments':[{'kind':'image','url':PNG}]}},
        {'source_event_id':'reply','session_id':'one','role':'assistant','text':'Agreed','created_at':'2025-01-01T00:01:00Z'}],source='synthetic')
    async def runner(role,request):return output_for(role,request)
    asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))
    with Store(settings.database) as store:
        store.conn.execute("UPDATE pipeline_batches SET result_json=json_set(result_json,'$.completed_at','2000-01-01T00:00:00Z') WHERE status='done'")
    p.initialize(settings.database)
    with Store(settings.database,read_only=True) as store:
        assert all(PNG not in row[0] for row in store.conn.execute('SELECT request_json FROM pipeline_jobs'))
        assert PNG in store.conn.execute('SELECT metadata_json FROM raw_events WHERE id=1').fetchone()[0]
        assert 'Visible book title' in store.conn.execute('SELECT details_json FROM pipeline_event_details').fetchone()[0]


def test_agent_adapter_returns_actual_mcp_image_blocks(monkeypatch):
    import importlib.util
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('event_agent_test',Path(__file__).parents[1]/'scripts/event_agent_mcp.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setattr(module,'call',lambda *args:{'status':'awaiting_agent','request':{'images':[{'url':PNG}]}})
    result=module.pipeline_next()
    assert result[0].type=='text' and PNG not in result[0].text
    assert result[1].type=='image' and result[1].mimeType=='image/png'


def test_remote_images_pin_validated_address_and_reject_internal_targets(monkeypatch):
    from serein.extensions import pipeline_images as images
    import base64
    connected=[];requests=[]
    monkeypatch.setattr(images.socket,'getaddrinfo',lambda *args:[(2,1,6,'',('8.8.8.8',80))])
    monkeypatch.setattr(images.socket,'create_connection',lambda address,**kwargs:connected.append(address))
    class Reply:
        status=200
        def read(self,size):
            if getattr(self,'done',False):return b''
            self.done=True;return base64.b64decode(PNG.split(',')[1])
    class Connection:
        def __init__(self,host,port,**kwargs):self.port=port;requests.append(host)
        def request(self,*args):requests.append(args)
        def getresponse(self):return Reply()
        def close(self):pass
    monkeypatch.setattr(images.http.client,'HTTPConnection',Connection)
    assert images.image_bytes('http://example.test/book.png')[1]=='image/png'
    assert connected==[('8.8.8.8',80)] and requests[0]=='example.test'
    monkeypatch.setattr(images.socket,'getaddrinfo',lambda *args:[(2,1,6,'',('127.0.0.1',80))])
    with pytest.raises(ValueError):images.image_bytes('http://example.test/private')
    assert len(connected)==1


def test_remote_images_follow_bounded_revalidated_redirects(monkeypatch):
    from serein.extensions import pipeline_images as images
    import base64
    addresses={'short.test':'8.8.8.8','cdn.test':'1.1.1.1'}
    connected=[];requests=[];responses={
        ('short.test','/start'): (302,'/next',b''),
        ('short.test','/next'): (307,'http://cdn.test/book.png',b''),
        ('cdn.test','/book.png'): (200,None,base64.b64decode(PNG.split(',')[1])),
    }
    monkeypatch.setattr(images.socket,'getaddrinfo',lambda host,port:[(2,1,6,'',(addresses[host],port))])
    monkeypatch.setattr(images.socket,'create_connection',lambda address,**kwargs:connected.append(address))
    class Reply:
        def __init__(self,status,location,body):self.status=status;self.location=location;self.body=body
        def getheader(self,name):return self.location if name.lower()=='location' else None
        def read(self,size):body,self.body=self.body,b'';return body
    class Connection:
        def __init__(self,host,port,**kwargs):self.host=host;self.port=port
        def request(self,method,path):self.path=path;requests.append((self.host,path))
        def getresponse(self):return Reply(*responses[(self.host,self.path)])
        def close(self):pass
    monkeypatch.setattr(images.http.client,'HTTPConnection',Connection)
    assert images.image_bytes('http://short.test/start')[1]=='image/png'
    assert requests==[('short.test','/start'),('short.test','/next'),('cdn.test','/book.png')]
    assert connected==[('8.8.8.8',80),('8.8.8.8',80),('1.1.1.1',80)]


def test_remote_image_redirects_reject_private_targets_and_loops(monkeypatch):
    from serein.extensions import pipeline_images as images
    connected=[]
    def address(host,port):
        return [(2,1,6,'',(('127.0.0.1' if host=='private.test' else '8.8.8.8'),port))]
    monkeypatch.setattr(images.socket,'getaddrinfo',address)
    monkeypatch.setattr(images.socket,'create_connection',lambda target,**kwargs:connected.append(target))
    class Reply:
        status=302
        def __init__(self,location):self.location=location
        def getheader(self,name):return self.location
    locations=['http://private.test/image.png','/loop']
    class Connection:
        def __init__(self,host,port,**kwargs):self.host=host;self.port=port
        def request(self,*args):pass
        def getresponse(self):return Reply(locations.pop(0))
        def close(self):pass
    monkeypatch.setattr(images.http.client,'HTTPConnection',Connection)
    with pytest.raises(ValueError,match='公开图片地址'):
        images.image_bytes('http://short.test/private')
    with pytest.raises(ValueError,match='循环'):
        images.image_bytes('http://short.test/loop')
    assert connected==[('8.8.8.8',80),('8.8.8.8',80)]


def test_remote_image_redirect_limit_is_enforced(monkeypatch):
    from serein.extensions import pipeline_images as images
    requests=[]
    monkeypatch.setattr(images.socket,'getaddrinfo',lambda host,port:[(2,1,6,'',('8.8.8.8',port))])
    monkeypatch.setattr(images.socket,'create_connection',lambda *args,**kwargs:None)
    class Reply:
        status=302
        def __init__(self,location):self.location=location
        def getheader(self,name):return self.location
    class Connection:
        def __init__(self,host,port,**kwargs):pass
        def request(self,method,path):requests.append(path)
        def getresponse(self):return Reply(f'/hop-{len(requests)}')
        def close(self):pass
    monkeypatch.setattr(images.http.client,'HTTPConnection',Connection)
    with pytest.raises(ValueError,match='超过 3 次'):
        images.image_bytes('http://short.test/start')
    assert requests==['/start','/hop-1','/hop-2','/hop-3']
