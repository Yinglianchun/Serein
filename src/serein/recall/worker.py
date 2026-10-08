"""Persistent changed-owner queue; provider failures leave work pending."""

from .index_safety import guarded_index, IndexBlocked
import logging
import threading

from ..core.store import Store
from .index import refresh_index
from .vectors import fill_vectors
from .passages import fill_passages, prepare_passages
from .entities import rebuild_entities
from .policy import RecallPolicy


@guarded_index
def update_pending(settings, *, client=None):
    with Store(settings.database,read_only=True) as store:
        rows=store.conn.execute('SELECT * FROM index_outbox ORDER BY sequence LIMIT 100').fetchall()
    if not rows:
        return {'status':'current','updated':0}
    ids={row['document_id'] for row in rows}
    from .passage_layouts import prepare_layouts
    prepare_layouts(settings, ids)
    from ..configured_models import effective_settings
    settings = effective_settings(settings)
    refresh_index(settings.database,settings.index,ids)
    prepare_passages(settings, document_ids=ids)
    if settings.embedding or client is not None:
        fill_vectors(settings,document_ids=ids,client=client)
        if RecallPolicy.from_config(settings.recall).passages_enabled:
            fill_passages(settings,document_ids=ids,client=client)
    from ..compat.entity_scope import update_scope
    entities=update_scope(settings)
    rebuild_entities(settings,legacy_rows=entities,document_ids=ids)
    from ..deployment import task_model
    if settings.embedding and settings.background.get('germany_config_file') and settings.recall.get('germany_policy_file') and not task_model(settings.database,'operit_tagging'):
        import asyncio
        from .legacy_indexes import refresh
        report=asyncio.run(refresh(settings))
        # Failed cue bindings have their own durable attempt record. The other
        # index updates succeeded, so retaining this outbox would poll forever.
    with Store(settings.database) as store:
        # Writes received during model requests stay queued for the next cycle.
        store.conn.execute('DELETE FROM index_outbox WHERE sequence<=?',(rows[-1]['sequence'],))
    return {'status':'updated','updated':len(ids)}


class IndexWorker:
    def __init__(self,settings):
        self.settings=settings
        self.stop=threading.Event()
        self.thread=threading.Thread(target=self.run,name='serein-index',daemon=True)

    def run(self):
        while not self.stop.is_set():
            try:
                update_pending(self.settings)
            except IndexBlocked:
                pass  # Durable pause/backoff/busy state is exposed in Settings.
            except Exception:
                logging.getLogger(__name__).warning('Index update deferred; inspect persisted index status')
            self.stop.wait(5)
