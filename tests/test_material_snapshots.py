"""Pure synthetic tests for versioned, Event-bound material inheritance."""

from copy import deepcopy

import pytest

from serein.extensions.pipeline_materials import (
    annotation_contract, material_contracts, merge_review, snapshot_for,
    trusted_materials,
)


def material(source_id, use='main', quotes=None):
    return {'source_message_id': source_id, 'use': use, 'reason': 'Synthetic decision',
            'omit_quotes': quotes or []}


def fixture():
    messages = [
        {'id': 1, 'content': 'Repair the cover. The kettle is on.', 'role': 'user',
         'session_id': 7, 'original_session_id': 'synthetic-session', 'source': 'fixture',
         'source_event_id': 'first', 'created_at': '2026-01-01T00:00:00Z', 'metadata': {}},
        {'id': 2, 'content': 'I will repair it.', 'role': 'assistant',
         'session_id': 7, 'original_session_id': 'synthetic-session', 'source': 'fixture',
         'source_event_id': 'second', 'created_at': '2026-01-01T00:01:00Z', 'metadata': {}},
        {'id': 3, 'content': 'Use the blue paper.', 'role': 'user', 'session_id': 7},
    ]
    old_event = {'event_ref': 'event:original', 'source_message_ids': [2, 1],
                 'source_materials': [material(2, 'background'),
                                      material(1, 'mixed', ['The kettle is on.'])]}
    snapshot = snapshot_for(old_event, messages)
    assert snapshot is not None
    snapshot.update(event_id='saved:original', event_fingerprint='saved-fingerprint')
    base = {'event_id': 'saved:original', 'fingerprint': 'saved-fingerprint',
            'source_message_ids': [2, 1], 'material_snapshot': snapshot}
    event = {'event_ref': 'event:new', 'action': 'extend',
             'base_event_ids': ['saved:original'],
             'source_bindings': [{'source_message_id': source_id} for source_id in [2, 1, 3]]}
    component = {'writer_material_review': True, 'material_contract_version': 1,
                 'context_messages': messages, 'base_event_candidates': [base]}
    return messages, old_event, base, event, component


def test_snapshot_is_full_sorted_deterministic_and_not_trusted_until_saved():
    messages, old_event, base, _, _ = fixture()
    snapshot = snapshot_for(old_event, messages)
    assert set(snapshot) == {'schema', 'event_ref', 'source_fingerprints', 'source_materials'}
    assert snapshot['schema'] == 1
    assert [row['source_message_id'] for row in snapshot['source_fingerprints']] == [1, 2]
    assert snapshot == snapshot_for(old_event, list(reversed(messages)))
    assert all(len(row['fingerprint']) == 64 for row in snapshot['source_fingerprints'])
    assert trusted_materials({**base, 'material_snapshot': snapshot}, messages) is None
    inherited = trusted_materials(base, messages)
    assert inherited == old_event['source_materials']
    inherited[0]['reason'] = 'changed copy'
    snapshot['source_materials'][1]['omit_quotes'].append('changed copy')
    assert old_event['source_materials'][0]['reason'] == 'Synthetic decision'
    assert old_event['source_materials'][1]['omit_quotes'] == ['The kettle is on.']


@pytest.mark.parametrize('key,value', [
    ('content', 'Different content'), ('role', 'assistant'), ('session_id', 99),
    ('original_session_id', 'another-session'), ('source', 'another-source'),
    ('source_event_id', 'another-original'), ('created_at', '2026-01-02T00:00:00Z'),
    ('metadata', {'attachments': [{'id': 'changed-image'}]}),
    ('image_transcription', {'text': 'Changed evidence'}),
])
def test_source_fingerprint_changes_force_full_annotation(key, value):
    messages, _, base, event, component = fixture()
    messages[0][key] = value
    assert trusted_materials(base, messages) is None
    assert annotation_contract(event, component) == {
        'annotation_source_message_ids': [2, 1, 3], 'inherited_materials': []}


