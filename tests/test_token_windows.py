"""Offline tokenizer and mocked-provider contract tests, not semantic benchmarks."""
import json
from pathlib import Path

import httpx
import pytest

from serein.adapters.token_window import TokenWindow
from serein.adapters.reranker import RerankerClient
from serein.recall.rendering import render


@pytest.fixture
def tokenizer_path(tmp_path):
    transformers = pytest.importorskip('transformers')
    tokenizers = pytest.importorskip('tokenizers')
    # Real tokenizer trained only on synthetic text; no hub, weights or credentials.
    tokenizer = tokenizers.Tokenizer(tokenizers.models.WordPiece(unk_token='[UNK]'))
    tokenizer.pre_tokenizer = tokenizers.pre_tokenizers.Whitespace()
    tokenizer.train_from_iterator(['inspection light window unchanged spare key K-47 blue toolbox second layer'],
        tokenizers.trainers.WordPieceTrainer(vocab_size=100, special_tokens=['[UNK]', '[CLS]', '[SEP]', '[PAD]']))
    tokenizer.post_processor = tokenizers.processors.TemplateProcessing(
        single='[CLS] $A [SEP]', pair='[CLS] $A [SEP] $B:1 [SEP]:1',
        special_tokens=[('[CLS]',tokenizer.token_to_id('[CLS]')),('[SEP]',tokenizer.token_to_id('[SEP]'))])
    transformers.PreTrainedTokenizerFast(tokenizer_object=tokenizer,unk_token='[UNK]',
        cls_token='[CLS]',sep_token='[SEP]',pad_token='[PAD]').save_pretrained(tmp_path)
    return str(tmp_path)


def test_windows_preserve_tail_offsets_and_both_budgets(tokenizer_path):
    window=TokenWindow({'path':tokenizer_path,'max_tokens':24})
    body='inspection light window unchanged '*90+'spare key K-47 blue toolbox second layer'
    prefix='inspection: '
    spans=list(window.spans(body,prefix=prefix,max_chars=120))
    covered=set()
    for a,b in spans:
        assert window.count(prefix+body[a:b])<=24
        assert len(prefix+body[a:b])<=120
        covered.update(range(a,b))
    assert covered==set(range(len(body)))
    assert any('K-47' in body[a:b] for a,b in spans)
    with pytest.raises(ValueError): list(window.spans('x',prefix=body))


def test_pair_limits_winning_excerpt_and_request_isolation(tokenizer_path):
    rank=RerankerClient('http://127.0.0.1/rerank','synthetic',api_key='',
        tokenizer={'path':tokenizer_path,'max_tokens':32})
    body='inspection light window unchanged '*90+'spare key K-47 blue toolbox second layer'
    query='spare key'
    captured=[]
    def server(request):
        payload=json.loads(request.content); captured.append(payload)
        assert all(rank.window.count(s,query=query)<=32 for s in payload['documents'])
        return httpx.Response(200,json={'results':[{'index':i,'relevance_score':.9 if 'K-47' in s else .1}
            for i,s in enumerate(payload['documents'])]})
    doc={'ref':'event:long','title':'inspection','body':'','source_body':body}
    with httpx.Client(transport=httpx.MockTransport(server)) as client:
        first=rank(query,[doc],client=client)
        second=rank(query,[{'ref':'event:other','title':'inspection','body':'', 'source_body':'light'}],client=client)
    assert first=={'event:long':.9} and second=={'event:other':.1}
    excerpt=first.evidence['event:long']
    assert 'event:long' not in second.evidence
    assert body[excerpt['start_offset']:excerpt['end_offset']]==excerpt['text']
    assert 'K-47' in excerpt['text']
    hit={'id':'long','kind':'event','score':.9,'object':{'document':{'kind':'event',
        'title':'inspection','body_md':body,'metadata':{}}},'body_excerpt':excerpt}
    result=render([hit])
    assert 'K-47' in result['context']
    assert result['cards'][0]['source_spans']==[{'start_offset':excerpt['start_offset'],'end_offset':excerpt['end_offset']}]
    hit['body_excerpt']={**excerpt,'text':'unsupported'}
    with pytest.raises(ValueError):render([hit])


