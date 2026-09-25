"""Germany Event transactions with an atomic Serein reading projection.

The original Event tables retain settlement receipts and replacement semantics.
The documents/evidence projection is updated on the same SQLite connection before
commit; it is never an asynchronously maintained second canonical Event store.
"""

import json
import sqlite3
from contextlib import closing
from contextvars import ContextVar
from pathlib import Path

from ..core.store import Store, digest, encode, now
from .germany.fact_events import FactEventStore, FactEventSettlementBlockedError, _normalized_search_text


_settling = ContextVar('event_settlement', default=False)
_settlement_check = ContextVar('event_settlement_check', default=None)
_append_only_predecessors = ContextVar('append_only_predecessors', default=frozenset())


class EventConnection(sqlite3.Connection):
    project = False

    def commit(self):
        if self.project and self.in_transaction:
            check = _settlement_check.get()
            if check is not None:check(self)
            project_events(self)
        super().commit()

    def __exit__(self, kind, value, traceback):
        if kind is not None:
            self.rollback()
        else:
            try:
                self.commit()
            except BaseException:
                self.rollback()
                raise
        return False


class Events(FactEventStore):
    def __init__(self, database, *, initialize=False):
        super().__init__()
        self.db_path = str(database)
        self._initialized = not initialize
        if initialize:
            with Store(database, read_only=True):
                pass
            self._init_db()
            with closing(self._connect()) as conn:
                conn.project = False
                conn.executescript('''
                    CREATE TABLE IF NOT EXISTS event_projection_pending (
                        item_id TEXT PRIMARY KEY
                    );
                    CREATE TRIGGER IF NOT EXISTS event_projection_insert AFTER INSERT ON fact_events
                    WHEN NEW.item_type='event' BEGIN
                        INSERT OR IGNORE INTO event_projection_pending VALUES (NEW.item_id);
                    END;
                    CREATE TRIGGER IF NOT EXISTS event_projection_update AFTER UPDATE ON fact_events
                    WHEN NEW.item_type='event' BEGIN
                        INSERT OR IGNORE INTO event_projection_pending VALUES (NEW.item_id);
                    END;
                    CREATE TRIGGER IF NOT EXISTS event_projection_delete AFTER DELETE ON fact_events
                    WHEN OLD.item_type='event' BEGIN
                        INSERT OR IGNORE INTO event_projection_pending VALUES (OLD.item_id);
                    END;
                    CREATE TRIGGER IF NOT EXISTS event_projection_source AFTER INSERT ON fact_event_sources
                    BEGIN
                        INSERT OR IGNORE INTO event_projection_pending VALUES (NEW.item_id);
                    END;
                    CREATE TRIGGER IF NOT EXISTS event_projection_arc AFTER INSERT ON fact_event_arc_links
                    BEGIN
                        INSERT OR IGNORE INTO event_projection_pending VALUES (NEW.event_id);
                    END;
                ''')

    def _connect(self):
        # Neither reads nor writes create a typo-path database.
        conn = sqlite3.connect(Path(self.db_path).resolve().as_uri()+'?mode=rw', uri=True,
                               timeout=10, factory=EventConnection)
        conn.row_factory = sqlite3.Row
        conn.create_function('normalized_search_text', 1, _normalized_search_text, deterministic=True)
        conn.execute('PRAGMA foreign_keys=ON')
        conn.project = self._initialized
        return conn

    def settle(self, operation_id, raw_items, *, before_commit=None):
        check_marker = _settlement_check.set(before_commit)
        marker = _settling.set(True)
        append_marker = _append_only_predecessors.set(frozenset(
            predecessor for item in (raw_items if isinstance(raw_items, list) else [])
            if isinstance(item, dict) and item.get('append_only') is True
            for predecessor in (item.get('supersedes_item_ids')
                                if isinstance(item.get('supersedes_item_ids'), list) else [])))
        try:
            return super().settle(operation_id, raw_items)
        finally:
            _append_only_predecessors.reset(append_marker)
            _settling.reset(marker)
            _settlement_check.reset(check_marker)

    def read_many(self, item_ids, *, include_sources=True, resolve_active_successors=False):
        result = super().read_many(item_ids, include_sources=include_sources,
                                   resolve_active_successors=resolve_active_successors)
        if resolve_active_successors:
            with closing(self._connect()) as conn:
                for resolution in result.get('resolutions', []):
                    reasons = set(resolution.get('blocking_reasons', []))
                    for key in resolution.get('active_leaf_ids', []):
                        reasons.update(reference_blockers(conn, key))
                    resolution['blocking_reasons'] = sorted(reasons)
                    resolution['blocked'] = bool(resolution.get('blocked') or reasons)
        return result


