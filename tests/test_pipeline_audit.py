"""Synthetic, inspectable receipts for the public Event pipeline."""
import copy

from serein.extensions.pipeline_audit import canonicalize_claim_group_ids, curator_receipt_errors, writer_receipt_errors


def test_unambiguous_writer_labels_are_renumbered_without_losing_references():
    receipt = {'claim_groups': [{'claim_group_id': 'reason'}, {'claim_group_id': 'result'}],
               'sentence_evidence': [{'claim_group_ids': ['reason', 'result']}]}
    canonicalize_claim_group_ids(receipt)
    assert [row['claim_group_id'] for row in receipt['claim_groups']] == ['g1', 'g2']
    assert receipt['sentence_evidence'][0]['claim_group_ids'] == ['g1', 'g2']
    ambiguous = {'claim_groups': [{'claim_group_id': 'same'}, {'claim_group_id': 'same'}],
                 'sentence_evidence': [{'claim_group_ids': ['same']}]}
    canonicalize_claim_group_ids(ambiguous)
    assert [row['claim_group_id'] for row in ambiguous['claim_groups']] == ['same', 'same']


def test_writer_claim_and_sentence_receipts():
    sources = [{'id': 1, 'content': 'I prefer the blue map to the red map.'},
               {'id': 2, 'content': 'The red map is smaller.'}]
    result = {'evidence_sufficient': True,
              'event_draft': 'I prefer the blue map to the red map.The red map is smaller.',
              'claim_groups': [
                  {'claim_group_id': 'g1', 'claim_type': 'subjective_comparison', 'owner': '我',
                   'render_mode': 'attribution_once', 'focus_role': 'core',
                   'summary': 'I prefer the blue map to the red map',
                   'comparison_sides': [{'subject': 'blue map', 'claim': 'preferred'},
                                        {'subject': 'red map', 'claim': 'less preferred'}],
                   'source_spans': [{'source_message_id': 1, 'quote': sources[0]['content']}]},
                  {'claim_group_id': 'g2', 'claim_type': 'fact', 'owner': '外部',
                   'render_mode': 'direct', 'focus_role': 'supporting',
                   'summary': sources[1]['content'],
                   'source_spans': [{'source_message_id': 2, 'quote': sources[1]['content']}]}],
              'sentence_evidence': [
                  {'sentence_index': 0, 'sentence': sources[0]['content'], 'claim_group_ids': ['g1'],
                   'source_spans': [{'source_message_id': 1, 'quote': sources[0]['content']}]},
                  {'sentence_index': 1, 'sentence': sources[1]['content'], 'claim_group_ids': ['g2'],
                   'source_spans': [{'source_message_id': 2, 'quote': sources[1]['content']}]}]}
    assert writer_receipt_errors(result, sources) == []
    spaced = copy.deepcopy(result)
    spaced['event_draft'] = sources[0]['content'] + '\n\n' + sources[1]['content'] + '\n'
    assert writer_receipt_errors(spaced, sources) == []
    broad = copy.deepcopy(result)
    broad['claim_groups'][0]['source_spans'].append({'source_message_id': 2, 'quote': sources[1]['content']})
    assert writer_receipt_errors(broad, sources) == []
    wrong = copy.deepcopy(result)
    wrong['claim_groups'][0]['render_mode'] = 'direct'
    assert any('主观命题' in error for error in writer_receipt_errors(wrong, sources))
    wrong = copy.deepcopy(result)
    wrong['sentence_evidence'][0]['source_spans'] = [wrong['sentence_evidence'][1]['source_spans'][0]]
    assert any('来源未被' in error for error in writer_receipt_errors(wrong, sources))
    wrong = copy.deepcopy(result)
    wrong['sentence_evidence'][0]['claim_group_ids'] = ['g3']
    assert any('不存在' in error for error in writer_receipt_errors(wrong, sources))
    wrong = copy.deepcopy(result)
    wrong['claim_groups'][1]['source_spans'][0]['quote'] = 'not present'
    assert any('逐字' in error for error in writer_receipt_errors(wrong, sources))
    wrong = copy.deepcopy(result)
    wrong['sentence_evidence'][0]['source_spans'][0]['quote'] = 'blue map'
    assert any('来源未被' in error for error in writer_receipt_errors(wrong, sources))
    wrong = copy.deepcopy(result)
    wrong['event_draft'] += 'Extra.'
    assert any('逐句' in error for error in writer_receipt_errors(wrong, sources))
    image_result = copy.deepcopy(result)
    image_result['claim_groups'][1]['source_spans'][0]['quote'] = 'Visible label'
    image_result['sentence_evidence'][1]['source_spans'][0]['quote'] = 'Visible label'
    image_sources = [{**sources[0]}, {**sources[1], 'evidence_texts': ['[文字] Visible label']}]
    assert writer_receipt_errors(image_result, image_sources) == []
    assert writer_receipt_errors({'evidence_sufficient': False, 'claim_groups': [], 'sentence_evidence': []}, sources) == []


