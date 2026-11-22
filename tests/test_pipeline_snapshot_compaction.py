import asyncio
import copy
import json

import pytest

from test_public_features import settings, ingest, output_for, synthetic_runner
from serein.compat.raw_archive import raw_archive
from serein.core.store import Store, digest, encode
from serein.extensions import pipeline as p, pipeline_recovery as recovery
from serein.extensions.pipeline_images import compact_batch_snapshot, hydrate_request_images


PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ'

REPLAY_KEYS = ('tracks', 'recent', 'next_track_ordinal', 'input_policy')


def legacy_compact(data):
    return {key:value for key,value in compact_batch_snapshot(data).items() if key not in REPLAY_KEYS}


@pytest.mark.parametrize('legacy', [False, True])
def test_completed_batch_with_prior_state_remains_replayable(settings, legacy):
    ingest(settings)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))['events']==1
    ingest(settings,2)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))
    assert result['events']==1
    with Store(settings.database) as store:
        batch=dict(store.conn.execute('SELECT * FROM pipeline_batches WHERE id=?',(result['batch_id'],)).fetchone())
        data=json.loads(batch['input_json'])
        if legacy:
            data=legacy_compact(data)
            batch['input_json']=encode(data)
            store.conn.execute('UPDATE pipeline_batches SET input_json=? WHERE id=?',(batch['input_json'],batch['id']))
        jobs=[tuple(row) for row in store.conn.execute('SELECT * FROM pipeline_jobs ORDER BY rowid')]
    frames=recovery._frames(settings.database,batch)
    assert frames[0]['assignments']==data['routing_result']['assignments']
    assert frames[0]['tracks']==data['routing_result']['tracks']
    assert frames[0]['next_track_ordinal']==data['routing_result']['next_track_ordinal']
    recovered=p.cached_route_result(settings.database,data)
    assert recovered['assignments']==frames[0]['assignments']
    with Store(settings.database,read_only=True) as store:
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM pipeline_jobs ORDER BY rowid')]==jobs
        assert store.conn.execute('SELECT input_json FROM pipeline_batches WHERE id=?',(batch['id'],)).fetchone()[0]==batch['input_json']


def test_compaction_retains_complete_replay_seed_and_policy():
    data={'tracks':[{'track_id':'earlier'}],'recent':[{'id':1}],
          'next_track_ordinal':7,'input_policy':{'track_candidates_enabled':True},
          'components':[{'large':'completed material'}],'messages':[{'id':2}]}
    compact=compact_batch_snapshot(data)
    assert {key:compact[key] for key in REPLAY_KEYS}=={key:data[key] for key in REPLAY_KEYS}
    assert 'components' not in compact and 'messages' not in compact


def completed_history(settings,monkeypatch,legacy,*,interleaved=False):
    from test_pipeline_route_history import freeze, history
    from serein.extensions import pipeline_track_candidates as candidates
    ingest(settings)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))['events']==1
    for number in (2,3,4):
        if interleaved and number==3:
            raw_archive(settings).ingest([{'source_event_id':'gap','session_id':'other','role':'user',
                'text':'Earlier independent activity','created_at':'2025-01-02T02:00:00Z'}],source='test')
        ingest(settings,number)
    consumer,data=freeze(settings)
    with Store(settings.database,read_only=True) as store:
        data['routing_messages']=[p.task_message(row) for row in store.conn.execute("SELECT * FROM raw_events WHERE id>2 AND session_id='books' ORDER BY id")]
    extra=copy.deepcopy(data['tracks'][0]);extra['track_id']='session_'+data['scope']+'_track_0009'
    data['tracks'].append(extra);data['next_track_ordinal']=10
    data['input_policy']['track_candidates_enabled']=True
    def alternating(cards,messages,recent,policy):
        if not policy.get('track_candidates_enabled'):return list(cards)
        return [cards[1 if messages[0]['id']==(6 if interleaved else 5) else 0]]
    monkeypatch.setattr(candidates,'select',alternating)
    routed=history(settings,data)
    with Store(settings.database) as store:
        batch=dict(store.conn.execute("SELECT * FROM pipeline_batches WHERE id='route:synthetic'").fetchone())
        compact=(legacy_compact if legacy else compact_batch_snapshot)(json.loads(batch['input_json']))
        batch.update(input_json=encode(compact),status='done')
        store.conn.execute("UPDATE pipeline_batches SET input_json=?,status='done',result_json=? WHERE id=?",
                           (batch['input_json'],encode({'task_snapshot_compacted':True}),batch['id']))
    return consumer,batch,routed