def reference_blockers(conn, key):
    family = FactEventStore._replacement_family_payload(conn, key)['family_ids']
    placeholders = ','.join('?' for _ in family)
    reasons = []
    if family and conn.execute(
        'SELECT 1 FROM narrative_materials m JOIN documents d ON d.id=m.document_id AND d.revision=m.revision '
        "WHERE d.lifecycle='active' AND m.kind='event' AND m.disposition NOT IN ('excluded','mentioned') "
        f'AND m.target_id IN ({placeholders}) LIMIT 1', family).fetchone():
        reasons.append('active_narrative_reference')
    if conn.execute(
        'SELECT 1 FROM evidence_bindings e JOIN sources es ON es.id=e.source_id '
        'JOIN sources ss ON ss.source_key=es.source_key JOIN evidence_bindings s ON s.source_id=ss.id '
        'JOIN documents d ON d.id=s.document_id '
        "WHERE e.document_id=? AND e.active=1 AND s.active=1 AND d.kind='scene' AND d.lifecycle='active' LIMIT 1",(key,)).fetchone():
        reasons.append('active_scene_dependency')
    return reasons


def _protected_append_preserves(conn, predecessor_id):
    """Allow a referenced old version to remain untouched when its successor appends."""
    if predecessor_id not in _append_only_predecessors.get():
        return False
    predecessor = conn.execute('SELECT title,body,recallable FROM fact_events WHERE item_id=?',
                               (predecessor_id,)).fetchone()
    successors = conn.execute('SELECT e.title,e.body,e.recallable,e.status FROM fact_event_replacement_edges r '
                              'JOIN fact_events e ON e.item_id=r.successor_id WHERE r.predecessor_id=?',
                              (predecessor_id,)).fetchall()
    if predecessor is None or len(successors) != 1:
        return False
    successor = successors[0]
    return (successor['status'] == 'active' and successor['title'] == predecessor['title']
            and successor['recallable'] == predecessor['recallable']
            and str(successor['body']).startswith(str(predecessor['body']) + '\n\n')
            and bool(str(successor['body'])[len(str(predecessor['body'])) + 2:].strip()))


