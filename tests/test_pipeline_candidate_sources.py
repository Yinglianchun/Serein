"""Historical source loading stays exact while using the existing raw-ID index."""
import json

import pytest

from test_public_features import settings
from serein.core.store import Store, digest, encode
from serein.extensions import pipeline as p


STAMP = '2026-10-01T00:00:00Z'


@pytest.fixture
def catalog(settings):
    p.initialize(settings.database)
    with Store(settings.database) as store:
        yield settings.database, store.conn


def _event(conn, key, *, manual=False):
    conn.execute('INSERT INTO fact_events(item_id,schema_version,fingerprint,origin_id,item_type,title,body,local_date,local_end_date,local_start_time,local_end_time,source_started_at,source_ended_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 (key, '1', digest(key), ('manual:' if manual else 'assistant_bridge:') + key,
                  'event', key, 'Saved body', '2026-10-01', '2026-10-01', STAMP, STAMP, STAMP, STAMP, STAMP, STAMP))
    conn.execute('INSERT INTO pipeline_track_events VALUES (?,?)', ('track', key))
    conn.execute('INSERT INTO pipeline_event_details VALUES (?,?)',
                 (key, json.dumps({'material_snapshot': {'synthetic': key},
                                   'source_activity_roles': {'1': 'primary_activity'}})))


def _raw(conn, key, *, source='synthetic', session='window', content=None, role='user',
         stamp=STAMP, metadata=None):
    content = content if content is not None else 'Full original ' + key
    return conn.execute('INSERT INTO raw_events(source,source_event_id,event_hash,role,text,created_at,ingested_at,session_id,metadata_json) VALUES (?,?,?,?,?,?,?,?,?)',
                        (source, key, digest(encode([key, session, content, stamp])), role, content,
                         stamp, STAMP, session, json.dumps(metadata or {}))).lastrowid