def test_explicit_region_cannot_leak_context_and_duplicate_results_rejected(tokenizer_path):
    rank=RerankerClient('http://127.0.0.1/rerank','synthetic',api_key='',
        tokenizer={'path':tokenizer_path,'max_tokens':24})
    body='inspection light\nPRIVATE CONTEXT'
    doc={'ref':'scene:one','title':'','body':'','source_body':body,'source_regions':[(0,16)]}
    def server(request):
        payload=json.loads(request.content)
        assert all('PRIVATE' not in s for s in payload['documents'])
        return httpx.Response(200,json={'results':[{'index':0,'relevance_score':.9},{'index':0,'relevance_score':.8}]})
    with httpx.Client(transport=httpx.MockTransport(server)) as client:
        with pytest.raises(ValueError,match='duplicate'):rank('light',[doc],client=client)


def test_provider_instruction_template_cannot_claim_pair_budget(tokenizer_path):
    with pytest.raises(ValueError,match='standard query/document'):
        RerankerClient('https://api.siliconflow.cn/v1/rerank','Qwen/Qwen3-Reranker-0.6B',api_key='',
            tokenizer={'path':tokenizer_path,'max_tokens':32})


def test_optional_window_changes_index_fingerprint_only_when_configured(tmp_path):
    from serein.config import Settings
    from serein.configured_models import model_index
    settings=Settings(tmp_path/'memory.db',tmp_path/'index.db')
    model={'id':'embed','model':'synthetic','base_url':'http://127.0.0.1'}
    assert model_index(settings,model)==model_index(settings,{**model,'tokenizer':None})
    assert model_index(settings,model)!=model_index(settings,{**model,'tokenizer':{'path':'local','max_tokens':24}})


def test_embedding_preparation_and_passages_send_tail_without_truncation(tmp_path,tokenizer_path):
    import sqlite3
    from serein.config import Settings
    from serein.core.store import Store
    from serein.recall.index import build_index
    from serein.recall.passages import fill_passages
    from serein.recall.vectors import fill_vectors
    from serein.adapters.embedding import EmbeddingClient
    body='inspection light window unchanged '*90+'spare key K-47 blue toolbox second layer'
    database,index=tmp_path/'memory.db',tmp_path/'index.db'
    with Store(database) as store: store.create('long','event','inspection',body)
    build_index(database,index)
    profile=dict(model='synthetic',provider_host='127.0.0.1',query_instruction='find',
        document_instruction='find',max_chars=120)
    with sqlite3.connect(index) as conn:
        conn.execute("INSERT INTO settings VALUES ('embedding_profile',?)",(json.dumps(profile),))
        conn.execute("INSERT INTO settings VALUES ('embedding_dimension','2')")
    settings=Settings(database,index,recall={'passages_enabled':True})
    client=EmbeddingClient(database,index,'http://127.0.0.1/embeddings',api_key='',
        tokenizer={'path':tokenizer_path,'max_tokens':24})
    with pytest.raises(ValueError,match='token window'): client.prepare('inspection '*40,'find')
    assert fill_vectors(settings,client=client)['embedded']==0
    captured=[]
    def request(inputs,count,**kwargs):
        captured.extend(inputs)
        assert all(len(s)<=120 and client.window.count(s)<=24 for s in inputs)
        return [[1,0] for _ in inputs]
    client._request=request
    report=fill_passages(settings,client=client)
    assert report['embedded']>1 and any('K-47' in s for s in captured)
    with sqlite3.connect(index) as conn:
        spans=conn.execute('SELECT start_offset,end_offset,text FROM passages').fetchall()
    assert all(body[a:b]==text for a,b,text in spans)


@pytest.mark.parametrize('budget',[1200,160])
def test_tail_fact_survives_actual_delivery_budget(tokenizer_path,budget):
    rank=RerankerClient('http://127.0.0.1/rerank','synthetic',api_key='',
        tokenizer={'path':tokenizer_path,'max_tokens':512})
    body='inspection light window unchanged '*180+'spare key K-47 blue toolbox second layer'
    doc={'ref':'event:long','title':'inspection','body':'','source_body':body,'body_char_limit':budget}
    def server(request):
        payload=json.loads(request.content)
        return httpx.Response(200,json={'results':[{'index':i,'relevance_score':.9 if 'K-47' in s and 'second layer' in s else .1}
            for i,s in enumerate(payload['documents'])]})
    with httpx.Client(transport=httpx.MockTransport(server)) as client:
        scores=rank('spare key',[doc],client=client)
    hit={'id':'long','kind':'event','score':.9,'object':{'document':{'kind':'event','title':'inspection',
        'body_md':body,'metadata':{}}},'body_excerpt':scores.evidence['event:long']}
    result=render([hit],body_char_limit=budget)
    assert 'K-47' in result['context'] and 'second layer' in result['context']
    assert len(result['cards'][0]['text'])<=budget


