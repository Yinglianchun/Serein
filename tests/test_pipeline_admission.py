"""Synthetic complete-exchange admission without special activity sources."""

import pytest

from test_public_features import settings
from serein.extensions.pipeline_admission import add_lookahead, apply_gate, count_rounds, validate


def _messages():
    return [{'id': 1, 'session_id': 1, 'role': 'user', 'content': 'Should we bind the notebook?'},
            {'id': 2, 'session_id': 1, 'role': 'assistant', 'content': 'The cover is sturdy enough.'},
            {'id': 3, 'session_id': 1, 'role': 'user', 'content': 'Use the blue thread.'},
            {'id': 4, 'session_id': 1, 'role': 'assistant', 'content': 'I will measure it first.'}]


def _plan(source_ids, admission):
    return {'events': [{'event_ref': 'event:0', 'source_message_ids': source_ids}],
            'event_admissions': {'event:0': admission},
            'skip_source_message_ids': [], 'defer_source_message_ids': []}


def test_two_complete_owned_exchanges_write_and_one_open_exchange_waits():
    messages = _messages()
    assert count_rounds([1, 2, 3, 4], messages) == 2
    assert count_rounds([1, 2], messages) == 1
    component = {'writer_round_gate': True, 'messages': messages,
                 'context_messages': messages}
    accepted = apply_gate(_plan([1, 2, 3, 4], {'closed_by': None}), component)
    assert len(accepted['events']) == 1
    assert accepted['admission_receipts'][0]['disposition'] == 'write'
    waiting = apply_gate(_plan([1, 2], {'closed_by': None}), component)
    assert waiting['events'] == []
    assert waiting['defer_source_message_ids'] == [1, 2]


def test_explicitly_closed_short_exchange_is_skipped_with_verbatim_receipt():
    messages = _messages()[:2]
    component = {'writer_round_gate': True, 'messages': messages,
                 'context_messages': messages}
    closure = {'source_message_id': 2, 'quote': 'cover is sturdy enough'}
    output = {'events': [{'event_ref': 'event:0', 'source_bindings': [
        {'source_message_id': 1}, {'source_message_id': 2}]}]}
    review = {'events': [{'event_index': 0, 'admission': {'closed_by': closure}}]}
    assert validate(review, output, component) == {'event:0': {'closed_by': closure}}
    result = apply_gate(_plan([1, 2], {'closed_by': closure}), component)
    assert result['events'] == []
    assert result['skip_source_message_ids'] == [1, 2]
    closure['quote'] = 'a different sentence'
    try:
        validate(review, output, component)
    except ValueError as error:
        assert 'quote' in str(error)
    else:
        raise AssertionError('Unverifiable closure must be rejected')


def test_background_and_split_bubbles_do_not_create_extra_rounds():
    messages = _messages()
    messages.insert(1, {'id': 5, 'session_id': 1, 'role': 'user', 'content': 'The thread is on the table.'})
    assert count_rounds([1, 2, 3, 4, 5], messages) == 2
    event = {'event_ref': 'event:0', 'source_message_ids': [1, 2, 3, 4, 5],
             'source_materials': [{'source_message_id': source_id,
                                   'use': 'background' if source_id in {1, 2} else 'main'}
                                  for source_id in [1, 2, 3, 4, 5]]}
    component = {'writer_round_gate': True, 'messages': messages,
                 'context_messages': messages}
    plan = {'events': [event], 'event_admissions': {'event:0': {'closed_by': None}},
            'skip_source_message_ids': [], 'defer_source_message_ids': []}
    result = apply_gate(plan, component)
    assert result['events'] == []
    assert result['admission_receipts'][0]['rounds'] == 1


def test_lookahead_is_bounded_to_later_originals_in_same_session():
    first = {'messages': _messages()[:2], 'context_messages': _messages()[:2]}
    second = {'messages': _messages()[2:], 'context_messages': _messages()[2:] + [
        {'id': 5, 'session_id': 2, 'role': 'user', 'content': 'Another session'}]}
    add_lookahead([first, second])
    assert [row['id'] for row in first['boundary_lookahead']] == [3, 4]
    assert [row['id'] for row in first['messages']] == [1, 2]
    assert [row['id'] for row in second['boundary_lookahead']] == []