@pytest.mark.parametrize('metadata', [{'invalid': object()}, {'invalid': {1, 2}},
                                     {'invalid': float('nan')}, {1: 'one', 'two': 2}])
def test_noncanonical_source_metadata_cannot_create_or_reuse_a_snapshot(metadata):
    messages, old_event, base, _, _ = fixture()
    messages[0]['metadata'] = metadata
    assert snapshot_for(old_event, messages) is None
    assert trusted_materials(base, messages) is None


def image_fixture():
    messages, old_event, base, event, component = fixture()
    messages[0]['image_transcription'] = {'status': 'complete', 'items': [
        {'source_message_id': 1, 'position': 1, 'sha256': 'a' * 64,
         'text': '[Picture] A torn book cover.', 'unreadable': False,
         'evidence_role': 'owned'}]}
    snapshot = snapshot_for(old_event, messages)
    snapshot.update(event_id=base['event_id'], event_fingerprint=base['fingerprint'])
    base['material_snapshot'] = snapshot
    return messages, old_event, base, event, component


def test_image_cache_bookkeeping_does_not_invalidate_semantically_identical_evidence():
    messages, old_event, base, event, component = image_fixture()
    record = messages[0]['image_transcription']
    record['updated_at'] = '2026-01-02T00:00:00Z'
    record['items'][0].update(source_fingerprint='host-cache-binding', evidence_role='context_only')
    assert trusted_materials(base, messages) == old_event['source_materials']
    assert annotation_contract(event, component)['annotation_source_message_ids'] == [3]
    assert snapshot_for(old_event, messages)['source_fingerprints'] == base['material_snapshot']['source_fingerprints']
    record['updated_at'] = '2026-01-03T00:00:00Z'
    record['items'][0]['source_fingerprint'] = 'refreshed-host-cache-binding'
    assert trusted_materials(base, messages) == old_event['source_materials']


@pytest.mark.parametrize('key,value', [
    ('sha256', 'b' * 64), ('text', '[Picture] A newly repaired cover.'),
    ('position', 2), ('unreadable', True), ('source_message_id', 2),
])
def test_meaningful_image_evidence_changes_still_require_full_annotation(key, value):
    messages, _, base, event, component = image_fixture()
    messages[0]['image_transcription']['items'][0][key] = value
    assert trusted_materials(base, messages) is None
    assert annotation_contract(event, component)['annotation_source_message_ids'] == [2, 1, 3]


def test_duplicate_image_positions_cannot_create_or_reuse_a_snapshot():
    messages, old_event, base, _, _ = image_fixture()
    rows = messages[0]['image_transcription']['items']
    rows.append(deepcopy(rows[0]))
    assert snapshot_for(old_event, messages) is None
    assert trusted_materials(base, messages) is None


@pytest.mark.parametrize('key,value', [
    ('schema', 0), ('schema', 2), ('schema', True), ('schema', '1'),
    ('event_id', 'another-event'), ('event_fingerprint', 'different-version'),
    ('event_ref', ''), ('source_fingerprints', []), ('source_fingerprints', {}),
    ('source_materials', []), ('source_materials', None),
])
def test_untrusted_snapshot_fails_closed(key, value):
    messages, _, base, event, component = fixture()
    base['material_snapshot'][key] = value
    assert trusted_materials(base, messages) is None
    assert annotation_contract(event, component)['annotation_source_message_ids'] == [2, 1, 3]


@pytest.mark.parametrize('field', ['event_id', 'fingerprint'])
def test_snapshot_cannot_be_reused_after_event_identity_changes(field):
    messages, _, base, _, _ = fixture()
    base[field] = 'changed'
    assert trusted_materials(base, messages) is None


