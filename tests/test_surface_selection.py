import json
import math
import sqlite3

import pytest

from serein.config import Settings
from serein.core import Store
from serein.recall.index import build_index,content_stamp
from serein.recall.passages import ensure_tables
from serein.recall.service import Recall
from serein.deployment import save_settings


@pytest.fixture
def legacy(tmp_path,monkeypatch):
    profile=dict(model='test',provider_host='test.invalid',query_instruction='',document_instruction='',max_chars=6000)
    settings=Settings(tmp_path/'memory.db',tmp_path/'index.db',embedding={'endpoint':'unused','api_key_env':'unused'},
                      recall={'routing_file':'test-routes','max_cards':2})
    monkeypatch.setattr('serein.recall.routing.route_query',lambda *a,**kw:{'route':'recall_needed','action':'recall'})
    class Embedding:
        def __init__(self,*a,**kw):pass
        def query(self,text):return dict(query=text,profile=profile,embedding=[1.,0.])
    monkeypatch.setattr('serein.adapters.embedding.EmbeddingClient',Embedding)
    def build(rows,metadata=None,bodies=None):
        with Store(settings.database) as store:
            for key,kind,score,day in rows:
                store.create(key,kind,'手机维修 '+key,(bodies or {}).get(key,'手机屏幕坏了，我们带着它到店里维修。'),
                             metadata={'date':day,'local_date':day,**(metadata or {}).get(key,{})})
        build_index(settings.database,settings.index)
        with sqlite3.connect(settings.index) as db:
            db.execute("INSERT INTO settings VALUES ('embedding_profile',?)",(json.dumps(profile),))
            db.execute("INSERT INTO settings VALUES ('embedding_dimension','2')")
            for key,kind,score,day in rows:
                db.execute('INSERT INTO vectors VALUES (?,?,2)',(key,json.dumps([score,math.sqrt(1-score*score)])))
        calls=[]
        def score(query,docs):
            calls.extend(docs);return {d['ref']:.9 for d in docs}
        return Recall(settings,reranker=score),calls
    return settings,build


def test_mixed_pool_has_no_type_quota_and_keeps_six_base_vectors(legacy):
    settings,build=legacy
    engine,calls=build([(f's{i}','scene',.90-i*.02,'2026-09-01') for i in range(5)] +
                       [(f'e{i}','event',.99-i*.02,'2026-09-01') for i in range(5)])
    result=engine.run('手机维修',method='semantic',min_cosine=.5)
    assert [d['ref'] for d in calls]==['event:e0','event:e1','event:e2','event:e3','event:e4',
                                        'scene:s0','scene:s1','scene:s2','scene:s3','scene:s4']
    assert result['selected_refs']==['event:e0','event:e1']
    assert result['candidate_policy']['base_vector_pool_limit']==6
    assert result['candidate_policy']['direct_pool_limit']==20


def test_rejected_top_six_do_not_promote_seventh_memory(legacy):
    _,build=legacy
    engine,_=build([(f's{i}','scene',.90-i*.02,'2026-09-01') for i in range(6)]+
                   [('s6','scene',.49,'2026-09-01')])
    seen=[]
    def score(query,docs):
        seen.extend(d['ref'] for d in docs)
        return {d['ref']:(.99 if d['ref']=='scene:s6' else .1) for d in docs}
    engine.reranker=score
    result=engine.run('手机维修',method='semantic',min_cosine=.5)
    assert 'scene:s6' not in seen
    assert result['selected_refs']==[]


def test_configured_body_candidate_threshold_applies_on_next_recall(legacy):
    from dataclasses import replace
    _,build=legacy
    engine,calls=build([(f's{i}','scene',.90-i*.02,'2026-09-01') for i in range(6)]+
                       [('tail','event',.48,'2026-09-01')])
    engine.run('手机维修',method='semantic',min_cosine=.5)
    assert 'event:tail' not in {row['ref'] for row in calls}
    calls.clear()
    engine.policy=replace(engine.policy,body_candidate_threshold=.47)
    result=engine.run('手机维修',method='semantic',min_cosine=.5)
    assert 'event:tail' in {row['ref'] for row in calls}
    assert result['candidate_policy']['tail_body_or_passage_floor']==.47


