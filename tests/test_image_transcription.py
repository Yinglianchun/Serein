import asyncio
import json

from serein.api.chat import prepare_image_transcription
from serein.chat_archive import prepare_turn
from serein.compat.raw_archive import raw_archive
from serein.core.store import Store
from serein.deployment import save_settings
from serein.extensions import pipeline as p

from test_event_handoff import PNG
from test_public_features import output_for, settings
from test_public_settings import deployment


def test_curator_reread_adds_transcribed_context_before_text_only_writer(settings,monkeypatch):
    from test_public_features import ingest
    configure(settings)
    ingest(settings)
    seen=[];context_ids=[]
    def reread(database,component,query):
        import copy
        reading=copy.deepcopy(component)
        raw_archive(settings).ingest([{'source_event_id':'context-image','session_id':'earlier',
            'role':'user','text':'Earlier picture','created_at':'2024-12-31T00:00:00Z',
            'metadata':{'attachments':[{'kind':'image','url':PNG}]}}],source='synthetic-context')
        with Store(database,read_only=True) as store:
            context_id=store.conn.execute("SELECT id FROM raw_events WHERE source_event_id='context-image'").fetchone()[0]
        context_ids.append(context_id)
        reading['context_messages'].append({**reading['messages'][0],'id':context_id,'content':'Earlier picture',
            'metadata':{'attachments':[{'kind':'image','url':PNG}]}})
        return reading
    monkeypatch.setattr(p,'extend_context',reread)
    async def runner(role,request):
        if request.get('transcription_only'):
            seen.append('transcription')
            return {'image_transcriptions':[{'input_image':1,'text':'[画面] A lamp on a desk.','unreadable':False}]}
        if role=='event_curator' and not request.get('context_read'):
            return {'context_request':{'track_id':request['component']['track_ids'][0],
                'before_message_id':request['component']['messages'][0]['id'],'reason':'missing_subject'}}
        if role=='event_curator':
            assert request['images']==[] and 'A lamp on a desk' in request['prompt']
        if role=='event_writer':
            seen.append('writer')
            assert request['images']==[] and 'A lamp on a desk' in request['prompt']
            assert request['curator_image_transcriptions'][0]['evidence_role']=='context_only'
            assert context_ids[0] not in request['event']['source_message_ids']
        return output_for(role,request)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))['events']==1
    assert seen==['transcription','writer']


def configure(settings, *, feature=False):
    changes = {
        'models': [{'id':'vision','model':'agnes-3.0-flash','base_url':'http://127.0.0.1:9/v1'}],
        'assignments': {'image_transcription':'vision'},
    }
    if feature:
        changes['features'] = {'image_eyes':True}
    save_settings(settings.database, changes)


def test_chat_transcription_is_byte_bound_persisted_and_reused(settings, monkeypatch):
    configure(settings, feature=True)
    calls=[]
    async def complete(model, payload):
        calls.append(payload)
        assert 'max_tokens' not in payload and 'max_completion_tokens' not in payload and 'max_output_tokens' not in payload
        return {'choices':[{'message':{'content':json.dumps({'image_transcriptions':[
            {'input_image':1,'text':'Visible title','unreadable':False}]})}}]}
    monkeypatch.setattr('serein.image_transcription.complete', complete)
    turn=prepare_turn('window',[{'role':'user','content':[{'type':'text','text':'read this'},
        {'type':'image_url','image_url':{'url':PNG}}]}])
    state={'features':{'image_eyes':True}}
    context,receipt=asyncio.run(prepare_image_transcription(settings,turn,state))
    assert 'Visible title' in context and receipt['status']=='complete' and len(calls)==1
    row=raw_archive(settings).get_event(receipt['message_id'])
    assert row['image_transcription_status']=='complete'
    assert row['image_transcription']['items'][0]['text']=='Visible title'
    context,receipt=asyncio.run(prepare_image_transcription(settings,turn,state))
    assert 'Visible title' in context and receipt['status']=='cached' and len(calls)==1


def test_eyes_injects_transcription_and_removes_pixels_from_chat(deployment, monkeypatch):
    settings, client = deployment
    from test_public_settings import configure as configure_chat
    configure_chat(client).raise_for_status()
    client.patch('/v1/settings', json={'assignments':{'image_transcription':'model-a'},
        'features':{'image_eyes':True}}).raise_for_status()
    async def transcribe(model, payload):
        return {'choices':[{'message':{'content':json.dumps({'image_transcriptions':[
            {'input_image':1,'text':'Visible title','unreadable':False}]})}}]}
    forwarded=[]
    async def chat(model, payload, **options):
        forwarded.append(payload)
        return {'choices':[{'message':{'role':'assistant','content':'I can read it now'}}]}
    monkeypatch.setattr('serein.image_transcription.complete', transcribe)
    monkeypatch.setattr('serein.api.chat.complete', chat)
    response=client.post('/v1/chat/completions',json={'messages':[{'role':'user','content':[
        {'type':'text','text':'What is in this picture?<attachment type="message_insert_extra_bundle">【当前天气】晴</attachment>'},
        {'type':'image_url','image_url':{'url':PNG}}]}],
        'serein':{'memory':False,'window_id':'eyes'}})
    assert response.status_code==200,response.text
    encoded=json.dumps(forwarded[0]['messages'],ensure_ascii=False)
    assert 'Visible title' in encoded and 'image_url' not in encoded and PNG not in encoded
    assert 'message_insert_extra_bundle' not in encoded and '当前天气' in encoded
    with Store(settings.database,read_only=True) as store:
        message_id=store.conn.execute("SELECT id FROM raw_events WHERE session_id='eyes' AND role='user'").fetchone()[0]
    user=raw_archive(settings).get_event(message_id)
    assert user['metadata']['attachments'][0]['url']==PNG
    assert user['image_transcription']['items'][0]['text']=='Visible title'


