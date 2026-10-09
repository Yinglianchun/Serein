from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from serein.api.http import create_app
from serein import chat_resume
from serein.chat_context import ClientContext
from serein.compat.window_shadows import WindowShadows
from serein.compat.raw_archive import raw_archive
from serein.core import Store
from serein.deployment import save_settings
from test_public_settings import deployment, configure


@pytest.fixture
def chat(deployment, monkeypatch):
    settings, client = deployment
    configure(client)
    save_settings(settings.database, {'features':{'resume':True,'window_shadows':True}})
    WindowShadows(settings.database).write('first', 'Synthetic continuity', content='Full shadow marker '+('long prose '*1800))
    with Store(settings.database) as store:
        store.create('resume-event','event','An event','Last event marker')
        source = store.add_source('synthetic-upstream-key', 'Original evidence marker',
                                  metadata={'message_id':'upstream-98765'})
        store.bind('resume-event', source)
    payloads = []
    async def complete(model, payload, **kwargs):
        payloads.append(deepcopy(payload))
        return {'choices':[{'message':{'role':'assistant','content':'Synthetic reply'}}]}
    monkeypatch.setattr('serein.api.chat.complete', complete)
    return settings, client, payloads


def post(client, messages, **values):
    return client.post('/v1/chat/completions', json={'messages':messages, **values})


def test_mcp_mode_blocks_command_and_does_not_reinject_retained_command_context(chat):
    settings,client,payloads=chat
    history=[{'role':'user','content':'/resume Continue here'}]
    assert post(client,history,window_id='exclusive',memory=False).status_code==200
    assert 'Full shadow marker' in json.dumps(payloads[-1])
    save_settings(settings.database,{'resume':{'mode':'mcp'}})
    before=len(payloads)
    response=post(client,[{'role':'user','content':'/resume'}],window_id='exclusive',memory=False)
    assert response.status_code==409 and len(payloads)==before
    history.extend([{'role':'assistant','content':'Synthetic reply'},{'role':'user','content':'A normal followup'}])
    response=post(client,history,window_id='exclusive',memory=False)
    assert response.status_code==200 and response.headers['X-Serein-Resume']=='none'
    assert 'Full shadow marker' not in json.dumps(payloads[-1])


def test_resume_and_followup_full_pages_persist_and_match_history(chat):
    settings, client, payloads = chat
    messages = [{'role':'user','content':'/resume 接着聊昨天的书吧'}]
    result = post(client, messages)
    assert result.status_code == 200, result.text
    assert result.headers['x-serein-resume']=='loaded'
    sent = payloads[-1]['messages'][-1]['content']
    assert sent.endswith('接着聊昨天的书吧')
    assert sent.count('Full shadow marker') == 1 and sent.count('long prose ')==1800
    assert 'Last event marker' in sent
    assert 'resume-event' in sent
    assert 'source_refs' not in sent and 'upstream-98765' not in sent
    assert 'Original evidence marker' not in sent
    assert messages[0]['content'].startswith('/resume')
    # A new process must carry the frozen resume context into subsequent turns.
    WindowShadows(settings.database).write('first','Synthetic continuity',content='Changed after resume',expected_revision=1)
    reopened = TestClient(create_app(settings, token='synthetic', live=True), headers={'Authorization':'Bearer synthetic'})
    followup = messages + [{'role':'assistant','content':'Synthetic reply'}, {'role':'user','content':'接着呢？顺便解释 /resume 的作用'}]
    assert post(reopened, followup).status_code==200
    forwarded = payloads[-1]['messages']
    serialized = json.dumps(forwarded, ensure_ascii=False)
    assert '/resume' not in forwarded[0]['content']
    assert '/resume' in forwarded[-1]['content']
    assert serialized.count('Serein resume:') == 1
    assert 'Full shadow marker' in forwarded[0]['content']
    assert 'Changed after resume' not in serialized
    assert forwarded[-1]['content'] == '接着呢？顺便解释 /resume 的作用'
    # Removing or changing the original prefix must not retain its frozen context.
    assert post(reopened, followup[1:]).headers['x-serein-resume']=='none'
    changed = deepcopy(followup)
    changed[0]['content'] = '/resume 改聊今天的事'
    assert post(reopened, changed).headers['x-serein-resume']=='none'
    # The default main window must not attach that snapshot to unrelated history.
    unrelated = [{'role':'user','content':'Different chat'}]
    assert post(reopened, unrelated).headers['x-serein-resume']=='none'
    assert payloads[-1]['messages']==unrelated
    assert post(reopened, messages).status_code==200
    assert 'Changed after resume' in payloads[-1]['messages'][-1]['content']
    save_settings(settings.database, {'features':{'resume':False}})
    assert post(reopened, followup).headers['x-serein-resume']=='none'


