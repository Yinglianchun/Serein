import asyncio
import json
import pytest
from fastapi.testclient import TestClient
from serein.api.http import create_app
from serein.api.mcp import create_server
from serein.application import Application
from serein.compat.window_shadows import WindowShadows
from serein.compat.raw_archive import raw_archive
from serein.core.personal import Personal
from serein.core.store import Store, Conflict
from serein.deployment import save_settings, read_settings
from test_public_features import settings


def test_resume_http_toggle_and_persistent_selection_without_mcp_tool(settings):
    server=create_server(Application(settings))
    names=lambda:{tool.name for tool in asyncio.run(server.list_tools())}
    with TestClient(create_app(settings,token='test',live=True),headers={'Authorization':'Bearer test'}) as client:
        assert 'resume' not in names()
        assert 'source_message_read' not in names()
        assert client.post('/v1/extensions/resume',json={'window_id':'new'}).status_code==404
        save_settings(settings.database,{'features':{'resume':True},'resume':{'pending_originals':False}})
        assert 'resume' not in names()
        with pytest.raises(Exception, match='Unknown tool'):
            asyncio.run(server.call_tool('resume', {'window_id': 'new'}))
        assert client.post('/v1/extensions/resume',json={'window_id':'new'}).json()['selection']['pending_originals'] is False
        assert Application(settings).contributions.tools['resume']('new')['selection']['pending_originals'] is False
        save_settings(settings.database,{'features':{'resume':False}})
        assert 'resume' not in names()
        assert client.post('/v1/extensions/resume',json={'window_id':'new'}).status_code==404


def test_draft_resume_preview_pages_without_saving_or_changing_tool_selection(settings):
    save_settings(settings.database,{'features':{'resume':True},'resume':{'mode':'mcp',
        'latest_shadow':False,'recent_events':False,'favorite_scenes':False,'pending_originals':False}})
    body='Synthetic long draft material.\n'*1000
    with Store(settings.database) as store:
        store.create('draft-preview','event','Synthetic draft',body)
        store.create('archived-preview','scene','Archived draft','Hidden',lifecycle='archived')
    before=read_settings(settings.database)
    draft={'selected_memories':True,'selected_ids':['draft-preview','archived-preview']}
    client=TestClient(create_app(settings,token='test',live=True),headers={'Authorization':'Bearer test'})
    first=client.post('/v1/extensions/resume',json={'selection':draft}).json()
    assert first['has_more'] and first['total_items']==1 and first['injected'] is False
    pages=[first];cursor=first['next_cursor']
    while cursor:
        response=client.post('/v1/extensions/resume',json={'selection':draft,'cursor':cursor})
        assert response.status_code==200,response.text
        page=response.json();assert page['collection_id']==first['collection_id']
        pages.append(page);cursor=page['next_cursor']
    assert ''.join(item['body_md'] for page in pages for item in page['items'])==body
    assert read_settings(settings.database)==before
    assert client.post('/v1/extensions/resume',json={}).json()['items']==[]
    result=asyncio.run(create_server(Application(settings)).call_tool('resume',{}))
    blocks=result[0] if isinstance(result,tuple) else result
    assert 'total_items: 0' in blocks[0].text
    changed=client.post('/v1/extensions/resume',json={'selection':{'selected_memories':False},'cursor':first['next_cursor']})
    assert changed.status_code==400 and 'changed; restart resume' in changed.json()['detail']
    with pytest.raises(Exception,match='Unexpected resume arguments'):
        asyncio.run(create_server(Application(settings)).call_tool('resume',{'selection':draft}))
    save_settings(settings.database,{'features':{'resume':False}})
    assert client.post('/v1/extensions/resume',json={'selection':draft}).status_code==404


