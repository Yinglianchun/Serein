import json

import httpx
import pytest
from fastapi.testclient import TestClient
from serein.api.http import create_app
from serein.core.store import Store
from test_chat_image_transport import install_transport, PNG
from test_public_settings import configure, deployment


TOOLS = [{'type':'function','name':'find_memory','parameters':{'type':'object','properties':{}}}]
REASONING = {'type':'reasoning','id':'rs_1','encrypted_content':'opaque-data','summary':[]}
CALL = {'type':'function_call','id':'fc_1','call_id':'call_1','name':'find_memory','arguments':'{}'}
TOOL_RESULT = {'type':'function_call_output','call_id':'call_1','output':'Synthetic tool output'}
FINAL = {'type':'message','role':'assistant','content':[{'type':'output_text','text':'Final answer','annotations':[]}]}


def result(items, identifier='resp_final'):
    return {'object':'response','id':identifier,'status':'completed','output':items}


def originals(settings):
    with Store(settings.database,read_only=True) as store:
        return [tuple(row) for row in store.conn.execute('SELECT role,text FROM raw_events ORDER BY id')]


@pytest.mark.parametrize('by_id',[False,True])
def test_native_reasoning_tools_recall_and_archive(deployment, monkeypatch, by_id):
    settings,client=deployment
    configure(client).raise_for_status()
    recalls=[]
    def recall(self,query,**kwargs):
        recalls.append(query)
        return {'context':'Synthetic recall','selected_refs':['scene:test']}
    monkeypatch.setattr('serein.configured_models.memory_ready',lambda _:True)
    monkeypatch.setattr('serein.application.Services.recall',recall)
    calls=[]
    def upstream(req):
        assert str(req.url)=='http://127.0.0.1:9999/v1/responses'
        assert req.headers['authorization']=='Bearer synthetic-provider-secret'
        payload=json.loads(req.content)
        assert payload['model']=='synthetic-model' and payload['reasoning']=={'effort':'low'}
        assert payload['tools']==TOOLS and 'messages' not in payload and 'serein' not in payload
        assert payload['instructions']=='Caller instructions' and payload['store'] is by_id
        calls.append(payload)
        return httpx.Response(200,json=result([REASONING,CALL],'resp_one') if len(calls)==1 else result([FINAL]))
    install_transport(monkeypatch,upstream)
    options={'tools':TOOLS,'instructions':'Caller instructions','store':by_id,'reasoning':{'effort':'low'},
             'serein':{'window_id':'demo','memory':True}}
    question={'role':'user','content':[{'type':'input_text','text':'Remember lunch?'},
                                      {'type':'input_image','image_url':PNG,'detail':'low'}]}
    first=client.post('/v1/responses',json={**options,'input':[question]})
    assert first.json()==result([REASONING,CALL],'resp_one')
    assert originals(settings)==[('user','Remember lunch?')]
    sent=calls[0]['input'][0]['content']
    assert sent[-1]==question['content'][-1] and 'Synthetic recall' in str(sent)
    if by_id:
        client=TestClient(create_app(settings,token='synthetic',live=True),headers={'Authorization':'Bearer synthetic'})
        follow={'input':[TOOL_RESULT],'previous_response_id':'resp_one'}
    else:
        follow={'input':[question,REASONING,CALL,TOOL_RESULT]}
    second=client.post('/v1/responses',json={**options,**follow})
    assert second.json()==result([FINAL])
    if by_id:
        assert calls[1]['input']==[TOOL_RESULT] and calls[1]['previous_response_id']=='resp_one'
    else:
        assert calls[1]['input']==calls[0]['input']+[REASONING,CALL,TOOL_RESULT]
    assert recalls==['Remember lunch?']
    assert originals(settings)==[('user','Remember lunch?'),('assistant','Final answer')]
    assert second.headers['x-serein-observation-id']==''


