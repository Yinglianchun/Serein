"""Host annotation scopes stay distinct from complete Event ownership."""
import copy
import json

from serein.extensions import pipeline as p, pipeline_latest as latest, pipeline_materials as materials


def test_repair_scope_uses_host_snapshot_not_model_claims():
    messages = [{'id': n, 'content': f'Synthetic source {n}', 'session_id': 1,
                 'role': 'user' if n % 2 else 'assistant'} for n in range(1, 5)]
    old = {'event_ref': 'event:old', 'source_message_ids': [1, 2],
           'source_bindings': [{'source_message_id': n} for n in [1, 2]],
           'source_materials': [{'source_message_id': n, 'use': 'main',
                                'reason': 'Synthetic', 'omit_quotes': []} for n in [1, 2]]}
    snapshot = materials.snapshot_for(old, messages)
    snapshot.update(event_id='old', event_fingerprint='synthetic-fingerprint')
    base = {'event_id': 'old', 'fingerprint': 'synthetic-fingerprint',
            'source_message_ids': [1, 2], 'material_snapshot': snapshot}
    component = {'writer_material_review': True, 'material_contract_version': 1,
                 'context_messages': messages, 'base_event_candidates': [base]}
    event = {'event_ref': 'event:1', 'action': 'extend', 'base_event_ids': ['old'],
             'source_bindings': [{'source_message_id': n} for n in range(1, 5)]}
    output = {'events': [event], 'skip_source_message_ids': [], 'defer_source_message_ids': [],
              'decision_review': {'events': [{'event_index': 0, 'materials': []}]}}
    request = {'role': 'event_curator', 'component': component}
    before = copy.deepcopy((request, output))
    assert p.curator_material_repair_scope(request, output) == {
        'material_annotation_source_ids_by_event': {'0': [3, 4]}}
    assert (request, output) == before
    for action in ('create', 'rewrite', 'merge'):
        event['action'] = action
        assert p.curator_material_repair_scope(request, output) == {
            'material_annotation_source_ids_by_event': {'0': [1, 2, 3, 4]}}
    for malformed in ({}, [], None, 'not-an-object'):
        assert p.curator_material_repair_scope(request, malformed) == {}
    assert p.curator_material_repair_scope({'role': 'event_writer', 'component': component}, output) == {}


def test_unknown_snapshot_version_is_full_annotation():
    # Use the complete prompt fixture rather than trusting a repair-only mapping.
    from test_curator_material_identity import annotated_proposals
    component, output = annotated_proposals()
    component['material_contract_version'] = 1
    prompt = latest.build_event_track_curator_prompt('2026-01-01', component)
    model_input = json.loads(prompt.split('<event_curator_input_json>\n')[1].split('\n</')[0])
    assert all(base['extend_material_mode'] == 'full_sources'
               for base in model_input['base_events'])
    assert '不重复标注 inherited_materials' in prompt
    assert '不可遗漏或重复本次需要标注的来源' in prompt


def test_malformed_contract_versions_keep_prompt_and_host_on_legacy_contract():
    from test_curator_material_identity import annotated_proposals
    for version in (True, 1.0, '1', 2):
        component, _ = annotated_proposals()
        component['material_contract_version'] = version
        prompt = latest.build_event_track_curator_prompt('2026-01-01', component)
        model_input = json.loads(prompt.split('<event_curator_input_json>\n')[1].split('\n</')[0])
        assert all('extend_material_mode' not in base for base in model_input['base_events'])
        assert '不可遗漏或重复 owned 来源' in prompt
        assert '不重复标注 inherited_materials' not in prompt
