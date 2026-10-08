import json
import sqlite3

import pytest

from serein.config import Settings
from serein.core import Store
from serein.recall.index import build_index, Search
from serein.recall.vectors import fill_vectors, coverage


@pytest.fixture
def setup(tmp_path):
    database, index = tmp_path / 'data.db', tmp_path / 'index.db'
    with Store(database) as store:
        store.create('a', 'event', '事件', '原始事件')
        store.create('b', 'scene', '场景', '场景正文\n## 评论\n不是正文证据')
        store.create('c', 'event', '旧事', '归档经历', lifecycle='archived')
        store.create('d', 'event', '删除', '删除经历', lifecycle='deleted')
        store.create('n', 'narrative', '叙事', '不能进入自动向量范围')
    build_index(database, index)
    profile = dict(model='test', provider_host='test.invalid', document_instruction='', query_instruction='', max_chars=6000)
    with sqlite3.connect(index) as conn:
        conn.execute("INSERT INTO settings VALUES ('embedding_profile',?)", (json.dumps(profile),))
        conn.execute("INSERT INTO settings VALUES ('embedding_dimension','2')")
    class Client:
        dimension = 2
        def __init__(self):
            self.profile = profile
            self.inputs = []
        def documents(self, texts):
            self.inputs.extend(texts)
            return [[1, 0] for _ in texts]
    return Settings(database, index), Client()


def test_backfill_reuses_vectors_cleans_scene_body_and_excludes_deleted_narrative(setup):
    settings, client = setup
    result = fill_vectors(settings, client=client, batch_size=2)
    assert result['embedded'] == 3 and result['requests'] == 2
    assert result['coverage']['event'] == dict(readable=2, surfaceable=1, covered=2, missing=0, surfaceable_covered=1)
    assert client.inputs == ['原始事件', '场景正文', '归档经历']
    assert fill_vectors(settings, client=client)['requests'] == 0
    with Search(settings.database, settings.index) as search:
        query = dict(query='问题', profile=client.profile, embedding=[1, 0])
        result = search.search('问题', query_embedding=query, min_cosine=.5)
        assert {item['kind'] for item in result['items']} == {'event', 'scene'}
        assert len(result['items']) == 2


def test_provider_failure_resumes_completed_batches_and_changed_input_is_not_cached(setup):
    settings, client = setup
    original = client.documents
    def fail_second(texts):
        if client.inputs:
            raise ValueError('provider unavailable')
        return original(texts)
    client.documents = fail_second
    with pytest.raises(ValueError, match='contract_error'):
        fill_vectors(settings, client=client, batch_size=1)
    assert coverage(settings.database, settings.index)['event']['covered'] == 1
    from serein.recall.index_safety import index_status, resume_index
    resume_index(settings.database, index_status(settings.database)["recovery_token"])
    client.documents = original
    assert fill_vectors(settings, client=client)['embedded'] == 2
    with Store(settings.database) as store:
        store.revise('b', expected_revision=1, title='修改', body_md='刚修改的正文')
    def change_again(texts):
        with Store(settings.database) as store:
            store.revise('b', expected_revision=2, title='再次修改', body_md='请求期间改过了')
        return original(texts)
    client.documents = change_again
    result = fill_vectors(settings, client=client)
    assert result['embedded'] == 0 and result['skipped']['changed_during_embedding'] == 1
    assert result['coverage']['scene']['covered'] == 0
