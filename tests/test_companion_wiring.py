import asyncio
import json
import httpx
import pytest
from test_public_settings import deployment
from serein.application import Application
from serein.api.mcp import create_server
from serein.chat_features import current_round, prepare
from serein.compat.memo_store import ReminderStore
from serein.core.store import Store, encode


def test_dream_read_is_optional_non_consuming_and_hides_deleted(deployment):
    from serein.compat.dreams import Dreams
    settings, client = deployment
    engine = Dreams(settings)
    engine._write_record({'dream_id':'dream_read_test','generated_at':'2030-01-01T04:00:00+08:00','surfaced':False}, '合成梦境')
    app = Application(settings)
    server = create_server(app)
    assert 'dream_read' not in {tool.name for tool in asyncio.run(server.list_tools())}
    memory_read = server._tool_manager.get_tool('read_memory').fn
    with pytest.raises(ValueError,match='disabled'):memory_read('dream:dream_read_test')
    client.patch('/v1/settings',json={'features':{'dream_read':True}}).raise_for_status()
    catalog = {tool.name:tool for tool in asyncio.run(server.list_tools())}
    assert catalog['dream_read'].annotations.readOnlyHint
    read = app.contributions.tools['dream_read']
    assert read()['items'][0]['dream_id']=='dream_read_test'
    assert 'body' not in read()['items'][0]
    assert read('dream_read_test')['dream']['body']=='合成梦境'
    assert '合成梦境' in memory_read('dream:dream_read_test')
    assert engine.list_records()[0].surfaced is False
    assert read(limit=1,offset=1)['items']==[]
    engine._delete_record(engine.list_records()[0], 'test')
    assert read('dream_read_test')['status']=='not_found'
    client.patch('/v1/settings',json={'features':{'dream_read':False}}).raise_for_status()
    assert 'dream_read' not in {tool.name for tool in asyncio.run(server.list_tools())}
    with pytest.raises(ValueError,match='disabled'):read()


def test_morning_candidate_time_freshness_and_daily_delivery(deployment):
    from datetime import datetime
    from serein.compat.dreams import Dreams
    from serein.chat_features import morning_dream, delivered
    from serein.deployment import read_settings
    settings, client = deployment
    assert not client.get('/v1/settings').json()['features']['dream_morning']
    client.patch('/v1/settings',json={'features':{'dream_morning':True}}).raise_for_status()
    state=read_settings(settings.database)
    engine=Dreams(settings)
    for key,stamp,surfaced in [('old','2029-12-30T04:00:00+08:00',False),('new','2030-01-01T04:00:00+08:00',False),('future','2030-01-02T04:00:00+08:00',False),('used','2030-01-01T05:00:00+08:00',True)]:
        engine._write_record({'dream_id':'dream_'+key,'generated_at':stamp,'surfaced':surfaced}, '合成梦境 '+key)
    assert morning_dream(settings.database,state,datetime.fromisoformat('2030-01-01T03:59:00+08:00'))==('',{})
    text,receipt=morning_dream(settings.database,state,datetime.fromisoformat('2030-01-01T09:00:00+08:00'))
    assert receipt['dream_id']=='dream_new' and '不是事实记忆' in text
    assert not next(r for r in engine.list_records() if r.dream_id=='dream_new').surfaced
    receipt['round']=1
    delivered(settings.database,'first',receipt)
    delivered(settings.database,'first',receipt)
    assert morning_dream(settings.database,state,datetime.fromisoformat('2030-01-01T23:00:00+08:00'))==('',{})
    with Store(settings.database) as store:
        assert store.conn.execute("SELECT COUNT(*) FROM historical_work_events WHERE work_id='dream_new' AND event='surfaced'").fetchone()[0]==1
    assert next(r for r in engine.list_records() if r.dream_id=='dream_new').surfaced
    text,next_receipt=morning_dream(settings.database,state,datetime.fromisoformat('2030-01-02T09:00:00+08:00'))
    assert next_receipt['dream_id']=='dream_future'


def test_morning_without_dream_and_disabled_feature(deployment):
    from datetime import datetime
    from serein.chat_features import morning_dream, delivered
    from serein.deployment import read_settings
    settings,client=deployment
    text,receipt=asyncio.run(prepare(settings.database,'off','早安',[]))
    assert text=='' and 'dream_morning_day' not in receipt
    client.patch('/v1/settings',json={'features':{'dream_morning':True},'clock':{'timezone':'UTC'}}).raise_for_status()
    state=read_settings(settings.database)
    assert morning_dream(settings.database,state,datetime.fromisoformat('2030-01-01T09:00:00+08:00'))==('',{})
    at=datetime.fromisoformat('2030-01-01T09:00:00+00:00')
    text,receipt=morning_dream(settings.database,state,at)
    assert text=='' and receipt=={'dream_morning_day':'2030-01-01'}
    delivered(settings.database,'empty',{**receipt,'round':1})
    assert morning_dream(settings.database,state,at)==('',{})


