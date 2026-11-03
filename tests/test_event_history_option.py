"""The Event review switch is independent of ordinary chat recall."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from test_public_features import settings
from test_pipeline_batch_recovery import seed, short_runner, short_output
from serein.api.http import create_app
from serein.core.store import Store
from serein.deployment import read_settings
from serein.extensions import pipeline as p, pipeline_recovery as recovery


def client_for(settings):
    return TestClient(create_app(settings, token='synthetic', live=True), headers={'Authorization': 'Bearer synthetic'})


@pytest.mark.parametrize('legacy_value', [True, False])
def test_legacy_reduce_input_field_does_not_reject_other_settings(settings, legacy_value):
    client = client_for(settings)
    response = client.patch('/v1/settings', json={'pipeline': {
        'curator_summary_first': legacy_value, 'timeout_seconds': 900}})
    assert response.status_code == 200, response.text
    assert response.json()['pipeline']['curator_summary_first'] is True
    assert client.get('/v1/settings').json()['pipeline']['timeout_seconds'] == 900


def test_event_history_defaults_on_and_roundtrips_without_changing_chat_memory(settings):
    client = client_for(settings)
    before = client.get('/v1/settings').json()
    assert before['pipeline']['event_history_enabled'] is True
    for enabled in (False, True):
        response = client.patch('/v1/settings', json={'pipeline': {'event_history_enabled': enabled}})
        assert response.status_code == 200, response.text
        state = response.json()
        assert state['pipeline']['event_history_enabled'] is enabled
        assert state['upstream'] == before['upstream'] and state['recall'] == before['recall']
        assert state['pipeline']['track_lookback_days'] == before['pipeline']['track_lookback_days']
    for invalid in (0, 1, 'false'):
        assert client.patch('/v1/settings', json={'pipeline': {'event_history_enabled': invalid}}).status_code == 422


def test_disabled_event_history_never_loads_old_candidates_and_only_creates_new_event(settings, monkeypatch):
    seed(settings, count=1, size=30)
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=short_runner))['events'] == 1
    with Store(settings.database, read_only=True) as store:
        previous = dict(store.conn.execute("SELECT * FROM fact_events WHERE status='active'").fetchone())
    client = client_for(settings)
    assert client.patch('/v1/settings', json={'pipeline': {'event_history_enabled': False}}).status_code == 200
    seed(settings, start=2, count=1, size=30)
    seen = []
    async def current_only(role, request):
        if role in ('event_curator', 'event_writer'):
            seen.append(role)
            assert request['component']['base_event_candidates'] == []
            assert {m['id'] for m in request['component']['context_messages']} == {3, 4}
            assert previous['body'] not in request['prompt']
        result = short_output(role, request)
        if role == 'event_curator':
            assert result['events'][0]['action'] == 'create'
        return result
    with monkeypatch.context() as scoped:
        scoped.setattr(p, 'candidates', lambda *a, **kw: pytest.fail('Old Event candidates must not be loaded'))
        assert asyncio.run(p.advance(settings.database, include_recent=True, runner=current_only))['events'] == 1
    assert seen == ['event_curator', 'event_writer']
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute("SELECT count(*) FROM fact_events WHERE status='active'").fetchone()[0] == 2
        assert dict(store.conn.execute('SELECT * FROM fact_events WHERE item_id=?', (previous['item_id'],)).fetchone()) == previous
    # Re-enabling restores the ordinary continuation/merge path for future batches.
    assert client.patch('/v1/settings', json={'pipeline': {'event_history_enabled': True}}).status_code == 200
    seed(settings, start=4, count=1, size=30)
    bases = []
    async def with_history(role, request):
        if role == 'event_curator':
            bases.extend(request['component']['base_event_candidates'])
        return short_output(role, request)
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=with_history))['events'] == 1
    assert len(bases) == 2


def test_explicit_rebuild_applies_current_event_history_choice_without_mutating_old_plan(settings):
    seed(settings, count=1, size=30)
    p.initialize(settings.database)
    batch = p.new_batch(settings.database, True)
    assert json.loads(batch['input_json'])['input_policy']['event_history_enabled'] is True
    client = client_for(settings)
    assert client.patch('/v1/settings', json={'pipeline': {'event_history_enabled': False}}).status_code == 200
    p.mark_needs_repair(settings.database, batch, p.RoutingRecoveryError('Synthetic explicit repair'))
    result = asyncio.run(recovery.rebuild(settings.database, batch['id'], 'REBUILD_PIPELINE_BATCH'))
    with Store(settings.database, read_only=True) as store:
        old = store.conn.execute('SELECT input_json FROM pipeline_batches WHERE id=?', (batch['id'],)).fetchone()[0]
        fresh = json.loads(store.conn.execute('SELECT input_json FROM pipeline_batches WHERE id=?', (result['batch_id'],)).fetchone()[0])
    assert old == batch['input_json']
    assert fresh['input_policy']['event_history_enabled'] is False
    assert read_settings(settings.database)['pipeline']['curator_summary_first'] is True
