"""Synthetic ID diagnostics preserve ownership boundaries and omit chat text."""
import copy

import pytest

from serein.extensions import pipeline_latest as latest


PRIVATE = 'PRIVATE_CHAT_DO_NOT_ECHO_8f2d'


def example():
    messages = [{'id': key, 'session_id': 1, 'role': 'user' if key % 2 else 'assistant',
                 'content': PRIVATE, 'created_at': f'2025-01-01T00:00:{key:02}Z'}
                for key in (11, 12, 13, 14, 21, 22)]
    component = {'messages': messages[:4], 'context_messages': messages,
        'track_ids': ['track-a'], 'track_cards': [{'track_id': 'track-a'}],
        'context_session_ids': [1], 'parked_context_source_ids': [21],
        'base_event_candidates': [], 'context_edges': [],
        'memberships': [{'unit_root_message_id': root, 'source_message_ids': ids,
                         'track_id': 'track-a', 'session_id': 1, 'routing_role': 'primary_activity'}
                        for root, ids in ((11, [11, 12]), (13, [13, 14]), (21, [21]), (22, [22]))]}
    proposal = {'events': [{'action': 'create', 'base_event_ids': [],
                           'primary_track_id': 'track-a', 'owned_unit_roots': [11, 13]}],
                'skip_unit_roots': [], 'defer_unit_roots': []}
    return component, proposal


def expand(component, proposal):
    return latest._expand_compact_event_curator_output(proposal, component)


@pytest.mark.parametrize('value,reason', [
    ('11', 'not_integer'), (True, 'not_integer'), (11.0, 'not_integer'),
    (None, 'not_integer'), ([PRIVATE], 'not_integer'), ({PRIVATE: 11}, 'not_integer'),
    (PRIVATE, 'not_integer'), (12, 'source_id_is_not_unit_root'),
    (21, 'parked_unit_root'), (22, 'context_only_unit_root'), (999, 'unknown_unit_root'),
])
@pytest.mark.parametrize('field', ['ownership', 'skip', 'defer'])
def test_root_error_identifies_the_field_position_and_branch_without_echoing_text(value, reason, field):
    component, proposal = example()
    if field == 'ownership':
        proposal['events'][0]['owned_unit_roots'] = [13, value]
        path = 'events[0].owned_unit_roots[1]'
    else:
        proposal[field + '_unit_roots'] = [value]
        path = field + '_unit_roots[0]'
    original = copy.deepcopy(proposal)
    with pytest.raises(ValueError, match=f'Track Curator {field} contains an invalid unit root') as error:
        expand(component, proposal)
    diagnostic = str(error.value)
    assert f'path={path}' in diagnostic and f'reason={reason}' in diagnostic
    assert 'stable_unit_count=2' in diagnostic and 'stable_unit_roots_sample=[11, 13]' in diagnostic
    assert PRIVATE not in diagnostic
    if value == 12:
        assert 'containing_unit_root=11' in diagnostic
    assert proposal == original


@pytest.mark.parametrize('field', ['owned_unit_roots', 'skip_unit_roots', 'defer_unit_roots'])
@pytest.mark.parametrize('value', [None, PRIVATE, {11: PRIVATE}])
def test_non_list_roots_raise_a_schema_value_error(field, value):
    component, proposal = example()
    container = proposal['events'][0] if field == 'owned_unit_roots' else proposal
    container[field] = value
    with pytest.raises(ValueError, match='reason=expected_list') as error:
        expand(component, proposal)
    assert field in str(error.value) and PRIVATE not in str(error.value)


@pytest.mark.parametrize('field', ['owned_unit_roots', 'skip_unit_roots', 'defer_unit_roots'])
def test_duplicate_roots_are_distinguished_from_unknown_roots(field):
    component, proposal = example()
    container = proposal['events'][0] if field == 'owned_unit_roots' else proposal
    container[field] = [11, 11]
    with pytest.raises(ValueError, match='reason=duplicate_unit_root') as error:
        expand(component, proposal)
    assert f'{field}[1]' in str(error.value)


def with_base(component):
    component['context_messages'].append({'id': 1, 'session_id': 1, 'content': PRIVATE})
    component['base_event_candidates'] = [{'event_id': 'saved-event', 'primary_track_id': 'track-a',
        'source_message_ids': [1], 'session_ids': [1], 'predecessor_event_ids': ['old-alias'],
        'title': PRIVATE, 'body': PRIVATE}]


@pytest.mark.parametrize('expanded', [False, True])
@pytest.mark.parametrize('has_base', [False, True])
def test_unknown_base_error_has_request_candidates_and_positions_without_echoing_model_text(expanded, has_base):
    component, proposal = example()
    if has_base:
        with_base(component)
    if expanded:
        proposal = expand(component, proposal)
        validate = latest._normalize_expanded_event_curator_output
    else:
        validate = expand
    event = proposal['events'][0]
    event.update(action='merge' if has_base else 'extend',
                 base_event_ids=['saved-event', PRIVATE] if has_base else [PRIVATE])
    with pytest.raises(ValueError, match='outside the bounded candidates') as error:
        validate(proposal, component) if expanded else validate(component, proposal)
    diagnostic = str(error.value)
    assert f'path=events[0].base_event_ids[{1 if has_base else 0}]' in diagnostic
    assert f'candidate_count={int(has_base)}' in diagnostic
    assert f'same_track_candidate_count={int(has_base)}' in diagnostic
    assert f'accepted_alias_count={int(has_base)}' in diagnostic
    assert f'received=str(length={len(PRIVATE)})' in diagnostic
    assert PRIVATE not in diagnostic
    if has_base:
        assert "candidate_event_ids_sample=['saved-event']" in diagnostic