def test_saved_pool_limit_admits_target_beyond_twenty_without_lowering_rerank_threshold(legacy):
    from serein.configured_models import effective_settings
    settings,build=legacy
    engine,_=build([(f's{i}','scene',.90-i*.01,'2026-09-01') for i in range(20)]+
                   [('target','scene',.59,'2026-09-01')])
    seen=[]
    def score(query,docs):
        seen.extend(d['ref'] for d in docs)
        return {d['ref']:(.95 if d['ref']=='scene:target' else .1) for d in docs}
    engine.reranker=score
    before=engine.run('手机维修',method='semantic',min_cosine=.5)
    assert len(seen)==20 and 'scene:target' not in seen
    assert before['selected_refs']==[]
    save_settings(settings.database,{'recall':{'direct_pool_limit':30}})
    seen.clear()
    engine=Recall(effective_settings(settings),reranker=score)
    after=engine.run('手机维修',method='semantic',min_cosine=.5)
    assert len(seen)==21 and 'scene:target' in seen
    assert after['selected_refs']==['scene:target']
    assert after['candidate_policy']['direct_pool_limit']==30
    assert after['candidate_policy']['pool_limit']==30
    assert after['candidate_policy']['base_vector_pool_limit']==6
    assert engine.policy.direct_threshold==.65


@pytest.mark.parametrize('limit',[6,30])
def test_configured_pool_stays_bounded_with_one_extra_relation(legacy,limit):
    from serein.configured_models import effective_settings
    settings,build=legacy
    _,calls=build([(f's{i}','scene',.90-i*.005,'2026-09-01') for i in range(32)]+
                  [('neighbor','scene',.4,'2026-09-01'),('other','scene',.3,'2026-09-01')])
    link_scenes(settings,[('a','s0','neighbor',1),('b','s0','other',1)])
    save_settings(settings.database,{'recall':{'direct_pool_limit':limit}})
    def score(query,docs):
        calls.extend(docs)
        return {d['ref']:.9 for d in docs}
    result=Recall(effective_settings(settings),reranker=score).run('手机维修',method='semantic',min_cosine=.5)
    assert len(calls)==limit+1
    assert result['candidate_retrieval']['actual_candidate_count']==limit+1
    assert result['candidate_policy']['pool_limit']==limit+1
    assert sum(row['candidate_origin']=='relation' for row in result['candidate_scores'])==1
    assert result['candidate_retrieval']['base_pool_count']==6


@pytest.mark.parametrize('cooled,expected',[(['scene:s0'],['scene:s1']),(['scene:s0','scene:s1'],[])])
def test_old_winners_are_cooled_without_refilling(legacy,cooled,expected):
    _,build=legacy
    engine,_=build([(f's{i}','scene',.9-i*.02,'2026-09-01') for i in range(3)]+[('e0','event',.8,'2026-09-01')])
    result=engine.run('手机维修',method='semantic',min_cosine=.5,delivered_ids=cooled)
    assert result['pre_cooldown_selected_refs']==['scene:s0','scene:s1']
    assert result['selected_refs']==expected


def test_recent_prior_changes_order_but_not_displayed_evidence_score(legacy):
    _,build=legacy
    engine,_=build([('old','scene',.81,'2025-01-01'),('new','scene',.78,'2026-09-01')])
    plain=engine.run('手机维修',method='semantic',min_cosine=.5)
    recent=engine.run('最近手机维修',method='semantic',min_cosine=.5)
    assert plain['selected_refs'][0]=='scene:old'
    assert recent['selected_refs'][0]=='scene:new'
    assert recent['cards'][0]['score']==.78
    assert recent['pools']['scene']['items'][0]['freshness']['weight']==.05


def test_surface_candidates_below_old_floor_are_judged_by_reranker(legacy):
    _,build=legacy
    engine,calls=build([('low','event',.49,'2026-09-08')])
    result=engine.run('最近手机维修',method='semantic',min_cosine=.5)
    assert result['selected_refs']==['event:low']
    assert calls and result['candidate_scores'][0]['vector_score']==.49
    assert result['candidate_policy']['vector_floor'] is None
    engine.reranker=lambda query,docs:{d['ref']:.64 for d in docs}
    assert engine.run('最近手机维修',method='semantic',min_cosine=.5)['selected_refs']==[]
    assert engine.run('最近手机维修',method='semantic',mode='lookup',min_cosine=.5)['selected_refs']==[]


