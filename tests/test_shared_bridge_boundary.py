"""Synthetic boundary receipts preserve full bridge ownership during retries."""
import copy

import pytest

from serein.extensions import pipeline as pipeline, pipeline_latest as latest
from serein.extensions.pipeline_materials import retains_boundary_quote


def case(inherited=False, protection=None):
    texts = ['Inspect the cover.', 'I will check it.',
             'The cover is checked. Next, design the label.', 'The label can be blue.',
             'Use the blue label.', 'I will make it.']
    messages = [{'id': index, 'session_id': 1, 'role': 'user' if index % 2 else 'assistant',
                 'content': text, 'created_at': f'2025-01-01T00:0{index}:00Z'}
                for index, text in enumerate(texts, 1)]
    component = {'messages': messages, 'context_messages': list(messages), 'track_ids': ['a', 'b'],
        'track_cards': [{'track_id': 'a'}, {'track_id': 'b'}], 'context_session_ids': [1],
        'memberships': [{'unit_root_message_id': root, 'source_message_ids': ids, 'session_id': 1,
                        'track_id': track, 'routing_role': role}
                       for root, ids, track, role in [(1, [1, 2], 'a', 'primary_activity'),
                           (3, [3, 4], 'a', 'bridge'), (5, [5, 6], 'b', 'primary_activity')]],
        'context_edges': [{'unit_root_message_id': 3, 'track_id': 'b', 'relation': 'bridge'}],
        'continuity_pairs': [{'left_track_id': 'a', 'right_track_id': 'b', 'bridge_unit_root': 3}],
        'writer_material_review': True, 'parked_context_source_ids': [], 'base_event_candidates': []}
    if inherited:
        component['context_messages'] += [{'id': index, 'session_id': 1, 'content': 'Earlier cover inspection.'}
                                          for index in (101, 102)]
        component['base_event_candidates'] = [{'event_id': 'earlier-cover', 'primary_track_id': 'a',
            'source_message_ids': [101, 102, 3, 4], 'session_ids': [1], 'predecessor_event_ids': [],
            'active': True, **({protection: True} if protection else {})}]
    proposal = {'events': [
        {'action': 'extend' if inherited else 'create', 'base_event_ids': ['earlier-cover'] if inherited else [],
         'primary_track_id': 'a', 'owned_unit_roots': [1] if inherited else [1, 3]},
        {'action': 'create', 'base_event_ids': [], 'primary_track_id': 'b', 'owned_unit_roots': [3, 5]}],
        'skip_unit_roots': [], 'defer_unit_roots': [], 'decision_review': {
            'events': [], 'boundaries': [{'left_event_index': 0, 'right_event_index': 1,
                'reason': 'Cover inspection closes; label design begins', 'evidence': [
                    {'source_message_id': 3, 'quote': 'The cover is checked.'},
                    {'source_message_id': 3, 'quote': 'Next, design the label.'}]}],
            'dispositions': [], 'continuations': []}}
    for index, ids in enumerate(([101, 102, 3, 4, 1, 2] if inherited else [1, 2, 3, 4], [3, 4, 5, 6])):
        materials = [{'source_message_id': source_id, 'use': 'main',
                      'reason': 'Synthetic activity evidence', 'omit_quotes': []} for source_id in ids]
        next(item for item in materials if item['source_message_id'] == 3).update(
            use='mixed', omit_quotes=['Next, design the label.' if index == 0 else 'The cover is checked.'])
        next(item for item in materials if item['source_message_id'] == 4)['use'] = 'omit' if index == 0 else 'main'
        proposal['decision_review']['events'].append({'event_index': index,
            'reason': 'Separate synthetic activity', 'materials': materials})
    return component, proposal


def material(proposal, side, source_id=3):
    return next(item for item in proposal['decision_review']['events'][side]['materials']
                if item['source_message_id'] == source_id)


def test_distinct_bridge_quotes_prove_boundary_without_changing_ownership():
    component, proposal = case()
    plan = latest.normalize_event_curator_output(proposal, component)
    assert [event['source_message_ids'] for event in plan['events']] == [[1, 2, 3, 4], [3, 4, 5, 6]]
    assert all(binding['activity_role'] == 'bridge' for event in plan['events']
               for binding in event['source_bindings'] if binding['source_message_id'] in (3, 4))
    assert plan['events'][0]['source_materials'] == proposal['decision_review']['events'][0]['materials']


@pytest.mark.parametrize('protection', [None, 'protected', 'manual', 'blocked'])
def test_retry_counts_bridge_in_selected_base_union(protection):
    component, proposal = case(inherited=True, protection=protection)
    expanded = latest._expand_compact_event_curator_output(
        {key: value for key, value in proposal.items() if key != 'decision_review'}, component)
    assert {binding['source_message_id'] for binding in expanded['events'][0]['source_bindings']} == {
        101, 102, 1, 2, 3, 4}
    assert all(binding['activity_role'] == 'bridge' for event in expanded['events']
               for binding in event['source_bindings'] if binding['source_message_id'] in (3, 4))
    if protection is None:
        with pytest.raises(ValueError, match='unselected base'):
            latest.normalize_event_curator_output(proposal, component)
        return
    plan = latest.normalize_event_curator_output(proposal, component)
    assert plan['events'] == [] and set(plan['defer_source_message_ids']) == set(range(1, 7))
    assert protection in plan['hard_skips'][0]['blocking_flags']