def test_async_transcription_keeps_pixels_out_of_injected_context(deployment, monkeypatch):
    settings, client = deployment
    from test_public_settings import configure as configure_chat
    configure_chat(client).raise_for_status()
    client.patch('/v1/settings', json={'assignments':{'image_transcription':'model-a'},
        'features':{'image_transcription_async':True}}).raise_for_status()
    async def transcribe(model, payload):
        return {'choices':[{'message':{'content':json.dumps({'image_transcriptions':[
            {'input_image':1,'text':'Archived visible title','unreadable':False}]})}}]}
    forwarded=[]
    async def chat(model, payload, **options):
        forwarded.append(payload)
        return {'choices':[{'message':{'role':'assistant','content':'Vision reply'}}]}
    monkeypatch.setattr('serein.image_transcription.complete', transcribe)
    monkeypatch.setattr('serein.api.chat.complete', chat)
    response=client.post('/v1/chat/completions',json={'messages':[{'role':'user','content':[
        {'type':'text','text':'Read directly'},{'type':'image_url','image_url':{'url':PNG}}]}],
        'serein':{'memory':False,'window_id':'async-images'}})
    assert response.status_code==200,response.text
    encoded=json.dumps(forwarded[0]['messages'],ensure_ascii=False)
    assert 'image_url' in encoded and PNG in encoded and 'Archived visible title' not in encoded
    with Store(settings.database,read_only=True) as store:
        message_id=store.conn.execute("SELECT id FROM raw_events WHERE session_id='async-images' AND role='user'").fetchone()[0]
    user=raw_archive(settings).get_event(message_id)
    assert user['image_transcription']['items'][0]['text']=='Archived visible title'


def test_async_transcription_failure_does_not_block_chat(deployment, monkeypatch):
    settings, client = deployment
    from test_public_settings import configure as configure_chat
    configure_chat(client).raise_for_status()
    client.patch('/v1/settings', json={'assignments':{'image_transcription':'model-a'},
        'features':{'image_transcription_async':True}}).raise_for_status()
    async def fail(*args, **kwargs):
        raise RuntimeError('synthetic image failure')
    async def chat(*args, **kwargs):
        return {'choices':[{'message':{'role':'assistant','content':'Reply still succeeds'}}]}
    monkeypatch.setattr('serein.image_transcription.complete', fail)
    monkeypatch.setattr('serein.api.chat.complete', chat)
    response=client.post('/v1/chat/completions',json={'messages':[{'role':'user','content':[
        {'type':'text','text':'Do not wait for the archive'},{'type':'image_url','image_url':{'url':PNG}}]}],
        'serein':{'memory':False,'window_id':'async-failure'}})
    assert response.status_code==200 and response.json()['choices'][0]['message']['content']=='Reply still succeeds'
    with Store(settings.database,read_only=True) as store:
        row=store.conn.execute("SELECT image_transcription_status,image_transcription_json FROM raw_events "
            "WHERE session_id='async-failure' AND role='user'").fetchone()
    assert row['image_transcription_status']=='failed' and 'RuntimeError' in row['image_transcription_json']


def test_pipeline_uses_separate_image_model_and_persists_transcription(settings, monkeypatch):
    configure(settings)
    save_settings(settings.database, {
        'assignments': {'track_router':'vision','image_transcription':'vision',
                        'event_curator':'vision','event_writer':'vision'},
        'pipeline': {'execution_mode':'api'},
    })
    raw_archive(settings).ingest([
        {'source_event_id':'image','session_id':'books','role':'user','text':'Read the title',
         'created_at':'2025-01-01T00:00:00Z','metadata':{'attachments':[{'kind':'image','url':PNG}]}},
        {'source_event_id':'reply','session_id':'books','role':'assistant','text':'I will read it',
         'created_at':'2025-01-01T00:01:00Z'}],source='test')
    seen=[]
    async def complete(model,payload):
        with Store(settings.database,read_only=True) as store:
            request=json.loads(store.conn.execute(
                'SELECT request_json FROM pipeline_jobs WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        seen.append(request['execution']['task'])
        if request['role']=='event_writer':
            assert request['images']==[] and request['image_input_mode']=='transcriptions_only'
            assert isinstance(payload['messages'][1]['content'],str)
            assert 'Visible title' in payload['messages'][1]['content']
        if request.get('transcription_only'):
            output={'image_transcriptions':[{'input_image':index,'text':'Visible title','unreadable':False}
                                             for index,_ in enumerate(request['images'],1)]}
        else:
            output=output_for(request['role'],request)
        return {'choices':[{'message':{'content':json.dumps(output)}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    result=asyncio.run(p.advance(settings.database,include_recent=True))
    assert result['events']==1
    assert seen[:4]==['track_router','image_transcription','event_curator','event_writer']
    with Store(settings.database,read_only=True) as store:
        rows=store.conn.execute("SELECT image_transcription_status,image_transcription_json FROM raw_events "
                                "WHERE image_transcription_status='complete'").fetchall()
    assert rows and 'Visible title' in rows[0]['image_transcription_json']