@pytest.mark.parametrize('marker', ['proactive', 'autonomy'])
def test_later_proactive_turn_does_not_erase_completed_rounds(marker):
    messages = _messages()
    later = {'id': 5, 'session_id': 1, 'role': 'assistant', 'content': 'A new observation.',
             'metadata': {marker: True}}
    component = {'writer_round_gate': True, 'messages': messages,
                 'context_messages': [*messages, later]}
    result = apply_gate(_plan([1, 2, 3, 4], {'closed_by': None}), component)
    assert result['admission_receipts'] == [
        {'event_ref': 'event:0', 'rounds': 2, 'minimum': 2, 'disposition': 'write'}]
    assert result['events'][0]['source_message_ids'] == [1, 2, 3, 4]


def test_proactive_bubbles_reply_and_landing_count_once_before_next_proactive_turn():
    messages = [
        {'id': 1, 'session_id': 1, 'role': 'assistant', 'metadata': {'proactive': True}},
        {'id': 2, 'session_id': 1, 'role': 'assistant', 'metadata': {'autonomy': True}},
        {'id': 3, 'session_id': 1, 'role': 'user'},
        {'id': 4, 'session_id': 1, 'role': 'assistant'},
        {'id': 5, 'session_id': 1, 'role': 'assistant', 'metadata': {'proactive': True}},
    ]
    assert count_rounds([1, 2, 3, 4], messages) == 1
    assert count_rounds([1, 2, 3], messages) == 0
    assert count_rounds([5], messages) == 0


def test_proactive_message_is_not_an_answer_to_pending_user():
    messages = _messages()[:2]
    messages[1]['metadata'] = {'proactive': True}
    assert count_rounds([1, 2], messages) == 0
    component = {'writer_round_gate': True, 'messages': messages}
    result = apply_gate(_plan([1, 2], {'closed_by': None}), component)
    assert result['defer_source_message_ids'] == [1, 2]


def test_reply_to_a_different_user_turn_cannot_count_as_current_answer():
    messages = _messages()
    messages[1]['metadata'] = {'reply_to_user_message_id': 99}
    assert count_rounds([1, 2, 3, 4], messages) == 1
    messages[1]['metadata']['reply_to_user_message_id'] = 1
    assert count_rounds([1, 2, 3, 4], messages) == 2


def test_partial_bubbles_hidden_messages_and_sessions_cannot_make_rounds():
    messages = _messages()
    messages.insert(2, {'id': 5, 'session_id': 1, 'role': 'assistant'})
    assert count_rounds([1, 2, 3, 4], messages) == 1
    messages[2]['metadata'] = {'memory_review_only': True}
    assert count_rounds([1, 2, 3, 4], messages) == 2
    messages[1]['session_id'] = 2
    assert count_rounds([1, 2, 3, 4], messages) == 1


def test_owned_closure_can_precede_acknowledgement_without_changing_ownership():
    messages = _messages()
    messages[2]['content'] = 'The notebook is bound now.'
    messages[3]['content'] = 'The blue thread matches the cover.'
    component = {'writer_round_gate': True, 'messages': messages}
    closure = {'source_message_id': 3, 'quote': 'notebook is bound now'}
    output = {'events': [{'event_ref': 'event:0', 'source_bindings': [
        {'source_message_id': row['id']} for row in messages]}]}
    review = {'events': [{'event_index': 0, 'admission': {'closed_by': closure}}]}
    admissions = validate(review, output, component)
    assert admissions == {'event:0': {'closed_by': closure}}
    result = apply_gate(_plan([1, 2, 3, 4], admissions['event:0']), component)
    assert result['events'][0]['source_message_ids'] == [1, 2, 3, 4]


