import asyncio
import json
import base64
import pytest

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


def pictures(count=3):
    raw = base64.b64decode(PNG.split(',', 1)[1])
    return ['data:image/png;base64,' + base64.b64encode(raw + bytes([i])).decode()
            for i in range(count)]


def test_chat_partial_failure_preserves_success_and_only_retries_missing(settings, monkeypatch):
    configure(settings, feature=True)
    urls = pictures()
    turn = prepare_turn('partial', [{'role': 'user', 'content': [
        {'type': 'image_url', 'image_url': {'url': url}} for url in urls]}])
    calls = []
    fail = True
    async def complete(model, payload):
        images = [part for part in payload['messages'][0]['content'] if part['type'] == 'image_url']
        assert len(images) == 1
        url = images[0]['image_url']['url']
        calls.append(url)
        if fail and url == urls[1]:
            raise RuntimeError('synthetic failure')
        return {'choices': [{'message': {'content': json.dumps({'image_transcriptions': [
            {'input_image': 1, 'text': 'page ' + str(urls.index(url)), 'unreadable': False}]})}}]}
    monkeypatch.setattr('serein.image_transcription.complete', complete)
    from fastapi import HTTPException
    from serein.api.chat import transcribe_image_turn
    from serein.chat_archive import archive_user_turn
    with pytest.raises(HTTPException):
        asyncio.run(transcribe_image_turn(settings, turn))
    message_id = archive_user_turn(settings, turn)['message_ids'][0]
    record = raw_archive(settings).get_event(message_id)['image_transcription']
    assert record['status'] == 'failed'
    assert [item['position'] for item in record['items']] == [1, 3]
    fail = False
    text, receipt = asyncio.run(transcribe_image_turn(settings, turn))
    assert receipt['status'] == 'complete' and calls == [*urls, urls[1]]
    assert text.index('page 0') < text.index('page 1') < text.index('page 2')
    assert raw_archive(settings).get_event(message_id)['image_transcription_status'] == 'complete'


def test_transcription_has_wall_clock_timeout(settings, monkeypatch):
    from serein.image_transcription import transcribe_images
    from serein.extensions.pipeline_images import freeze_images
    async def stall(*args, **kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr('serein.image_transcription.complete', stall)
    images = freeze_images([{'source_message_id': 1, 'position': 1, 'evidence_role': 'owned', 'url': PNG}])
    with pytest.raises(TimeoutError):
        asyncio.run(transcribe_images({}, images, timeout_seconds=.01))


def test_pipeline_partial_success_survives_resume(settings):
    configure(settings)
    urls = pictures()
    raw_archive(settings).ingest([
        {'source_event_id':'pages','session_id':'pages','role':'user','text':'Read these pages',
         'created_at':'2025-01-01T00:00:00Z', 'metadata':{'attachments':[
             {'kind':'image','url':url} for url in urls]}},
        {'source_event_id':'reply','session_id':'pages','role':'assistant','text':'Reading',
         'created_at':'2025-01-01T00:01:00Z'}], source='synthetic')
    seen = []
    fail = True
    async def runner(role, request):
        if request.get('transcription_only'):
            assert len(request['images']) == 1
            position = request['images'][0]['position']
            seen.append(position)
            if fail and position == 2:
                raise RuntimeError('synthetic failure')
            return {'image_transcriptions':[{'input_image':1,'text':f'page {position}','unreadable':False}]}
        if role in ('event_curator', 'event_writer'):
            assert request['images'] == []
            assert [item['position'] for item in request['curator_image_transcriptions']] == [1, 2, 3]
        return output_for(role, request)
    with pytest.raises(RuntimeError):
        asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))
    fail = False
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))['events'] == 1
    assert seen == [1, 2, 3, 2]


def test_waiting_for_transcription_agent_is_pending_not_failed(settings):
    configure(settings)
    raw_archive(settings).ingest([
        {'source_event_id':'picture','session_id':'agent','role':'user','text':'Read',
         'created_at':'2025-01-01T00:00:00Z','metadata':{'attachments':[{'kind':'image','url':PNG}]}},
        {'source_event_id':'reply','session_id':'agent','role':'assistant','text':'Reading',
         'created_at':'2025-01-01T00:01:00Z'}], source='synthetic')
    task = asyncio.run(p.advance(settings.database, include_recent=True))
    while task['role'] == 'track_router':
        p.submit(settings.database, task['job_id'], output_for(task['role'], task['request']))
        task = asyncio.run(p.advance(settings.database, include_recent=True))
    assert task['status'] == 'awaiting_agent' and task['request']['transcription_only']
    message_id = task['request']['images'][0]['source_message_id']
    assert raw_archive(settings).get_event(message_id)['image_transcription_status'] == 'pending'


