"""An adopted pending range shares the validation, repair and retention scope."""
import asyncio
import copy
import json

import pytest

from serein.core.store import Conflict, Store, encode
from serein.deployment import save_settings
from serein.extensions import pipeline as p, pipeline_latest as latest
from test_public_features import settings, ingest, output_for
from test_pipeline_pending import retained


def request_and_omission():
    messages = [{'id': key, 'session_id': 1, 'role': 'user' if key % 2 else 'assistant',
                 'content': f'Original {key}', 'created_at': f'2025-01-01T00:0{key}:00Z'}
                for key in range(1, 5)]
    component = {'messages': messages[2:], 'context_messages': messages,
        'track_ids': ['track-a'], 'track_cards': [{'track_id': 'track-a'}],
        'context_session_ids': [1], 'parked_context_source_ids': [], 'base_event_candidates': [],
        'memberships': [{'unit_root_message_id': row['id'], 'source_message_ids': [row['id']],
                         'track_id': 'track-a', 'session_id': 1, 'routing_role': 'primary_activity'}
                        for row in messages], 'pending_material_read': {'id': 7, 'messages': messages[:2]}}
    request = {'role': 'event_curator', 'identity': {'user_name': 'User', 'ai_name': 'AI'},
               'component': component, 'prompt': 'Synthetic original prompt', 'images': []}
    output = {'events': [{'action': 'create', 'base_event_ids': [], 'primary_track_id': 'track-a',
                          'owned_unit_roots': [3, 4]}], 'skip_unit_roots': [], 'defer_unit_roots': [],
              'adopt_pending_material_ids': [7]}
    return request, output


def test_adopted_missing_messages_are_present_in_the_repair_request():
    request, output = request_and_omission()
    before = copy.deepcopy(request)
    with pytest.raises(latest.CuratorCoverageError) as error:
        p.validate(request, output)
    assert error.value.missing_source_ids == [1, 2]
    repair = p.curator_repair_prompt(request, output, error.value)
    payload = json.loads(repair.rsplit('\n', 1)[1])
    assert [row['id'] for row in payload['missing_messages']] == [1, 2]
    assert payload['missing_unit_roots'] == [1, 2]
    assert payload['allowed_ids']['stable_unit_roots'] == [1, 2, 3, 4]
    assert request == before


def test_material_repair_scope_includes_explicitly_adopted_sources():
    request, output = request_and_omission()
    request['component']['writer_material_review'] = True
    output['events'][0]['owned_unit_roots'] = [1, 2, 3, 4]
    scope = p.curator_material_repair_scope(request, output)
    assert set(scope['material_annotation_source_ids_by_event']['0']) == {1, 2, 3, 4}


def test_background_reading_does_not_expand_repair_ownership():
    request, output = request_and_omission()
    output.pop('adopt_pending_material_ids')
    output['events'][0]['owned_unit_roots'] = [3]
    with pytest.raises(latest.CuratorCoverageError) as error:
        p.validate(request, output)
    payload = json.loads(p.curator_repair_prompt(request, output, error.value).rsplit('\n', 1)[1])
    assert [row['id'] for row in payload['missing_messages']] == [4]
    assert payload['allowed_ids']['stable_unit_roots'] == [3, 4]


@pytest.mark.parametrize('api', [False, True])
@pytest.mark.parametrize('repair_succeeds', [False, True])
def test_pending_adoption_omission_repairs_or_retains_both_ranges(settings, monkeypatch, api, repair_succeeds):
    old = retained(settings)
    ingest(settings, 2)
    calls = []
    async def run(role, request):
        if role == 'event_curator':
            component = request['component']
            if not request.get('context_read'):
                return {'context_request': {'pending_material_id': old['id'], 'reason': 'continue_pending'}}
            calls.append(request['prompt'])
            if len(calls) == 2:
                repair = json.loads(request['prompt'].rsplit('\n', 1)[1])
                assert [row['id'] for row in repair['missing_messages']] == [1, 2]
                assert sorted(repair['allowed_ids']['stable_unit_roots']) == [1, 2, 3, 4]
            output = output_for(role, request)
            output['adopt_pending_material_ids'] = [old['id']]
            if repair_succeeds and len(calls) == 2:
                output['events'][0]['owned_unit_roots'] = [u['unit_root_message_id'] for u in component['memberships']]
            return output
        return output_for(role, request)
    if api:
        save_settings(settings.database, {'models': [{'id': 'synthetic', 'model': 'synthetic',
            'base_url': 'http://127.0.0.1:9/v1'}], 'assignments': {role: 'synthetic' for role in p.ROLES}})
        async def complete(model, payload):
            with Store(settings.database, read_only=True) as store:
                request = json.loads(store.conn.execute('SELECT request_json FROM pipeline_jobs '
                    'WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1').fetchone()[0])
            request['prompt'] = payload['messages'][1]['content']
            return {'choices': [{'message': {'content': json.dumps(await run(request['role'], request))}}]}
        monkeypatch.setattr('serein.model_runtime.complete', complete)
    result = asyncio.run(p.advance(settings.database, include_recent=True, runner=None if api else run))
    assert len(calls) == 2
    with Store(settings.database, read_only=True) as store:
        originals = [row[0] for row in store.conn.execute('SELECT id FROM raw_events ORDER BY id')]
        assert originals == [1, 2, 3, 4]
        old_status = store.conn.execute('SELECT status FROM pipeline_pending_materials WHERE id=?', (old['id'],)).fetchone()[0]
        if repair_succeeds:
            assert result['events'] == 1 and result['processed_originals'] == 4
            assert old_status == 'resolved'
        else:
            assert result['events'] == 0 and result['processed_originals'] == 0
            assert old_status == 'open'
            entry = result['curator_omission_deferrals'][0]
            assert entry['missing_source_message_ids'] == [1, 2]
            assert entry['deferred_source_message_ids'] == [1, 2, 3, 4]
            assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 0
            receipt = json.loads(store.conn.execute("SELECT output_json FROM pipeline_jobs WHERE role='event_curator:0:context'").fetchone()[0])
            assert receipt['_host_curator_omission']['adopt_pending_material_ids'] == [old['id']]
            assert p.curator_omission_attempts(settings.database, store.conn.execute(
                "SELECT id FROM pipeline_jobs WHERE role='event_curator:0:context'").fetchone()[0]) == 2
    if not repair_succeeds:
        async def forbidden(*args):
            pytest.fail('Retained omission range was called again without explicit resume')
        assert asyncio.run(p.advance(settings.database, include_recent=True, runner=forbidden))['status'] == 'current'