@pytest.mark.parametrize('kind', ['both_keep', 'background', 'omit', 'one_sided', 'foreign',
                                 'bad_omission', 'missing_material', 'no_material_review', 'ordinary_overlap'])
def test_bridge_relaxation_does_not_allow_invalid_boundaries(kind):
    component, proposal = case()
    if kind == 'both_keep':
        material(proposal, 0).update(use='main', omit_quotes=[])
        material(proposal, 1).update(use='main', omit_quotes=[])
    elif kind in {'background', 'omit'}:
        material(proposal, 0).update(use=kind, omit_quotes=[])
    elif kind == 'one_sided':
        proposal['decision_review']['boundaries'][0]['evidence'].pop()
    elif kind == 'foreign':
        component['context_messages'].append({'id': 777, 'content': 'Foreign context.'})
        proposal['decision_review']['boundaries'][0]['evidence'][0] = {
            'source_message_id': 777, 'quote': 'Foreign context.'}
    elif kind == 'bad_omission':
        material(proposal, 0)['omit_quotes'] = ['Invented text']
    elif kind == 'missing_material':
        proposal['decision_review']['events'][0]['materials'].pop()
    elif kind == 'no_material_review':
        component['writer_material_review'] = False
    else:
        component['memberships'][1]['routing_role'] = 'primary_activity'
    with pytest.raises(ValueError):
        latest.normalize_event_curator_output(proposal, component)


def test_deferred_inherited_proposal_still_requires_all_materials():
    component, proposal = case(inherited=True, protection='protected')
    proposal['decision_review']['events'][0]['materials'] = [
        item for item in proposal['decision_review']['events'][0]['materials'] if item['source_message_id'] != 102]
    with pytest.raises(ValueError, match='exactly cover'):
        latest.normalize_event_curator_output(proposal, component)


def test_partial_inherited_bridge_still_cannot_split_an_atomic_unit():
    component, proposal = case(inherited=True, protection='protected')
    component['base_event_candidates'][0]['source_message_ids'].remove(4)
    proposal['decision_review']['events'][0]['materials'] = [
        item for item in proposal['decision_review']['events'][0]['materials'] if item['source_message_id'] != 4]
    with pytest.raises(ValueError, match='atomic dialogue unit'):
        latest.normalize_event_curator_output(proposal, component)


def test_shared_quote_cannot_be_reconstructed_across_omitted_text():
    component, proposal = case()
    component['messages'][2]['content'] = 'abXcd abcd RIGHT'
    material(proposal, 0)['omit_quotes'] = ['abcd', 'X', 'RIGHT']
    material(proposal, 1)['omit_quotes'] = ['abXcd', 'abcd']
    proposal['decision_review']['boundaries'][0]['evidence'] = [
        {'source_message_id': 3, 'quote': 'abcd'}, {'source_message_id': 3, 'quote': 'RIGHT'}]
    with pytest.raises(ValueError, match='boundary'):
        latest.normalize_event_curator_output(proposal, component)


@pytest.mark.parametrize('omissions', [['ABC', 'BCD'], ['word']])
def test_overlapping_or_repeated_omissions_cannot_be_boundary_evidence(omissions):
    text, quote = ('ABCD', 'D') if omissions[0] == 'ABC' else ('word word', 'word')
    assert not retains_boundary_quote({'use': 'mixed', 'omit_quotes': omissions}, text, quote)


def test_prompts_explain_opt_in_bridge_evidence_and_default_exclusive_evidence():
    component, _ = case()
    enabled = latest.build_event_track_curator_prompt('2025-01-01', component)
    assert '不要删改完整 bridge ownership' in enabled and '引文不得跨越省略片段拼接' in enabled
    assert 'activity_role 由 host 生成' in enabled
    assert 'stable units 全是其他 Track 给它的 bridge' in enabled
    component['writer_material_review'] = False
    disabled = latest.build_event_track_curator_prompt('2025-01-01', component)
    assert '边界证据须从左右各自独占的 owned 原文逐字引用' in disabled