def test_optional_receipts_check_sources_without_prescribing_attribution_phrases():
    source = {'id': 7, 'role': 'user', 'content': '一人一把'}
    span = {'source_message_id': 7, 'quote': source['content']}
    def result(sentence):
        return {'evidence_sufficient': True, 'event_draft': sentence,
                'claim_groups': [{'claim_group_id': 'g1', 'claim_type': 'fact', 'owner': '她',
                                  'render_mode': 'direct', 'focus_role': 'core',
                                  'summary': source['content'], 'source_spans': [span]}],
                'sentence_evidence': [{'sentence_index': 0, 'sentence': sentence,
                                       'claim_group_ids': ['g1'], 'source_spans': [span]}]}
    assert writer_receipt_errors(result('她答：“一人一把”。我仍走在她旁边。'), [source]) == []
    assert writer_receipt_errors(result('阿澄答：“一人一把”。我仍走在她旁边。'), [source],
                                 user_name='阿澄') == []
    assert writer_receipt_errors(result('她的“一人一把”让我仍想走在她旁边。'), [source]) == []
    # These checks cannot prove whether attribution or dialogue punctuation is
    # correct. A surrounding paragraph may already establish the speaker.
    assert writer_receipt_errors(result('“一人一把”。我仍走在她旁边。'), [source]) == []
    assert writer_receipt_errors(result('她答：“一人一把”我仍走在她旁边。'), [source]) == []
    assert writer_receipt_errors(result('记录来自另一位参与者：“一人一把”。'), [source]) == []
    invented = result('她答：“一人一把”。')
    invented['sentence_evidence'][0]['source_spans'] = [
        {'source_message_id': 99, 'quote': source['content']}]
    assert any('owned' in error for error in writer_receipt_errors(invented, [source]))
    image_source = {**source, 'content': '请看图片', 'evidence_texts': ['[文字] 一人一把']}
    assert writer_receipt_errors(result('画面写着“一人一把”。'), [image_source]) == []


def test_writer_accepts_long_exact_owned_quote_in_optional_receipt():
    quote = '这份记录逐项写清了背景、选择和实际结果。' * 5
    source = {'id': 9, 'role': 'user', 'content': quote}
    span = {'source_message_id': 9, 'quote': quote}
    receipt = {'evidence_sufficient': True, 'event_draft': '她留下了这份记录。',
               'claim_groups': [{'claim_group_id': 'g1', 'claim_type': 'fact', 'owner': '她',
                                 'render_mode': 'direct', 'focus_role': 'core',
                                 'summary': '她留下了记录', 'source_spans': [span]}],
               'sentence_evidence': [{'sentence_index': 0, 'sentence': '她留下了这份记录。',
                                      'claim_group_ids': ['g1'], 'source_spans': [span]}]}
    assert writer_receipt_errors(receipt, [source]) == []