def test_passage_winner_keeps_short_canonical_body_whole_for_reranker(legacy,monkeypatch):
    settings,build=legacy
    engine,calls=build([('long','scene',.55,'2026-09-01')])
    with Store(settings.database,read_only=True) as store:stamp=content_stamp(store.read('long'))
    with sqlite3.connect(settings.index) as db:
        ensure_tables(db)
        for number,score in enumerate((.92,.88,.60)):
            db.execute('INSERT INTO passages VALUES (?,?,?,?,?,?,?,?,?)',
                ('long',number,stamp,number*10,number*10+4,f'证据 {number}',json.dumps([score,math.sqrt(1-score*score)]),2,'test'))
    from dataclasses import replace
    engine.policy=replace(engine.policy,passages_enabled=True)
    monkeypatch.setattr('serein.recall.passages.slices',lambda *a,**kw:pytest.fail('Recall attempted to split memory'))
    monkeypatch.setattr('serein.recall.passages.fill_passages',lambda *a,**kw:pytest.fail('Recall attempted to generate passage vectors'))
    result=engine.run('手机维修',method='semantic',min_cosine=.5)
    assert calls[0]['rerank_text']=='title: 手机维修 long\nbody: 手机屏幕坏了，我们带着它到店里维修。'
    assert result['cards'][0]['score']==.92
    engine.policy=replace(engine.policy,passages_enabled=False)
    calls.clear()
    result=engine.run('手机维修',method='semantic',min_cosine=.5,use_passages=True)
    assert result['cards'][0]['score']==.55
    assert '证据 0' not in calls[0]['rerank_text']


def test_generic_query_is_blocked_before_reranking(legacy):
    _,build=legacy
    engine,calls=build([('s','scene',.99,'2026-09-01')])
    result=engine.run('嗯嗯',method='semantic',min_cosine=.5)
    assert result['status']=='skipped' and not calls
    assert result['reason']=='global_query_lacks_specific_terms'


@pytest.mark.parametrize('query',['影分身','误召回'])
def test_weak_automation_topic_alone_does_not_retrieve(legacy,query):
    _,build=legacy
    engine,calls=build([('s','scene',.99,'2026-09-01')])
    result=engine.run(query,method='semantic',min_cosine=.5)
    assert result['reason']=='global_query_lacks_specific_terms' and not calls


def test_weak_phrase_does_not_leak_fragments_or_hide_real_topic():
    from serein.recall.germany.recall_policy import RecallPolicy
    policy=RecallPolicy()
    terms=policy.specific_query_terms('影分身修好了通知推送的误召回')
    assert terms==policy.specific_query_terms('修好了通知推送的')
    assert any('通知' in term for term in terms)
    assert '分身' in policy.specific_query_terms('分身')


def test_final_cards_follow_reranker_across_types_not_vector_order(legacy):
    _,build=legacy
    engine,_=build([('s','scene',.99,'2026-09-01'),('e1','event',.7,'2026-09-01'),('e2','event',.6,'2026-09-01')])
    engine.reranker=lambda query,docs:{'scene:s':.66,'event:e1':.81,'event:e2':.98}
    result=engine.run('手机维修',method='semantic',min_cosine=.5)
    assert result['selected_refs']==['event:e2','event:e1']
    cooled=engine.run('手机维修',method='semantic',min_cosine=.5,delivered_ids=['event:e2'])
    assert cooled['selected_refs']==['event:e1']
    assert len(result['candidate_scores'])==3


def test_one_qualified_card_never_fills_with_rejected_memory(legacy):
    _,build=legacy
    engine,_=build([('s','scene',.99,'2026-09-01'),('e','event',.98,'2026-09-01')])
    engine.reranker=lambda query,docs:{'scene:s':.649,'event:e':.65}
    assert engine.run('手机维修',method='semantic',min_cosine=.5)['selected_refs']==['event:e']