def foreign_bridge_component():
    messages = [
        {'id': 1, 'session_id': 1, 'role': 'user', 'content': 'Synthetic bridge question.',
         'created_at': '2025-01-01T00:00:00Z'},
        {'id': 2, 'session_id': 1, 'role': 'assistant', 'content': 'Synthetic bridge answer.',
         'created_at': '2025-01-01T00:01:00Z'},
    ]
    return {
        'messages': messages, 'context_messages': messages, 'track_ids': ['b'],
        'track_cards': [{'track_id': 'b'}], 'context_session_ids': [1],
        'memberships': [{'unit_root_message_id': 1, 'source_message_ids': [1, 2],
                         'session_id': 1, 'track_id': 'a', 'routing_role': 'bridge'}],
        'context_edges': [{'unit_root_message_id': 1, 'track_id': 'b', 'relation': 'bridge'}],
        'continuity_pairs': [], 'parked_context_source_ids': [], 'base_event_candidates': [],
        'writer_material_review': False,
    }


def test_routine_hint_is_visible_but_does_not_override_curator_decision():
    component = foreign_bridge_component()
    component['track_ids'] = ['a']
    component['track_cards'] = [{'track_id': 'a'}]
    component['context_edges'] = []
    component['memberships'][0]['routing_role'] = 'routine'
    model_input = latest.event_curator_model_input(component)
    assert model_input['units'][0]['routing_role_hint'] == 'routine'
    skipped = latest.normalize_event_curator_output({
        'events': [], 'skip_unit_roots': [1], 'defer_unit_roots': [],
        'decision_review': {'events': [], 'boundaries': [], 'dispositions': [
            {'disposition': 'skip', 'unit_roots': [1], 'reason': 'Routine check only', 'parked_source_message_ids': []}]},
    }, component)
    assert skipped['skip_source_message_ids'] == [1, 2]
    developed = latest.normalize_event_curator_output({
        'events': [{'action': 'create', 'base_event_ids': [], 'primary_track_id': 'a',
                    'owned_unit_roots': [1]}],
        'skip_unit_roots': [], 'defer_unit_roots': [],
        'decision_review': {'events': [{'event_index': 0, 'reason': 'User developed the activity'}],
                            'boundaries': [], 'dispositions': []},
    }, component)
    assert developed['events'][0]['source_bindings'] == [
        {'source_message_id': 1, 'activity_role': 'primary_activity'},
        {'source_message_id': 2, 'activity_role': 'primary_activity'},
    ]


def test_foreign_bridge_only_corridor_must_skip_instead_of_creating_event():
    component = foreign_bridge_component()
    create = {
        'events': [{'action': 'create', 'base_event_ids': [], 'primary_track_id': 'b',
                    'owned_unit_roots': [1]}],
        'skip_unit_roots': [], 'defer_unit_roots': [],
    }
    with pytest.raises(ValueError) as caught:
        latest._expand_compact_event_curator_output(create, component)
    error = str(caught.value)
    assert "Event #1 has no generated primary_activity" in error
    assert "owned_unit_roots=[1]" in error
    assert "unit_primary_track': 'a'" in error
    assert "primary_track_id='b'" in error
    assert 'foreign bridge units must skip/defer' in error

    skipped = latest.normalize_event_curator_output({
        'events': [], 'skip_unit_roots': [1], 'defer_unit_roots': [],
        'decision_review': {'events': [], 'boundaries': [], 'dispositions': [{
            'disposition': 'skip', 'unit_roots': [1],
            'reason': 'This side has only a foreign bridge unit.',
            'parked_source_message_ids': [],
        }]},
    }, component)
    assert skipped['events'] == []
    assert skipped['skip_source_message_ids'] == [1, 2]


def test_host_shortcuts_only_bridge_corridors_without_a_primary_or_defer_path():
    component = foreign_bridge_component()
    plan = pipeline.bridge_only_skip_plan(component)
    assert plan['events'] == []
    assert plan['skip_source_message_ids'] == []
    assert plan['host_bridge_only_skips'] == [{
        'primary_track_id': 'b', 'unit_roots': [1], 'source_message_ids': [1, 2],
        'reason': 'foreign_bridge_only'}]

    inherited = copy.deepcopy(component)
    inherited['base_event_candidates'] = [{
        'event_id': 'old-b', 'source_message_ids': [101],
        'source_activity_roles': {'101': 'primary_activity'},
    }]
    assert pipeline.bridge_only_skip_plan(inherited) is None

    legacy_inherited = copy.deepcopy(inherited)
    legacy_inherited['base_event_candidates'][0].pop('source_activity_roles')
    assert pipeline.bridge_only_skip_plan(legacy_inherited) is None

    bridge_inherited = copy.deepcopy(inherited)
    bridge_inherited['base_event_candidates'][0]['source_activity_roles']['101'] = 'bridge'
    assert pipeline.bridge_only_skip_plan(bridge_inherited)['skip_source_message_ids'] == []

    parked = copy.deepcopy(component)
    parked['parked_context_source_ids'] = [99]
    assert pipeline.bridge_only_skip_plan(parked) is None

    local = copy.deepcopy(component)
    local['memberships'][0].update(track_id='b', routing_role='primary_activity')
    local['context_edges'] = []
    assert pipeline.bridge_only_skip_plan(local) is None