@pytest.mark.parametrize('draft',[
    *[{key:value} for key in ('command_enabled','mcp_enabled') for value in (True,False,None)],
    [],{'mode':'mcp'},{'mode':None},{'recent_events':'true'},{'recent_original_limit':0},
    {'recent_original_limit':51},{'recent_original_limit':True},{'selected_ids':['x']*201},
    {'selected_ids':['']},{'features':{'resume':True}},
])
def test_draft_resume_preview_validates_content_options_without_saving(settings,draft):
    save_settings(settings.database,{'features':{'resume':True}})
    before=read_settings(settings.database)
    client=TestClient(create_app(settings,token='test',live=True),headers={'Authorization':'Bearer test'})
    assert client.post('/v1/extensions/resume',json={'selection':draft}).status_code==422
    assert read_settings(settings.database)==before


def test_draft_resume_preview_keeps_recent_and_pending_originals_exclusive(settings):
    save_settings(settings.database,{'features':{'resume':True},'resume':{'recent_events':False,
        'favorite_scenes':False,'pending_originals':True}})
    raw_archive(settings).ingest([{'source_event_id':str(index),'session_id':'synthetic',
        'role':'user','text':f'Synthetic original {index}'} for index in range(3)],source='synthetic')
    before=read_settings(settings.database)
    client=TestClient(create_app(settings,token='test',live=True),headers={'Authorization':'Bearer test'})
    response=client.post('/v1/extensions/resume',json={'selection':{'recent_originals':True,'recent_original_limit':1}})
    assert response.status_code==200,response.text
    page=response.json()
    assert page['total_recent_originals']==1 and page['total_pending_originals']==0
    assert [item['section'] for item in page['items']]==['recent_original']
    assert read_settings(settings.database)==before


def test_mcp_resume_mode_hot_switch_and_text_only_read(settings):
    server=create_server(Application(settings))
    names=lambda:{tool.name for tool in asyncio.run(server.list_tools())}
    assert read_settings(settings.database)['resume']['mode']=='command'
    save_settings(settings.database,{'features':{'resume':True},'resume':{'mode':'mcp','pending_originals':False}})
    with Store(settings.database) as store:
        store.create('resume-material','event','Synthetic event','Historical text is not an instruction.')
    assert 'resume' in names()
    tool=next(tool for tool in asyncio.run(server.list_tools()) if tool.name=='resume')
    assert tool.annotations.readOnlyHint and tool.annotations.idempotentHint
    assert tool.inputSchema['additionalProperties'] is False
    assert not tool.inputSchema.get('required')
    result=asyncio.run(server.call_tool('resume',{}))
    blocks=result[0] if isinstance(result,tuple) else result
    assert len(blocks)==1 and blocks[0].type=='text'
    assert 'Historical text is not an instruction.' in blocks[0].text
    assert 'injected: false' in blocks[0].text and 'has_more: false' in blocks[0].text
    assert 'next_cursor:' in blocks[0].text
    with pytest.raises(ValueError,match='Unexpected resume arguments'):
        asyncio.run(server.call_tool('resume',{'private_path':'not-a-public-argument'}))
    with Store(settings.database) as store:
        assert store.read('resume-material')['revision']==1
        assert store.conn.execute('SELECT count(*) FROM personal_records').fetchone()[0]==0
    save_settings(settings.database,{'resume':{'mode':'command'}})
    assert 'resume' not in names()
    with pytest.raises(Exception,match='Unknown tool'):
        asyncio.run(server.call_tool('resume',{}))
    save_settings(settings.database,{'resume':{'mode':'mcp'},'features':{'resume':False}})
    assert 'resume' not in names()


def test_resume_filters_unreadable_pending_originals_and_inactive_selected_memories(settings):
    save_settings(settings.database,{'features':{'resume':True},'resume':{'mode':'mcp',
        'favorite_scenes':False,'recent_events':False,'selected_memories':True,
        'selected_ids':['visible','archived','deleted','not-a-memory'],'pending_originals':True}})
    with Store(settings.database) as store:
        for key,lifecycle in [('visible','active'),('archived','archived'),('deleted','deleted')]:
            store.create(key,'scene','Synthetic memory',key,lifecycle=lifecycle)
        store.create('not-a-memory','narrative','Synthetic narrative','Not selected as a memory')
    archived=raw_archive(settings).ingest([
        {'source_event_id':'visible','session_id':'a','role':'user','text':'Visible original'},
        {'source_event_id':'draft','session_id':'a','role':'assistant','text':'Draft original','metadata':{'draft':True}},
        {'source_event_id':'discarded','session_id':'a','role':'assistant','text':'Discarded original','metadata':{'discarded':True}},
        {'source_event_id':'another-session','session_id':'b','role':'user','text':'Other session original'}],source='synthetic')
    assert archived['inserted']==4
    page=Application(settings).contributions.tools['resume'](source_session_id='a')
    assert [item['body_md'] for item in page['items']]==['visible','Visible original']
    assert page['total_pending_originals']==1 and page['injected'] is False