def test_curator_review_requires_exclusive_boundary_and_parked_defer():
    component = {'context_messages': [{'id': 1, 'content': 'We picked the shelf.'},
                                      {'id': 2, 'content': 'The lamp needs a new switch.'},
                                      {'id': 3, 'content': 'Wait, the switch is loose.'}],
                 'memberships': [{'unit_root_message_id': 1, 'source_message_ids': [1]},
                                 {'unit_root_message_id': 2, 'source_message_ids': [2]}],
                 'parked_context_source_ids': [3]}
    plan = {'events': [{'primary_track_id': 'home', 'source_bindings': [{'source_message_id': 1}]},
                       {'primary_track_id': 'home', 'source_bindings': [{'source_message_id': 2}]}],
            'skip_source_message_ids': [], 'defer_source_message_ids': []}
    review = {'events': [{'event_index': 0, 'reason': 'Picked a shelf'},
                         {'event_index': 1, 'reason': 'Discussed lamp switch'}],
              'boundaries': [{'left_event_index': 0, 'right_event_index': 1,
                              'reason': 'The second activity concerns a different object',
                              'evidence': [{'source_message_id': 1, 'quote': 'picked the shelf'},
                                           {'source_message_id': 2, 'quote': 'new switch'}]}],
              'dispositions': []}
    assert curator_receipt_errors(review, plan, component) == []
    wrong = copy.deepcopy(review)
    wrong['boundaries'][0]['evidence'][1]['source_message_id'] = 1
    assert any('独占' in error for error in curator_receipt_errors(wrong, plan, component))
    wrong = copy.deepcopy(review)
    wrong['events'].pop()
    assert any('所有拟议' in error for error in curator_receipt_errors(wrong, plan, component))
    deferred = {'events': [], 'skip_source_message_ids': [], 'defer_source_message_ids': [1]}
    defer_review = {'events': [], 'boundaries': [],
                    'dispositions': [{'disposition': 'defer', 'unit_roots': [1],
                                      'reason': 'Parked correction changes the outcome',
                                      'parked_source_message_ids': [3]}]}
    assert curator_receipt_errors(defer_review, deferred, component) == []
    defer_review['dispositions'][0]['parked_source_message_ids'] = [999]
    assert any('真实 parked' in error for error in curator_receipt_errors(defer_review, deferred, component))


def test_joint_review_keeps_interleaved_same_track_boundary():
    messages = [{'id': index, 'content': f'Activity {index}'} for index in (1, 2, 3)]
    component = {'context_messages': messages, 'memberships': [],
                 'parked_context_source_ids': [], 'continuity_pairs': [{'bridge_unit_root': 9}]}
    plan = {'events': [{'primary_track_id': track,
                        'source_bindings': [{'source_message_id': index}]}
                       for index, track in ((1, 'A'), (2, 'B'), (3, 'A'))],
            'skip_source_message_ids': [], 'defer_source_message_ids': []}
    review = {'events': [{'event_index': index, 'reason': 'Separate activity'} for index in range(3)],
              'boundaries': [{'left_event_index': left, 'right_event_index': right,
                              'reason': 'Distinct originals',
                              'evidence': [{'source_message_id': index, 'quote': f'Activity {index}'}
                                           for index in (left + 1, right + 1)]}
                             for left, right in ((0, 1), (1, 2))],
              'dispositions': [], 'continuations': []}
    assert any('未覆盖全部相邻边界' in error
               for error in curator_receipt_errors(review, plan, component))
    review['boundaries'].append({'left_event_index': 0, 'right_event_index': 2,
                                 'reason': 'Same Track still has two distinct activities',
                                 'evidence': [{'source_message_id': 1, 'quote': 'Activity 1'},
                                              {'source_message_id': 3, 'quote': 'Activity 3'}]})
    assert curator_receipt_errors(review, plan, component) == []