@pytest.mark.parametrize('recent', [False, True])
@pytest.mark.parametrize('zone,expected', [
    ('Asia/Shanghai', '2026-09-27T16:39:21+08:00'),
    ('Europe/Berlin', '2026-09-27T10:39:21+02:00'),
])
def test_resume_original_timezone_survives_wire_transport_and_followup(chat, monkeypatch, recent, zone, expected):
    settings, client, _ = chat
    save_settings(settings.database, {'clock':{'timezone':zone}, 'resume':{
        'pending_originals':not recent, 'recent_originals':recent}})
    raw_archive(settings).ingest([{'source_event_id':'utc-message', 'role':'user',
        'text':'Synthetic UTC original', 'created_at':'2026-09-27 08:39:21'}], source='haven_bridge_codex')
    requests = []
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices':[{'message':{'role':'assistant','content':'Synthetic reply'}}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw:original(transport=httpx.MockTransport(handle), **kw))
    from serein.model_runtime import complete
    monkeypatch.setattr('serein.api.chat.complete', complete)
    messages = [{'role':'user','content':'/resume Synthetic test'}]
    assert post(client, messages).status_code==200
    followup = messages + [{'role':'assistant','content':'Synthetic reply'}, {'role':'user','content':'Continue'}]
    assert post(client, followup).headers['x-serein-resume']=='loaded'
    for request in requests:
        content = next(m['content'] for m in request['messages'] if 'Serein resume:' in str(m.get('content')))
        start = content.index('\n', content.index('Serein resume:')) + 1
        payload, _ = json.JSONDecoder().raw_decode(content[start:])
        item = next(i for i in payload['items'] if i['body_md']=='Synthetic UTC original')
        assert item['title']==item['created_at']==expected
        assert item['section']==('recent_original' if recent else 'pending_original')
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute("SELECT created_at FROM raw_events WHERE source_event_id='utc-message'").fetchone()[0]=='2026-09-27 08:39:21'


@pytest.mark.parametrize('text', ['/resumex hello', '请解释 /resume', '`/resume`', '继续聊'])
def test_command_only_at_current_message_start(chat, text):
    _, client, payloads = chat
    messages = [{'role':'user','content':'/resume old'}, {'role':'assistant','content':'old reply'}, {'role':'user','content':text}]
    response = post(client, messages)
    assert response.status_code==200 and response.headers['x-serein-resume']=='none'
    assert payloads[-1]['messages'] == messages


def test_resume_only_multimodal_and_external_context(chat):
    _, client, payloads = chat
    assert post(client,[{'role':'user','content':'/resume'}]).status_code==200
    assert payloads[-1]['messages'][-1]['content'].endswith('请读完接续资料，然后接着聊。')
    image = {'type':'image_url','image_url':{'url':'https://example.test/synthetic.png'}}
    content = [{'type':'text','text':'/resume\n这张图接上了吗？'},image]
    assert post(client,[{'role':'user','content':content}]).status_code==200
    sent = payloads[-1]['messages'][-1]['content']
    assert sent[-1]==image and sent[-2]['text']=='这张图接上了吗？'
    assert post(client,[{'role':'user','content':[{'type':'text','text':'/resume'}, {'type':'text','text':'Actual followup'},image]}]).status_code==200
    assert '请读完接续资料' not in json.dumps(payloads[-1],ensure_ascii=False)
    assert post(client,[{'role':'user','content':'/resume 后面的话<attachment>【当前时间】synthetic</attachment>'}]).status_code==200
    assert '后面的话' in json.dumps(payloads[-1], ensure_ascii=False)


