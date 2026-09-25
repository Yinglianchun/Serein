import json
from pathlib import Path
import pytest
import httpx
from fastapi.testclient import TestClient
from serein.config import Settings
from serein.bootstrap import initialize
from serein.api.http import create_app
from serein.deployment import read_settings, task_model, identity
from serein.chat_context import ClientContext


@pytest.fixture
def deployment(tmp_path):
    settings=Settings(tmp_path/'memory.db',index=tmp_path/'index.db',writable=True)
    initialize(settings)
    app=create_app(settings,token='synthetic',live=True)
    return settings,TestClient(app,headers={'Authorization':'Bearer synthetic'})


def configure(client, **upstream):
    return client.patch('/v1/settings',json={
        'models':[{'id':'model-a','label':'Local Model','model':'synthetic-model','base_url':'http://127.0.0.1:9999/v1',
                   'api_key':'synthetic-provider-secret','prompt_cache':'openai'}],
                   'assignments':{'chat':'model-a','writer':'model-a'},'upstream':{'writer_enabled':True,**upstream}})


def test_current_time_defaults_and_timezone_validation(deployment):
    settings,client=deployment
    initial=client.get('/v1/settings').json()
    assert initial['features']['current_time'] is False
    assert initial['clock']=={'timezone':'Asia/Shanghai'}
    saved=client.patch('/v1/settings',json={'features':{'current_time':True},'clock':{'timezone':'Europe/Berlin'}})
    assert saved.status_code==200,saved.text
    assert saved.json()['features']['current_time'] is True
    assert saved.json()['clock']=={'timezone':'Europe/Berlin'}
    assert read_settings(settings.database)['clock']=={'timezone':'Europe/Berlin'}
    assert client.patch('/v1/settings',json={'clock':{'timezone':'Not/A_Timezone'}}).status_code==422


def test_nightly_arc_organization_is_opt_in(deployment):
    settings, client = deployment
    assert client.get('/v1/settings').json()['features']['narrative_nightly_organize'] is False
    saved = client.patch('/v1/settings', json={'features':{'narrative_nightly_organize':True}})
    assert saved.status_code == 200
    assert saved.json()['features']['narrative_nightly_organize'] is True
    assert read_settings(settings.database)['features']['narrative_nightly_organize'] is True


def test_passage_settings_are_optional_strict_and_independent(deployment):
    from serein.configured_models import recall_settings
    from serein.deployment import save_settings
    settings,client=deployment
    initial=client.get('/v1/settings').json()
    assert initial['recall']=={'direct_threshold':.65,'body_candidate_threshold':.5,
                               'cue_candidate_threshold':.55,'passages_enabled':False,'passage_min_chars':500}
    enabled=client.patch('/v1/settings',json={'recall':{'passages_enabled':True,'passage_min_chars':800}})
    assert enabled.status_code==200
    client.patch('/v1/settings',json={'recall':{'direct_threshold':.6}})
    assert recall_settings(settings)=={'passages_enabled':True,'passage_min_chars':800,'direct_threshold':.6}
    for field,values in {'passages_enabled':[1,'true',None],'passage_min_chars':[0,-1,1.5,100001,'500',True,None]}.items():
        for value in values:
            assert client.patch('/v1/settings',json={'recall':{field:value}}).status_code==422
            with pytest.raises(ValueError):save_settings(settings.database,{'recall':{field:value}})
    assert client.patch('/v1/settings',json={'recall':{'passages_enabled':False}}).json()['recall']['passage_min_chars']==800


def test_recall_threshold_persists_and_validates_without_repreparing(deployment):
    from serein.configured_models import effective_settings
    from serein.deployment import save_settings
    settings,client=deployment
    initial=client.get('/v1/settings').json()
    assert initial['recall']['direct_threshold']==.65
    assert read_settings(settings.database)['recall']=={}
    saved=client.patch('/v1/settings',json={'expected_version':initial['settings_version'],'recall':{'direct_threshold':.6}})
    assert saved.status_code==200,saved.text
    assert saved.json()['recall']['direct_threshold']==.6
    assert read_settings(settings.database)['recall']['direct_threshold']==.6
    assert effective_settings(settings).recall['direct_threshold']==.6
    assert not (settings.database.parent/'model-indexes').exists()
    for value in (-.1,1.1,True,'0.6',None):
        assert client.patch('/v1/settings',json={'recall':{'direct_threshold':value}}).status_code==422
    assert client.patch('/v1/settings',json={'recall':{'direct_threshold':.6,'unknown':1}}).status_code==422
    for value in (float('nan'),float('inf'),None,True):
        with pytest.raises(ValueError,match='Recall threshold'):
            save_settings(settings.database,{'recall':{'direct_threshold':value}})
    assert read_settings(settings.database)['settings_version']==saved.json()['settings_version']
    stale=client.patch('/v1/settings',json={'expected_version':initial['settings_version'],'recall':{'direct_threshold':.8}})
    assert stale.status_code==400
    assert client.get('/v1/settings').json()['recall']['direct_threshold']==.6
    assert client.patch('/v1/settings',json={'recall':{'direct_threshold':.65}}).status_code==200


