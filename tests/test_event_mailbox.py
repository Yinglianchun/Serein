import json
import concurrent.futures
import pytest
from fastapi.testclient import TestClient
from serein.application import Application
from serein.config import Settings
from serein.core.store import Store, Conflict, promoted_scene_id
from serein.core.event_mailbox import EventMailbox
from serein.deployment import save_settings
from serein.extensions.optional import tools_for


@pytest.fixture
def setup(tmp_path):
    database=tmp_path/'mailbox.db'
    with Store(database):pass
    settings=Settings(database,writable=True)
    services=Application(settings).services
    save_settings(database,{'features':{'event_to_scene':True}})
    event=services.write('event','save',{'kind':'event','title':'Event','body_md':'Original body',
        'sources':[{'source_key':'original','content':'Original evidence'}]})
    return settings,services,event['id']


def request(event_id,q=0,**extra):
    return {'event_id':event_id,'expected_revision':1,'expected_queue_revision':q,**extra}


def select(services,event_id):return services.write('select','mailbox_select',request(event_id))


def test_explicit_selection_retention_and_isolation(setup):
    settings,services,event_id=setup
    mailbox=EventMailbox(settings.database)
    assert mailbox.list()['items']==[]
    assert mailbox.read(event_id)['status']=='not_selected'
    with Store(settings.database) as store:
        store.conn.execute("INSERT INTO personal_records VALUES ('favorite',?,?,?,1,0,'a','a')",(event_id,event_id,'{"favorite":true}'))
        before=store.conn.execute("SELECT * FROM personal_records WHERE scope='favorite'").fetchone()
    first=select(services,event_id)
    assert first['queue_revision']==1 and first['event_revision']==1
    assert mailbox.list()['items'][0]['processable']
    assert 'draft' not in mailbox.list()['items'][0]
    assert services.write('select','mailbox_select',request(event_id))==first
    draft=services.write('draft','mailbox_draft',request(event_id,1,title='Scene draft',body_md='Draft body',cues=[]))
    assert draft['queue_revision']==2
    services.write('remove','mailbox_remove',request(event_id,2))
    assert mailbox.list()['items']==[]
    assert mailbox.list(status='removed')['items'][0]['status']=='removed'
    assert mailbox.read(event_id)['draft']['title']=='Scene draft'
    assert services.read(event_id)['document']['body_md']=='Original body'
    again=services.write('reselect','mailbox_select',request(event_id,3))
    assert again['queue_revision']==4 and again['draft']['title']=='Scene draft'
    with Store(settings.database,read_only=True) as store:
        assert tuple(store.conn.execute("SELECT * FROM personal_records WHERE scope='favorite'").fetchone())==tuple(before)
        assert store.read(event_id)['revision']==1
        assert store.conn.execute('PRAGMA user_version').fetchone()[0]==9


def test_promotion_atomic_cues_receipts_and_validation(setup):
    settings,services,event_id=setup
    select(services,event_id)
    services.write('draft','mailbox_draft',request(event_id,1,title='Scene draft',body_md='Draft body',cues=[' cue ','cue','second']))
    promote=request(event_id,2,title='Saved title',body_md='Saved body')
    for cues in ([],['x'*81],['x'+str(i) for i in range(9)],123):
        with pytest.raises(ValueError):services.write('invalid','promote_event',{**promote,'cues':cues})
        with Store(settings.database,read_only=True) as store:
            assert store.read(promoted_scene_id(event_id)) is None
            assert store.conn.execute("SELECT 1 FROM write_receipts WHERE operation_id='invalid'").fetchone() is None
        assert EventMailbox(settings.database).read(event_id)['queue_revision']==2
    with pytest.raises(Conflict):services.write('stale','promote_event',{**promote,'expected_queue_revision':1})
    with pytest.raises(Conflict):services.write('missing-q','promote_event',{k:v for k,v in promote.items() if k!='expected_queue_revision'})
    result=services.write('promote','promote_event',promote)
    assert result['queue_status']=='completed' and result['queue_revision']==3
    assert services.write('promote','promote_event',promote)==result
    with pytest.raises(Conflict):services.write('promote','promote_event',{**promote,'body_md':'changed'})
    scene=services.read(result['id'])
    assert scene['document']['metadata']['scene_cues']==['cue','second']
    assert len(scene['evidence'])==1
    assert EventMailbox(settings.database).list()['items']==[]
    assert EventMailbox(settings.database).read(event_id)['draft']['body_md']=='Draft body'
    assert EventMailbox(settings.database).list(status='completed')['items'][0]['scene_id']==result['id']