def test_disabled_and_oversized_resume_do_not_call_upstream(chat, monkeypatch):
    settings, client, payloads = chat
    save_settings(settings.database, {'features':{'resume':False}})
    response = post(client,[{'role':'user','content':'/resume hello'}])
    assert response.status_code==409 and not payloads
    save_settings(settings.database, {'features':{'resume':True}})
    monkeypatch.setattr('serein.chat_resume.MAX_CONTEXT_CHARS',100)
    response = post(client,[{'role':'user','content':'/resume hello'}])
    assert response.status_code==413 and not payloads


@pytest.mark.parametrize('mcp', [False, True])
def test_resume_does_not_require_embedding_and_tool_round_keeps_context(chat, monkeypatch, mcp):
    settings, client, payloads = chat
    save_settings(settings.database, {'upstream':{'memory_enabled':True}, 'resume':{'mcp_enabled':mcp}})
    monkeypatch.setattr('serein.configured_models.memory_ready',lambda *_:False)
    tool_message = {'role':'assistant','content':None,'tool_calls':[{'id':'call1','type':'function','function':{'name':'lookup','arguments':'{}'}}]}
    async def complete(model, payload, **kwargs):
        payloads.append(deepcopy(payload))
        return {'choices':[{'message':tool_message}]}
    monkeypatch.setattr('serein.api.chat.complete', complete)
    messages = [{'role':'user','content':'/resume hello'}]
    assert post(client,messages).status_code==200
    first = deepcopy(payloads[-1]['messages'])
    tail = [tool_message, {'role':'tool','tool_call_id':'call1','content':'Tool reply'}]
    response = post(client,messages+tail)
    assert response.status_code==200 and response.headers['x-serein-context-replayed']=='true'
    assert payloads[-1]['messages'] == first+tail
    reopened = TestClient(create_app(settings,token='synthetic',live=True),headers={'Authorization':'Bearer synthetic'})
    assert post(reopened,messages+tail).status_code==200
    serialized = json.dumps(payloads[-1]['messages'], ensure_ascii=False)
    assert '/resume' not in payloads[-1]['messages'][0]['content']
    assert serialized.count('Serein resume:') == 1
    assert 'Full shadow marker' in payloads[-1]['messages'][0]['content']
    assert payloads[-1]['messages'][-2:] == tail


def test_retained_resume_anchor_survives_dropped_operit_prefix(chat):
    settings, client, payloads = chat
    prefix = {'role':'user','content':'<attachment filename="time:synthetic">【当前时间】\nPure Operit prefix marker</attachment>'}
    resume = {'role':'user','content':'/resume Original anchor text'}
    assert post(client,[prefix,resume]).status_code==200
    reopened = TestClient(create_app(settings,token='synthetic',live=True),headers={'Authorization':'Bearer synthetic'})
    followup = [prefix,resume,{'role':'assistant','content':'Synthetic reply'},
                {'role':'user','content':'Latest followup mentions /resume normally'}]
    response = post(reopened,followup)
    assert response.status_code==200 and response.headers['x-serein-resume']=='loaded'
    forwarded = payloads[-1]['messages']
    serialized = json.dumps(forwarded,ensure_ascii=False)
    assert len(forwarded)==3
    assert forwarded[0]['role']=='user' and 'Original anchor text' in forwarded[0]['content']
    assert '/resume' not in forwarded[0]['content']
    assert forwarded[0]['content'].count('Serein resume:')==1
    assert serialized.count('Serein resume:')==1
    assert forwarded[-1]['content'].endswith('Latest followup mentions /resume normally')
    assert '/resume' in forwarded[-1]['content']
    assert '__serein_internal_resume_anchor__' not in serialized


