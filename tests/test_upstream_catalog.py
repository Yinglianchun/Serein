import json
import httpx
import pytest
from fastapi.testclient import TestClient
from serein.api.http import create_app
from serein.bootstrap import initialize
from serein.config import Settings
from serein.deployment import task_model


@pytest.fixture
def deployment(tmp_path):
    settings=Settings(tmp_path/'memory.db',index=tmp_path/'index.db',writable=True)
    initialize(settings)
    client=TestClient(create_app(settings,token='synthetic',live=True),headers={'Authorization':'Bearer synthetic'})
    return settings,client


def upstreams():
    return [
        {'name':'provider-a','base_url':'https://a.example/v1','api_key':'synthetic-a-secret',
         'models':['chat-fast',{'id':'a-smart','upstream_model':'shared-model'}]},
        {'name':'provider-b','base_url':'https://b.example/v1','api_key_env':'SEREIN_TEST_PROVIDER_B',
         'protocol':'anthropic','prompt_cache':'anthropic_explicit','prompt_cache_retention':'1h',
         'models':[{'id':'b-smart','upstream_model':'shared-model'}]},
    ]


def test_aggregate_and_route_every_configured_upstream(deployment,monkeypatch):
    settings,client=deployment
    monkeypatch.setenv('SEREIN_TEST_PROVIDER_B','synthetic-b-secret')
    saved=client.patch('/v1/settings',json={'upstreams':upstreams()})
    assert saved.status_code==200,saved.text
    assert 'synthetic-a-secret' not in saved.text and 'synthetic-b-secret' not in saved.text
    assert all(item['api_key_configured'] for item in saved.json()['upstreams'])
    models=client.get('/v1/models').json()['data']
    assert [item['id'] for item in models]==['provider-a/chat-fast','provider-a/shared-model','provider-b/shared-model']
    assert [item['owned_by'] for item in models]==['provider-a','provider-a','provider-b']
    requests=[]
    original=httpx.AsyncClient
    def handle(request):
        body=json.loads(request.content);requests.append((request.url.host,body['model']))
        if request.url.host=='b.example':
            assert request.headers['x-api-key']=='synthetic-b-secret'
            assert request.url.path=='/v1/messages'
            return httpx.Response(200,json={'id':'native','content':[{'type':'text','text':'native answer'}],'stop_reason':'end_turn','usage':{}})
        assert request.headers['authorization']=='Bearer synthetic-a-secret'
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'answer'}}]})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
    for model in ('provider-a/chat-fast','provider-a/shared-model','provider-b/shared-model'):
        result=client.post('/v1/chat/completions',json={'model':model,'messages':[{'role':'user','content':'hello'}]})
        assert result.status_code==200,result.text
    assert requests==[('a.example','chat-fast'),('a.example','shared-model'),('b.example','shared-model')]
    assert client.post('/v1/chat/completions',json={'messages':[{'role':'user','content':'hello'}]}).status_code==400
    assert client.post('/v1/chat/completions',json={'model':'shared-model','messages':[{'role':'user','content':'hello'}]}).status_code==400


def test_template_import_and_shared_task_choices(deployment):
    settings,client=deployment
    template='''gateway:
  upstreams:
    - name: local
      base_url: http://127.0.0.1:9999/v1
      default_model: assistant
      models:
        - id: assistant
          upstream_model: actual-assistant
        - local-embedding
'''
    result=client.post('/v1/settings/upstreams-template',json={'template':template})
    assert result.status_code==200,result.text
    assert result.headers['Cache-Control']=='no-store'
    assert [row['id'] for row in result.json()['available_models']]==['assistant','local-embedding']
    saved=client.patch('/v1/settings',json={'assignments':{task:'assistant' for task in ('writer','relations','dreams','narrative_scout','event_pipeline')}})
    assert saved.status_code==200
    assert task_model(settings.database,'chat')['model']=='actual-assistant'
    for task in ('writer','relations','dreams','narrative_scout','event_pipeline'):
        assert task_model(settings.database,task)['model']=='actual-assistant'
    from serein.compat.jobs import BackgroundJobs
    from serein.model_runtime import TaskClient
    assert isinstance(BackgroundJobs(settings,features={'dreams'}).dreams.client,TaskClient)