@pytest.mark.parametrize('lifecycle',['archived','deleted','superseded'])
def test_unavailable_events_not_processable_but_removable(setup,lifecycle):
    settings,services,event_id=setup
    select(services,event_id)
    services.write('state','state',{'document_id':event_id,'expected_revision':1,'lifecycle':lifecycle})
    tools=tools_for(settings)
    assert tools['list_event_mailbox']()['items']==[]
    with pytest.raises(ValueError):tools['read_event_mailbox'](event_id)
    with pytest.raises(Conflict):services.write('draft','mailbox_draft',request(event_id,1,expected_revision=2,title='no'))
    with pytest.raises(Conflict):services.write('promote','promote_event',request(event_id,1,expected_revision=2,title='no',body_md='no'))
    removed=services.write('remove','mailbox_remove',request(event_id,1,expected_revision=2))
    assert removed['status']=='removed'
    assert services.read(event_id)['status']==lifecycle


def test_revision_races_gate_and_no_ai_selection(setup):
    settings,services,event_id=setup
    select(services,event_id)
    tools=tools_for(settings)
    assert 'select_event_mailbox' not in tools
    def draft(i):
        try:return Application(settings).services.write('race'+str(i),'mailbox_draft',request(event_id,1,title='Draft '+str(i)))
        except Conflict:return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(draft,range(2)))
    assert sum(result is not None for result in results)==1
    services.write('edit','save',{'document_id':event_id,'expected_revision':1,'kind':'event','title':'Changed','body_md':'Changed'})
    with pytest.raises(Conflict):services.write('stale-event','mailbox_draft',request(event_id,2,title='Stale'))
    save_settings(settings.database,{'features':{'event_to_scene':False}})
    with pytest.raises(ValueError,match='disabled'):tools['list_event_mailbox']()
    with pytest.raises(ValueError,match='disabled'):tools['save_event_mailbox_draft']('cached',event_id,2,2,'Title','Body')
    with pytest.raises(ValueError,match='disabled'):services.write('select','mailbox_select',request(event_id))
    readonly=Settings(settings.database,writable=False)
    assert 'save_event_mailbox_draft' not in tools_for(readonly)
    with pytest.raises(ValueError,match='read-only'):Application(readonly).services.write('no','mailbox_remove',request(event_id,2))


def test_pagination_and_reserved_scope(setup):
    settings,services,event_id=setup
    from serein.core.personal import Personal
    for operation in [lambda:Personal(settings.database).save('event_mailbox',event_id,{},document_id=event_id),
                      lambda:Personal(settings.database).import_legacy([{'scope':'event_mailbox','key':event_id,'value':{}}])]:
        with pytest.raises(ValueError):operation()
    select(services,event_id)
    for n in range(2):
        new=services.write('e'+str(n),'save',{'kind':'event','title':str(n),'body_md':'body',
            'sources':[{'source_key':'source'+str(n),'content':'evidence'}]})
        services.write('s'+str(n),'mailbox_select',request(new['id']))
    mailbox=EventMailbox(settings.database)
    first=mailbox.list(limit=2)
    second=mailbox.list(limit=2,offset=first['next_offset'])
    assert first['has_more'] and not second['has_more']
    assert len({x['event_id'] for x in first['items']+second['items']})==3


def test_http_contract_and_mcp_read_annotations(setup):
    settings,services,event_id=setup
    from serein.api.http import create_app
    from serein.api.mcp import create_server
    import asyncio
    with TestClient(create_app(settings,token='test',live=True)) as client:
        headers={'Authorization':'Bearer test'}
        assert client.get('/api/event-mailbox').status_code==401
        assert client.get('/api/event-mailbox/'+event_id,headers=headers).json()['queue_revision']==0
        body={'operation_id':'http-select','action':'select','expected_revision':1,'expected_queue_revision':0}
        assert client.post('/api/event-mailbox/'+event_id,headers=headers,json=body).json()['queue_revision']==1
        assert client.post('/api/event-mailbox/'+event_id,headers=headers,json={**body,'operation_id':'http-stale'}).status_code==409
        promotion={'operation_id':'http-promote','event_id':event_id,'expected_revision':1,'expected_queue_revision':1,'title':'Scene','body_md':'Body','cues':['one']}
        result=client.post('/v1/extensions/promote_event_to_scene',headers=headers,json=promotion)
        assert result.status_code==200 and result.json()['queue_status']=='completed'
        assert client.post('/v1/extensions/promote_event_to_scene',headers=headers,json={**promotion,'operation_id':'duplicate'}).status_code==409
    async def check():
        server=create_server(Application(settings))
        tools={x.name:x for x in await server.list_tools()}
        for name in ('list_event_mailbox','read_event_mailbox'):assert tools[name].annotations.readOnlyHint
    asyncio.run(check())


