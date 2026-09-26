"""Synthetic source decisions for Curator-to-Writer material handoff."""

import pytest

from serein.extensions.pipeline_materials import attach, substantive_ids
from serein.extensions.pipeline_latest import build_event_writer_prompt


def test_materials_keep_ownership_and_reach_writer():
    component = {'writer_material_review': True, 'context_messages': [
        {'id': 1, 'content': 'The cover is torn; keep the sketch.', 'role': 'user'},
        {'id': 2, 'content': 'I will repair the cover tomorrow. Also, the kettle is on.', 'role': 'assistant'},
    ]}
    plan = {'events': [{'event_ref': 'event:0', 'source_message_ids': [1, 2]}]}
    output = {'events': [{'event_ref': 'event:0', 'source_bindings': [
        {'source_message_id': 1}, {'source_message_id': 2}]}]}
    materials = [
        {'source_message_id': 1, 'use': 'main', 'reason': 'The repair choice', 'omit_quotes': []},
        {'source_message_id': 2, 'use': 'mixed', 'reason': 'Plan with an unrelated aside',
         'omit_quotes': ['Also, the kettle is on.']},
    ]
    attach({'events': [{'event_index': 0, 'materials': materials}]}, output, plan, component)
    assert plan['events'][0]['source_message_ids'] == [1, 2]
    assert substantive_ids(plan['events'][0]) == [1, 2]
    prompt = build_event_writer_prompt('2026-01-01', '', [
        {'id': 1, 'role': 'user', 'content': component['context_messages'][0]['content']},
        {'id': 2, 'role': 'assistant', 'content': component['context_messages'][1]['content']},
    ], source_materials=materials)
    assert '<curator_materials_json>' in prompt
    assert 'Also, the kettle is on.' in prompt


def test_materials_reject_unquoted_or_missing_owned_sources():
    component = {'writer_material_review': True, 'context_messages': [{'id': 1, 'content': 'Use the blue paper.'},
                                      {'id': 2, 'content': 'I will check the size.'}]}
    plan = {'events': [{'event_ref': 'event:0', 'source_message_ids': [1, 2]}]}
    output = {'events': [{'event_ref': 'event:0', 'source_bindings': [
        {'source_message_id': 1}, {'source_message_id': 2}]}]}
    row = {'event_index': 0, 'materials': [
        {'source_message_id': 1, 'use': 'mixed', 'reason': 'Keep part', 'omit_quotes': ['green paper']},
        {'source_message_id': 2, 'use': 'omit', 'reason': 'Aside', 'omit_quotes': []},
    ]}
    with pytest.raises(ValueError, match='verbatim'):
        attach({'events': [row]}, output, plan, component)
    row['materials'][0] = {'source_message_id': 1, 'use': 'main', 'reason': 'Choice', 'omit_quotes': []}
    row['materials'].pop()
    with pytest.raises(ValueError, match='exactly cover'):
        attach({'events': [row]}, output, plan, component)


def test_materials_follow_event_ref_when_a_protected_proposal_is_deferred():
    component = {'writer_material_review': True, 'context_messages': [
        {'id': 1, 'content': 'Earlier note.'}, {'id': 2, 'content': 'Use the blue paper.'}]}
    output = {'events': [
        {'event_ref': 'event:0', 'source_bindings': [{'source_message_id': 1}]},
        {'event_ref': 'event:1', 'source_bindings': [{'source_message_id': 2}]},
    ]}
    plan = {'events': [{'event_ref': 'event:1', 'source_message_ids': [2]}]}
    review = {'events': [
        {'event_index': 0, 'materials': [{'source_message_id': 1, 'use': 'main',
                                         'reason': 'Earlier note', 'omit_quotes': []}]},
        {'event_index': 1, 'materials': [{'source_message_id': 2, 'use': 'main',
                                         'reason': 'Paper choice', 'omit_quotes': []}]},
    ]}
    attach(review, output, plan, component)
    assert plan['events'][0]['source_materials'] == review['events'][1]['materials']


def test_material_review_does_not_reject_a_main_greeting_by_keyword():
    message = {'id': 1, 'role': 'user', 'content': 'Goodnight is the title I chose for the sketch.'}
    component = {'writer_material_review': True, 'messages': [message]}
    output = {'events': [{'event_ref': 'event:0', 'source_bindings': [{'source_message_id': 1}]}]}
    plan = {'events': [{'event_ref': 'event:0', 'source_message_ids': [1]}]}
    materials = [{'source_message_id': 1, 'use': 'main', 'reason': 'The sketch title', 'omit_quotes': []}]
    attach({'events': [{'event_index': 0, 'materials': materials}]}, output, plan, component)
    assert substantive_ids(plan['events'][0]) == [1]
    prompt = build_event_writer_prompt('2026-01-01', '', [message], source_materials=materials)
    assert message['content'] in prompt


def test_whole_message_cannot_be_omitted_as_mixed():
    component = {'writer_material_review': True, 'messages': [{'id': 1, 'content': 'Goodnight.'}]}
    output = {'events': [{'event_ref': 'event:0', 'source_bindings': [{'source_message_id': 1}]}]}
    plan = {'events': [{'event_ref': 'event:0', 'source_message_ids': [1]}]}
    materials = [{'source_message_id': 1, 'use': 'mixed', 'reason': 'A closing aside',
                  'omit_quotes': ['Goodnight.']}]
    with pytest.raises(ValueError, match='retain content'):
        attach({'events': [{'event_index': 0, 'materials': materials}]}, output, plan, component)