def project_events(conn):
    conn.execute("INSERT OR IGNORE INTO event_settlement_receipts "
        "SELECT operation_id,request_sha256,result_json,created_at,'germany_event' FROM fact_event_settlement_operations")
    pending = [r[0] for r in conn.execute('SELECT item_id FROM event_projection_pending')]
    if not pending:
        return
    store = Store.__new__(Store)
    store.conn = conn
    for key in pending:
        row = conn.execute('SELECT * FROM fact_events WHERE item_id=?', (key,)).fetchone()
        if row is None:
            if store.read(key):
                store.set_lifecycle(key, 'deleted')
                conn.execute('INSERT INTO index_outbox(document_id) VALUES (?)', (key,))
            continue
        if row['item_type'] != 'event':
            continue
        meta = dict(row)
        meta['recallable'] = None if row['recallable'] is None else bool(row['recallable'])
        lifecycle = 'deleted' if row['status'] == 'tombstoned' else row['status']
        old = store.read(key)
        if old and old['metadata'].get('entity_extraction_version'):
            from ..tagging_entities import TAGGING_FIELDS
            meta.update({name: old['metadata'][name] for name in TAGGING_FIELDS if name in old['metadata']})
        changed = old is None or (old['metadata'] != meta or old['body_md'] != row['body']
            or old['title'] != row['title'] or old['lifecycle'] != lifecycle
            or old['manual_surface'] != row['recallable'])
        if _settling.get() and old and old['lifecycle']=='active' and lifecycle=='superseded':
            blockers = reference_blockers(conn, key)
            if blockers and not _protected_append_preserves(conn, key):
                raise FactEventSettlementBlockedError(','.join(blockers))
        if old is None:
            store.create(key, 'event', row['title'], row['body'], metadata=meta,
                         lifecycle=lifecycle, manual_surface=meta['recallable'],
                         created_at=row['created_at'], updated_at=row['updated_at'])
        elif changed:
            revision = old['revision'] + 1
            store._add_revision(key, revision, row['title'], row['body'], meta, row['updated_at'])
            conn.execute('UPDATE documents SET revision=?,lifecycle=?,manual_surface=?,updated_at=? WHERE id=?',
                         (revision,lifecycle,row['recallable'],row['updated_at'],key))
        for ref in conn.execute('SELECT * FROM fact_event_sources WHERE item_id=? ORDER BY id', (key,)).fetchall():
            source_key = encode([ref['source_system'], ref['session_id'] or ref['thread_id'] or '', ref['message_id']])
            if digest(ref['content']) != ref['content_sha256']:
                raise ValueError('Event evidence must include its exact original content')
            source_id = store.add_source(source_key, ref['content'], metadata=dict(ref))
            # Preserve imported binding IDs rather than creating duplicate bindings.
            if not conn.execute('SELECT 1 FROM evidence_bindings WHERE document_id=? AND source_id=? AND active=1', (key,source_id)).fetchone():
                store.bind(key, source_id, metadata=dict(ref), actor='event_pipeline')
                changed = True
        for edge in conn.execute('SELECT * FROM fact_event_replacement_edges WHERE predecessor_id=? OR successor_id=?', (key,key)).fetchall():
            conn.execute('INSERT INTO event_replacements VALUES (?,?,?,?) ON CONFLICT(predecessor_id) DO UPDATE SET successor_id=excluded.successor_id,metadata_json=excluded.metadata_json',
                         (edge['predecessor_id'],edge['successor_id'],'germany_event',encode(dict(edge))))
        for link in conn.execute('SELECT * FROM fact_event_arc_links WHERE event_id=?', (key,)).fetchall():
            conn.execute('INSERT INTO event_arc_links VALUES (?,?,?,?) ON CONFLICT(arc_key,event_id) DO UPDATE SET metadata_json=excluded.metadata_json',
                         (link['arc_key'],key,'germany_event',encode(dict(link))))
        if lifecycle == 'deleted':
            store.record_deletion(key, row['updated_at'], meta)
        if changed:
            conn.execute('INSERT INTO index_outbox(document_id) VALUES (?)', (key,))
    conn.execute('DELETE FROM event_projection_pending')


def seed_events(database, source_database):
    """Explicit one-time transplant, on an offline cutover copy only."""
    events = Events(database, initialize=True)
    with sqlite3.connect(Path(source_database).resolve().as_uri()+'?mode=ro', uri=True) as source, closing(events._connect()) as conn:
        if conn.execute('SELECT 1 FROM fact_events LIMIT 1').fetchone():
            raise ValueError('Event transplant requires empty destination Event tables')
        tables = ('fact_events','fact_event_sources','fact_event_replacement_edges',
                  'fact_event_settlement_operations','fact_event_arc_links','fact_relation_proposals')
        conn.execute('BEGIN IMMEDIATE')
        try:
            for table in tables:
                cursor = source.execute('SELECT * FROM '+table)
                columns = [column[0] for column in cursor.description]
                conn.executemany('INSERT INTO '+table+' ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')', cursor.fetchall())
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return events.stats()
