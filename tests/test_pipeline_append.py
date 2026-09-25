"""Synthetic protected Event continuation keeps the referenced version intact."""

import pytest

from serein.compat.events import Events
from serein.compat.germany.fact_events import FactEventSettlementBlockedError
from serein.core.store import Store


def _ref(number, text):
    return {'source_system': 'synthetic', 'session_id': 'notebook', 'message_id': str(number),
            'role': 'user', 'created_at': f'2026-01-01T00:0{number}:00Z',
            'content': text, 'binding_method': 'archive_pipeline',
            'evidence_kind': 'primary' if number == 1 else 'supporting'}


def test_referenced_event_accepts_only_exact_append(tmp_path):
    database = tmp_path / 'empty.db'
    with Store(database):
        pass
    events = Events(database, initialize=True)
    old_ref = _ref(1, 'The notebook cover is loose.')
    first = events.settle('synthetic:first', [{
        'type': 'event', 'title': 'Notebook repair', 'body': 'The cover is loose.',
        'origin_id': 'assistant_bridge:synthetic:first', 'recallable': True,
        'source_refs': [old_ref],
    }])['items'][0]
    old_id = first['item_id']
    with Store(database) as store:
        source_id = store.conn.execute('SELECT source_id FROM evidence_bindings WHERE document_id=?',
                                       (old_id,)).fetchone()[0]
        store.create('synthetic_scene', 'scene', 'Cover sketch', 'Sketch before repair')
        store.bind('synthetic_scene', source_id)
    new_ref = _ref(2, 'I will use a blue stitch.')
    replacement = {
        'type': 'event', 'title': 'Notebook repair',
        'body': 'The cover is loose.\n\nI will use a blue stitch.',
        'origin_id': 'assistant_bridge:synthetic:second', 'recallable': True,
        'source_refs': [old_ref, new_ref], 'supersedes_item_ids': [old_id],
        'expected_predecessors': [{'item_id': old_id, 'fingerprint': first['fingerprint'],
                                   'source_keys': [{'source_system': 'synthetic',
                                                    'session_id': 'notebook', 'message_id': '1'}]}],
        'append_only': True,
    }
    bad = {**replacement, 'body': 'Rewritten old text.\n\nI will use a blue stitch.'}
    with pytest.raises(FactEventSettlementBlockedError, match='append_only'):
        events.settle('synthetic:bad', [bad])
    second = events.settle('synthetic:second', [replacement])['items'][0]
    with Store(database, read_only=True) as store:
        old = store.conn.execute('SELECT title,body,status FROM fact_events WHERE item_id=?',
                                 (old_id,)).fetchone()
        new = store.conn.execute('SELECT title,body,status FROM fact_events WHERE item_id=?',
                                 (second['item_id'],)).fetchone()
        assert tuple(old) == ('Notebook repair', 'The cover is loose.', 'superseded')
        assert tuple(new) == ('Notebook repair', replacement['body'], 'active')
        assert store.conn.execute('SELECT count(*) FROM fact_event_sources WHERE item_id=?',
                                  (second['item_id'],)).fetchone()[0] == 2