def test_provider_failure_does_not_fall_back_to_vector_injection(legacy):
    _,build=legacy
    engine,_=build([('s','scene',.99,'2026-09-01')])
    def failed(query,docs):raise ValueError('provider unavailable')
    engine.reranker=failed
    result=engine.run('手机维修',method='semantic',min_cosine=.5)
    assert not result['selected_refs']
    assert result['reranker_error']=='provider_score_unavailable'


def test_provider_auth_failure_is_visible_without_admitting_candidates(legacy):
    from serein.adapters.reranker import RerankerProviderError
    _,build=legacy
    engine,_=build([('s','scene',.99,'2026-09-01')])
    def failed(query,docs):raise RerankerProviderError('http_401','Authentication failed')
    engine.reranker=failed
    result=engine.run('手机维修',method='semantic',min_cosine=.5)
    assert not result['selected_refs'] and result['reranker_error']=='http_401'


def test_replaced_and_scene_covered_events_are_filtered_before_reranker(legacy,monkeypatch):
    settings,build=legacy
    engine,_=build([('replaced','event',.99,'2026-09-01'),('covered','event',.98,'2026-09-01'),
                    ('keep','event',.97,'2026-09-01'),('cover','scene',.96,'2026-09-01'),
                    ('archived','event',.95,'2026-09-01'),('manual','event',.94,'2026-09-01')])
    with Store(settings.database) as store:
        source=store.add_source('chat:1','同一条完整原话')
        store.bind('covered',source)
        store.bind('cover',source)
        store.conn.execute("INSERT INTO event_replacements VALUES ('replaced','successor','test','{}')")
        store.set_lifecycle('archived','archived')
        store.set_manual_surface('manual',False)
    seen=[]
    def rank(query,docs):
        seen.extend(row['ref'] for row in docs)
        return {row['ref']:.1 for row in docs}
    engine.reranker=rank
    monkeypatch.setattr('serein.core.reader.Reader.read',lambda *a,**kw:pytest.fail('candidate snapshot used Reader.read'))
    result=engine.run('手机维修',method='semantic',min_cosine=.5)
    assert set(seen)=={'event:keep','scene:cover'}
    assert result['candidate_retrieval']['snapshot_suppressed']['replaced_by_event']==1
    assert result['candidate_retrieval']['snapshot_suppressed']['covered_by_scene']==1
    assert result['candidate_retrieval']['snapshot_suppressed']['lifecycle_not_active']==1
    assert result['candidate_retrieval']['snapshot_suppressed']['manual_surface_not_enabled']==1


def link_scenes(settings, edges, *, association=True):
    if association:save_settings(settings.database,{'features':{'association':True}})
    with Store(settings.database) as store:
        for edge, source, target, active in edges:
            store.conn.execute('INSERT INTO scene_relations VALUES (?,?,?,?,?,?,?)',
                (edge,'test',source,target,'active',active,'{}'))


def test_one_best_reviewed_neighbor_joins_the_same_reranker_batch(legacy):
    settings,build=legacy
    engine,calls=build([(f's{i}','scene',.9-i*.02,'2026-09-01') for i in range(6)]+
        [('unreviewed','scene',.49,'2026-09-01'),('hop2','scene',.48,'2026-09-01'),
         ('neighbor','scene',.4,'2026-09-01'),('other','scene',.3,'2026-09-01')])
    link_scenes(settings,[('a','s0','other',1),('b','s0','neighbor',1),
        ('c','s1','neighbor',1),('d','s0','s1',1),('e','neighbor','hop2',1)])
    with Store(settings.database) as store:
        store.conn.execute("INSERT INTO scene_proposals VALUES ('pending','test','s0','unreviewed','pending','{}')")
    result=engine.run('手机维修',method='semantic',min_cosine=.5)
    assert [d['ref'] for d in calls]==[f'scene:s{i}' for i in range(6)]+['scene:neighbor']
    assert result['selected_refs']==['scene:s0','scene:s1']  # No guaranteed relation card.
    related=result['candidate_scores'][-1]
    assert related['candidate_origin']=='relation' and related['vector_score']==.4
    assert related['relation_candidate']['seed_id']=='s0'
    assert related['relation_candidate']['edge_id']=='b'
    assert calls[-1]['rerank_text']=='title: 手机维修 neighbor\nbody: 手机屏幕坏了，我们带着它到店里维修。'
    assert result['candidate_retrieval']['candidate_count']==7