@pytest.mark.parametrize('code,reason', [(403,'index_configuration_or_contract_error'), (200,'embedding_invalid_response')])
def test_index_status_distinguishes_missing_selection_and_provider_response(deployment, monkeypatch, code, reason):
    _, client = deployment
    def status():
        return client.get('/v1/settings/index-status').json()

    calls = []
    def respond(self, request):
        calls.append(str(request.url))
        return httpx.Response(code, text='synthetic-private-body')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', respond)
    assert client.post('/v1/settings/prepare-memory').status_code == 400
    state = status()
    assert state['reason'] == 'embedding_not_selected' and not calls
    assert client.post('/v1/settings/resume-index', json={'confirm':'RESUME_INDEX_EMBEDDING',
                       'recovery_token':state['recovery_token']}).status_code == 200
    assert client.patch('/v1/settings', json={'upstreams':upstreams(), 'assignments':{'embedding':'a-smart'}}).status_code == 200
    assert client.post('/v1/settings/prepare-memory').status_code == 400
    state = status()
    assert calls == ['https://a.example/v1/embeddings']
    assert state['reason'] == reason and state['status'] == 'paused'
    assert state.get('provider_http_status') == (403 if code == 403 else None)
    assert 'synthetic-private-body' not in json.dumps(state)


def test_template_explains_url_and_binding_errors_separately(deployment):
    _, client = deployment
    def submit(url, models):
        template = {'gateway': {'upstreams': [{'name':'example', 'base_url':url, 'models':models}]}}
        return client.post('/v1/settings/upstreams-template', json={'template':json.dumps(template)})

    url = 'https://example.invalid/v1'
    invalid = submit('image[' + url + ']', ['one'])
    assert invalid.status_code == 400 and 'base_url' in invalid.json()['detail']
    assert url not in invalid.text
    assert submit(url, ['one']).status_code == 200
    assert client.patch('/v1/settings', json={'assignments':{'writer':'one'}}).status_code == 200
    missing = submit(url, ['two'])
    assert missing.status_code == 400 and 'Narrative Writer' in missing.json()['detail']
    assert client.get('/v1/settings').json()['assignments']['writer'] == 'one'


def test_upstream_keys_survive_save_and_alias_conflicts_are_atomic(deployment):
    settings,client=deployment
    client.patch('/v1/settings',json={'upstreams':upstreams()})
    public=client.get('/v1/settings').json()['upstreams']
    for item in public:item.pop('api_key_configured')
    public[0]['name']='renamed-provider'
    assert client.patch('/v1/settings',json={'upstreams':public}).status_code==200
    assert task_model(settings.database,'chat',requested='a-smart')['api_key']=='synthetic-a-secret'
    public[0]['api_key']='synthetic-replacement-secret'
    assert client.patch('/v1/settings',json={'upstreams':public}).status_code==200
    assert task_model(settings.database,'chat',requested='a-smart')['api_key']=='synthetic-replacement-secret'
    duplicate=[{**upstreams()[0],'models':['same']},{**upstreams()[1],'models':['same']}]
    assert client.patch('/v1/settings',json={'upstreams':duplicate}).status_code==400
    assert client.get('/v1/models').json()['data'][0]['id']=='renamed-provider/chat-fast'
    public[0]['api_key']=''
    assert client.patch('/v1/settings',json={'upstreams':public}).status_code==200
    assert task_model(settings.database,'chat',requested='a-smart')['api_key']==''
    invalid=client.post('/v1/settings/upstreams-template',json={'template':'gateway: {upstreams: [{api_key: never-echo-this-secret}]}'})
    assert invalid.status_code==400 and 'never-echo-this-secret' not in invalid.text