def test_legal_root_order_and_predecessor_alias_still_produce_the_same_plan():
    component, proposal = example()
    with_base(component)
    proposal['events'][0].update(action='extend', base_event_ids=['saved-event'])
    expected = latest._normalize_expanded_event_curator_output(expand(component, proposal), component)
    proposal['events'][0].update(base_event_ids=[' old-alias '], owned_unit_roots=[13, 11])
    actual = latest._normalize_expanded_event_curator_output(expand(component, proposal), component)
    assert actual == expected
    assert actual['events'][0]['source_message_ids'] == [1, 11, 12, 13, 14]


def test_coverage_diagnostic_maps_missing_messages_to_the_complete_unit():
    component, proposal = example()
    proposal['events'][0]['owned_unit_roots'] = [11]
    with pytest.raises(latest.CuratorCoverageError) as error:
        latest._normalize_expanded_event_curator_output(expand(component, proposal), component)
    assert error.value.missing_source_ids == [13, 14]
    assert error.value.missing_unit_roots == [13]
    assert error.value.unmapped_source_ids == []
    diagnostic = str(error.value)
    assert 'unaccounted source_message_ids=[13, 14]' in diagnostic
    assert 'missing_unit_roots_sample=[13]' in diagnostic
    assert 'unmapped_source_count=0' in diagnostic
    assert 'stable_source_count=4, accounted_source_count=2' in diagnostic
    assert PRIVATE not in diagnostic


def test_damaged_host_scope_is_identified_and_the_prompt_builder_rejects_it_before_model_input():
    component, proposal = example()
    component['memberships'] = [unit for unit in component['memberships'] if unit['unit_root_message_id'] != 13]
    with pytest.raises(ValueError, match='settlement sources lack an atomic unit'):
        latest.event_curator_model_input(component)
    proposal['events'][0]['owned_unit_roots'] = [11]
    with pytest.raises(latest.CuratorCoverageError) as error:
        latest._normalize_expanded_event_curator_output(expand(component, proposal), component)
    assert error.value.missing_unit_roots == []
    assert error.value.unmapped_source_ids == [13, 14]
    assert 'unmapped_source_count=2' in str(error.value)
    proposal['events'][0]['owned_unit_roots'].append(13)
    with pytest.raises(ValueError, match='reason=stable_source_without_unit'):
        expand(component, proposal)


@pytest.mark.parametrize('damage,error', [
    ('split', 'stable scope splits an atomic unit'),
    ('overlap', 'source belongs to more than one unit'),
    ('missing_original', 'unit is missing from the reading corridor'),
])
def test_other_damaged_host_unit_sets_are_rejected_before_model_input(damage, error):
    component, _ = example()
    if damage == 'split':
        component['messages'] = component['messages'][:-1]
    elif damage == 'overlap':
        component['memberships'][1]['source_message_ids'].append(12)
    else:
        component['context_messages'] = component['context_messages'][1:]
    with pytest.raises(ValueError, match=error):
        latest.event_curator_model_input(component)


def test_coverage_error_keeps_the_full_repair_ids_but_bounds_public_diagnostic_samples():
    missing = list(range(1, 308))
    component = {'messages': [{'id': key} for key in missing], 'memberships': [
        {'unit_root_message_id': key, 'source_message_ids': [key]} for key in missing]}
    error = latest.CuratorCoverageError(missing, component=component, accounted_source_count=0)
    assert error.missing_source_ids == missing and error.missing_unit_roots == missing
    assert 'unaccounted_source_count=307' in str(error) and 'missing_unit_count=307' in str(error)
    assert '307]' not in str(error)
    assert len(str(error)) < 1500


@pytest.mark.parametrize('place', ['event', 'top'])
def test_arbitrary_model_field_names_are_not_error_text(place):
    component, proposal = example()
    (proposal if place == 'top' else proposal['events'][0])[PRIVATE] = PRIVATE
    with pytest.raises(ValueError) as error:
        latest.normalize_event_curator_output(proposal, component)
    assert 'unexpected=' in str(error.value) and PRIVATE not in str(error.value)


def test_non_list_disposition_preserves_the_original_schema_error_prefix():
    component, proposal = example()
    proposal['skip_unit_roots'] = PRIVATE
    with pytest.raises(ValueError, match='^Track Curator compact dispositions must be lists') as error:
        expand(component, proposal)
    assert 'path=skip_unit_roots' in str(error.value) and PRIVATE not in str(error.value)


def test_bridge_exclusion_reports_only_known_track_names():
    component, _ = example()
    row = {'unit_root_message_id': 11, 'excluded_track_id': PRIVATE, 'reason': PRIVATE, 'evidence': []}
    with pytest.raises(ValueError, match='Unexpected bridge exclusion') as error:
        latest.validate_bridge_owners({'events': []}, component, {'bridge_exclusions': [row]})
    assert "(11, 'unknown')" in str(error.value)
    assert PRIVATE not in str(error.value)