def test_short_scene_keeps_all_discontinuous_evidence_in_one_pair(tokenizer_path):
    from serein.recall.passages import regions
    rank=RerankerClient('http://127.0.0.1/rerank','synthetic',api_key='',
        tokenizer={'path':tokenizer_path,'max_tokens':512})
    body='inspection light\n## comment\nPRIVATE CONTEXT\n## spare key\nK-47 blue toolbox second layer\n'
    document={'kind':'scene','title':'inspection','body_md':body,'metadata':{}}
    allowed=regions(document)
    def server(request):
        payload=json.loads(request.content)
        assert len(payload['documents'])==1
        text=payload['documents'][0]
        assert 'inspection light' in text and 'K-47' in text and 'PRIVATE' not in text
        return httpx.Response(200,json={'results':[{'index':0,'relevance_score':.9}]})
    doc={'ref':'scene:one','title':'inspection','body':'','source_body':body,'source_regions':allowed}
    with httpx.Client(transport=httpx.MockTransport(server)) as client:
        scores=rank('spare key',[doc],client=client)
    hit={'id':'one','kind':'scene','score':.9,'object':{'document':document},'body_excerpt':scores.evidence['scene:one']}
    result=render([hit])
    assert 'inspection light' in result['context'] and 'K-47' in result['context']
    assert 'PRIVATE' not in result['context']
    assert result['cards'][0]['source_spans']==[{'start_offset':a,'end_offset':b} for a,b in allowed]


def test_empty_scene_excerpt_is_safely_skipped():
    document={'kind':'scene','title':'inspection','body_md':'## comment\nPRIVATE CONTEXT\n','metadata':{}}
    hit={'id':'empty','kind':'scene','score':.9,'object':{'document':document},
         'body_excerpt':{'text':'','source_spans':[],'input_tokens':17}}
    result=render([hit])
    assert result['cards']==[] and 'PRIVATE' not in result['context']


def test_empty_scene_candidate_does_not_fail_recall(tmp_path,tokenizer_path,monkeypatch):
    import sqlite3
    from serein.config import Settings
    from serein.core.store import Store
    from serein.recall.index import build_index
    from serein.recall.service import Recall
    profile=dict(model='synthetic',provider_host='127.0.0.1',query_instruction='',document_instruction='',max_chars=6000)
    settings=Settings(tmp_path/'memory.db',tmp_path/'index.db',embedding={'endpoint':'unused'},
        recall={'routing_file':'synthetic-routes'})
    with Store(settings.database) as store:
        store.create('empty','scene','手机维修','## comment\nPRIVATE CONTEXT\n')
    build_index(settings.database,settings.index)
    # Abnormal legacy/custom candidate; normal indexing excludes empty evidence.
    with sqlite3.connect(settings.index) as conn:
        conn.execute("INSERT INTO settings VALUES ('embedding_profile',?)",(json.dumps(profile),))
        conn.execute("INSERT INTO settings VALUES ('embedding_dimension','2')")
        conn.execute("INSERT INTO vectors VALUES ('empty','[1,0]',2)")
    class Embedding:
        def __init__(self,*args,**kwargs):pass
        def query(self,text):return dict(query=text,profile=profile,embedding=[1.,0.])
    monkeypatch.setattr('serein.adapters.embedding.EmbeddingClient',Embedding)
    monkeypatch.setattr('serein.recall.routing.route_query',lambda *a,**kw:{'route':'recall_needed','action':'recall'})
    rank=RerankerClient('http://127.0.0.1/rerank','synthetic',api_key='',
        tokenizer={'path':tokenizer_path,'max_tokens':512})
    requests=[]
    def server(client,url,**kwargs):
        requests.append(kwargs['json'])
        return httpx.Response(200,json={'results':[{'index':i,'relevance_score':.9}
            for i,_ in enumerate(kwargs['json']['documents'])]})
    monkeypatch.setattr(httpx.Client,'post',server)
    result=Recall(settings,reranker=rank).run('手机维修',method='semantic',min_cosine=.5)
    assert result['cards']==[] and result['selected_refs']==[]
    assert 'PRIVATE' not in result['context']
    assert requests==[]