def test_mcp_resume_paginates_full_unicode_body_and_rejects_changed_material(settings):
    save_settings(settings.database,{'features':{'resume':True},'resume':{'mode':'mcp','pending_originals':False}})
    body='雨🌧️\n'*6000+'The final line.'
    with Store(settings.database) as store:
        store.create('long-resume','event','Synthetic long event',body)
    application=Application(settings)
    server=create_server(application)
    cursor='';fragments=[];first_cursor=None
    while True:
        result=asyncio.run(server.call_tool('resume',{'cursor':cursor}))
        blocks=result[0] if isinstance(result,tuple) else result
        assert len(blocks)==1 and blocks[0].type=='text'
        text=blocks[0].text
        fragments.append(text.split('\nbody:\n',1)[1].split('\n[/material]',1)[0])
        cursor=text.split('next_cursor: ',1)[1].split('\n',1)[0]
        if first_cursor is None:first_cursor=cursor
        if not cursor:
            assert 'has_more: false' in text
            break
        assert 'has_more: true' in text
    assert len(fragments)>1 and ''.join(fragments)==body
    with Store(settings.database) as store:
        store.revise('long-resume',title='Synthetic long event',body_md='Changed material',expected_revision=1)
    with pytest.raises(Exception,match='changed; restart resume'):
        asyncio.run(server.call_tool('resume',{'cursor':first_cursor}))
    for cursor in ('x'*121,'not-a-cursor'):
        with pytest.raises(Exception,match='Invalid resume cursor'):
            asyncio.run(server.call_tool('resume',{'cursor':cursor}))


def test_resume_limits_auth_and_selection_conflicts(settings,monkeypatch):
    from serein.extensions import handoff
    save_settings(settings.database,{'features':{'resume':True},'resume':{'mode':'mcp','pending_originals':False}})
    with Store(settings.database) as store:
        store.create('first','event','Synthetic first','First body')
        store.create('second','event','Synthetic second','Second body')
    client=TestClient(create_app(settings,token='test',live=True))
    assert client.post('/v1/extensions/resume',json={}).status_code==401
    client.headers['Authorization']='Bearer test'
    monkeypatch.setattr(handoff,'MAX_ITEMS',1)
    response=client.post('/v1/extensions/resume',json={})
    assert response.status_code==413 and 'items' not in response.json()
    assert 'Nothing was truncated' in response.json()['detail']
    monkeypatch.setattr(handoff,'MAX_ITEMS',500)
    monkeypatch.setattr(handoff,'MAX_PAGE_BYTES',100)
    response=client.post('/v1/extensions/resume',json={})
    assert response.status_code==413 and 'items' not in response.json()
    assert 'transport limit' in response.json()['detail']
    assert client.patch('/v1/settings',json={'resume':{'mode':'both'}}).status_code==422
    with pytest.raises(ValueError,match='Resume mode'):
        save_settings(settings.database,{'resume':{'mode':'both'}})
    value=read_settings(settings.database)
    save_settings(settings.database,{'resume':{'recent_events':False}})
    response=client.patch('/v1/settings',json={'expected_version':value['settings_version'],'resume':{'mode':'command'}})
    assert response.status_code==400 and read_settings(settings.database)['resume']['mode']=='mcp'
    assert '设置已在其他页面更新' in response.json()['detail']


