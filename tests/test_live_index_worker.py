import json
import sqlite3

import pytest

from serein.compat.events import Events
from serein.config import Settings
from serein.core import Store
from serein.recall.index import build_index,Search
from serein.recall.worker import update_pending
from test_live_events import item


def test_failed_provider_is_retried_and_old_backlog_does_not_block_new_event(tmp_path):
    settings=Settings(tmp_path/'serein.db',tmp_path/'index.sqlite',writable=True)
    with Store(settings.database) as store:
        store.create('scene_backlog','scene','旧的长篇','过去的一段经历。'*50)
    events=Events(settings.database,initialize=True)
    build_index(settings.database,settings.index)
    profile=dict(model='test',provider_host='test.invalid',document_instruction='',query_instruction='',max_chars=6000)
    with sqlite3.connect(settings.index) as conn:
        conn.execute("INSERT INTO settings VALUES ('embedding_profile',?)",(json.dumps(profile),))
        conn.execute("INSERT INTO settings VALUES ('embedding_dimension','2')")
    class Client:
        dimension=2
        inputs=[]
        def documents(self,texts):
            self.inputs+=texts
            if self.fail:raise ValueError('offline')
            return [[1,0] for _ in texts]
    client=Client();client.profile=profile;client.fail=True
    key=events.write_many([item()])['items'][0]['item_id']
    with pytest.raises(ValueError,match='contract_error'):
        update_pending(settings,client=client)
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM index_outbox').fetchone()[0]>0
    from serein.recall.index_safety import index_status, resume_index
    resume_index(settings.database, index_status(settings.database)["recovery_token"])
    client.fail=False;client.inputs=[]
    assert update_pending(settings,client=client)['updated']==1
    assert client.inputs==[item()['body']] # Short Event has no passage request.
    with Search(settings.database,settings.index) as search:
        assert search.conn.execute('SELECT 1 FROM vectors WHERE id=?',(key,)).fetchone()
        assert not search.conn.execute("SELECT 1 FROM vectors WHERE id='scene_backlog'").fetchone()
    assert update_pending(settings,client=client)['status']=='current'