def _bind(conn, event, raw_id=None, *, key='missing', source='synthetic', session='window',
          content='Bound original without raw', role='user', stamp=STAMP):
    if raw_id is not None:
        raw = conn.execute('SELECT * FROM raw_events WHERE id=?', (raw_id,)).fetchone()
        key, source, session, content, role, stamp = (
            raw['source_event_id'], raw['source'], raw['session_id'], raw['text'], raw['role'], raw['created_at'])
    conn.execute('INSERT INTO fact_event_sources(item_id,source_system,session_id,message_id,role,content,created_at,content_sha256,hash_algorithm,evidence_kind,binding_method) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                 (event, source, session, key, role, content, stamp, digest(content), 'sha256', 'primary', 'synthetic'))


def _legacy_originals(conn, event):
    """One-row lookup oracle used before batching, including its missing-raw fallback."""
    result = []
    for ref in conn.execute('SELECT * FROM fact_event_sources WHERE item_id=? ORDER BY id', (event,)):
        raw = conn.execute('SELECT * FROM raw_events WHERE source=? AND session_id=? AND source_event_id=?',
                           p.source_key(ref)).fetchone()
        if raw is None:
            raw = {'id': -int(digest(encode(p.source_key(ref)))[:12], 16),
                   'source': ref['source_system'], 'source_event_id': ref['message_id'],
                   'session_id': ref['session_id'], 'role': ref['role'], 'text': ref['content'],
                   'created_at': ref['created_at']}
        result.append(p.task_message(raw))
    return result


def _trace_stores(monkeypatch, statements):
    parent = p.Store
    class TracedStore(parent):
        def __init__(self, path, *, read_only=False):
            super().__init__(path, read_only=read_only)
            if read_only:
                self.conn.set_trace_callback(statements.append)
    monkeypatch.setattr(p, 'Store', TracedStore)


def test_261_sources_use_four_indexed_reads_and_keep_every_original(catalog, monkeypatch):
    database, conn = catalog
    conn.execute('BEGIN')
    for index in range(4):
        _event(conn, f'event-{index}')
    for index in range(261):
        raw_id = _raw(conn, f'message-{index}', content=f'Original {index}:' + '字' * 620)
        _bind(conn, f'event-{index % 4}', raw_id)
    conn.executemany('INSERT INTO raw_events(source,source_event_id,event_hash,role,text,created_at,ingested_at,session_id) VALUES (?,?,?,?,?,?,?,?)',
                     [('synthetic', f'noise-{i}', digest(f'noise-{i}'), 'user', 'Unrelated raw',
                       STAMP, STAMP, 'window') for i in range(10000)])
    conn.commit()
    expected = {f'event-{index}': _legacy_originals(conn, f'event-{index}') for index in range(4)}
    statements = []
    _trace_stores(monkeypatch, statements)

    bases = p.candidates(database, ['track'])

    reads = [sql for sql in statements if sql.lstrip().upper().startswith('SELECT') and 'raw_events' in sql]
    assert len(reads) == 4
    assert len(bases) == 4 and sum(len(base['originals']) for base in bases) == 261
    for base in bases:
        assert base['originals'] == expected[base['event_id']]
        assert base['source_message_ids'] == [message['id'] for message in expected[base['event_id']]]
        assert base['material_snapshot'] == {'synthetic': base['event_id']}
    plan = list(conn.execute('EXPLAIN QUERY PLAN ' + reads[0]))
    assert any('SEARCH r USING INDEX idx_raw_events_source_event_id' in row[3] for row in plan)


def test_source_and_session_match_exactly_before_raw_can_replace_bound_ref(catalog):
    database, conn = catalog
    _event(conn, 'event')
    _raw(conn, 'same-id', session='another-window', content='Wrong session original')
    _raw(conn, 'same-id', source='another-system', content='Wrong system original')
    _bind(conn, 'event', key='same-id', content='Exact bound fallback')
    exact = _raw(conn, 'exact-id', content='Exact matching original')
    _bind(conn, 'event', exact)

    base, = p.candidates(database, ['track'])

    assert base['originals'] == _legacy_originals(conn, 'event')
    assert [message['content'] for message in base['originals']] == ['Exact bound fallback', 'Exact matching original']
    assert base['source_message_ids'][0] < 0 and base['source_message_ids'][1] == exact
    assert base['material_snapshot'] == {'synthetic': 'event'}


def test_legacy_empty_id_keeps_the_original_first_row_lookup(catalog, monkeypatch):
    database, conn = catalog
    _event(conn, 'event')
    old = _raw(conn, '', content='Earlier empty ID', stamp='2026-09-01T00:00:00Z')
    newest = _raw(conn, '', content='Later empty ID', stamp='2026-10-02T00:00:00Z')
    _bind(conn, 'event', old)
    expected = _legacy_originals(conn, 'event')
    assert expected[0]['id'] == newest
    statements = []
    _trace_stores(monkeypatch, statements)

    base, = p.candidates(database, ['track'])

    assert base['originals'] == expected
    assert base['material_snapshot'] is None
    reads = [sql for sql in statements if sql.lstrip().upper().startswith('SELECT') and 'raw_events' in sql]
    assert len(reads) == 1 and 'JOIN' not in reads[0]


@pytest.mark.parametrize('field,value', [('text', 'Changed raw body'), ('role', 'assistant'),
                                        ('created_at', '2026-10-02T00:00:00Z'),
                                        ('content_sha256', 'incorrect-bound-content-hash')])
def test_changed_source_still_invalidates_inherited_material_snapshot(catalog, field, value):
    database, conn = catalog
    _event(conn, 'event')
    raw_id = _raw(conn, 'source')
    _bind(conn, 'event', raw_id)
    if field == 'content_sha256':
        conn.execute('UPDATE fact_event_sources SET content_sha256=? WHERE item_id=?', (value, 'event'))
    else:
        conn.execute(f'UPDATE raw_events SET {field}=? WHERE id=?', (value, raw_id))

    base, = p.candidates(database, ['track'])

    assert base['material_snapshot'] is None
    assert base['originals'] == _legacy_originals(conn, 'event')


def test_raw_projection_retains_metadata_without_copying_inline_image_bytes(catalog):
    database, conn = catalog
    _event(conn, 'event')
    raw_id = _raw(conn, 'image', metadata={'attachments': [{'kind': 'image', 'content_base64': 'INLINE_BYTES',
                                                          'url': 'data:image/png;base64,INLINE_BYTES'}]})
    _bind(conn, 'event', raw_id)

    base, = p.candidates(database, ['track'])

    assert base['originals'] == _legacy_originals(conn, 'event')
    assert base['originals'][0]['metadata']['attachments'] == [
        {'kind': 'image', 'content_base64': '[task image source]', 'url': '[task image source]'}]


def test_component_keeps_ref_order_shared_source_dedup_and_protection(catalog, monkeypatch):
    database, conn = catalog
    for event in ('event-a', 'event-b'):
        _event(conn, event, manual=event == 'event-b')
    shared = _raw(conn, 'shared')
    later = _raw(conn, 'later')
    _bind(conn, 'event-a', later)
    _bind(conn, 'event-a', shared)
    _bind(conn, 'event-b', shared)
    _bind(conn, 'event-b', key='missing')
    expected = {key: _legacy_originals(conn, key) for key in ('event-a', 'event-b')}
    current = p.task_message({'id': 10001, 'source': 'synthetic', 'source_event_id': 'new',
                              'session_id': 'window', 'role': 'user', 'text': 'New input', 'created_at': STAMP})
    card = {'track_id': 'track', 'subject': 'Work', 'throughline': 'Continue work', 'status': 'active',
            'event_policy': 'default'}
    assignment = {'source_message_id': current['id'], 'primary_track_id': 'track',
                  'context_track_ids': [], 'routing_role': 'primary_activity'}
    monkeypatch.setattr(p, 'route_result', lambda *args: ([assignment], [card], 1))
    monkeypatch.setattr(p, 'reference_blockers', lambda conn, key: ['active_scene_dependency'])

    component, = p.components(database, {'messages': [current], 'routing_messages': [current]}, {})

    fallback = expected['event-b'][-1]
    assert component['context_messages'] == [current, expected['event-a'][0], expected['event-a'][1], fallback]
    a, b = component['base_event_candidates']
    assert a['source_message_ids'] == [later, shared] and b['source_message_ids'] == [shared, fallback['id']]
    assert 'originals' not in a and 'originals' not in b
    assert a['protected'] and b['protected'] and a['continuation_allowed'] and not b['continuation_allowed']
    assert not a['manual'] and b['manual']
    assert a['source_activity_roles'] == b['source_activity_roles'] == {'1': 'primary_activity'}
    assert component['context_session_ids'] == [current['session_id']]