@pytest.mark.parametrize('selected',[False,True])
def test_mcp_resume_respects_explicit_tool_allowlist(settings,selected):
    from serein.config import Settings
    save_settings(settings.database,{'features':{'resume':True},'resume':{'mode':'mcp'}})
    server=create_server(Application(Settings(settings.database,settings.index,writable=True,
        mcp_tools=['read_memory',*(['resume'] if selected else [])])))
    names={tool.name for tool in asyncio.run(server.list_tools())}
    assert ('resume' in names) is selected


def test_latest_shadow_introductions_manual_edit_retry_and_older_revision(settings):
    save_settings(settings.database,{'features':{'window_shadows':True,'resume':True}})
    shadows=WindowShadows(settings.database)
    old=dict(window_id='a',title='Earlier',user_view='Earlier view',self_view='Earlier self',recent_events='Earlier events')
    current=dict(window_id='b',title='Latest',user_view='Current view',self_view='Current self',recent_events='Current events')
    shadows.write(**old);shadows.write(**current)
    assert shadows.read()['window_id']=='b'
    assert read_settings(settings.database)['identity']['user_description']=='Current view'
    save_settings(settings.database,{'identity':{'user_description':'Human edit'}})
    assert shadows.write(**current)['status']=='unchanged'
    assert read_settings(settings.database)['identity']['user_description']=='Human edit'
    shadows.write(**{**old,'user_view':'Older revised'},expected_revision=1)
    assert read_settings(settings.database)['identity']['user_description']=='Human edit'
    result=Application(settings).contributions.tools['resume']('new')
    assert [item['id'] for item in result['items'] if item['kind']=='shadow']==['shadow:b']
    assert 'Current events' in result['items'][0]['body_md']
    with Store(settings.database) as store:
        assert result['items'][0]['created_at']==store.conn.execute("SELECT json_extract(metadata_json,'$.created_at') FROM historical_works WHERE id='shadow:b'").fetchone()[0]
    with Store(settings.database) as store:store.record_deletion('shadow:b','2026-09-10',{})
    assert shadows.read()['window_id']=='a'
    with pytest.raises(Conflict):shadows.write(**current)


def test_selected_events_scenes_original_ids_and_pending_toggle(settings):
    save_settings(settings.database,{'features':{'resume':True,'originals':True},'resume':{'selected_memories':True,'selected_ids':['event']}})
    with Store(settings.database) as store:
        store.create('scene','scene','Scene','Selected scene',created_at='2025-01-01T00:00:00Z')
        store.create('event','event','Event','Selected event',created_at='2025-01-02T00:00:00Z')
        source=store.add_source('session-a:7','Actual original',metadata={'message_id':'7','session_id':'session-a'})
        store.bind('event',source)
    for key in ('scene','event'):Personal(settings.database).save('favorite',key,{'favorite':True},document_id=key)
    raw_archive(settings).ingest([{'source_event_id':'7','session_id':'session-b','role':'user','text':'Different original'}],source='synthetic')
    tools=Application(settings).contributions.tools
    result=tools['resume']('new')
    event=next(item for item in result['items'] if item['id']=='event')
    assert event['created_at']=='2025-01-02T00:00:00Z'
    assert next(item for item in result['items'] if item['id']=='scene')['created_at']=='2025-01-01T00:00:00Z'
    assert sum(item['id']=='event' for item in result['items'])==1
    assert 'source_refs' not in event
    assert Application(settings).services.read(event['id'],with_evidence=True)['evidence'][0]['content']=='Actual original'
    originals=tools['source_message_read'](['source:'+source,result['items'][-1]['id']])
    assert [item['content'] for item in originals['items']]==['Actual original','Different original']
    assert tools['source_message_read'](['raw:9999'])['missing_ids']==['raw:9999']
    save_settings(settings.database,{'resume':{'favorite_scenes':False,'recent_events':False,'pending_originals':False}})
    assert [item['id'] for item in tools['resume']('new')['items']]==['event']
    with Store(settings.database) as store:store.set_lifecycle('event','deleted')
    assert tools['resume']('new')['items']==[]
    assert tools['source_message_read'](['source:'+source])['missing_ids']==['source:'+source]