@pytest.mark.parametrize('damage', ['source_ids', 'messages', 'materials', 'fingerprints'])
def test_duplicates_cannot_be_hidden_by_dictionary_projection(damage):
    messages, _, base, event, component = fixture()
    if damage == 'source_ids':
        base['source_message_ids'].append(1)
    elif damage == 'messages':
        messages.append(deepcopy(messages[0]))
    elif damage == 'materials':
        base['material_snapshot']['source_materials'].append(material(1))
    else:
        base['material_snapshot']['source_fingerprints'].append(
            deepcopy(base['material_snapshot']['source_fingerprints'][0]))
    assert trusted_materials(base, messages) is None
    assert annotation_contract(event, component)['inherited_materials'] == []


@pytest.mark.parametrize('row', [
    {'source_message_id': 1, 'use': 'main', 'reason': 'extra field', 'omit_quotes': [], 'extra': True},
    material(1, 'mixed'), material(1, 'main', ['The kettle is on.']),
    material(1, 'background', ['The kettle is on.']), material(1, 'mixed', ['not verbatim']),
    material(1, 'mixed', ['Repair the cover. The kettle is on.']),
    material(1, 'invalid'), material(1, []),
    {'source_message_id': 1, 'use': 'main', 'reason': '', 'omit_quotes': []},
])
def test_invalid_inherited_materials_fail_full_existing_checks(row):
    messages, _, base, event, component = fixture()
    base['material_snapshot']['source_materials'][1] = row
    assert trusted_materials(base, messages) is None
    assert annotation_contract(event, component)['inherited_materials'] == []


@pytest.mark.parametrize('damage', ['no_materials', 'missing_original', 'duplicate_ownership',
                                  'inconsistent_ownership', 'invalid_binding', 'invalid_id'])
def test_snapshot_constructor_refuses_incomplete_or_malformed_inputs(damage):
    messages, old_event, _, _, _ = fixture()
    if damage == 'no_materials':
        old_event.pop('source_materials')
    elif damage == 'missing_original':
        messages.pop(0)
    elif damage == 'duplicate_ownership':
        old_event['source_message_ids'].append(1)
    elif damage == 'inconsistent_ownership':
        old_event['source_bindings'] = [{'source_message_id': 1}]
    elif damage == 'invalid_binding':
        old_event['source_bindings'] = [{'source_message_id': []}]
    else:
        messages[0]['id'] = True
    assert snapshot_for(old_event, messages) is None


@pytest.mark.parametrize('version', [None, 0, 2, True, '1'])
def test_frozen_legacy_and_unknown_contract_versions_require_full_materials(version):
    _, _, _, event, component = fixture()
    component['material_contract_version'] = version
    assert annotation_contract(event, component)['annotation_source_message_ids'] == [2, 1, 3]
    with pytest.raises(ValueError, match=r'missing source_message_ids=\[1, 2\]'):
        merge_review({'events': [{'event_index': 0, 'materials': [material(3)]}]},
                     {'events': [event]}, component)
    full = {'events': [{'event_index': 0, 'materials': [material(i) for i in [2, 1, 3]]}]}
    assert merge_review(full, {'events': [event]}, component) == full


@pytest.mark.parametrize('action', ['create', 'merge', 'rewrite'])
def test_only_single_base_extend_can_inherit(action):
    _, _, _, event, component = fixture()
    event['action'] = action
    assert annotation_contract(event, component)['inherited_materials'] == []
    event['action'] = 'extend'
    event['base_event_ids'].append('another-base')
    assert annotation_contract(event, component)['inherited_materials'] == []