def test_legacy_candidate_first_seen_in_second_frame_requires_its_gap_receipt(settings,monkeypatch):
    _,batch,_=completed_history(settings,monkeypatch,True,interleaved=True)
    with Store(settings.database) as store:
        job=store.conn.execute("SELECT * FROM pipeline_jobs WHERE batch_id=? AND role='track_router:1'",(batch['id'],)).fetchone()
        request=json.loads(job['request_json'])
        card=request['active_tracks'][0]
        raw=p.task_message(store.conn.execute('SELECT * FROM raw_events WHERE id=5').fetchone())
        card['recent_source_message_ids']=[raw['id']]
        with p.latest.identity_scope(request['identity']):
            card['recent_turns']=p.latest.transcript_payload([raw])
        request['routing_frame']['pre_tracks_sha256']=digest(encode(request['active_tracks']))
        store.conn.execute('UPDATE pipeline_jobs SET request_json=? WHERE id=?',(encode(request),job['id']))
        receipt={'source_message_id':5,'primary_track_id':card['track_id'],'context_track_ids':[],'routing_role':'origin'}
        store.conn.execute('INSERT INTO pipeline_routes VALUES (?,?)',(5,encode(receipt)))
    assert [f['messages'][0]['id'] for f in recovery._frames(settings.database,batch)]==[3,6,8]
    with Store(settings.database) as store:
        store.conn.execute('DELETE FROM pipeline_routes WHERE raw_id=5')
    with pytest.raises(p.RoutingRecoveryError,match='Unproved Track anchor'):
        recovery._frames(settings.database,batch)


@pytest.mark.parametrize('legacy',[False,True])
def test_completed_multiframe_candidates_reuse_routes_without_scope_hold(settings,monkeypatch,legacy):
    consumer,batch,routed=completed_history(settings,monkeypatch,legacy)
    frames=recovery._frames(settings.database,batch)
    assert [frame['messages'][0]['id'] for frame in frames]==[3,5,7]
    assert [a for frame in frames for a in frame['assignments']]==routed['assignments']
    assert frames[-1]['tracks'][0]==routed['tracks'][0]
    calls=[]
    async def runner(role,request):
        calls.append(role)
        assert role!='track_router'
        return output_for(role,request)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))
    assert result['status']=='processed' and result['batch_id']==consumer['id']
    assert result['processed_originals']==2 and p.scope_holds(settings.database)==[]
    assert calls and set(calls)=={'event_curator','event_writer'}


@pytest.mark.parametrize('upgraded',[False,True])
@pytest.mark.parametrize('damage',['card','ordinal','recent','hash','source','future_card','missing_proof'])
def test_compacted_replay_still_rejects_damaged_frames(settings,monkeypatch,damage,upgraded):
    _,batch,_=completed_history(settings,monkeypatch,not upgraded)
    if upgraded:
        data=json.loads(batch['input_json']);data['runtime_revision']='synthetic-before-upgrade'
        batch['input_json']=encode(data)
    with Store(settings.database) as store:
        jobs=store.conn.execute("SELECT * FROM pipeline_jobs WHERE batch_id=? ORDER BY rowid",(batch['id'],)).fetchall()
        job=jobs[1 if damage=='future_card' else 2]
        request=json.loads(job['request_json'])
        proof=request['routing_frame']
        if damage=='card':
            request['active_tracks'][0]['throughline']='wrong predecessor'
            proof['pre_tracks_sha256']=digest(encode(request['active_tracks']))
        elif damage=='ordinal':
            request['next_track_ordinal']+=1;proof['next_track_ordinal']=request['next_track_ordinal']
        elif damage=='recent':
            request['recent_context']=[];proof['recent_context_sha256']=digest(encode([]))
        elif damage=='hash':proof['messages_sha256']='wrong hash'
        elif damage=='source':
            store.conn.execute("UPDATE raw_events SET event_hash='changed' WHERE id=?",(request['messages'][0]['id'],))
        elif damage=='future_card':
            request['active_tracks'][0]['recent_source_message_ids']=[4]
            proof['pre_tracks_sha256']=digest(encode(request['active_tracks']))
        else:request.pop('routing_frame')
        store.conn.execute('UPDATE pipeline_jobs SET request_json=? WHERE id=?',(encode(request),job['id']))
    with pytest.raises(p.RoutingRecoveryError):recovery._frames(settings.database,batch)


@pytest.mark.parametrize('revision',[*sorted(recovery.COMPACTED_ROUTER_REVISIONS),'unsupported'])
def test_legacy_compacted_runtime_compatibility_is_limited_to_known_completed_producers(settings,monkeypatch,revision):
    _,batch,_=completed_history(settings,monkeypatch,True)
    data=json.loads(batch['input_json']);data['runtime_revision']=revision;batch['input_json']=encode(data)
    if revision=='unsupported':
        with pytest.raises(p.RoutingRecoveryError,match='incompatible frozen contract'):
            recovery._frames(settings.database,batch)
    else:
        assert len(recovery._frames(settings.database,batch))==3
        batch['status']='routing_only'
        with pytest.raises(p.RoutingRecoveryError,match='incompatible frozen contract'):
            recovery._frames(settings.database,batch)


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
        if request.get('transcription_only'):
            return {'image_transcriptions':[{'input_image':1,'text':'Visible image','unreadable':False}]}
        if role=='event_curator' and not request.get('transcription_only'):
            with Store(settings.database,read_only=True) as store:
                saved=store.conn.execute("SELECT request_json FROM pipeline_jobs WHERE role LIKE 'event_curator:%'").fetchone()[0]
                assert 'data:image/' not in saved
                assert store.conn.execute('SELECT count(*) FROM pipeline_media').fetchone()[0]==1
        return output_for(role,request)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))
    assert result['events']==1
    curator=next(request for request in captured if request['role']=='event_curator' and not request.get('transcription_only'))
    transcription=next(request for request in captured if request.get('transcription_only'))
    assert encode(transcription).count(PNG)==1
    assert transcription['images'][0]['url']==PNG
    assert 'data:image/' not in encode(curator) and curator['images']==[]
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