def test_removed_entry_stale_requests_and_deleted_draft_redaction(setup):
    settings,services,event_id=setup
    select(services,event_id)
    services.write('remove','mailbox_remove',request(event_id,1))
    with pytest.raises(Conflict,match='not pending'):
        services.write('stale-ai','promote_event',{'event_id':event_id,'expected_revision':1,'title':'Title','body_md':'Body'})
    services.write('delete','state',{'document_id':event_id,'expected_revision':1,'lifecycle':'deleted'})
    item=EventMailbox(settings.database).read(event_id)
    assert item['draft']=={'title':'','body_md':'','cues':[]}
    assert item['title']=='Deleted Event'
    assert 'Original body' not in json.dumps(item)


def test_draft_revision_survives_reselection_and_source_changes(setup):
    settings,services,event_id=setup
    select(services,event_id)
    services.write('edit','save',{'document_id':event_id,'expected_revision':1,'kind':'event','title':'Changed','body_md':'Changed'})
    item=EventMailbox(settings.database).read(event_id)
    assert item['draft_stale'] and item['draft_event_revision']==1 and item['event_revision']==2
    services.write('remove','mailbox_remove',request(event_id,1,expected_revision=2))
    item=services.write('again','mailbox_select',request(event_id,2,expected_revision=2))
    assert item['draft_stale'] and item['draft']['body_md']=='Original body'
    reviewed=services.write('review','mailbox_draft',request(event_id,3,expected_revision=2,body_md='Reviewed draft'))
    assert reviewed['draft_event_revision']==2 and not reviewed['draft_stale']


def test_concurrent_promotion_and_draft_only_one_commits(setup):
    settings,services,event_id=setup
    select(services,event_id)
    def write(action):
        try:return services.write(action,action,request(event_id,1,title='Scene',body_md='Body',cues=['cue']))
        except Conflict:return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(write,['promote_event','mailbox_draft']))
    assert sum(result is not None for result in results)==1
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute("SELECT count(*) FROM write_receipts WHERE operation_id IN ('promote_event','mailbox_draft')").fetchone()[0]==1
    item=EventMailbox(settings.database).read(event_id)
    assert item['queue_revision']==2
    if results[0]:assert item['status']=='completed' and item['scene_id']
    else:assert item['status']=='pending' and item['scene_id'] is None


def test_cues_index_and_existing_state_retained(setup,tmp_path):
    settings,services,event_id=setup
    from serein.core.search import build_index
    from serein.recall.index import Search
    select(services,event_id)
    with Store(settings.database) as store:
        store.conn.execute("INSERT INTO background_state VALUES ('sentinel','{\"value\":7}')")
        store.conn.execute("INSERT INTO memory_candidates VALUES ('candidate','{}','pending',NULL,'stamp',NULL)")
        initial=[tuple(row) for row in store.conn.execute('SELECT * FROM personal_records')]
    with Store(settings.database) as store:
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM personal_records')]==initial
        assert json.loads(store.conn.execute("SELECT value_json FROM background_state WHERE name='sentinel'").fetchone()[0])=={'value':7}
    result=services.write('promote','promote_event',request(event_id,1,title='Scene',body_md='Body',cues='uniqueharbor;second'))
    index=tmp_path/'index.db'
    build_index(settings.database,index)
    from serein.recall.service import Recall
    from dataclasses import replace
    assert Recall(replace(settings,index=index)).run('uniqueharbor',mode='lookup')['selected_refs']==['scene:'+result['id']]
    from serein.recall.scene import cue_matches
    from types import SimpleNamespace
    assert cue_matches(services.read(result['id'])['document'],SimpleNamespace(search_text='uniqueharbor'))
    from serein.recall.index import content_stamp
    with Search(settings.database,index) as search:
        assert search.conn.execute('SELECT stamp FROM documents WHERE id=?',(result['id'],)).fetchone()[0]==content_stamp(services.read(result['id'])['document'])
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute("SELECT status FROM memory_candidates WHERE id='candidate'").fetchone()[0]=='pending'