def test_durable_image_queue_recovers_with_bounded_delayed_retry(settings, monkeypatch):
    import httpx
    from serein import work_tasks as tasks
    from serein.chat_archive import archive_user_turn
    configure(settings)
    save_settings(settings.database, {'features': {'image_transcription_async': True}})
    turn = prepare_turn('recovery', [{'role':'user','content':[{'type':'image_url','image_url':{'url':PNG}}]}])
    message_id = archive_user_turn(settings, turn)['message_ids'][0]
    queued = tasks.enqueue_image(settings.database, message_id)
    assert tasks.enqueue_image(settings.database, message_id)['run_id'] == queued['run_id']
    calls = []
    async def unavailable(*args):
        calls.append(True)
        raise httpx.ReadTimeout('synthetic timeout')
    monkeypatch.setattr('serein.api.chat.transcribe_archived_images', unavailable)
    for attempt in range(4):
        with pytest.raises(httpx.ReadTimeout):
            asyncio.run(tasks.execute(settings.database, queued['id'],
                lambda: tasks.work(settings, queued['id'], queued['arguments']), queued_id=queued['run_id']))
        tasks.recover_image_work(settings.database)
        queued = tasks.status(settings.database, queued['id'])
        assert queued['attempts'] == attempt + 1
        if attempt < 3:
            assert queued['status'] == 'queued'
            waiting = asyncio.run(tasks.execute(settings.database, queued['id'], unavailable, queued_id=queued['run_id']))
            assert waiting['status'] == 'waiting'
            stamp = queued['next_attempt_at'] + 1
            monkeypatch.setattr(tasks.time, 'time', lambda: stamp)
        else:
            assert queued['status'] == 'failed'
    assert len(calls) == 4


def test_historical_curator_text_is_reused_with_current_scope(settings):
    from serein.image_transcription import reusable_transcriptions
    from serein.extensions.pipeline_images import freeze_images
    from test_public_features import ingest, synthetic_runner
    ingest(settings)
    asyncio.run(p.advance(settings.database, include_recent=True, runner=synthetic_runner))
    with Store(settings.database) as store:
        message_id = store.conn.execute('SELECT id FROM raw_events ORDER BY id LIMIT 1').fetchone()[0]
        image = freeze_images([{'source_message_id':message_id,'position':1,'evidence_role':'context_only','url':PNG}])[0]
        detail = {'curator_image_transcriptions': [{
            **{key:image[key] for key in ('source_message_id','position','sha256')},
            'evidence_role':'owned','text':'historical page','unreadable':False}]}
        store.conn.execute('UPDATE pipeline_event_details SET details_json=?', (json.dumps(detail),))
    rows = reusable_transcriptions(settings, [], [image])
    assert rows[0]['text'] == 'historical page' and rows[0]['evidence_role'] == 'context_only'
    assert reusable_transcriptions(settings, [], [{**image, 'sha256':'changed'}]) == []


def test_image_queue_recovers_expired_lease_and_can_finish_after_restart(settings, monkeypatch):
    from serein import work_tasks as tasks
    configure(settings)
    save_settings(settings.database, {'features': {'image_transcription_async': True}})
    queued = tasks.enqueue_image(settings.database, 1)
    queued.update(status='running', attempts=1, lease_until=0)
    with Store(settings.database) as store:
        store.conn.execute('UPDATE background_state SET value_json=? WHERE name=?',
                           (json.dumps(queued), 'work:'+queued['id']))
    tasks.recover_image_work(settings.database)
    recovered = tasks.status(settings.database, queued['id'])
    assert recovered['status'] == 'queued' and recovered['run_id'] != queued['run_id']
    async def success(*args):
        return '', {'status':'complete','images':1}
    monkeypatch.setattr('serein.api.chat.transcribe_archived_images', success)
    asyncio.run(tasks.execute(settings.database, recovered['id'],
        lambda: tasks.work(settings, recovered['id'], recovered['arguments']), queued_id=recovered['run_id']))
    assert tasks.status(settings.database, recovered['id'])['status'] == 'completed'


def test_image_freezing_runs_outside_async_request_thread(settings, monkeypatch):
    import threading
    from serein.extensions import pipeline_images
    configure(settings, feature=True)
    main_thread = threading.get_ident()
    original = pipeline_images.freeze_images
    threads = []
    def freeze(*args, **kwargs):
        threads.append(threading.get_ident())
        return original(*args, **kwargs)
    async def complete(*args, **kwargs):
        return {'choices':[{'message':{'content':json.dumps({'image_transcriptions':[
            {'input_image':1,'text':'synthetic','unreadable':False}]})}}]}
    monkeypatch.setattr(pipeline_images, 'freeze_images', freeze)
    monkeypatch.setattr('serein.image_transcription.complete', complete)
    turn = prepare_turn('thread', [{'role':'user','content':[{'type':'image_url','image_url':{'url':PNG}}]}])
    asyncio.run(prepare_image_transcription(settings, turn, {'features':{'image_eyes':True}}))
    assert threads and all(thread != main_thread for thread in threads)