def test_replace_catalog_and_all_task_models_atomically(deployment):
    settings,client=deployment
    roles=('writer','persona','dreams','relations','narrative_scout','operit_tagging','arc_linker',
           'image_transcription','track_router','event_curator','event_writer')
    catalog=[{'id':'provider','name':'Synthetic','base_url':'https://synthetic.invalid/v1',
              'api_key':'synthetic-secret','models':['old-model','embedding','reranker']}]
    initial=client.patch('/v1/settings',json={'upstreams':catalog,'assignments':{
        **{role:'old-model' for role in roles},'embedding':'embedding','reranker':'reranker'},
        'pipeline':{'execution_mode':'api'},'features':{'image_eyes':True}})
    initial.raise_for_status()
    public=initial.json()
    replacement=public['upstreams']
    for upstream in replacement:upstream.pop('api_key_configured',None)
    replacement[0]['models']=['new-model','embedding','reranker']
    # An old client that omits the summary assignments gets actionable task names,
    # and the rejected request must leave both catalog and settings version intact.
    failed=client.patch('/v1/settings',json={'expected_version':public['settings_version'],
        'models':[],'upstreams':replacement,'assignments':{'writer':'new-model'}})
    assert failed.status_code==400,failed.text
    for label in ('图片转录 / 眼睛','原话 · 归线','原话 · 切分','原话 · Event 写作'):
        assert label in failed.json()['detail']
    assert 'synthetic-secret' not in failed.text
    assert client.get('/v1/settings').json()['settings_version']==public['settings_version']
    assert task_model(settings.database,'track_router')['model']=='old-model'
    saved=client.patch('/v1/settings',json={'expected_version':public['settings_version'],
        'models':[],'upstreams':replacement,'assignments':{role:'new-model' for role in roles}})
    saved.raise_for_status()
    for role in roles:
        model=task_model(settings.database,role)
        assert model['model']=='new-model'
        assert model['api_key']=='synthetic-secret'
    assert task_model(settings.database,'embedding')['model']=='embedding'
    assert task_model(settings.database,'reranker')['model']=='reranker'
    assert 'synthetic-secret' not in saved.text


def test_streaming_routes_alias_to_its_upstream(deployment,monkeypatch):
    settings,client=deployment
    client.patch('/v1/settings',json={'upstreams':upstreams()})
    original=httpx.AsyncClient
    def handle(request):
        assert request.url.host=='a.example'
        assert json.loads(request.content)['model']=='shared-model'
        return httpx.Response(200,headers={'content-type':'text/event-stream'},
            text='data: {"choices":[{"index":0,"delta":{"content":"stream answer"}}]}\n\ndata: [DONE]\n\n')
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
    result=client.post('/v1/chat/completions',json={'model':'a-smart','stream':True,'messages':[{'role':'user','content':'hello'}]})
    assert result.status_code==200 and 'stream answer' in result.text and '[DONE]' in result.text


def test_display_names_hide_internal_ids_and_auxiliary_models(deployment):
    settings,client=deployment
    rows=[{'id':'uuid-chat','upstream_model':'actual-chat','label':'聊天'},
          {'id':'uuid-embed','upstream_model':'actual-embedding'},
          {'id':'uuid-rerank','upstream_model':'actual-reranker'},
          {'id':'uuid-other','upstream_model':'another-chat','label':''}]
    client.patch('/v1/settings',json={'upstreams':[{'name':'Provider','base_url':'https://provider.example/v1','models':rows}],
         'assignments':{'embedding':'uuid-embed','reranker':'uuid-rerank'}}).raise_for_status()
    assert [m['id'] for m in client.get('/v1/models').json()['data']]==['Provider/聊天','Provider/another-chat']
    assert task_model(settings.database,'chat',requested='Provider/聊天')['model']=='actual-chat'
    assert task_model(settings.database,'chat',requested='uuid-chat')['model']=='actual-chat'
    assert task_model(settings.database,'embedding')['model']=='actual-embedding'
    with pytest.raises(ValueError):task_model(settings.database,'chat',requested='uuid-embed')
    client.patch('/v1/settings',json={'assignments':{'embedding':'uuid-other','reranker':''}}).raise_for_status()
    assert [m['id'] for m in client.get('/v1/models').json()['data']]==['Provider/聊天','Provider/actual-embedding','Provider/actual-reranker']


