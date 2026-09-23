import asyncio
import copy
import json

from test_public_features import settings, output_for, synthetic_runner
from serein.compat.raw_archive import raw_archive
from serein.core.store import Store, encode
from serein.extensions import pipeline as p
from serein.extensions.pipeline_images import hydrate_request_images


PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ'


def image_rows():
    return [
        {'source_event_id':'u1','session_id':'images','role':'user','text':'Please read this image.',
         'created_at':'2025-01-01T00:00:00Z','metadata':{'attachments':[
             {'id':'image-1','kind':'image','mime_type':'image/png','name':'one.png','url':PNG}]}},
        {'source_event_id':'a1','session_id':'images','role':'assistant','text':'I will read it.',
         'created_at':'2025-01-01T00:01:00Z'},
    ]


def test_task_projection_drops_duplicate_columns_and_inline_media():
    raw={'id':1,'source':'test','source_event_id':'u1','event_hash':'hash','role':'user','text':'Image',
         'created_at':'2025-01-01T00:00:00Z','ingested_at':'2025-01-01T00:00:00Z',
         'conversation_id':'c','session_id':'s','client':'web',
         'metadata_json':encode({'attachments':[{'id':'one','kind':'image','mime_type':'image/png','url':PNG}]}),
         'image_transcription_status':'complete','image_transcription_json':encode({'status':'complete'}),
         'image_transcription_updated_at':'2025-01-01T00:02:00Z'}
    full=p.message(raw)
    assert full['metadata']['attachments'][0]['url']==PNG
    assert full['content']=='Image' and full['image_transcription']=={'status':'complete'}
    assert not {'metadata_json','text','event_hash','ingested_at','conversation_id','client','image_transcription_json'}&set(full)
    task=p.task_message(raw)
    assert task['metadata']['attachments'][0]=={
        'id':'one','kind':'image','mime_type':'image/png','url':'[task image source]'}
    assert 'data:image/' not in encode(task)


def test_legacy_pending_job_keeps_its_inline_frozen_image(settings):
    request={'images':[{'source_message_id':1,'position':0,'sha256':'legacy','url':PNG}]}
    assert hydrate_request_images(settings.database,'legacy',request)['images'][0]['url']==PNG


def test_component_stores_bound_original_once_without_candidate_copy(settings):
    from test_public_features import ingest
    ingest(settings)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))['events']==1
    ingest(settings,2)
    batch=p.new_batch(settings.database,True);data=json.loads(batch['input_json'])
    routed=asyncio.run(p.route_batch(settings.database,batch,data,synthetic_runner))
    component=p.components(settings.database,data,routed)[0]
    assert component['base_event_candidates']
    assert all('originals' not in item for item in component['base_event_candidates'])
    ids=[item['id'] for item in component['context_messages']]
    assert len(ids)==len(set(ids))


def test_inline_image_bytes_exist_once_only_in_live_model_job(settings):
    raw_archive(settings).ingest(image_rows(),source='test')
    captured=[]
    async def runner(role,request):
        captured.append(copy.deepcopy(request))
        if role=='event_curator' and not request.get('transcription_only'):
            with Store(settings.database,read_only=True) as store:
                saved=store.conn.execute("SELECT request_json FROM pipeline_jobs WHERE role LIKE 'event_curator:%'").fetchone()[0]
                assert 'data:image/' not in saved
                assert store.conn.execute('SELECT count(*) FROM pipeline_media').fetchone()[0]==1
        return output_for(role,request)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))
    assert result['events']==1
    curator=next(request for request in captured if request['role']=='event_curator' and not request.get('transcription_only'))
    assert encode(curator).count(PNG)==1
    assert curator['images'][0]['url']==PNG
    assert 'url' not in curator['component']['images'][0]
    assert 'data:image/' not in encode(curator['component'])
    with Store(settings.database,read_only=True) as store:
        batch=store.conn.execute("SELECT input_json,result_json FROM pipeline_batches WHERE status='done'").fetchone()
        assert json.loads(batch['result_json'])['task_snapshot_compacted'] is True
        saved=json.loads(batch['input_json'])
        assert saved['task_snapshot_compacted'] is True and 'components' not in saved
        assert 'data:image/' not in batch['input_json']
        downstream=[json.loads(row[0]) for row in store.conn.execute(
            "SELECT request_json FROM pipeline_jobs WHERE role NOT LIKE 'track_router%'")]
        assert downstream and all(item['task_snapshot_compacted'] is True for item in downstream)
        assert all('prompt' not in item and 'component' not in item for item in downstream)
        assert store.conn.execute('SELECT count(*) FROM pipeline_media').fetchone()[0]==0


def test_initialize_compacts_historical_done_snapshots_but_keeps_router_proof(settings):
    p.initialize(settings.database)
    message={'id':1,'source':'test','source_event_id':'u1','original_session_id':'s','session_id':1,
             'role':'user','content':'hello','created_at':'2025-01-01T00:00:00Z',
             'metadata':{'attachments':[{'kind':'image','url':PNG}]}}
    data={'contract':p.CONTRACT,'scope':'scope','source':'test','day':'2025-01-01',
          'routing_messages':[message],'components':[{'context_messages':[message],'images':[{'url':PNG}]}],
          'routing_result':{'_public_normalized':True,'assignments':[],'tracks':[],
                            'track_state_updates':[],'next_track_ordinal':1}}
    router={'role':'track_router','batch_id':'old','contract':p.CONTRACT,'messages':[message],
            'active_tracks':[],'next_track_ordinal':1,'identity':{},'execution':{},'rules':'rules','prompt':'prompt'}
    curator={'role':'event_curator','batch_id':'old','contract':p.CONTRACT,'component':data['components'][0],
             'identity':{},'execution':{},'rules':'rules','prompt':'prompt '+PNG,'images':[{'url':PNG}]}
    with Store(settings.database) as store:
        store.conn.execute("INSERT INTO pipeline_batches(id,scope,input_json,status,result_json) VALUES (?,?,?,'done',?)",
                           ('old','scope',encode(data),encode({'status':'processed','completed_at':'2025-01-01T00:00:00Z'})))
        store.conn.execute('INSERT INTO pipeline_jobs(id,batch_id,role,request_json,output_json) VALUES (?,?,?,?,?)',
                           ('old:r','old','track_router:0',encode(router),encode({'ok':True})))
        store.conn.execute('INSERT INTO pipeline_jobs(id,batch_id,role,request_json,output_json) VALUES (?,?,?,?,?)',
                           ('old:c','old','event_curator:0',encode(curator),encode({'ok':True})))
    p.initialize(settings.database)
    with Store(settings.database,read_only=True) as store:
        batch=store.conn.execute('SELECT input_json,result_json FROM pipeline_batches WHERE id=?',('old',)).fetchone()
        compact=json.loads(batch['input_json'])
        assert compact['routing_messages'][0]['content']=='hello' and 'components' not in compact
        assert 'data:image/' not in batch['input_json']
        assert json.loads(batch['result_json'])['task_snapshot_compacted'] is True
        router_saved=json.loads(store.conn.execute('SELECT request_json FROM pipeline_jobs WHERE id=?',('old:r',)).fetchone()[0])
        curator_saved=json.loads(store.conn.execute('SELECT request_json FROM pipeline_jobs WHERE id=?',('old:c',)).fetchone()[0])
        assert router_saved['messages'][0]['content']=='hello' and 'data:image/' not in encode(router_saved)
        assert curator_saved['task_snapshot_compacted'] is True and len(curator_saved['request_sha256'])==64