@pytest.mark.parametrize('mutation', ['text', 'attachments', 'legacy', 'sha'])
def test_archived_remote_cache_survives_expired_url(settings, monkeypatch, mutation):
    from serein.api.chat import transcribe_archived_images
    from serein.extensions import pipeline_images
    configure(settings)
    archive = raw_archive(settings)
    archive.ingest([{'source_event_id':'cached-remote','session_id':'cache', 'role':'user',
        'text':'Synthetic question','created_at':'2025-01-01T00:00:00Z',
        'metadata':{'attachments':[{'kind':'image','url':'https://example.com/expired.png'}]}}], source='synthetic')
    with Store(settings.database, read_only=True) as store:
        message_id = store.conn.execute('SELECT id FROM raw_events').fetchone()[0]
    downloads = []
    original_bytes = pipeline_images.image_bytes
    def image_bytes(url):
        if url.startswith('data:'):
            return original_bytes(url)
        downloads.append(url)
        if len(downloads) > 1:
            raise ValueError('Synthetic expired URL')
        return original_bytes(PNG)
    calls = []
    async def complete(*args, **kwargs):
        calls.append(1)
        return {'choices':[{'message':{'content':json.dumps({'image_transcriptions':[
            {'input_image':1,'text':'Cached remote transcription','unreadable':False}]})}}]}
    monkeypatch.setattr(pipeline_images, 'image_bytes', image_bytes)
    monkeypatch.setattr('serein.image_transcription.complete', complete)
    asyncio.run(transcribe_archived_images(settings, message_id))
    context, receipt = asyncio.run(transcribe_archived_images(settings, message_id))
    assert receipt['status'] == 'cached' and 'Cached remote transcription' in context
    assert len(downloads) == len(calls) == 1
    # Same URL cannot authorize reuse after the canonical source changes.
    with Store(settings.database) as store:
        if mutation == 'text':
            store.conn.execute("UPDATE raw_events SET text='Changed source' WHERE id=?", (message_id,))
        elif mutation == 'attachments':
            store.conn.execute("UPDATE raw_events SET metadata_json=? WHERE id=?",
                (json.dumps({'attachments':[{'kind':'image','url':'https://example.com/changed.png'}]}), message_id))
        else:
            record = archive.get_event(message_id)['image_transcription']
            if mutation == 'legacy':
                record['items'][0].pop('source_fingerprint')
            else:
                record['items'][0]['sha256'] = 'invalid'
            store.conn.execute('UPDATE raw_events SET image_transcription_json=? WHERE id=?',
                               (json.dumps(record), message_id))
    with pytest.raises(ValueError, match='expired URL'):
        asyncio.run(transcribe_archived_images(settings, message_id))
    assert len(downloads) == 2 and len(calls) == 1


def test_partial_cache_downloads_only_missing_sibling(settings, monkeypatch):
    from serein.api.chat import transcribe_archived_images
    from serein.extensions import pipeline_images
    configure(settings)
    archive = raw_archive(settings)
    archive.ingest([{'source_event_id':'partial-cache','session_id':'cache', 'role':'user',
        'text':'Synthetic question','created_at':'2025-01-01T00:00:00Z',
        'metadata':{'attachments':[{'kind':'image','url':'https://example.com/one.png'},
                                 {'kind':'image','url':'https://example.com/two.png'}]}}], source='synthetic')
    with Store(settings.database, read_only=True) as store:
        message_id = store.conn.execute('SELECT id FROM raw_events').fetchone()[0]
    downloads = []
    original_bytes = pipeline_images.image_bytes
    def image_bytes(url):
        if url.startswith('data:'):
            return original_bytes(url)
        downloads.append(url)
        assert downloads.count('https://example.com/one.png') <= 1
        return original_bytes(PNG)
    calls = []
    async def complete(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise ValueError('Synthetic sibling failure')
        return {'choices':[{'message':{'content':json.dumps({'image_transcriptions':[
            {'input_image':1,'text':'Synthetic image','unreadable':False}]})}}]}
    monkeypatch.setattr(pipeline_images, 'image_bytes', image_bytes)
    monkeypatch.setattr('serein.image_transcription.complete', complete)
    with pytest.raises(ValueError, match='sibling failure'):
        asyncio.run(transcribe_archived_images(settings, message_id))
    context, receipt = asyncio.run(transcribe_archived_images(settings, message_id))
    assert receipt['status'] == 'complete' and receipt['images'] == 2
    assert downloads == ['https://example.com/one.png', 'https://example.com/two.png', 'https://example.com/two.png']
    assert len(calls) == 3
