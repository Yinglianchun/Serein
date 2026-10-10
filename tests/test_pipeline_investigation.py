"""Isolated regressions for frozen Event settlement and bounded reading."""
import asyncio
import json

from test_public_features import settings, ingest, output_for, synthetic_runner
from serein.core.store import Store, encode
from serein.compat.raw_archive import raw_archive
from serein.extensions import pipeline as p
from serein.extensions import pipeline_recovery as recovery
from serein.extensions import pipeline_latest as latest
from serein.deployment import save_settings


def test_deleted_predecessor_holds_batch_without_repeating_models(settings):
    ingest(settings)
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=synthetic_runner))['events']==1
    with Store(settings.database, read_only=True) as store:
        predecessor=store.conn.execute("SELECT item_id FROM fact_events WHERE status='active'").fetchone()[0]
    ingest(settings,2)
    calls=[]
    async def delete_during_writer(role, request):
        calls.append(role)
        if role=='event_writer':
            # Reproduce lifecycle drift after Curator froze its predecessor.
            with Store(settings.database) as store:
                store.conn.execute("UPDATE fact_events SET status='tombstoned' WHERE item_id=?",(predecessor,))
        return output_for(role,request)
    held=asyncio.run(p.advance(settings.database,include_recent=True,runner=delete_during_writer))
    assert held['status']=='needs_repair' and held['repair_kind']=='event_settlement'
    assert 'no_active_leaf' in held['reason']
    assert calls.count('event_writer')==1
    async def forbidden(*args):
        raise AssertionError('accepted model result must not be requested again')
    blocked=asyncio.run(p.advance(settings.database,include_recent=True,runner=forbidden))
    assert {key:blocked[key] for key in held}==held
    assert blocked['blocked_scopes'][0]['hold_status']=='needs_repair'
    assert blocked['blocked_scopes'][0]['batch_id']==held['batch_id']
    retried=asyncio.run(p.advance(settings.database,include_recent=True,runner=forbidden,retry_repair=True))
    assert retried['status']=='needs_repair' and 'no_active_leaf' in retried['reason']
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM fact_events').fetchone()[0]==1
        assert store.conn.execute('SELECT count(*) FROM raw_processing WHERE operation_id=?',(held['batch_id'],)).fetchone()[0]==0
        assert store.conn.execute('SELECT count(*) FROM pipeline_jobs WHERE batch_id=? AND output_json IS NULL',(held['batch_id'],)).fetchone()[0]==0
    rebuilt=asyncio.run(recovery.rebuild(settings.database,held['batch_id'],'REBUILD_PIPELINE_BATCH'))
    assert rebuilt['status']=='rebuilt'
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))['events']==1
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=forbidden))['status']=='current'


def test_context_query_limits_after_filtering_session(settings,monkeypatch):
    p.initialize(settings.database)
    entries=[{'source_event_id':str(i),'session_id':'own' if i<12 else 'foreign',
              'role':'user','text':f'Context {i}','created_at':'2025-01-01T00:00:00Z'} for i in range(32)]
    raw_archive(settings).ingest(entries,source='synthetic')
    with Store(settings.database) as store:
        ids=[row[0] for row in store.conn.execute('SELECT id FROM raw_events ORDER BY id')]
        for key in ids:
            store.conn.execute('INSERT INTO pipeline_routes VALUES (?,?)',(key,encode({'primary_track_id':'t','context_track_ids':[]})))
        message=p.task_message(store.conn.execute('SELECT * FROM raw_events WHERE id=?',(ids[11],)).fetchone())
    component={'context_messages':[message],'context_session_ids':[message['session_id']],'memberships':[]}
    original=p.task_message
    projected=[]
    def counted(row):
        projected.append(row['id'])
        return original(row)
    monkeypatch.setattr(p,'task_message',counted)
    result=p.extend_context(settings.database,component,{'before_message_id':ids[-1]+1,'track_id':'t'})
    assert result['context_receipt']['read_source_ids']==ids[6:12]
    assert len(projected)==6  # No full-history rows materialized in Python.