@pytest.mark.parametrize('ids,read', [([7], False), ([8], True), ([True], True), ([7, 7], True)])
def test_invalid_adoption_cannot_authorize_repair_or_retention(settings, ids, read):
    request, output = request_and_omission()
    request['component']['writer_material_review'] = True
    if not read:
        request['component'].pop('pending_material_read')
        request['component']['pending_materials'] = [{'pending_material_id': 7}]
    output['adopt_pending_material_ids'] = ids
    with pytest.raises(ValueError) as error:
        p.validate(request, output)
    assert not isinstance(error.value, latest.CuratorCoverageError)
    scope = p.correction_scope(request, output)
    assert scope['allowed_ids']['message_ids'] == [3, 4]
    assert scope['allowed_ids']['stable_unit_roots'] == [3, 4]
    assert 'material_annotation_source_ids_by_event' not in scope

    frozen = save_job(settings, request)
    with pytest.raises(ValueError):
        p.accept_curator_omission(settings.database, 'job', latest.CuratorCoverageError([1]), output, frozen)
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute("SELECT output_json FROM pipeline_jobs WHERE id='job'").fetchone()[0] is None


def save_job(settings, request):
    p.initialize(settings.database)
    frozen = encode(request)
    with Store(settings.database) as store:
        store.conn.execute("INSERT INTO pipeline_batches(id,scope,input_json,status) VALUES ('batch','one','{}','pending')")
        store.conn.execute("INSERT INTO pipeline_jobs(id,batch_id,role,request_json) VALUES ('job','batch','event_curator:0',?)", (frozen,))
    return frozen


def test_changed_request_cannot_inherit_an_earlier_omission(settings):
    request, output = request_and_omission()
    frozen = save_job(settings, request)
    with pytest.raises(latest.CuratorCoverageError) as error:
        p.validate(request, output)
    # Even a replacement with the same pending ID and a superset of sources
    # cannot retain material on behalf of the request that failed validation.
    request['component']['messages'].append({'id': 42})
    with Store(settings.database) as store:
        store.conn.execute("UPDATE pipeline_jobs SET request_json=? WHERE id='job'", (encode(request),))
    with pytest.raises(Conflict, match='changed'):
        p.accept_curator_omission(settings.database, 'job', error.value, output, frozen)
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute("SELECT output_json FROM pipeline_jobs WHERE id='job'").fetchone()[0] is None


@pytest.mark.parametrize('disposition', ['skip_unit_roots', 'defer_unit_roots'])
def test_illegal_partial_adoption_does_not_become_an_omission(disposition):
    request, output = request_and_omission()
    output[disposition] = [1]  # Source 2 is still missing.
    with pytest.raises(ValueError, match='整份承接') as error:
        p.validate(request, output)
    assert not isinstance(error.value, latest.CuratorCoverageError)


def test_expanded_foreign_binding_cannot_become_a_material_instruction():
    request, output = request_and_omission()
    request['component']['writer_material_review'] = True
    expanded = latest._expand_compact_event_curator_output(p.decision(output), request['component'])
    bindings = expanded['events'][0]['source_bindings']
    bindings.append({'source_message_id': 1, 'activity_role': 'primary_activity'})
    assert 'material_annotation_source_ids_by_event' not in p.correction_scope(request, expanded)
    expanded['adopt_pending_material_ids'] = [7]
    assert set(p.correction_scope(request, expanded)['material_annotation_source_ids_by_event']['0']) == {1, 3, 4}
    bindings.append({'source_message_id': 42, 'activity_role': 'primary_activity'})
    assert 'material_annotation_source_ids_by_event' not in p.correction_scope(request, expanded)


def test_repair_roots_follow_unit_membership_even_when_root_and_source_ids_differ():
    request, output = request_and_omission()
    request['component']['memberships'][0]['unit_root_message_id'] = 101
    with pytest.raises(latest.CuratorCoverageError) as error:
        p.validate(request, output)
    repair = json.loads(p.curator_repair_prompt(request, output, error.value).rsplit('\n', 1)[1])
    assert repair['missing_unit_roots'] == [2, 101]
    assert sorted(repair['allowed_ids']['stable_unit_roots']) == [2, 3, 4, 101]
    output['events'][0]['owned_unit_roots'] += [101, 2]
    expanded = latest._expand_compact_event_curator_output(
        p.decision(output), p.pending_materials.adopted_component(request['component'], output))
    assert {row['source_message_id'] for row in expanded['events'][0]['source_bindings']} == {1, 2, 3, 4}