@pytest.mark.parametrize('ending',['complete','truncated','tool','failure'])
def test_morning_proxy_consumes_only_final_success(deployment,monkeypatch,ending):
    from datetime import datetime
    from serein.compat.dreams import Dreams
    from serein.chat_features import morning_dream
    settings,client=deployment
    configure(client,False)
    client.patch('/v1/settings',json={'features':{'persona':False,'dream_morning':True}}).raise_for_status()
    engine=Dreams(settings)
    engine._write_record({'dream_id':'dream_proxy','generated_at':'2030-01-01T04:00:00+08:00','surfaced':False},'合成晨间梦')
    monkeypatch.setattr('serein.chat_features.morning_dream',lambda db,state:morning_dream(db,state,datetime.fromisoformat('2030-01-01T09:00:00+08:00')))
    calls=[]
    def handle(request):
        body=json.loads(request.content);calls.append(body)
        assert '合成晨间梦' in json.dumps(body,ensure_ascii=False)
        if ending=='failure':return httpx.Response(500,json={'error':{'message':'synthetic'}})
        delta={'content':'合成答复'} if ending!='tool' else {'tool_calls':[{'index':0,'id':'t1','type':'function','function':{'name':'lookup','arguments':'{}'}}]}
        raw='data: '+json.dumps({'choices':[{'index':0,'delta':delta}]})+'\n\n'
        if ending!='truncated':raw+='data: [DONE]\n\n'
        return httpx.Response(200,text=raw,headers={'content-type':'text/event-stream'})
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    body={'messages':[{'role':'user','content':'早安'}],'stream':True}
    response=client.post('/v1/chat/completions',headers={'X-Serein-Window-ID':'dream-window'},json=body)
    assert response.status_code==(502 if ending=='failure' else 200)
    assert engine.list_records()[0].surfaced==(ending=='complete')
    if ending!='complete':
        retry=client.post('/v1/chat/completions',headers={'X-Serein-Window-ID':'dream-window'},json=body)
        assert retry.headers.get('x-serein-context-replayed')=='true' or ending=='failure'
        assert calls[0]['messages']==calls[1]['messages']
    if ending=='tool':
        ending='complete'
        continuation={**body,'messages':[*body['messages'],
            {'role':'assistant','content':None,'tool_calls':[{'id':'t1','type':'function','function':{'name':'lookup','arguments':'{}'}}]},
            {'role':'tool','tool_call_id':'t1','content':'合成工具结果'}]}
        final=client.post('/v1/chat/completions',headers={'X-Serein-Window-ID':'dream-window'},json=continuation)
        assert final.status_code==200 and final.headers['x-serein-context-replayed']=='true'
        assert engine.list_records()[0].surfaced


def configure(client, memos=True):
    client.patch('/v1/settings',json={
        'models':[{'id':'chat','label':'Chat','model':'chat-model','base_url':'http://127.0.0.1:9/v1'},
                  {'id':'persona','label':'Persona','model':'persona-model','base_url':'http://127.0.0.1:9/v1'}],
        'assignments':{'chat':'chat','persona':'persona'},
        'features':{'persona':True,'memos':memos}}).raise_for_status()


def evaluation():
    return {'event_type':'affection','inner_thought':'刚才答得太快了。其实还想再听两句，不过也不用急着追问，等这段话慢慢说完吧。','surface_trigger':'合成对话',
            'mood_label':'warm','affect_delta':{'tenderness':.15,'security':.12},
            'relationship_event':True,'relationship_delta':{'trust':.02},'confidence':.9}