def test_picker_filters_pagination_and_explicit_selection_survives_reload(settings):
    with Store(settings.database) as store:
        for index in range(32):
            store.create(f'pick-{index:02}','event','Selected topic',f'Body {index}',created_at='2025-01-02T00:00:00Z')
        store.create('pick-scene','scene','Scene title','Distinct prose',created_at='2025-01-03T00:00:00Z')
        store.create('archived','event','Selected topic','Archived',lifecycle='archived')
    with TestClient(create_app(settings,token='test',live=True),headers={'Authorization':'Bearer test'}) as client:
        first=client.get('/v1/settings/resume-candidates?kind=event&q=topic&date=2025-01-02').json()
        assert first['total']==32 and len(first['items'])==30 and first['has_more']
        second=client.get('/v1/settings/resume-candidates?kind=event&offset=30').json()
        assert len(second['items'])==2 and not second['has_more']
        assert not ({item['id'] for item in first['items']}&{item['id'] for item in second['items']})
        assert client.get('/v1/settings/resume-candidates?kind=scene&q=Distinct').json()['items'][0]['id']=='pick-scene'
        assert client.get('/v1/settings/resume-candidates?kind=&ids=pick-00&ids=pick-scene').json()['total']==2
        changes={'features':{'resume':True},'resume':{'selected_memories':True,'selected_ids':['pick-scene','pick-00','pick-00'],
            'recent_events':False,'favorite_scenes':False,'pending_originals':False,'latest_shadow':False}}
        saved=client.patch('/v1/settings',json=changes)
        assert saved.status_code==200,saved.text
        assert saved.json()['resume']['selected_ids']==['pick-scene','pick-00']
        assert client.patch('/v1/settings',json={'resume':{'selected_ids':['x']*201}}).status_code==422
    resume=Application(settings).contributions.tools['resume']
    assert [item['id'] for item in resume('new')['items']]==['pick-scene','pick-00']
    with Store(settings.database) as store:store.set_lifecycle('pick-00','archived')
    assert [item['id'] for item in resume('new')['items']]==['pick-scene']
    save_settings(settings.database,{'resume':{'selected_memories':False}})
    assert resume('new')['items']==[]
    assert read_settings(settings.database)['resume']['selected_ids']==['pick-scene','pick-00']


def test_resume_can_include_a_bounded_number_of_recent_originals(settings):
    entries=[{'source_event_id':str(index),'session_id':'recent-chat','role':'user' if index%2 else 'assistant',
              'text':f'Original {index}','created_at':f'2026-09-15T00:0{index}:00Z'} for index in range(1,6)]
    raw_archive(settings).ingest(entries,source='synthetic')
    save_settings(settings.database,{'features':{'resume':True},'resume':{
        'latest_shadow':False,'recent_events':False,'favorite_scenes':False,'selected_memories':False,
        'pending_originals':False,'recent_originals':True,'recent_original_limit':3}})
    resume=Application(settings).contributions.tools['resume']
    result=resume('new')
    originals=[item for item in result['items'] if item['section']=='recent_original']
    assert [item['body_md'] for item in originals]==['Original 3','Original 4','Original 5']
    assert result['total_recent_originals']==3
    assert result['raw_message_ids']==[item['raw_id'] for item in originals]
    scoped=resume('new',source_session_id='missing-chat')
    assert scoped['items']==[] and scoped['total_recent_originals']==0
    save_settings(settings.database,{'resume':{'pending_originals':True}})
    pending=resume('new')
    assert pending['selection']['pending_originals'] is True
    assert pending['selection']['recent_originals'] is False
    assert len([item for item in pending['items'] if item['kind']=='raw'])==5