@pytest.mark.parametrize('score,cooled,expected',[
    (.99,[],['scene:neighbor','scene:s0']),
    (.99,['scene:neighbor'],['scene:s0']),
    (.64,[],['scene:s0','scene:s1']),
])
def test_related_candidate_requires_score_and_obeys_final_cooldown(legacy,score,cooled,expected):
    settings,build=legacy
    engine,_=build([(f's{i}','scene',.9-i*.02,'2026-09-01') for i in range(6)]+
        [('neighbor','scene',.4,'2026-09-01'),('other','scene',.3,'2026-09-01')])
    link_scenes(settings,[('a','s0','neighbor',1),('b','s0','other',1)])
    seen=[]
    def rank(query,docs):
        seen.append(docs)
        return {d['ref']:score if d['ref']=='scene:neighbor' else .9 for d in docs}
    engine.reranker=rank
    result=engine.run('手机维修',method='semantic',min_cosine=.5,delivered_ids=cooled)
    assert len(seen)==1 and len(seen[0])==7
    assert result['selected_refs']==expected
    assert result['candidate_scores'][-1]['ref']=='scene:neighbor'
    assert all(d['ref']!='scene:other' for d in seen[0])


def test_relation_slot_does_not_bypass_domains_exclusions_or_inactive_edges(legacy):
    from dataclasses import replace
    settings,build=legacy
    engine,calls=build([(f's{i}','scene',.9-i*.02,'2026-09-01') for i in range(6)]+
        [(key,'scene',.7,'2026-09-01') for key in ('tech','excluded','archived','inactive')],
        metadata={'tech':{'canonical_domain':'tech'}})
    engine.policy=replace(engine.policy,domains={'tech':'excluded'})
    link_scenes(settings,[(key,'s0',key,int(key!='inactive')) for key in ('tech','excluded','archived','inactive')])
    with Store(settings.database) as store:store.set_lifecycle('archived','archived')
    result=engine.run('手机维修',method='semantic',min_cosine=.5,exclude_ids=['scene:excluded'])
    assert len(calls)==7 and all(d['ref'].startswith('scene:s') or d['ref']=='scene:inactive' for d in calls)
    assert all(d['candidate_origin']=='direct' for d in result['candidate_scores'])


def test_relation_candidate_stays_within_the_resolved_arc(legacy,monkeypatch):
    settings,build=legacy
    engine,calls=build([(f's{i}','scene',.9-i*.02,'2026-09-01') for i in range(6)]+
        [('outside','scene',.75,'2026-09-01'),('inside','scene',.4,'2026-09-01')])
    link_scenes(settings,[('out','s0','outside',1),('in','s0','inside',1)])
    monkeypatch.setattr('serein.recall.typed_surface.scope_members',lambda reader:{
        'arc:synthetic':{('scene',f's{i}') for i in range(6)}|{('scene','inside')}})
    monkeypatch.setattr('serein.recall.typed_surface.CandidateGateway._passage_candidate_shadow_debug',
        lambda *args:{'status':'retrieved','entity_scope':{'scope_anchor':{'arc_key':'arc:synthetic'},
            'operator':'none'},'lanes':{}})
    result=engine.run('手机维修',method='semantic',min_cosine=.5)
    assert [row['ref'] for row in calls]==[f'scene:s{i}' for i in range(6)]+['scene:inside']
    assert result['candidate_scores'][-1]['relation_candidate']['edge_id']=='in'


