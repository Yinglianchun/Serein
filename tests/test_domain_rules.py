"""Synthetic public instances only: per-kind rules never rewrite memories."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from serein.api.http import create_app
from serein.bootstrap import initialize
from serein.config import Settings
from serein.core.store import Store, Conflict, encode
from serein.deployment import DEFAULT_DOMAINS, read_settings, save_settings
from serein.recall.index import build_index
from serein.recall.service import Recall


@pytest.fixture
def instance(tmp_path):
    settings = Settings(tmp_path/'memory.db', tmp_path/'index.db', writable=True)
    initialize(Settings(settings.database, writable=True))
    with Store(settings.database) as store:
        for kind in ('event', 'scene'):
            store.create(kind, kind, kind + '合成标题', 'Mira 在雨亭约好读书。',
                         metadata={'canonical_domain':'life', 'scene_cues':['雨亭约定']})
    build_index(settings.database, settings.index)
    return settings


def test_old_rules_apply_to_both_and_old_client_cannot_erase_overrides(instance):
    legacy = [{**domain, 'policy':'excluded' if domain['key']=='life' else 'normal'} for domain in DEFAULT_DOMAINS]
    save_settings(instance.database, {'tagging':{'domains':legacy}})
    for kind in ('event', 'scene'):
        assert Recall(instance).run(kind+'合成标题', mode='lookup')['selected_refs'] == []
    save_settings(instance.database, {'tagging':{'policies':{'event':{'life':'normal'}}}})
    assert Recall(instance).run('event合成标题')['selected_refs'] == ['event:event']
    assert Recall(instance).run('scene合成标题')['selected_refs'] == []
    save_settings(instance.database, {'tagging':{'domains':legacy}})
    assert read_settings(instance.database)['tagging']['policies'] == {'event':{'life':'normal'}}
    assert Recall(instance).run('event合成标题')['selected_refs'] == ['event:event']


def test_settings_api_independent_rules_conflicts_and_no_memory_mutation(instance):
    client = TestClient(create_app(instance, token='synthetic', live=True), headers={'Authorization':'Bearer synthetic'})
    from serein.imports import initialize_imports
    initialize_imports(instance.database)
    with Store(instance.database, read_only=True) as store:
        before = [store.read(kind) for kind in ('event', 'scene')]
        queue = list(store.conn.execute('SELECT document_id FROM tagging_outbox'))
    version = client.get('/v1/settings').json()['tagging_version']
    first = client.patch('/v1/settings', json={'expected_tagging_version':version,
        'tagging':{'policies':{'event':{'life':'excluded'}}}})
    assert first.status_code == 200, first.text
    stale = client.patch('/v1/settings', json={'expected_tagging_version':version,
        'tagging':{'policies':{'scene':{'life':'excluded'}}}})
    assert stale.status_code == 400 and 'domain_policy_publish_version_conflict' in stale.text
    second = client.patch('/v1/settings', json={'expected_tagging_version':first.json()['tagging_version'],
        'tagging':{'policies':{'scene':{'life':'explicit_only'}}}})
    assert second.status_code == 200, second.text
    assert second.json()['tagging']['policies'] == {'event':{'life':'excluded'}, 'scene':{'life':'explicit_only'}}
    for policies in ({'diary':{'life':'normal'}}, {'event':{'unknown':'normal'}}, {'scene':{'life':'maybe'}}):
        assert client.patch('/v1/settings', json={'tagging':{'policies':policies}}).status_code in (400,422)
    with Store(instance.database, read_only=True) as store:
        assert before == [store.read(kind) for kind in ('event', 'scene')]
        assert queue == list(store.conn.execute('SELECT document_id FROM tagging_outbox'))
    old_alias = client.post('/api/semantic-recall/domain-policies/publish', json={
        'confirm':'PUBLISH_DOMAIN_RECALL_POLICIES', 'expected_dataset_version':version,
        'domains':DEFAULT_DOMAINS})
    assert old_alias.status_code == 409


def test_compare_and_save_is_inside_the_database_write_lock(instance):
    version = read_settings(instance.database)['tagging_version']
    def save(kind):
        try:
            save_settings(instance.database, {'expected_tagging_version':version,
                'tagging':{'policies':{kind:{'life':'excluded'}}}})
            return 'saved'
        except Conflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, ('event', 'scene'))) == ['conflict', 'saved']
    assert len(read_settings(instance.database)['tagging']['policies']) == 1


@pytest.mark.parametrize('kind', ['event', 'scene'])
def test_explicit_only_checks_title_id_cue_but_not_body_or_entity(instance, kind):
    from serein.recall.query import Query
    from serein.recall.scene import domain_rejection
    save_settings(instance.database, {'tagging':{'policies':{kind:{'life':'explicit_only'}}}})
    engine = Recall(instance)
    with Store(instance.database, read_only=True) as store:
        doc = store.read(kind)
        for anchor in (kind+'合成标题', kind, '雨亭约定'):
            assert domain_rejection(doc, Query('还记得'+anchor+'吗' if anchor != kind else anchor), engine.policy) is None
        assert domain_rejection(doc, Query('Mira 在哪里读书'), engine.policy) == 'domain_explicit_only'
    other = 'scene' if kind == 'event' else 'event'
    assert engine.run('Mira', mode='lookup')['selected_refs'] == [other+':'+other]
    assert engine.run(kind+'合成标题')['selected_refs'] == [kind+':'+kind]


@pytest.mark.parametrize('kind', ['event', 'scene'])
def test_entity_candidates_apply_rules_for_each_kind(instance, kind):
    from serein.recall.entities import ensure_tables, candidates, content_stamp, evidence_stamp
    from serein.recall.index import Search
    from serein.recall.query import Query
    with sqlite3.connect(instance.index) as conn, Store(instance.database, read_only=True) as store:
        ensure_tables(conn)
        for owner in ('event', 'scene'):
            conn.execute('INSERT INTO entity_observations VALUES (?,?,?,?,?,?,?)',
                         (owner, 'mira', 'Mira', content_stamp(store.read(owner)), evidence_stamp([]), encode([]), 'synthetic'))
    save_settings(instance.database, {'tagging':{'policies':{kind:{'life':'excluded'}}}})
    with Search(instance.database, instance.index) as search:
        assert [hit['kind'] for hit in candidates(search, Query('还记得 Mira 吗'), Recall(instance).policy)] == ['scene' if kind=='event' else 'event']


@pytest.mark.parametrize('blocked', ['event', 'scene'])
def test_typed_auto_candidates_use_the_correct_rules(instance, tmp_path, monkeypatch, blocked):
    from dataclasses import replace
    profile = dict(model='synthetic', provider_host='synthetic.invalid', query_instruction='', document_instruction='', max_chars=6000)
    with sqlite3.connect(instance.index) as conn:
        conn.execute("INSERT INTO settings VALUES ('embedding_profile',?)", (json.dumps(profile),))
        conn.execute("INSERT INTO settings VALUES ('embedding_dimension','2')")
        for kind in ('event', 'scene'):
            conn.execute('INSERT INTO vectors VALUES (?,?,2)', (kind, '[1,0]'))
    class Embedding:
        def __init__(self, *args, **kwargs): pass
        def query(self, text, **kwargs): return {'query':text, 'profile':profile, 'embedding':[1,0]}
    monkeypatch.setattr('serein.adapters.embedding.EmbeddingClient', Embedding)
    monkeypatch.setattr('serein.recall.routing.route_query', lambda *args, **kwargs: {'route':'recall_needed','action':'recall','reason':'synthetic','scores':[]})
    policy = tmp_path/'policy.json'
    policy.write_text('{}', encoding='utf-8')
    configured = replace(instance, embedding={'endpoint':'unused', 'api_key_env':'unused'},
                         recall={'routing_file':'unused', 'germany_policy_file':str(policy)})
    save_settings(instance.database, {'tagging':{'policies':{blocked:{'life':'excluded'}}}})
    seen = []
    def rerank(query, docs):
        seen.extend(doc['ref'] for doc in docs)
        return {doc['ref']:.9 for doc in docs}
    result = Recall(configured, reranker=rerank).run('Mira 读书的约定', method='semantic', min_cosine=.5)
    other = 'scene' if blocked=='event' else 'event'
    assert result['selected_refs'] == [other+':'+other]
    assert seen == [other+':'+other]


@pytest.mark.parametrize('blocked', ['event', 'scene'])
def test_relation_diffusion_checks_both_endpoints_and_adds_at_most_one(instance, blocked):
    from serein.core.reader import Reader
    from serein.recall.query import Query
    from serein.recall.scene import related_candidates
    save_settings(instance.database, {'features':{'association':True}})
    with Store(instance.database) as store:
        store.create('second', 'scene', '第二个合成标题', '雨亭约定后续', metadata={'canonical_domain':'life'})
        store.conn.execute("INSERT INTO scene_relations VALUES ('edge','synthetic','event','scene','active',1,'{}')")
        store.conn.execute("INSERT INTO scene_relations VALUES ('edge2','synthetic','event','second','active',1,'{}')")
    with Reader(instance.database) as reader:
        assert len(related_candidates(reader, ['event'], Query('Mira'), Recall(instance).policy)) == 1
    save_settings(instance.database, {'tagging':{'policies':{blocked:{'life':'excluded'}}}})
    with Reader(instance.database) as reader:
        assert related_candidates(reader, ['event'], Query('Mira'), Recall(instance).policy) == []