@pytest.mark.parametrize('terminal',['completed','failed','incomplete','disconnect'])
def test_native_stream_preserves_events_and_only_archives_success(deployment,monkeypatch,terminal):
    settings,client=deployment
    configure(client)
    events=[{'type':'response.output_text.delta','delta':'Final answer'}]
    if terminal!='disconnect':
        events.append({'type':'response.'+terminal,'response':{**result([FINAL]),'status':terminal}})
    wire=''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events)
    def upstream(req):
        payload=json.loads(req.content)
        assert req.url.path=='/v1/responses' and payload['input']=='Question' and payload['stream']
        return httpx.Response(200,text=wire,headers={'content-type':'text/event-stream'})
    clients=install_transport(monkeypatch,upstream)
    reply=client.post('/v1/responses',json={'input':'Question','stream':True})
    assert reply.status_code==200
    if terminal=='disconnect':
        assert 'incomplete_stream' in reply.text
    else:
        assert reply.text==wire
    assert '[DONE]' not in reply.text and all(c.is_closed for c in clients)
    assert originals(settings)==([('user','Question'),('assistant','Final answer')] if terminal=='completed' else [])


def test_responses_auth_and_upstream_rejection(deployment,monkeypatch):
    settings,client=deployment
    configure(client)
    def reject(_):
        return httpx.Response(400,json={'error':{'message':'private upstream text'}})
    install_transport(monkeypatch,reject)
    for stream in (True,False):
        reply=client.post('/v1/responses',json={'input':'Question','stream':stream})
        assert reply.status_code==400 and 'private upstream text' not in reply.text
        assert reply.json()['error']['code']=='upstream_invalid_request'
    client.headers.clear()
    assert client.post('/v1/responses',json={'input':'Question'}).status_code==401
    assert originals(settings)==[]


def test_recent_tool_result_owns_answer_in_replayed_history(deployment,monkeypatch):
    settings,client=deployment
    configure(client)
    replies=iter([result([CALL],'resp_a'),result([FINAL]),result([{**CALL,'call_id':'call_new'}],'resp_b'),result([FINAL])])
    install_transport(monkeypatch,lambda _:httpx.Response(200,json=next(replies)))
    items=[{'role':'user','content':'First'}]
    def send(items,**options):
        return client.post('/v1/responses',json={'conversation':{'id':'conv_one'},'input':items,**options})
    send(items).raise_for_status()
    send([TOOL_RESULT]).raise_for_status()
    send([{'role':'user','content':'Second'}]).raise_for_status()
    complete_history=items+[CALL,TOOL_RESULT,FINAL,{'role':'user','content':'Second'},
                            {**CALL,'call_id':'call_new'},{**TOOL_RESULT,'call_id':'call_new'}]
    send(complete_history).raise_for_status()
    assert originals(settings)==[('user','First'),('assistant','Final answer'),('user','Second'),('assistant','Final answer')]
    with Store(settings.database,read_only=True) as store:
        assert [r[0] for r in store.conn.execute('SELECT DISTINCT session_id FROM raw_events')]==['conv_one']


def test_stateful_repeated_question_deduplicates_only_retries(deployment,monkeypatch):
    settings,client=deployment
    configure(client)
    install_transport(monkeypatch,lambda _:httpx.Response(200,json=result([FINAL])))
    for parent in ('resp_a','resp_a','resp_b'):
        client.post('/v1/responses',json={'previous_response_id':parent,'input':'Again?'}).raise_for_status()
    assert originals(settings)==[('user','Again?'),('assistant','Final answer')]*2


def test_official_sdk_consumes_native_json_and_sse(deployment,monkeypatch):
    sdk=pytest.importorskip('openai')
    _,client=deployment
    configure(client)
    answer=result([FINAL])
    def upstream(req):
        payload=json.loads(req.content)
        assert req.url.path=='/v1/responses' and payload['reasoning']=={'effort':'low'} and payload['tools']==TOOLS
        if payload['stream']:
            event={'type':'response.completed','sequence_number':0,'response':answer}
            return httpx.Response(200,text='event: response.completed\ndata: '+json.dumps(event)+'\n\n',
                                  headers={'content-type':'text/event-stream'})
        return httpx.Response(200,json=answer)
    install_transport(monkeypatch,upstream)
    with sdk.OpenAI(api_key='synthetic',base_url='http://testserver/v1',http_client=client,max_retries=0) as api:
        options={'model':'model-a','input':'Hello','reasoning':{'effort':'low'},'tools':TOOLS}
        assert api.responses.create(**options).output_text=='Final answer'
        with api.responses.create(**options,stream=True) as stream:
            event,=list(stream)
            assert event.type=='response.completed' and event.response.output_text=='Final answer'