@pytest.mark.parametrize('command,mcp', [(False, False), (True, False), (False, True), (True, True)])
def test_resume_entry_switches_are_independent_and_hot_reload(settings, command, mcp):
    server = create_server(Application(settings))
    client = TestClient(create_app(settings, token='synthetic', live=True), headers={'Authorization':'Bearer synthetic'})
    saved = client.patch('/v1/settings', json={'features':{'resume':True}, 'resume':{
        'command_enabled':command, 'mcp_enabled':mcp}})
    assert saved.status_code == 200, saved.text
    assert saved.json()['resume']['command_enabled'] is command
    assert saved.json()['resume']['mcp_enabled'] is mcp
    names = lambda: {tool.name for tool in asyncio.run(server.list_tools())}
    assert ('resume' in names()) is mcp
    if mcp:
        result = asyncio.run(server.call_tool('resume', {}))
        assert result
    else:
        with pytest.raises(Exception):
            asyncio.run(server.call_tool('resume', {}))
    # Content preview is independent of both transport switches.
    assert client.post('/v1/extensions/resume', json={}).status_code == 200
    save_settings(settings.database, {'features':{'resume':False}})
    assert 'resume' not in names()
    with pytest.raises(Exception):
        asyncio.run(server.call_tool('resume', {}))
    assert client.post('/v1/extensions/resume', json={}).status_code == 404
    save_settings(settings.database, {'features':{'resume':True}})
    assert ('resume' in names()) is mcp
    state = read_settings(settings.database)['resume']
    assert (state['command_enabled'], state['mcp_enabled']) == (command, mcp)


@pytest.mark.parametrize('legacy', [{}, {'mode':'command'}, {'mode':'mcp'},
    {'mode':'mcp','command_enabled':True}, {'mode':'command','mcp_enabled':True},
    {'mode':'mcp','mcp_enabled':False}, {'mode':'command','command_enabled':False}])
def test_resume_legacy_settings_migrate_without_enabling_an_extra_entry(settings, legacy):
    with Store(settings.database) as store, store.transaction(immediate=True):
        store.conn.execute("INSERT OR REPLACE INTO background_state(name,value_json) VALUES (?,?)",
                           ('deployment_settings', json.dumps({'resume':legacy})))
    state = read_settings(settings.database)['resume']
    mode = legacy.get('mode','command')
    assert state['command_enabled'] is legacy.get('command_enabled', mode == 'command')
    assert state['mcp_enabled'] is legacy.get('mcp_enabled', mode == 'mcp')
    # Any later unrelated save persists the normalized values, not new defaults.
    save_settings(settings.database, {'resume':{'selected_ids':['synthetic-choice']}})
    reloaded = read_settings(settings.database)['resume']
    assert (reloaded['command_enabled'], reloaded['mcp_enabled']) == (state['command_enabled'], state['mcp_enabled'])


def test_resume_partial_and_legacy_patches_preserve_explicit_switches(settings):
    save_settings(settings.database, {'features':{'resume':True}, 'resume':{
        'selected_ids':['synthetic-choice'], 'command_enabled':True, 'mcp_enabled':True}})
    save_settings(settings.database, {'resume':{'command_enabled':False}})
    state = read_settings(settings.database)
    assert state['features']['resume'] is True
    assert state['resume']['mcp_enabled'] is True
    assert state['resume']['selected_ids'] == ['synthetic-choice']
    save_settings(settings.database, {'resume':{'mcp_enabled':False}})
    assert read_settings(settings.database)['resume']['command_enabled'] is False
    for mode in ('command','mcp'):
        save_settings(settings.database, {'resume':{'mode':mode}})
        state = read_settings(settings.database)['resume']
        assert (state['command_enabled'],state['mcp_enabled']) == (mode == 'command',mode == 'mcp')
    save_settings(settings.database, {'resume':{'mode':'mcp','command_enabled':True,'mcp_enabled':False}})
    state = read_settings(settings.database)['resume']
    assert (state['command_enabled'],state['mcp_enabled']) == (True,False)


@pytest.mark.parametrize('key', ['command_enabled','mcp_enabled'])
@pytest.mark.parametrize('value', [None, 'false', 'true', 0, 1, [], {}])
def test_resume_entry_switches_require_booleans(settings, key, value):
    client = TestClient(create_app(settings, token='synthetic', live=True), headers={'Authorization':'Bearer synthetic'})
    before = read_settings(settings.database)
    assert client.patch('/v1/settings', json={'resume':{key:value}}).status_code == 422
    with pytest.raises(ValueError, match='booleans'):
        save_settings(settings.database, {'resume':{key:value}})
    assert read_settings(settings.database) == before