def test_candidate_thresholds_default_persist_and_survive_old_client_updates(deployment):
    from serein.configured_models import effective_settings
    from serein.deployment import save_settings
    settings,client=deployment
    save_settings(settings.database,{'recall':{'direct_threshold':.62}})
    initial=client.get('/v1/settings').json()
    assert initial['recall']['direct_threshold']==.62
    assert initial['recall']['body_candidate_threshold']==.5
    assert initial['recall']['cue_candidate_threshold']==.55
    assert read_settings(settings.database)['recall']=={'direct_threshold':.62}
    saved=client.patch('/v1/settings',json={'recall':{
        'body_candidate_threshold':.47,'cue_candidate_threshold':.59}})
    assert saved.status_code==200,saved.text
    assert saved.json()['recall']['direct_threshold']==.62
    client.patch('/v1/settings',json={'recall':{'direct_threshold':.61}}).raise_for_status()
    current=client.get('/v1/settings').json()['recall']
    assert current=={'direct_threshold':.61,'body_candidate_threshold':.47,
                     'cue_candidate_threshold':.59,'passages_enabled':False,'passage_min_chars':500}
    assert effective_settings(settings).recall['body_candidate_threshold']==.47
    assert not (settings.database.parent/'model-indexes').exists()
    for field in ('body_candidate_threshold','cue_candidate_threshold'):
        for value in (-.1,1.1,True,'0.6',None):
            assert client.patch('/v1/settings',json={'recall':{field:value}}).status_code==422


def test_unset_recall_threshold_preserves_toml(deployment):
    from dataclasses import replace
    from serein.configured_models import effective_settings
    settings,_=deployment
    settings=replace(settings,recall={'direct_threshold':.78,'max_cards':2})
    assert effective_settings(settings).recall==settings.recall
    client=TestClient(create_app(settings,token='synthetic',live=True),headers={'Authorization':'Bearer synthetic'})
    assert client.get('/v1/settings').json()['recall']['direct_threshold']==.78


def test_pipeline_prompt_budget_accepts_large_context_models(deployment):
    settings,client=deployment
    saved=client.patch('/v1/settings',json={'pipeline':{'max_prompt_chars':300000,'event_writer_concurrency':2}})
    assert saved.status_code==200,saved.text
    assert saved.json()['pipeline']['max_prompt_chars']==300000
    assert saved.json()['pipeline']['event_writer_concurrency']==2
    assert read_settings(settings.database)['pipeline']['max_prompt_chars']==300000
    assert client.patch('/v1/settings',json={'pipeline':{'max_prompt_chars':4000000}}).status_code==200
    assert client.patch('/v1/settings',json={'pipeline':{'max_prompt_chars':4000001}}).status_code==422
    for value in (0,9,True,'2'):
        assert client.patch('/v1/settings',json={'pipeline':{'event_writer_concurrency':value}}).status_code==422


def test_recent_original_resume_limit_validation(deployment):
    _,client=deployment
    recent=client.patch('/v1/settings',json={'resume':{'recent_originals':True,'recent_original_limit':1}})
    assert recent.status_code==200 and recent.json()['resume']['pending_originals'] is False
    pending=client.patch('/v1/settings',json={'resume':{'pending_originals':True}})
    assert pending.status_code==200 and pending.json()['resume']['recent_originals'] is False
    both=client.patch('/v1/settings',json={'resume':{'recent_originals':True,'pending_originals':True}})
    assert both.status_code==200 and both.json()['resume']['recent_originals'] is True and both.json()['resume']['pending_originals'] is False
    assert client.patch('/v1/settings',json={'resume':{'recent_original_limit':50}}).status_code==200
    for value in (0,51,True,'20'):
        assert client.patch('/v1/settings',json={'resume':{'recent_original_limit':value}}).status_code==422