def test_cursor_does_not_skip_after_processing_previous_page(setup):
    settings,services,event_id=setup
    select(services,event_id)
    event2=services.write('event2','save',{'kind':'event','title':'Next','body_md':'Next',
        'sources':[{'source_key':'next','content':'Next'}]})['id']
    services.write('select2','mailbox_select',request(event2))
    listing=tools_for(settings)['list_event_mailbox']
    first=listing(limit=1)
    assert first['has_more']
    services.write('promote','promote_event',request(first['items'][0]['event_id'],1,title='Scene',body_md='Body'))
    second=listing(limit=1,cursor=first['next_cursor'])
    assert second['items'][0]['event_id']!=first['items'][0]['event_id']
    assert not second['has_more']
    with pytest.raises(ValueError):listing(cursor='invalid')
    with pytest.raises(ValueError):listing(cursor=first['next_cursor'],offset=1)


def test_failure_after_scene_creation_rolls_back_entire_promotion(setup,monkeypatch):
    settings,services,event_id=setup
    select(services,event_id)
    scene_id=promoted_scene_id(event_id)
    promotion=request(event_id,1,title='Scene',body_md='Body',cues=['cue'])
    import serein.core.event_mailbox as mailbox_module
    complete=mailbox_module.complete
    def injected_failure(store,row,created_scene_id):
        # Fail at the transaction's last step, after all canonical writes exist.
        assert created_scene_id==scene_id and store.read(scene_id) is not None
        assert store.conn.execute('SELECT 1 FROM evidence_bindings WHERE document_id=?',(scene_id,)).fetchone()
        assert store.conn.execute('SELECT 1 FROM index_outbox WHERE document_id=?',(scene_id,)).fetchone()
        raise RuntimeError('Injected queue completion failure')
    monkeypatch.setattr(mailbox_module,'complete',injected_failure)
    with pytest.raises(RuntimeError,match='Injected queue completion failure'):
        services.write('promotion-retry','promote_event',promotion)
    with Store(settings.database,read_only=True) as store:
        assert store.read(scene_id) is None
        assert store.conn.execute('SELECT 1 FROM evidence_bindings WHERE document_id=?',(scene_id,)).fetchone() is None
        assert store.conn.execute('SELECT 1 FROM index_outbox WHERE document_id=?',(scene_id,)).fetchone() is None
        assert store.conn.execute('SELECT 1 FROM scene_jobs WHERE scene_id=?',(scene_id,)).fetchone() is None
        assert store.conn.execute("SELECT 1 FROM write_receipts WHERE operation_id='promotion-retry'").fetchone() is None
    item=EventMailbox(settings.database).read(event_id)
    assert item['status']=='pending' and item['queue_revision']==1
    monkeypatch.setattr(mailbox_module,'complete',complete)
    result=services.write('promotion-retry','promote_event',promotion)
    assert result['id']==scene_id and result['queue_status']=='completed' and result['queue_revision']==2
    assert services.write('promotion-retry','promote_event',promotion)==result


def test_draft_does_not_schedule_models_or_index_work(setup,monkeypatch):
    settings,services,event_id=setup
    select(services,event_id)
    with Store(settings.database,read_only=True) as store:
        initial_outbox=[tuple(row) for row in store.conn.execute('SELECT * FROM index_outbox ORDER BY sequence')]
        initial_jobs=[tuple(row) for row in store.conn.execute('SELECT * FROM scene_jobs ORDER BY sequence')]
    def unexpected_model_preparation(*args,**kwargs):
        raise AssertionError('Draft must not prepare model/index work')
    monkeypatch.setattr('serein.recall.passage_layouts.prepare_layouts',unexpected_model_preparation)
    result=services.write('draft-only','mailbox_draft',request(event_id,1,title='Draft',body_md='Draft body',cues=['cue']))
    assert result['status']=='pending' and result['index']['status']=='current'
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute("SELECT count(*) FROM documents WHERE kind='scene'").fetchone()[0]==0
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM index_outbox ORDER BY sequence')]==initial_outbox
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM scene_jobs ORDER BY sequence')]==initial_jobs