@pytest.mark.parametrize('memos',[False,True])
def test_real_proxy_persona_cadence_context_and_tool_continuation(deployment,monkeypatch,memos):
    settings,client=deployment;configure(client,memos)
    calls=[];evaluations=[]
    tool={'role':'assistant','content':None,'tool_calls':[{'id':'t1','type':'function','function':{'name':'lookup','arguments':'{}'}}]}
    def handle(request):
        body=json.loads(request.content)
        if body['model']=='persona-model':
            assert not {'thinking','reasoning','enable_thinking'} & body.keys()
            evaluations.append(json.loads(body['messages'][1]['content']))
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(evaluation())}}]})
        calls.append(body)
        answer=tool if len(calls)==3 else {'role':'assistant','content':'合成答复'+str(len(calls))}
        return httpx.Response(200,json={'choices':[{'message':answer,'finish_reason':'tool_calls' if len(calls)==3 else 'stop'}]})
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    history=[]
    for number in (1,2):
        history.append({'role':'user','content':'合成问题'+str(number)})
        response=client.post('/v1/chat/completions',headers={'X-Serein-Window-ID':'w'},json={'messages':history})
        assert response.status_code==200,response.text
        history.append(response.json()['choices'][0]['message'])
    assert evaluations==[] and current_round(settings.database,'w')==2
    store=ReminderStore({'serein_database':settings.database})
    if memos:store.create(title='临时备忘',content='合成工具轮备忘',repeat_rule='once',reminder_id='m1')
    history.append({'role':'user','content':'合成问题3'})
    response=client.post('/v1/chat/completions',headers={'X-Serein-Window-ID':'w'},json={'messages':history})
    assert response.status_code==200
    assert current_round(settings.database,'w')==2 and evaluations==[]
    if memos:assert store.get('m1')['reminder_count']==0
    history.extend([response.json()['choices'][0]['message'],{'role':'tool','tool_call_id':'t1','content':'合成工具结果'}])
    response=client.post('/v1/chat/completions',headers={'X-Serein-Window-ID':'w'},json={'messages':history})
    assert response.status_code==200 and response.headers['x-serein-context-replayed']=='true'
    assert current_round(settings.database,'w')==3 and len(evaluations)==1
    assert evaluations[0]['latest_user_message']=='合成问题3'
    assert [turn['user_message'] for turn in evaluations[0]['recent_conversation_turns']]==['合成问题1','合成问题2']
    assert evaluations[0]['assistant_response']=='合成答复4'
    if memos:assert store.get('m1')['reminder_count']==1 and store.get('m1')['status']=='archived'
    if memos:assert 'm1' in json.dumps(calls[-1],ensure_ascii=False)
    retry=client.post('/v1/chat/completions',headers={'X-Serein-Window-ID':'w'},json={'messages':history})
    assert retry.status_code==200 and current_round(settings.database,'w')==3
    if memos:assert store.get('m1')['reminder_count']==1
    state=client.get('/v1/companion/persona?session_id=w').json()
    assert state['session']['inner_thought']=='刚才答得太快了。其实还想再听两句，不过也不用急着追问，等这段话慢慢说完吧。' and state['relationship']['trust']>.51
    assert state['events']
    # Persona remains evaluation and display state; it never enters the next prompt.
    with Store(settings.database) as db,db.transaction():
        db.conn.execute('UPDATE background_state SET value_json=? WHERE name=?',(encode(14),'feature_round:w'))
    text,_=asyncio.run(prepare(settings.database,'w','继续',history))
    assert text=='' and len(evaluations)==1


@pytest.mark.parametrize('ending',['complete','truncated','tool'])
def test_streaming_memo_consumption_and_persona_only_after_final_reply(deployment,monkeypatch,ending):
    settings,client=deployment;configure(client)
    with Store(settings.database) as db,db.transaction():
        db.conn.execute('INSERT INTO background_state VALUES (?,?)',('feature_round:w',encode(2)))
    store=ReminderStore({'serein_database':settings.database})
    store.create(title='流式备忘',content='合成流式提醒',repeat_rule='once',reminder_id='m1')
    evaluations=[]
    def handle(request):
        body=json.loads(request.content)
        if body['model']=='persona-model':
            assert not {'thinking','reasoning','enable_thinking'} & body.keys()
            evaluations.append(body)
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(evaluation())}}]})
        assert '合成流式提醒' in json.dumps(body,ensure_ascii=False)
        delta={'content':'合成流式答案'} if ending!='tool' else {'tool_calls':[{'index':0,'id':'t1','type':'function','function':{'name':'lookup','arguments':'{}'}}]}
        raw='data: '+json.dumps({'choices':[{'index':0,'delta':delta}]})+'\n\n'
        if ending!='truncated':raw+='data: [DONE]\n\n'
        return httpx.Response(200,text=raw,headers={'content-type':'text/event-stream'})
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    result=client.post('/v1/chat/completions',headers={'X-Serein-Window-ID':'w'},json={'messages':[{'role':'user','content':'合成流式问题'}],'stream':True})
    assert result.status_code==200
    success=ending=='complete'
    assert store.get('m1')['reminder_count']==int(success)
    assert len(evaluations)==int(success) and current_round(settings.database,'w')==2+int(success)


def test_memo_tools_expose_schedule_and_use_same_store(deployment):
    settings,client=deployment;configure(client)
    server=create_server(Application(settings))
    catalog={tool.name:tool for tool in asyncio.run(server.list_tools())}
    assert {'start_at','end_at','daily_limit','max_injections','cooldown_minutes'}<=catalog['memo_create'].inputSchema['properties'].keys()
    app=Application(settings);app.refresh_optional()
    tools=app.contributions.tools
    args=dict(title='早晚备忘',content='合成提醒',memo_id='tool-memo',repeat_rule='morning_evening',start_at='2030-01-01',end_at='2030-01-03',max_injections=4)
    row=tools['memo_create'](**args)
    assert row['daily_limit']==2 and row['interval_rounds']==0 and row['source']=='mcp'
    assert tools['memo_create'](**args)['id']==row['id']
    with pytest.raises(Exception):tools['memo_create'](**{**args,'start_at':'2030-01-02'})
    tools['memo_update'](memo_id=row['id'],start_at='2030-01-02',daily_limit=3,content='修改后的合成提醒')
    saved=client.get('/v1/companion/memos').json()['items'][0]
    assert saved['start_at']=='2030-01-02' and saved['daily_limit']==3 and saved['content']=='修改后的合成提醒'
    tools['memo_list']();assert saved['reminder_count']==0
    client.patch('/v1/settings',json={'features':{'memos':False}}).raise_for_status()
    assert not {'memo_create','memo_list','memo_update'}&{tool.name for tool in asyncio.run(server.list_tools())}