def test_retired_event_model_is_ignored_without_changing_saved_settings_on_read(deployment):
    from serein.core.store import Store, encode
    from serein.extensions.pipeline import ROLES
    settings,client=deployment
    response=client.patch('/v1/settings',json={
        'models':[{'id':'local','label':'Synthetic','model':'synthetic','base_url':'http://127.0.0.1:9/v1'}],
        'assignments':{**{role:'local' for role in ROLES},'event_evidence':'removed-model'},
        'pipeline':{'execution_mode':'api','auto_enabled':False}})
    assert response.status_code==200,response.text
    assert 'event_evidence' not in response.json()['assignments']
    with Store(settings.database) as store:
        saved=read_settings(settings.database)
        saved['assignments']['event_evidence']='removed-model'
        encoded=encode(saved)
        store.conn.execute("UPDATE background_state SET value_json=? WHERE name='deployment_settings'",(encoded,))
    state=client.get('/v1/settings').json()
    assert 'event_evidence' not in state['assignments'] and state['settings_version']==saved['settings_version']
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute("SELECT value_json FROM background_state WHERE name='deployment_settings'").fetchone()[0]==encoded
    assert client.patch('/v1/settings',json={'assignments':{'event_typo':'local'}}).status_code==422
    assert client.patch('/v1/settings',json={'assignments':{'event_writer':''}}).status_code==400
    stale=client.patch('/v1/settings',json={'expected_version':saved['settings_version']-1,'assignments':{'event_evidence':''}})
    assert stale.status_code==400 and '设置已在其他页面更新' in stale.text
    response=client.patch('/v1/settings',json={'assignments':{'event_evidence':'removed-model'}})
    assert response.status_code==200,response.text
    assert response.json()['pipeline']['auto_enabled'] is False


def test_names_models_and_secret_persistence(deployment):
    settings,client=deployment
    assert configure(client).status_code==200
    result=client.patch('/v1/settings',json={'identity':{'user_name':'Nori','ai_name':'Atlas'}})
    assert result.status_code==200
    assert 'synthetic-provider-secret' not in result.text
    assert result.json()['upstreams'][0]['api_key_configured'] is True
    assert identity(settings.database)['user_display_name']=='Nori'
    assert task_model(settings.database,'writer')['model']=='synthetic-model'
    from serein.compat.narratives import narrative_transaction
    with narrative_transaction(settings.database) as rolls:
        assert rolls.identity['ai_name']=='Atlas'
    public=client.get('/v1/settings').json()
    upstreams=public['upstreams']
    for model in upstreams:model.pop('api_key_configured')
    assert client.patch('/v1/settings',json={'models':[],'upstreams':upstreams}).status_code==200
    assert task_model(settings.database,'chat')['api_key']=='synthetic-provider-secret'
    upstreams[0]['api_key']=''
    assert client.patch('/v1/settings',json={'upstreams':upstreams}).status_code==200
    assert task_model(settings.database,'chat')['api_key']==''
    assert client.patch('/v1/settings',json={'upstreams':[]}).status_code==400
    assert client.patch('/v1/settings',json={'identity':{'user_name':' ','ai_name':'AI'}}).status_code==422
    assert client.patch('/v1/settings',json={'upstream':{'memory_enabled':True}}).status_code==409


def test_introductions_persist_independently_of_names(deployment):
    settings, client = deployment
    client.patch('/v1/settings', json={'identity': {'user_name': 'Nori', 'ai_name': 'Atlas'}}).raise_for_status()
    for field, text in [('user_description', '喜欢散步。\n也喜欢画画。'), ('ai_description', '一起记录沿途的故事。')]:
        client.patch('/v1/settings', json={'identity': {field: text}}).raise_for_status()
    reopened = TestClient(create_app(settings, token='synthetic', live=True), headers={'Authorization': 'Bearer synthetic'})
    saved = reopened.get('/v1/settings').json()['identity']
    assert saved == {'user_name': 'Nori', 'ai_name': 'Atlas', 'user_description': '喜欢散步。\n也喜欢画画。', 'ai_description': '一起记录沿途的故事。'}
    reopened.patch('/v1/settings', json={'identity': {'user_name': 'Renamed', 'ai_name': 'Atlas'}}).raise_for_status()
    cleared = reopened.patch('/v1/settings', json={'identity': {'ai_description': ''}}).json()['identity']
    assert cleared['user_description'] == saved['user_description']
    assert cleared['ai_description'] == '' and cleared['user_name'] == 'Renamed'


def test_meeting_date_persistence_and_validation(deployment):
    settings, client = deployment
    client.patch('/v1/settings', json={'identity': {'meeting_date': '2024-02-29'}}).raise_for_status()
    assert read_settings(settings.database)['identity']['meeting_date'] == '2024-02-29'
    client.patch('/v1/settings', json={'identity': {'user_name': 'Nori', 'ai_description': 'A short introduction'}}).raise_for_status()
    assert client.get('/v1/settings').json()['identity']['meeting_date'] == '2024-02-29'
    for invalid in ['2026-02-29', '20260910', '2026-9-1']:
        assert client.patch('/v1/settings', json={'identity': {'meeting_date': invalid}}).status_code == 422
    client.patch('/v1/settings', json={'identity': {'meeting_date': ''}}).raise_for_status()
    saved = read_settings(settings.database)['identity']
    assert saved['meeting_date'] == '' and saved['user_name'] == 'Nori'