def test_retained_prefers_longest_matching_prefix(chat):
    settings, _, _ = chat
    context = ClientContext()
    messages = [{'role':'user','content':'/resume first'},
                {'role':'assistant','content':'First reply'},
                {'role':'user','content':'/resume second'},
                {'role':'assistant','content':'Second reply'},
                {'role':'user','content':'Latest'}]
    short = {'source_count':1,
             'source_digest':context._turn_injection_messages_digest(messages[:1]),
             'context':'short frozen context','items':1}
    long = {'source_count':3,
            'source_digest':context._turn_injection_messages_digest(messages[:3]),
            'context':'long frozen context','items':2}
    with Store(settings.database) as store, store.transaction(immediate=True):
        store.conn.execute('CREATE TABLE IF NOT EXISTS chat_resume_contexts '
                           '(key TEXT PRIMARY KEY, window_id TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL)')
        store.conn.execute('INSERT INTO chat_resume_contexts VALUES (?,?,?,?)',
                           ('long','specific-window',json.dumps(long),'2026-01-01T00:00:00Z'))
        store.conn.execute('INSERT INTO chat_resume_contexts VALUES (?,?,?,?)',
                           ('short','specific-window',json.dumps(short),'9999-01-01T00:00:00Z'))
    found = chat_resume.retained(SimpleNamespace(_settings=settings),'specific-window',messages,context)
    assert found == long


@pytest.mark.parametrize('stream', [False, True])
def test_upstream_context_rejection_is_explicit(chat, monkeypatch, stream):
    _, client, _ = chat
    original = httpx.AsyncClient
    def handle(request):
        assert 'Full shadow marker' in request.content.decode()
        return httpx.Response(400,json={'error':{'code':'context_length_exceeded'}})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    from serein.model_runtime import complete
    monkeypatch.setattr('serein.api.chat.complete',complete)
    response = post(client,[{'role':'user','content':'/resume hello'}],stream=stream)
    assert response.status_code==413
    assert 'context length' in response.json()['detail']


def test_streaming_resume_forwards_full_context_and_retains_after_success(chat, monkeypatch):
    settings, client, _ = chat
    original = httpx.AsyncClient
    def handle(request):
        sent = json.loads(request.content)
        assert sent['messages'][-1]['content'].count('long prose ')==1800
        assert sent['messages'][-1]['content'].endswith('Streaming followup')
        return httpx.Response(200,text='data: '+json.dumps({'choices':[{'delta':{'role':'assistant','content':'Streaming reply'}}]})+'\n\ndata: [DONE]\n\n',headers={'content-type':'text/event-stream'})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    response = post(client,[{'role':'user','content':'/resume Streaming followup'}],stream=True)
    assert response.status_code==200 and 'Streaming reply' in response.text and '[DONE]' in response.text
    assert response.headers['x-serein-resume']=='loaded'
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('select count(*) from chat_resume_contexts').fetchone()[0]==1


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('command,mcp', [(False, False), (True, False), (False, True), (True, True)])
def test_resume_command_entry_matrix_and_single_injection(chat, enabled, command, mcp):
    settings, client, payloads = chat
    save_settings(settings.database, {'features':{'resume':enabled}, 'resume':{
        'command_enabled':command, 'mcp_enabled':mcp}})
    messages = [{'role':'user','content':'/resume Continue here'}]
    response = post(client, messages, window_id='matrix', memory=False)
    if not (enabled and command):
        assert response.status_code == 409
        assert not payloads
        return
    assert response.status_code == 200, response.text
    for _ in range(2):
        assert post(client, messages, window_id='matrix', memory=False).status_code == 200
        serialized = json.dumps(payloads[-1])
        assert serialized.count('Serein resume:') == 1
        assert serialized.count('Full shadow marker') == 1
        assert serialized.count('long prose ') == 1800
    # Enabling MCP does not load anything in an unrelated ordinary request.
    ordinary = [{'role':'user','content':'An unrelated ordinary message'}]
    assert post(client, ordinary, window_id='other', memory=False).headers['x-serein-resume'] == 'none'
    assert payloads[-1]['messages'] == ordinary
    followup = messages + [{'role':'assistant','content':'Synthetic reply'}, {'role':'user','content':'Continue'}]
    assert post(client, followup, window_id='matrix', memory=False).headers['x-serein-resume'] == 'loaded'
    assert json.dumps(payloads[-1]).count('Serein resume:') == 1
    # Disabling only the command stops frozen context, without changing MCP.
    save_settings(settings.database, {'resume':{'command_enabled':False}})
    response = post(client, followup, window_id='matrix', memory=False)
    assert response.status_code == 200 and response.headers['x-serein-resume'] == 'none'
    assert 'Full shadow marker' not in json.dumps(payloads[-1])