@pytest.mark.parametrize('old_settings',[False,True])
def test_association_api_toggle_is_persistent_live_and_independent(legacy,monkeypatch,old_settings):
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from serein.api.http import create_app
    from serein.recall import typed_surface
    settings,build=legacy
    engine,_=build([(f's{i}','scene',.9-i*.02,'2026-09-01') for i in range(6)]+
                   [('neighbor','scene',.4,'2026-09-01'),('other','scene',.3,'2026-09-01')])
    link_scenes(settings,[('a','s0','neighbor',1),('b','s0','other',1)],association=False)
    if old_settings:
        with Store(settings.database) as store:
            store.conn.execute("INSERT INTO background_state(name,value_json) VALUES ('deployment_settings',?)",
                               (json.dumps({'features':{'persona':False}}),))
    client=TestClient(create_app(replace(settings,writable=True),token='synthetic',live=True),
                      headers={'Authorization':'Bearer synthetic'})
    def state():return client.get('/v1/settings').json()['features']
    assert state()['association'] is False  # Missing field in old stored settings is opt-in too.
    batches=[]
    def rank(query,docs):
        batches.append([d['ref'] for d in docs]);return {d['ref']:.9 for d in docs}
    engine.reranker=rank
    additions=[];original=typed_surface.add_related_candidate
    def add(*args):additions.append(True);return original(*args)
    monkeypatch.setattr(typed_surface,'add_related_candidate',add)
    before=engine.run('手机维修',method='semantic',min_cosine=.5)
    assert not additions and before['related_candidates']==[]
    assert batches==[[f'scene:s{i}' for i in range(6)]]
    assert before['candidate_policy']['relation_pool_limit']==0

    client.patch('/v1/settings',json={'features':{'association':True}}).raise_for_status()
    assert state()['association'] and not state()['relations_auto_accept']
    reopened=TestClient(create_app(replace(settings,writable=True),token='synthetic',live=True),
                        headers={'Authorization':'Bearer synthetic'})
    assert reopened.get('/v1/settings').json()['features']['association'] is True
    enabled=engine.run('手机维修',method='semantic',min_cosine=.5)  # Same Recall instance, no restart/index rebuild.
    assert len(additions)==1 and len(batches)==2
    assert batches[-1]==batches[0]+['scene:neighbor']
    assert enabled['candidate_policy']['pool_limit']==21
    assert enabled['candidate_policy']['association_enabled'] is True
    assert enabled['selected_refs']==before['selected_refs']  # No reserved final slot.

    client.patch('/v1/settings',json={'features':{'association':False,'relations_auto_accept':True}}).raise_for_status()
    disabled=engine.run('手机维修',method='semantic',min_cosine=.5)
    assert state()['relations_auto_accept'] and not state()['association']
    assert len(additions)==1 and len(batches)==3 and batches[-1]==batches[0]
    assert disabled['candidate_scores']==before['candidate_scores']
    assert disabled['selected_refs']==before['selected_refs'] and disabled['related_candidates']==[]
    assert disabled['candidate_policy']['association_enabled'] is False
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM scene_relations WHERE active=1').fetchone()[0]==2


@pytest.mark.parametrize('kind', ['scene', 'event'])
def test_missing_opening_reaches_the_existing_single_reranker_call(legacy, kind):
    from dataclasses import replace
    settings, build = legacy
    opening = 'Mira和Orion第一次是在读书会上认识的。'
    parts = [opening + '一起讨论了那本书。' * 12, '后来一起散步。' * 15, '现在仍然喜欢彼此。' * 15]
    body = '\n'.join(parts)
    engine, _ = build([('opening', kind, .55, '2026-09-01')], bodies={'opening': body})
    engine.policy = replace(engine.policy, passages_enabled=True)
    with Store(settings.database, read_only=True) as store:
        stamp = content_stamp(store.read('opening'))
    with sqlite3.connect(settings.index) as db:
        ensure_tables(db)
        start = 0
        for ordinal, (part, score) in enumerate(zip(parts, (.6, .88, .92))):
            db.execute('INSERT INTO passages VALUES (?,?,?,?,?,?,?,?,?)',
                       ('opening', ordinal, stamp, start, start + len(part), part,
                        json.dumps([score, math.sqrt(1-score*score)]), 2, 'test'))
            start += len(part) + 1
    calls = []
    def rank(query, documents):
        calls.append((query, documents))
        return {d['ref']: .9 if opening in d['rerank_text'] else .01 for d in documents}
    engine.reranker = rank
    result = engine.run('我们是怎么认识的？', method='semantic', min_cosine=.5)
    assert len(calls) == 1
    assert calls[0][1][0]['rerank_text'] == f'title: 手机维修 opening\nbody: {body}'
    assert result['selected_refs'] == [f'{kind}:opening']
    assert result['cards'][0]['score'] == .92