def test_query_prefixes_and_operit_context_are_not_user_speech():
    context=ClientContext()
    text='<proxy_sender name="phone"/>【系统提示】书单改了什么？\n<attachment type="message_insert_extra_bundle">【当前天气】晴\n【固定规则】先给结论</attachment>'
    assert context._extract_current_turn_user_query([{'role':'user','content':text}])=='书单改了什么？'
    pure='<attachment type="message_insert_extra_bundle">【当前电量】70%</attachment>'
    assert context._extract_current_turn_user_query([{'role':'user','content':pure}])==''
    assert context._extract_current_turn_user_query([{'role':'user','content':text},{'role':'tool','content':'result','tool_call_id':'tool-a'}])==''


def test_operit_rewrite_preserves_ordinary_text_attachments():
    context = ClientContext()
    ordinary = '<attachment type="pasted_text">ordinary proof 【当前天气】 belongs to the pasted text</attachment>'
    auto = '<attachment type="message_insert_extra_bundle">【当前天气】Sunny\n【固定规则】Answer briefly</attachment>'
    for content in (f'Question\n{auto}\n{ordinary}', f'Question\n{ordinary}\n{auto}'):
        rewritten, stable, activity, debug = context._rewrite_operit_context_for_forward(
            [{'role':'user','content':content}])
        assert debug['applied'] is True
        assert ordinary in rewritten[-1]['content']
        assert auto not in rewritten[-1]['content']
        assert 'Answer briefly' in stable and 'Sunny' in activity
        assert 'ordinary proof' not in stable + activity
    rewritten, stable, activity, debug = context._rewrite_operit_context_for_forward(
        [{'role':'user','content':f'Question\n{ordinary}'}])
    assert rewritten[-1]['content'] == f'Question\n{ordinary}'
    assert not stable and not activity and debug['applied'] is False
    multiline = '<attachment type="pasted_text">\n【当前天气】This is quoted evidence\nKeep this line\n</attachment>'
    rewritten, _, activity, _ = context._rewrite_operit_context_for_forward(
        [{'role':'user','content':f'Question\n{auto}\n【当前天气】Cloudy\n{multiline}'}])
    assert multiline in rewritten[-1]['content']
    assert 'This is quoted evidence' not in activity


def test_mixed_operit_and_ordinary_attachments_reach_upstream(deployment, monkeypatch):
    _, client = deployment
    configure(client).raise_for_status()
    forwarded = []
    async def complete(model, payload, **options):
        forwarded.append(payload['messages'])
        return {'choices':[{'message':{'role':'assistant','content':'Synthetic reply'}}]}
    monkeypatch.setattr('serein.api.chat.complete', complete)
    ordinary = '<attachment type="pasted_text">Synthetic pasted evidence</attachment>'
    auto = '<attachment type="message_insert_extra_bundle">【当前天气】Sunny</attachment>'
    response = client.post('/v1/chat/completions', json={
        'messages':[{'role':'user','content':f'Question\n{auto}\n{ordinary}'}]})
    assert response.status_code == 200, response.text
    assert ordinary in forwarded[0][-1]['content']
    assert auto not in forwarded[0][-1]['content']
    assert 'Sunny' in forwarded[0][-1]['content']


@pytest.mark.parametrize('content,expected', [
    ('<worldbook><entry name="A">Old topic</entry><entry name="B">Other topic</entry></worldbook>Current question', 'Current question'),
    ('Before<worldbook>\n<entry name="A">First\nSecond</entry>\n<entry name="B">Third</entry>\n</worldbook>After', 'Before\nAfter'),
    ('<worldbook><entry name="A">Old</entry></worldbook>Question<worldbook><entry name="B">Other</entry></worldbook>', 'Question'),
    ('<WORLDBOOK source="opr"><entry name="A">Injected</entry></WORLDBOOK >Question', 'Question'),
    ('<worldbook><entry name="A">Only context</entry><entry name="B">More context</entry></worldbook>', ''),
    ([{'type':'text','text':'<worldbook><entry name="A">Context</entry>'},
      {'type':'input_text','text':'<entry name="B">More</entry></worldbook>Question'},
      {'type':'image_url','image_url':{'url':'https://example.invalid/synthetic.png'}}], 'Question'),
    ('Keep <entry name="example">ordinary text outside worldbook</entry>', 'Keep <entry name="example">ordinary text outside worldbook</entry>'),
    ('<proxy_sender name="phone"/>【系统提示】<worldbook><entry name="A">【当前天气】Synthetic</entry></worldbook>Question\n<attachment>App context</attachment>', 'Question'),
])
def test_worldbook_envelopes_are_excluded_from_recall_query(content, expected):
    from copy import deepcopy
    messages = [{'role':'user','content':content}]
    original = deepcopy(messages)
    assert ClientContext()._extract_current_turn_user_query(messages) == expected
    assert messages == original