def test_large_writer_prompt_is_one_request_with_once_only_reading_sources():
    def message(key,text):
        return {'id':key,'content':text,'role':'user','created_at':'2025-01-01T00:00:00Z'}
    current=[message(100,'New synthetic activity')]
    bound=[message(i,f'Historical source {i}: '+'x'*12000) for i in range(40)]
    prompt=latest.build_event_writer_prompt('2025-01-01','',current,context_messages=[*bound,*current],include_role_rules=False)
    block=json.loads(prompt.split('<event_reading_block_json>\n',1)[1].split('\n</event_reading_block_json>',1)[0])
    assert len(block)==41 and len({row['source_message_id'] for row in block})==41
    assert sum(len(row['text']) for row in block if row['evidence_role']=='context_only')>480000
    assert prompt.count('New synthetic activity')==1
    assert len(prompt)>490000


def test_retry_progress_counts_the_actual_corrective_request(settings,monkeypatch):
    ingest(settings)
    save_settings(settings.database,{
        'models':[{'id':'synthetic','model':'synthetic','base_url':'http://127.0.0.1:9/v1'}],
        'assignments':{role:'synthetic' for role in p.ROLES},
        'pipeline':{'execution_mode':'api','max_prompt_chars':80000}})
    progress=[]
    monkeypatch.setattr('serein.work_tasks.progress',lambda **fields:progress.append(fields))
    calls=[]
    async def complete(model,payload):
        calls.append(sum(len(message['content']) for message in payload['messages']))
        if len(calls)==1:return {'choices':[{'message':{'content':'{'}}]}
        with Store(settings.database,read_only=True) as store:
            row=store.conn.execute('SELECT request_json FROM pipeline_jobs WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1').fetchone()
        request=json.loads(row[0])
        return {'choices':[{'message':{'content':json.dumps(output_for(request['role'],request))}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    assert asyncio.run(p.advance(settings.database,include_recent=True))['events']==1
    reported=[entry['prompt_chars'] for entry in progress if 'attempt' in entry and 'prompt_chars' in entry]
    assert reported==calls and calls[1]>calls[0]


def test_writer_omits_large_old_sources_and_upgrades_pending_frozen_request(settings):
    save_settings(settings.database,{'pipeline':{'max_prompt_chars':800000}})
    ingest(settings)
    old_text='OLD_SYNTHETIC_ATTACHMENT_EXPLANATION '+'x'*200000
    with Store(settings.database) as store:
        store.conn.execute("UPDATE raw_events SET text=? WHERE role='assistant'",(old_text,))
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))['events']==1
    ingest(settings,2)
    task=asyncio.run(p.advance(settings.database,include_recent=True))
    while task['role']!='event_writer':
        p.submit(settings.database,task['job_id'],output_for(task['role'],task['request']))
        task=asyncio.run(p.advance(settings.database,include_recent=True))
    assert len(task['request']['prompt'])<12000
    assert 'OLD_SYNTHETIC_ATTACHMENT_EXPLANATION' not in task['request']['prompt']
    assert 'We agreed to Book club plan 1' in task['request']['prompt']
    # A Writer task created before this change also needs a smaller prompt on resume.
    assert old_text not in json.dumps(task['request'])
    with Store(settings.database,read_only=True) as store:
        frozen=json.loads(store.conn.execute('SELECT request_json FROM pipeline_jobs WHERE id=?',(task['job_id'],)).fetchone()[0])
    frozen.pop('writer_previous_body_only')
    frozen['prompt']+='\n'+old_text
    frozen['component']['images']=[{'source_message_id':1,'position':1,'sha256':'a'*64}]
    frozen['component']['curator_image_transcriptions']=[{
        'source_message_id':1,'position':1,'sha256':'a'*64,'text':'OLD_SYNTHETIC_IMAGE_TRANSCRIPT'}]
    with Store(settings.database) as store:
        store.conn.execute('UPDATE pipeline_jobs SET request_json=? WHERE id=?',(encode(frozen),task['job_id']))
    called=[]
    async def writer_only(role,request):
        called.append(role)
        assert role=='event_writer'
        assert len(request['prompt'])<12000 and old_text not in request['prompt']
        assert request['curator_image_transcriptions']==[]
        assert 'OLD_SYNTHETIC_IMAGE_TRANSCRIPT' not in request['prompt']
        assert {m['id'] for m in request['messages']}=={3,4}
        return output_for(role,request)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=writer_only))['events']==1
    assert called==['event_writer']
    with Store(settings.database,read_only=True) as store:
        event=store.conn.execute("SELECT item_id FROM fact_events WHERE status='active'").fetchone()[0]
        assert store.conn.execute('SELECT count(*) FROM fact_event_sources WHERE item_id=?',(event,)).fetchone()[0]==4