@pytest.mark.parametrize('source_id, session_id, quote', [
    (0, 1, 'A previous activity ended.'),
    (5, 2, 'A different session ended.'),
    (5, 1, 'Invented closure'),
])
def test_closure_still_rejects_earlier_unowned_foreign_or_invented_evidence(
        source_id, session_id, quote):
    messages = _messages()
    component = {'writer_round_gate': True, 'messages': messages, 'context_messages': [
        {'id': source_id, 'session_id': session_id, 'content':
         'A previous activity ended.' if source_id == 0 else 'A different session ended.'}]}
    output = {'events': [{'event_ref': 'event:0', 'source_bindings': [
        {'source_message_id': row['id']} for row in messages]}]}
    review = {'events': [{'event_index': 0, 'admission': {'closed_by':
        {'source_message_id': source_id, 'quote': quote}}}]}
    with pytest.raises(ValueError, match='closure'):
        validate(review, output, component)


def test_enough_rounds_still_wait_for_an_owned_unanswered_question():
    messages = [*_messages(), {'id': 5, 'session_id': 1, 'role': 'user',
                             'content': 'Should the label use the same thread?'}]
    result = apply_gate(_plan([1, 2, 3, 4, 5], {'closed_by': None}),
                        {'writer_round_gate': True, 'messages': messages})
    assert result['events'] == []
    assert result['defer_source_message_ids'] == [1, 2, 3, 4, 5]


def test_round_gate_remains_opt_in():
    plan = _plan([1, 2], {'closed_by': None})
    assert apply_gate(plan, {'messages': _messages()[:2]}) is plan


def test_materials_owned_closure_and_relaxed_writer_survive_pipeline_save(settings):
    import asyncio

    from test_public_features import output_for
    from serein.compat.raw_archive import raw_archive
    from serein.core.store import Store
    from serein.deployment import save_settings
    from serein.extensions import pipeline

    save_settings(settings.database, {'pipeline': {
        'material_review_enabled': True, 'round_gate_enabled': True}})
    rows = _messages()
    rows[2]['content'] = 'The notebook is bound now.'
    rows[3]['content'] = 'The blue thread matches the cover. Goodnight.'
    raw_archive(settings).ingest([
        {'source_event_id': str(row['id']), 'session_id': 'notebook', 'role': row['role'],
         'text': row['content'], 'created_at': f'2026-01-01T00:0{row["id"]}:00Z'}
        for row in rows], source='test')
    seen = []

    async def runner(role, request):
        seen.append(role)
        result = output_for(role, request)
        if role == 'event_curator':
            assert request['component']['writer_material_review'] is True
            assert request['component']['writer_round_gate'] is True
            originals = request['component']['messages']
            review = result['decision_review']['events'][0]
            review['admission'] = {'closed_by': {
                'source_message_id': originals[2]['id'], 'quote': originals[2]['content']}}
            review['materials'] = [
                {'source_message_id': row['id'], 'use': 'mixed' if index == 3 else 'main',
                 'reason': 'Binding and its result', 'omit_quotes': ['Goodnight.'] if index == 3 else []}
                for index, row in enumerate(originals)]
        if role == 'event_writer':
            assert len(request['messages']) == 4
            assert '<curator_materials_json>' in request['prompt']
            assert 'Goodnight.' in request['prompt']  # Original and omission receipt remain readable.
            result.pop('claim_groups')
            result.pop('sentence_evidence')
            result['event_draft'] = 'The notebook is bound with blue thread that matches its cover.'
            result['self_review']['result_preserved'] = False
        return result

    result = asyncio.run(pipeline.advance(settings.database, include_recent=True, runner=runner))
    assert seen == list(pipeline.ROLES)
    assert result['events'] == 1 and result['pending'] == 0
    with Store(settings.database, read_only=True) as store:
        saved = store.conn.execute('SELECT item_id, body FROM fact_events').fetchone()
        assert 'Goodnight' not in saved['body']
        assert store.conn.execute('SELECT count(*) FROM fact_event_sources WHERE item_id=?',
                                  (saved['item_id'],)).fetchone()[0] == 4