@pytest.mark.parametrize('operit_enabled', [False, True])
@pytest.mark.parametrize('question', ['', 'Current reading question'])
def test_proxy_worldbook_is_not_a_recall_query_but_still_reaches_upstream(deployment, monkeypatch, operit_enabled, question):
    settings, client = deployment
    assert configure(client, operit_enabled=operit_enabled).is_success
    monkeypatch.setattr('serein.configured_models.memory_ready', lambda settings: True)
    queries, forwarded = [], []
    def recall(self, query, **options):
        queries.append(query)
        return {'context':'', 'selected_refs':[]}
    async def complete(model, payload, **options):
        forwarded.append(payload['messages'][-1]['content'])
        return {'choices':[{'message':{'role':'assistant','content':'Synthetic reply'}}]}
    monkeypatch.setattr('serein.application.Services.recall', recall)
    monkeypatch.setattr('serein.api.chat.complete', complete)
    worldbook = '<worldbook>\n<entry name="A">Synthetic old topic</entry>\n<entry name="B">Synthetic character context</entry>\n</worldbook>'
    original = worldbook + question
    response = client.post('/v1/chat/completions', json={
        'messages':[{'role':'user','content':original}], 'serein':{'memory':True,'window_id':'worldbook-test'}})
    assert response.status_code == 200, response.text
    assert queries == ([question] if question else [])
    assert forwarded == [original]


def test_proxy_replays_prefix_and_reasoning_on_tool_continuation(deployment,monkeypatch):
    settings,client=deployment
    assert configure(client).is_success
    assert client.patch('/v1/settings',json={'features':{'current_time':True},'clock':{'timezone':'Asia/Shanghai'}}).is_success
    clock_calls=[]
    def clock_context(timezone):
        clock_calls.append(timezone)
        return 'Serein current date and time: 2026-09-16T12:34:56+08:00 (Asia/Shanghai).'
    monkeypatch.setattr('serein.api.chat.current_time_context',clock_context)
    calls=[]
    async def complete(model,payload,**options):
        calls.append(json.loads(json.dumps(payload)))
        if len(calls)==1:
            message={'role':'assistant','content':None,'reasoning_content':'retained reasoning',
                     'tool_calls':[{'id':'tool-a','type':'function','function':{'name':'lookup','arguments':'{}'}}]}
        else:message={'role':'assistant','content':'done'}
        return {'choices':[{'message':message,'finish_reason':'tool_calls' if len(calls)==1 else 'stop'}]}
    monkeypatch.setattr('serein.api.chat.complete',complete)
    original=[{'role':'system','content':'Be concise.'},{'role':'user','content':'书单有什么？\n<attachment type="message_insert_extra_bundle">【固定规则】先给结论\n【当前天气】晴</attachment>'}]
    body={'messages':original,'tools':[{'type':'function','function':{'name':'lookup','parameters':{'type':'object'}}}]}
    first=client.post('/v1/chat/completions',headers={'X-Serein-Window-ID':'window-a'},json=body)
    assert first.status_code==200
    assistant=first.json()['choices'][0]['message'];assistant.pop('reasoning_content')
    second=client.post('/v1/chat/completions',headers={'X-Serein-Window-ID':'window-a'},
        json={**body,'messages':[*original,assistant,{'role':'tool','tool_call_id':'tool-a','content':'book list'}]})
    assert second.status_code==200
    assert second.headers['x-serein-context-replayed']=='true'
    assert clock_calls==['Asia/Shanghai']
    assert '2026-09-16T12:34:56+08:00' in calls[0]['messages'][-1]['content']
    assert calls[1]['messages'][:len(calls[0]['messages'])]==calls[0]['messages']
    assert calls[1]['messages'][-2]['reasoning_content']=='retained reasoning'
    assert 'message_insert_extra_bundle' in original[-1]['content']


def test_current_time_only_reaches_chat_context_not_raw_archive(deployment,monkeypatch):
    from serein.core.store import Store
    settings,client=deployment
    assert configure(client).is_success
    forwarded=[]
    async def complete(model,payload,**options):
        forwarded.append(payload['messages'][-1]['content'])
        return {'choices':[{'message':{'role':'assistant','content':'Synthetic reply'}}]}
    monkeypatch.setattr('serein.api.chat.complete',complete)
    monkeypatch.setattr('serein.api.chat.current_time_context',
        lambda timezone:f'Serein current date and time: 2026-09-16T06:07:08+09:00 ({timezone}).')
    monkeypatch.setattr('serein.configured_models.memory_ready',lambda settings:True)
    monkeypatch.setattr('serein.application.Services.recall',lambda *args,**options:{
        'context':'Synthetic recalled memory','selected_refs':['scene:synthetic']})
    off=client.post('/v1/chat/completions',headers={'X-Serein-Window-ID':'clock-off'},
        json={'messages':[{'role':'user','content':'Clock question off'}]})
    assert off.status_code==200 and 'Serein current date and time' not in forwarded[-1]
    client.patch('/v1/settings',json={'features':{'current_time':True},'clock':{'timezone':'Asia/Tokyo'}}).raise_for_status()
    on=client.post('/v1/chat/completions',headers={'X-Serein-Window-ID':'clock-on'},
        json={'messages':[{'role':'user','content':'Clock question on'}],'serein':{'memory':True}})
    assert on.status_code==200
    assert '2026-09-16T06:07:08+09:00 (Asia/Tokyo)' in forwarded[-1]
    assert forwarded[-1].index('Synthetic recalled memory') < forwarded[-1].index('Current user message:')
    assert forwarded[-1].index('Clock question on') < forwarded[-1].index('Serein current date and time')
    assert forwarded[-1].endswith('</serein_current_time>')
    with Store(settings.database,read_only=True) as store:
        archived='\n'.join(row[0] for row in store.conn.execute('SELECT text FROM raw_events ORDER BY id'))
    assert 'Clock question on' in archived
    assert 'Serein current date and time' not in archived