def test_legacy_models_share_upstream_editor_and_keep_credentials(deployment):
    from serein.deployment import read_settings
    settings,client=deployment
    models=[{'id':key,'model':model,'label':label,'base_url':'https://provider.example/v1',
             'api_key':'synthetic-key'} for key,model,label in [('old-chat','chat-model','Chat'),('old-embed','embed-model','Embedding')]]
    client.patch('/v1/settings',json={'models':models,'assignments':{'writer':'old-chat','embedding':'old-embed'}}).raise_for_status()
    raw=read_settings(settings.database)
    page=client.get('/v1/settings').json()
    assert page['models']==[] and len(page['upstreams'])==1
    assert len(page['upstreams'][0]['models'])==2 and 'synthetic-key' not in json.dumps(page)
    assert read_settings(settings.database)==raw, 'Reading the editor must not migrate storage'
    groups=page['upstreams']
    for group in groups:group.pop('api_key_configured')
    groups[0]['name']='Named Provider'
    client.patch('/v1/settings',json={'models':[],'upstreams':groups}).raise_for_status()
    assert read_settings(settings.database)['models']==[]
    assert task_model(settings.database,'embedding')['api_key']=='synthetic-key'
    assert task_model(settings.database,'writer')['id']=='old-chat'
    assert client.get('/v1/models').json()['data'][0]['id']=='Named Provider/Chat'


def test_duplicate_display_names_are_rejected_and_distinct_secrets_not_merged(deployment):
    settings,client=deployment
    duplicate={'name':'Provider','base_url':'https://provider.example/v1','models':[
        {'id':'a','upstream_model':'one','label':'Same'},{'id':'b','upstream_model':'two','label':'Same'}]}
    assert client.patch('/v1/settings',json={'upstreams':[duplicate]}).status_code==400
    models=[{'id':key,'label':'Chat','model':'model','base_url':'https://provider.example/v1','api_key':secret}
            for key,secret in [('one','secret-one'),('two','secret-two')]]
    page=client.patch('/v1/settings',json={'models':models}).json()
    assert len(page['upstreams'])==2
    assert task_model(settings.database,'chat',requested='one')['api_key']=='secret-one'
    assert task_model(settings.database,'chat',requested='two')['api_key']=='secret-two'



def test_opencode_go_session_is_optional_and_scoped_to_stable_chat_windows(deployment, monkeypatch):
    _, client = deployment
    go = {'id':'go','name':'Go','base_url':'https://opencode.ai/inference/go/openai/v1',
          'opencode_go_session':True,'models':['go-model']}
    ordinary = {'name':'Other','base_url':'https://ordinary.invalid/v1','models':['ordinary-model']}
    client.patch('/v1/settings', json={'upstreams':[go,ordinary]}).raise_for_status()
    bad = client.patch('/v1/settings',json={'upstreams':[{**go,'base_url':'https://ordinary.invalid/v1'}]})
    assert bad.status_code == 422
    headers = []
    original = httpx.AsyncClient

    def upstream(request):
        headers.append(request.headers.get('x-opencode-session'))
        if json.loads(request.content).get('stream'):
            return httpx.Response(200, text='data: {"choices":[{"index":0,"delta":{"content":"reply"}}]}\n\ndata: [DONE]\n\n')
        return httpx.Response(200, json={'choices':[{'message':{'role':'assistant','content':'reply'}}]})

    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs: original(transport=httpx.MockTransport(upstream),**kwargs))

    def chat(model, window='', stream=False):
        return client.post('/v1/chat/completions', headers={'X-Serein-Window-ID':window} if window else {},
                           json={'model':model,'messages':[{'role':'user','content':'hello'}],'stream':stream})

    chat('Go/go-model','first').raise_for_status()
    chat('Go/go-model','first',stream=True).raise_for_status()
    chat('Go/go-model','second').raise_for_status()
    chat('Go/go-model').raise_for_status()
    chat('Other/ordinary-model','first').raise_for_status()
    assert headers[0].startswith('ses_') and 'first' not in headers[0]
    assert headers[0] == headers[1] and headers[0] != headers[2]
    assert headers[3:] == [None,None]