def test_contract_uses_complete_expanded_ownership_and_never_promotes_old_sources():
    _, old_event, base, event, component = fixture()
    before = deepcopy(component)
    contract = annotation_contract(event, component)
    assert contract == {'annotation_source_message_ids': [3],
                        'inherited_materials': old_event['source_materials']}
    assert material_contracts({'events': [event]}, component) == {0: contract}
    assert component == before
    assert [row['source_message_id'] for row in event['source_bindings']] == [2, 1, 3]
    event['source_bindings'].pop(0)
    assert annotation_contract(event, component) == {
        'annotation_source_message_ids': [1, 3], 'inherited_materials': []}
    base['predecessor_event_ids'] = ['saved:ancestor']
    event['source_bindings'].insert(0, {'source_message_id': 2})
    event['base_event_ids'] = ['saved:ancestor']
    assert annotation_contract(event, component)['annotation_source_message_ids'] == [3]


def test_merge_preserves_all_old_decisions_and_returns_independent_full_receipt():
    messages, old_event, _, event, component = fixture()
    review = {'events': [{'event_index': 0, 'reason': 'Continue repair',
                          'admission': {'closed_by': None}, 'materials': [material(3)]}],
              'boundaries': [], 'dispositions': []}
    before = deepcopy((review, component, event))
    merged = merge_review(review, {'events': [event]}, component)
    assert merged['events'][0]['materials'] == [*old_event['source_materials'], material(3)]
    assert merged['events'][0]['admission'] == {'closed_by': None}
    assert (review, component, event) == before
    merged['events'][0]['materials'][1]['omit_quotes'].append('changed copy')
    assert (review, component, event) == before
    merged = merge_review(review, {'events': [event]}, component)
    next_snapshot = snapshot_for({**event, 'source_materials': merged['events'][0]['materials']}, messages)
    next_snapshot.update(event_id='saved:next', event_fingerprint='next-fingerprint')
    next_base = {'event_id': 'saved:next', 'fingerprint': 'next-fingerprint',
                 'source_message_ids': [2, 1, 3], 'material_snapshot': next_snapshot}
    assert trusted_materials(next_base, messages) == merged['events'][0]['materials']


@pytest.mark.parametrize('rows,diagnostic', [
    ([], r'missing source_message_ids=\[3\]'),
    ([material(3), material(3)], r'duplicate source_message_ids=\[3\]'),
    ([material(1), material(3)], r'unexpected source_message_ids=\[1\]'),
    ([material(99)], r'unexpected source_message_ids=\[99\]'),
])
def test_merge_rejects_missing_duplicate_and_unexpected_delta_ids(rows, diagnostic):
    _, _, _, event, component = fixture()
    review = {'events': [{'event_index': 0, 'materials': rows}]}
    before = deepcopy(review)
    with pytest.raises(ValueError, match=diagnostic):
        merge_review(review, {'events': [event]}, component)
    assert review == before


def test_merged_new_annotations_still_validate_exact_omission_quotes():
    _, _, _, event, component = fixture()
    with pytest.raises(ValueError, match='verbatim'):
        merge_review({'events': [{'event_index': 0,
                                  'materials': [material(3, 'mixed', ['green paper'])]}]},
                     {'events': [event]}, component)


def test_malformed_new_use_is_a_repairable_validation_error():
    _, _, _, event, component = fixture()
    with pytest.raises(ValueError, match='Invalid Curator material fields'):
        merge_review({'events': [{'event_index': 0, 'materials': [material(3, [])]}]},
                     {'events': [event]}, component)


@pytest.mark.parametrize('indexes', [[], [0, 0], [1], [True]])
def test_merge_rejects_missing_duplicate_or_invalid_event_reviews(indexes):
    _, _, _, event, component = fixture()
    with pytest.raises(ValueError, match='event_index'):
        merge_review({'events': [{'event_index': index, 'materials': [material(3)]}
                                 for index in indexes]}, {'events': [event]}, component)


def test_disabled_material_review_returns_independent_unchanged_review():
    _, _, _, event, component = fixture()
    component['writer_material_review'] = False
    review = {'events': [{'event_index': 0, 'reason': 'No optional materials'}]}
    merged = merge_review(review, {'events': [event]}, component)
    assert merged == review
    assert merged is not review
    assert annotation_contract(event, component)['inherited_materials'] == []
