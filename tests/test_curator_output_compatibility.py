"""A known extra prose summary cannot grant ownership or supply evidence."""
import asyncio
import copy
import json

import pytest

from test_public_features import settings, ingest, output_for
from test_latest_pipeline import curator_task
from test_shared_bridge_boundary import case, foreign_bridge_component
from serein.core.store import Store
from serein.deployment import save_settings
from serein.extensions import pipeline as p, pipeline_latest as latest


EXTRA_SUMMARY = 'UNSUPPORTED_EXTRA_SUMMARY_6a3f'


def with_summary(proposal):
    result = copy.deepcopy(proposal)
    for event in result['events']:
        event['primary_activity'] = EXTRA_SUMMARY
    return result


def test_extra_primary_activity_string_preserves_the_exact_host_plan():
    component, proposal = case()
    expected = latest.normalize_event_curator_output(proposal, component)
    explained = with_summary(proposal)
    before = copy.deepcopy(explained)
    assert latest.normalize_event_curator_output(explained, component) == expected
    assert explained == before
    assert EXTRA_SUMMARY not in json.dumps(expected)


@pytest.mark.parametrize('change, error', [
    ({'primary_track_id': 'invented-track'}, 'invalid Event owners'),
    ({'owned_unit_roots': [999999]}, 'invalid unit root'),
    ({'primary_activity': ['invented ownership']}, 'compact Event fields differ'),
    ({'activity_role': 'primary_activity'}, 'compact Event fields differ'),
    ({'explanation': EXTRA_SUMMARY}, 'compact Event fields differ'),
])
def test_extra_summary_does_not_relax_structural_or_ownership_checks(change, error):
    component, proposal = case()
    explained = with_summary(proposal)
    explained['events'][0].update(change)
    with pytest.raises(ValueError, match=error):
        latest.normalize_event_curator_output(explained, component)


def test_extra_summary_cannot_replace_a_required_field():
    component, proposal = case()
    explained = with_summary(proposal)
    del explained['events'][0]['owned_unit_roots']
    with pytest.raises(ValueError, match='compact Event fields differ'):
        latest.normalize_event_curator_output(explained, component)


def test_extra_summary_cannot_promote_foreign_or_shared_bridge_to_primary():
    foreign = foreign_bridge_component()
    proposal = {'events': [{'action': 'create', 'base_event_ids': [], 'primary_track_id': 'b',
                           'owned_unit_roots': [1], 'primary_activity': EXTRA_SUMMARY}],
                'skip_unit_roots': [], 'defer_unit_roots': []}
    with pytest.raises(ValueError, match='has no generated primary_activity'):
        latest.normalize_event_curator_output(proposal, foreign)

    component, proposal = case()
    explained = with_summary(proposal)
    explained['events'][0]['owned_unit_roots'] = [3]
    explained['skip_unit_roots'] = [1]
    with pytest.raises(ValueError, match='has no generated primary_activity'):
        latest.normalize_event_curator_output(explained, component)


def test_extra_summary_cannot_supply_boundary_evidence():
    component, proposal = case()
    explained = with_summary(proposal)
    explained['decision_review']['boundaries'][0]['evidence'][0]['quote'] = EXTRA_SUMMARY
    with pytest.raises(ValueError, match='boundary|边界'):
        latest.normalize_event_curator_output(explained, component)


def test_extra_summary_is_not_accepted_on_expanded_host_events():
    component, proposal = case()
    expanded = latest._expand_compact_event_curator_output(
        {key: value for key, value in proposal.items() if key != 'decision_review'}, component)
    expanded['decision_review'] = proposal['decision_review']
    expanded['events'][0]['primary_activity'] = EXTRA_SUMMARY
    with pytest.raises(ValueError, match='invalid fields'):
        latest.normalize_event_curator_output(expanded, component)


def test_curator_submit_saves_canonical_output_and_is_idempotent(settings):
    ingest(settings)
    task = curator_task(settings)
    output = output_for('event_curator', task['request'])
    explained = with_summary(output)
    before = copy.deepcopy(explained)
    assert p.submit(settings.database, task['job_id'], explained)['status'] == 'accepted'
    assert explained == before
    assert p.submit(settings.database, task['job_id'], output)['status'] == 'unchanged'
    with Store(settings.database, read_only=True) as store:
        saved = json.loads(store.conn.execute('SELECT output_json FROM pipeline_jobs WHERE id=?',
                                             (task['job_id'],)).fetchone()[0])
    assert saved == output


def test_model_attempt_keeps_original_summary_but_saved_output_is_canonical(settings, monkeypatch):
    ingest(settings)
    task = curator_task(settings)
    output = output_for('event_curator', task['request'])
    raw = json.dumps(with_summary(output), ensure_ascii=False, indent=2)
    save_settings(settings.database, {
        'models': [{'id': 'synthetic', 'model': 'synthetic', 'base_url': 'http://127.0.0.1:9/v1'}],
        'assignments': {'event_curator': 'synthetic'},
    })
    calls = []
    async def complete(model, payload):
        calls.append(payload)
        return {'choices': [{'message': {'content': raw}}]}
    monkeypatch.setattr('serein.model_runtime.complete', complete)
    result = asyncio.run(p.advance(settings.database, include_recent=True))
    assert result['role'] == 'event_writer' and len(calls) == 1
    with Store(settings.database, read_only=True) as store:
        attempts = store.conn.execute(
            'SELECT output_text,error FROM pipeline_attempts WHERE job_id=? ORDER BY id',
            (task['job_id'],)).fetchall()
        assert [(row['output_text'], row['error']) for row in attempts] == [(raw, '')]
        saved = json.loads(store.conn.execute('SELECT output_json FROM pipeline_jobs WHERE id=?',
                                             (task['job_id'],)).fetchone()[0])
    assert saved == output


def test_extra_summary_reaches_writer_without_becoming_material_or_saved_text(settings):
    ingest(settings)
    calls = []
    async def run(role, request):
        calls.append(role)
        output = output_for(role, request)
        if role == 'event_curator':
            return with_summary(output)
        if role == 'event_writer':
            assert EXTRA_SUMMARY not in json.dumps(request)
            assert request['event']['source_message_ids'] == [1, 2]
            assert all(row['activity_role'] == 'primary_activity'
                       for row in request['event']['source_bindings'])
        return output
    result = asyncio.run(p.advance(settings.database, include_recent=True, runner=run))
    assert result['events'] == 1 and result['pending'] == 0
    assert calls == ['track_router', 'event_curator', 'event_writer']
    with Store(settings.database, read_only=True) as store:
        saved = store.conn.execute("SELECT output_json FROM pipeline_jobs WHERE role LIKE 'event_curator:%'").fetchone()[0]
        assert EXTRA_SUMMARY not in saved
        assert 'primary_activity' not in json.loads(saved)['events'][0]
        assert EXTRA_SUMMARY not in store.conn.execute('SELECT body FROM fact_events').fetchone()[0]
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 2
