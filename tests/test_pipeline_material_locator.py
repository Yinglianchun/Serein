"""Locate retained originals without altering evidence or issuing model calls."""
import asyncio

from fastapi.testclient import TestClient

from serein.api.http import create_app
from serein.core.store import Store
from serein.extensions import pipeline as p
from test_public_features import settings
from test_pipeline_upgrade_backlog import legacy_backlog
from test_pipeline_router_retry_report import configure, transport, invalid_response


def test_upgraded_batch_can_be_identified_without_loading_or_reprocessing_full_originals(settings, monkeypatch):
    batch_id=legacy_backlog(settings,failed=True)
    with Store(settings.database) as store:
        # Reproduce the existing table layout before any upgraded worker starts.
        store.conn.execute('ALTER TABLE pipeline_attempts DROP COLUMN model')
        store.conn.execute('ALTER TABLE pipeline_attempts DROP COLUMN prompt_chars')
        job=store.conn.execute('SELECT id FROM pipeline_jobs WHERE batch_id=? ORDER BY rowid DESC LIMIT 1',(batch_id,)).fetchone()[0]
        store.conn.execute("INSERT INTO pipeline_attempts(job_id,attempt,created_at,output_text,error) VALUES (?,1,'2025-01-01','','Upstream returned HTTP 400')",(job,))
    def forbidden(*args,**kwargs):raise AssertionError('Reading a locator must not invoke a model')
    monkeypatch.setattr('serein.model_runtime.complete',forbidden)
    client=TestClient(create_app(settings,token='synthetic',live=True),headers={'Authorization':'Bearer synthetic'})
    result=client.get('/v1/pipeline/status')
    assert result.status_code==200
    item=next(item for item in result.json()['pending_materials'] if item['batch_id']==batch_id)
    locator=item['locator']
    assert item['last_attempt']['model'] is None and item['last_attempt']['prompt_chars'] is None
    assert item['last_attempt']['error']=='Upstream returned HTTP 400'
    assert item['can_skip'] is True and item['original_chars']==49870
    assert locator['source']=='synthetic' and locator['session_id']=='upgrade-backlog'
    assert locator['timestamp_source']=='import_time'
    assert (locator['first_raw_id'],locator['last_raw_id'])==(1,10)
    assert len(locator['preview'])==160 and 'messages' not in item
    assert locator['longest_message']['chars']==4987
    detail=client.get('/v1/pipeline/pending/'+str(item['id'])).json()
    assert detail['batch_id']==batch_id and detail['locator']==locator
    assert len(detail['messages'])==10 and detail['original_chars']==49870
    assert sum(len(m['content']) for m in detail['messages'])==49870
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0


def test_http400_batch_exposes_longest_original_and_can_be_skipped_without_deletion(settings, monkeypatch):
    configure(settings)
    calls=[]
    def reply(request,frozen):
        calls.append(frozen['role'])
        return invalid_response(request,frozen,'http400')
    transport(settings,monkeypatch,reply)
    result=asyncio.run(p.advance(settings.database,include_recent=True))
    assert result['status']=='retained'
    client=TestClient(create_app(settings,token='synthetic',live=True),headers={'Authorization':'Bearer synthetic'})
    status=client.get('/v1/pipeline/status').json()
    item=status['pending_materials'][0]
    attempt,=status['attempts']
    assert attempt['batch_id']==item['batch_id'] and attempt['id']==item['last_attempt']['id']
    assert attempt['prompt_chars']==item['last_attempt']['prompt_chars'] and attempt['model']=='synthetic'
    detail=client.get('/v1/pipeline/pending/'+str(item['id'])).json()
    assert detail['original_chars']==88420
    assert detail['locator']['longest_message']['chars']==44210
    assert detail['last_error']['output_text']=='' and '上游拒绝了归线请求（HTTP 400）' in detail['last_error']['error']
    assert item['last_attempt']['attempt']==1 and item['last_attempt']['model']=='synthetic'
    assert item['last_attempt']['prompt_chars']>item['original_chars']
    preview=client.post('/v1/pipeline/recovery-preview',json={'batch_id':item['batch_id']}).json()
    assert preview['can_rebatch'] is False and preview['originals']==2
    response=client.post('/v1/pipeline/skip-batch',json={'batch_id':item['batch_id'],'confirm':'SKIP_PIPELINE_BATCH'})
    assert response.status_code==200,response.text
    assert response.json()['skipped_originals']==2
    assert calls==['track_router']
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT sum(length(text)) FROM raw_events').fetchone()[0]==88420
        assert store.conn.execute('SELECT count(*) FROM fact_events').fetchone()[0]==0
        assert store.conn.execute("SELECT count(*) FROM raw_processing WHERE outcome='manual_skip'").fetchone()[0]==2
        assert store.conn.execute('SELECT status FROM pipeline_pending_materials WHERE id=?',(item['id'],)).fetchone()[0]=='resolved_skipped'