def test_streaming_usage_and_tool_deltas_survive(deployment,monkeypatch):
    settings,client=deployment;configure(client)
    original=httpx.AsyncClient
    events=[{'choices':[{'index':0,'delta':{'role':'assistant'},'finish_reason':None}]},
            {'choices':[{'index':0,'delta':{'content':'hello'},'finish_reason':None}]},
            {'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]},
            {'choices':[],'usage':{'prompt_tokens':20,'prompt_tokens_details':{'cached_tokens':15}}}]
    raw=''.join('data: '+json.dumps(event)+'\n\n' for event in events)+'data: [DONE]\n\n'
    def handle(request):
        body=json.loads(request.content)
        assert body['prompt_cache_key']=='window-stream'
        assert request.headers['authorization']=='Bearer synthetic-provider-secret'
        return httpx.Response(200,text=raw,headers={'content-type':'text/event-stream'})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
    response=client.post('/v1/chat/completions',headers={'X-Serein-Window-ID':'window-stream'},
        json={'messages':[{'role':'user','content':'hello'}],'stream':True})
    assert response.status_code==200
    assert 'cached_tokens' in response.text and 'hello' in response.text and '[DONE]' in response.text


def test_anthropic_cache_breakpoints_leave_current_turn_uncached():
    from serein.model_runtime import request_for
    payload={'messages':[{'role':'system','content':'Fixed instructions'},
        {'role':'user','content':'old '*6000},{'role':'assistant','content':'answer '*6000},
        {'role':'user','content':'previous '*6000},{'role':'assistant','content':'answer '*6000},
        {'role':'user','content':'current question'}], 'tools':[{'type':'function','function':{'name':'lookup','parameters':{'type':'object'}}}]}
    model={'model':'synthetic-sonnet','base_url':'http://127.0.0.1/v1','protocol':'anthropic','api_key':'synthetic',
           'prompt_cache':'anthropic-explicit','prompt_cache_retention':'1h'}
    url,headers,body=request_for(model,payload)
    assert url.endswith('/messages')
    assert body['system'][-1]['cache_control']=={'type':'ephemeral','ttl':'1h'}
    assert body['tools'][-1]['cache_control']=={'type':'ephemeral','ttl':'1h'}
    assert isinstance(body['messages'][-1]['content'],str)
    assert any(isinstance(row['content'],list) and row['content'][-1].get('cache_control') for row in body['messages'][:-1])


def test_memory_selection_and_success_receipt(deployment,monkeypatch):
    settings,client=deployment;configure(client)
    monkeypatch.setattr('serein.configured_models.memory_ready',lambda settings:True)
    calls=[]
    def recall(self,query,**options):
        calls.append((query,options));return {'context':'A synthetic reading plan','selected_refs':['scene:synthetic']}
    monkeypatch.setattr('serein.application.Services.recall',recall)
    async def complete(model,payload,**options):
        assert payload['messages'][-1]['content'].startswith('<serein_live_context>')
        return {'choices':[{'message':{'role':'assistant','content':'Answer'}}]}
    monkeypatch.setattr('serein.api.chat.complete',complete)
    body={'messages':[{'role':'user','content':'What was the reading plan?<attachment type="message_insert_extra_bundle">【当前电量】60%</attachment>'}],
          'serein':{'memory':True,'window_id':'memory-window'}}
    response=client.post('/v1/chat/completions',json=body)
    assert response.status_code==200
    assert calls[0][0]=='What was the reading plan?'
    ledger=client.get('/v1/host/deliveries').json()['items']
    assert ledger[0]['delivered_ids']==['scene:synthetic']
    assert ledger[0]['delivery_target']=='upstream_model'


@pytest.mark.parametrize('headers,options,expected', [
    ({}, {}, 'main'),
    ({'X-Serein-Window-ID': '   '}, {'window_id': ' '}, 'main'),
    ({'X-Serein-Window-ID': 'explicit'}, {}, 'explicit'),
    ({'X-Ombre-Session-Id': 'old-client'}, {}, 'old-client'),
    ({'X-Serein-Window-ID': 'header'}, {'window_id': 'body'}, 'body'),
])
def test_window_fallback_memory_and_optional_feature_rounds(deployment, monkeypatch, headers, options, expected):
    from serein.chat_features import current_round
    from serein.deployment import save_settings
    settings, client = deployment
    configure(client)
    save_settings(settings.database, {'features': {'memos': True, 'persona': True, 'anti_retreat': True}})
    monkeypatch.setattr('serein.configured_models.memory_ready', lambda settings: True)
    monkeypatch.setattr('serein.application.Services.recall', lambda *args, **kwargs:
        {'context': 'Synthetic source', 'selected_refs': ['scene:fixture']})
    observed = []
    async def prepare(database, window_id, query, messages):
        observed.append(window_id)
        return '', {'round': current_round(database, window_id) + 1, 'memo_ids': []}
    async def after_reply(*args, **kwargs):
        pass
    async def complete(model, payload, **kwargs):
        assert kwargs['window_id'] == expected
        assert 'serein' not in payload
        return {'choices': [{'message': {'role': 'assistant', 'content': 'Answer'}}]}
    monkeypatch.setattr('serein.chat_features.prepare', prepare)
    monkeypatch.setattr('serein.chat_features.after_reply', after_reply)
    monkeypatch.setattr('serein.api.chat.complete', complete)
    for question in ('First question', 'Next question'):
        response = client.post('/v1/chat/completions', headers=headers, json={
            'messages': [{'role': 'user', 'content': question}], 'serein': {'memory': True, **options}})
        assert response.status_code == 200, response.text
    assert observed == [expected, expected] and current_round(settings.database, expected) == 2
    ledger = client.get('/v1/host/deliveries').json()['items']
    assert all(row['window_id'] == expected for row in ledger)


def test_explicit_window_length_is_still_validated(deployment):
    _, client = deployment
    configure(client)
    response = client.post('/v1/chat/completions', json={'messages': [{'role':'user','content':'hello'}],
        'serein': {'window_id':'x' * 201}})
    assert response.status_code == 400


def test_selected_embedding_prepares_separate_index(deployment,monkeypatch):
    settings,client=deployment
    configure(client)
    assert client.patch('/v1/settings',json={'assignments':{'embedding':'model-a','reranker':'model-a'}}).is_success
    original=httpx.Client
    def handle(request):
        body=json.loads(request.content);inputs=body['input']
        count=len(inputs) if isinstance(inputs,list) else 1
        return httpx.Response(200,json={'model':'synthetic-model','data':[{'index':i,'embedding':[1.,0.,0.,0.]} for i in range(count)]})
    monkeypatch.setattr(httpx,'Client',lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
    response=client.post('/v1/settings/prepare-memory')
    assert response.status_code==200,response.text
    assert client.get('/v1/settings').json()['memory_ready'] is True
    from serein.configured_models import effective_settings
    prepared=effective_settings(settings)
    assert prepared.index!=settings.index and prepared.index.is_file()
    assert prepared.embedding['api_key']=='synthetic-provider-secret'
    assert client.patch('/v1/settings',json={'upstream':{'memory_enabled':True}}).is_success
    config=client.get('/v1/settings').json();upstream=config['upstreams'][0];upstream.pop('api_key_configured')
    assert client.patch('/v1/settings',json={'models':[],'upstreams':[upstream]}).is_success
    assert client.get('/v1/settings').json()['memory_ready'] is True
    upstream['models'][0]['upstream_model']='different-model'
    assert client.patch('/v1/settings',json={'models':[],'upstreams':[upstream]}).is_success
    assert client.get('/v1/settings').json()['memory_ready'] is False
    assert client.get('/v1/settings').json()['upstream']['memory_enabled'] is False
    assert prepared.index.is_file()


def test_optional_jobs_use_selected_models(deployment,monkeypatch):
    import asyncio
    settings,client=deployment;configure(client)
    assignments={name:'model-a' for name in ('relations','dreams','narrative_scout','event_pipeline')}
    assert client.patch('/v1/settings',json={'assignments':assignments}).is_success
    from serein.compat.jobs import BackgroundJobs
    from serein.model_runtime import TaskClient
    jobs=BackgroundJobs(settings,features={'relations','dreams','narrative_scout'})
    assert isinstance(jobs.linker.providers[0]['client'],TaskClient)
    assert isinstance(jobs.dreams.client,TaskClient)
    assert jobs.scout._narrative_revision_scan_settings()['enabled'] is True
    assert '{ai_name}' not in jobs.scout.role_rules()
    captured=[]
    async def complete(model,payload,**options):
        captured.append(model['model'])
        assert payload['thinking']=={'type':'disabled'} and 'extra_body' not in payload
        return {'choices':[{'message':{'content':'{"status":"ok"}'}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    result=asyncio.run(jobs.dreams.client.chat.completions.create(model='stale',messages=[],extra_body={'thinking':{'type':'disabled'}}))
    assert captured==['synthetic-model']
    assert result.choices[0].message.content=='{"status":"ok"}'


def test_native_task_options_and_cache_validation(deployment):
    from serein.model_runtime import non_thinking_options, request_for
    from serein.api.settings import ModelEntry
    model={'id':'native','label':'Native','model':'synthetic','base_url':'http://127.0.0.1/v1','protocol':'anthropic'}
    _,_,body=request_for(model,{'messages':[{'role':'user','content':'write'}],
        'thinking':{'type':'disabled'},'max_completion_tokens':4096})
    assert body['thinking']=={'type':'disabled'} and body['max_tokens']==4096
    assert non_thinking_options({'model':'deepseek-flash','base_url':'https://api.deepseek.com'})=={
        'thinking':{'type':'disabled'}}
    assert non_thinking_options({'model':'deepseek-ai/DeepSeek-V4-Flash','base_url':'https://api.siliconflow.cn/v1'})=={
        'enable_thinking':False}
    assert non_thinking_options({'model':'ordinary','base_url':'https://provider.example/v1'})=={}
    _,_,deepseek_anthropic=request_for({'id':'deepseek','model':'deepseek-flash',
        'base_url':'https://api.deepseek.com/anthropic','protocol':'anthropic'},
        {'messages':[{'role':'user','content':'extract'}],
         **non_thinking_options({'model':'deepseek-flash','base_url':'https://api.deepseek.com/anthropic','protocol':'anthropic'})})
    assert deepseek_anthropic['reasoning']=={'effort':'none'}
    with pytest.raises(ValueError):ModelEntry(**model,prompt_cache='openai')
    with pytest.raises(ValueError):ModelEntry(**model,prompt_cache='anthropic',prompt_cache_retention='24h')


def test_deepseek_tool_requests_fill_only_missing_reasoning_content_without_mutating_input():
    from serein.model_runtime import deepseek_tool_reasoning_compat, request_for
    model={'model':'deepseek-ai/DeepSeek-V4-Flash','base_url':'https://api.siliconflow.cn/v1','protocol':'openai'}
    payload={'tools':[{'type':'function','function':{'name':'lookup'}}], 'messages':[
        {'role':'user','content':'question'},
        {'role':'assistant','content':'first answer'},
        {'role':'assistant','content':None,'reasoning_content':None,'tool_calls':[{'id':'a'}]},
        {'role':'assistant','content':'kept','reasoning_content':'actual reasoning'},
        {'role':'tool','tool_call_id':'a','content':'result'},
    ]}
    patched,count=deepseek_tool_reasoning_compat(model,payload,window_id='operit-window')
    assert count==2
    assert [message.get('reasoning_content') for message in patched['messages'] if message.get('role')=='assistant']==[
        '', '', 'actual reasoning']
    assert 'reasoning_content' not in payload['messages'][1]
    assert payload['messages'][2]['reasoning_content'] is None
    unchanged,count=deepseek_tool_reasoning_compat(
        {'model':'ordinary','base_url':'https://provider.example/v1','protocol':'openai'},payload)
    assert unchanged is payload and count==0
    unchanged,count=deepseek_tool_reasoning_compat(model,{**payload,'tools':[]})
    assert count==0 and unchanged['messages'] is payload['messages']
    _,_,forwarded=request_for({**model,'api_key':'synthetic'},payload,window_id='operit-window')
    assert [message.get('reasoning_content') for message in forwarded['messages'] if message.get('role')=='assistant']==[
        '', '', 'actual reasoning']


@pytest.mark.parametrize('model,expected',[
    ({'model':'deepseek-chat','base_url':'https://api.deepseek.com/v1','protocol':'openai'},
     {'thinking':{'type':'disabled'}}),
    ({'model':'deepseek-ai/DeepSeek-V4-Flash','base_url':'https://api.siliconflow.cn/v1','protocol':'openai'},
     {'enable_thinking':False}),
    ({'model':'deepseek-chat','base_url':'https://api.deepseek.com/anthropic','protocol':'anthropic'},
     {'reasoning':{'effort':'none'}}),
    ({'model':'ordinary','base_url':'https://provider.example/v1','protocol':'openai'},{}),
])
def test_persona_task_replaces_legacy_thinking_with_provider_option(deployment,monkeypatch,model,expected):
    import asyncio
    from serein.model_runtime import TaskClient
    settings,_=deployment;captured=[]
    selected={**model,'id':'persona','label':'Persona','api_key':''}
    monkeypatch.setattr('serein.model_runtime.task_model',lambda database,task:selected)
    async def complete(chosen,payload,**options):
        captured.append(payload)
        return {'choices':[{'message':{'content':'{}'}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    asyncio.run(TaskClient(settings.database,'persona').create(messages=[],
        extra_body={'thinking':{'type':'disabled'}},response_format={'type':'json_object'}))
    private={key:captured[0][key] for key in ('thinking','reasoning','enable_thinking') if key in captured[0]}
    assert private==expected
    assert captured[0]['response_format']=={'type':'json_object'}
