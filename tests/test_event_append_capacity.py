"""Near-full Events keep their prose and give new evidence a follow-up Event."""
import asyncio
import copy
import json

import pytest

from test_protected_append_sources import case
from test_public_features import ingest, output_for, settings, synthetic_runner
from serein.core.store import Store
from serein.extensions import pipeline as p
from serein.extensions import pipeline_latest as latest
from serein.extensions import pipeline_recovery as recovery


def _seed_base(settings, remaining):
    ingest(settings)
    body_chars = latest.EVENT_BODY_ACCEPT_MAX_CHARS - 2 - remaining
    with Store(settings.database) as store:
        store.conn.execute("UPDATE raw_events SET text=? WHERE source_event_id='u1'",
                           ('x' * (body_chars - len('We agreed to ')),))
    assert asyncio.run(p.advance(settings.database, include_recent=True,
                                runner=synthetic_runner))['events'] == 1
    with Store(settings.database, read_only=True) as store:
        old = dict(store.conn.execute("SELECT * FROM fact_events WHERE status='active'").fetchone())
    assert latest.append_remaining_chars(old) == remaining
    return old


@pytest.mark.parametrize('remaining', range(1, 11))
def test_one_to_ten_remaining_rolls_over_before_writer(settings, remaining):
    old = _seed_base(settings, remaining)
    ingest(settings, 2)
    writers = []

    async def runner(role, request):
        if role == 'event_curator':
            base, = latest.event_curator_model_input(request['component'])['base_events']
            assert base['append_remaining_chars'] == remaining
        if role == 'event_writer':
            writers.append(request)
            assert request['writer_mode'] == 'create'
            assert request['event']['capacity_recovery']['base_event_id'] == old['item_id']
            assert {message['id'] for message in request['messages']} == {3, 4}
        return output_for(role, request)

    result = asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))
    assert result['events'] == 1 and result['processed_originals'] == 2
    assert len(writers) == 1
    with Store(settings.database, read_only=True) as store:
        previous = dict(store.conn.execute('SELECT * FROM fact_events WHERE item_id=?',
                                          (old['item_id'],)).fetchone())
        assert previous == old
        current = store.conn.execute("SELECT item_id,body FROM fact_events WHERE status='active' AND item_id!=?",
                                     (old['item_id'],)).fetchone()
        assert current['body'] == 'We agreed to Book club plan 2'
        for event_id in (old['item_id'], current['item_id']):
            assert store.conn.execute('SELECT count(*) FROM fact_event_sources WHERE item_id=?',
                                      (event_id,)).fetchone()[0] == 2
        tracks = store.conn.execute('SELECT DISTINCT track_id FROM pipeline_track_events WHERE event_id IN (?,?)',
                                    (old['item_id'], current['item_id'])).fetchall()
        assert len(tracks) == 1
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))['status'] == 'current'
    assert len(writers) == 1


@pytest.mark.parametrize('remaining,expected', [(49, 'create'), (50, 'extend'), (500, 'extend')])
@pytest.mark.parametrize('protected', [False, True])
def test_capacity_reserve_boundary_keeps_ordinary_append(remaining, expected, protected):
    component, proposal = case(stable=(3, 4))
    component['base_event_candidates'][0].update(
        protected=protected, body='x' * (1500 - 2 - remaining))
    before = copy.deepcopy(component)
    event, = latest.normalize_event_curator_output(proposal, component)['events']
    assert component == before
    assert event['action'] == expected
    if expected == 'create':
        assert event['base_event_ids'] == [] and not event.get('append_only')
        assert event['source_message_ids'] == [3, 4]
    else:
        assert event['base_event_ids'] == ['target'] and event['append_only']
        assert event['source_message_ids'] == [1, 2, 3, 4]


def test_fifty_remaining_still_appends_and_preserves_settings(settings):
    old = _seed_base(settings, 50)
    ingest(settings, 2)
    writers = []

    async def runner(role, request):
        if role == 'event_writer':
            writers.append(request)
            assert request['writer_mode'] == 'append'
            assert request['append_remaining_chars'] == 50
            assert {message['id'] for message in request['messages']} == {3, 4}
        return output_for(role, request)

    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))['events'] == 1
    assert len(writers) == 1
    with Store(settings.database, read_only=True) as store:
        current = store.conn.execute("SELECT title,body,recallable FROM fact_events WHERE status='active'").fetchone()
        assert current['body'] == old['body'] + '\n\nWe agreed to Book club plan 2'
        assert current['title'] == old['title'] and current['recallable'] == old['recallable']


@pytest.mark.parametrize('remaining', [1, 10, 49])
@pytest.mark.parametrize('kind', ['disabled', 'forked', 'unselected_manual', 'stable_old_sources'])
def test_small_capacity_cannot_bypass_protected_source_boundary(remaining, kind):
    stable = (1, 2, 3, 4) if kind in ('unselected_manual', 'stable_old_sources') else (3, 4)
    component, proposal = case(stable=stable, opt_in=kind != 'disabled',
                               manual=kind == 'unselected_manual')
    component['base_event_candidates'][0]['body'] = 'x' * (1500 - 2 - remaining)
    if kind == 'forked':
        component['base_event_candidates'][0]['forked'] = True
    plan = latest.normalize_event_curator_output(proposal, component)
    assert plan['events'] == []
    assert plan['defer_source_message_ids'] == list(stable)


@pytest.mark.parametrize('remaining', [1, 10, 49])
@pytest.mark.parametrize('action', ['rewrite', 'merge'])
def test_small_capacity_base_cannot_be_rewritten_or_merged(remaining, action):
    component, proposal = case(stable=(5, 6), action=action)
    base = component['base_event_candidates'][0]
    base.update(protected=False, body='x' * (1500 - 2 - remaining))
    if action == 'merge':
        component['base_event_candidates'].append({
            **base, 'event_id': 'manual-other', 'body': 'Second body', 'source_message_ids': [3, 4]})
    with pytest.raises(ValueError, match='容量.*只作背景'):
        latest.normalize_event_curator_output(proposal, component)


@pytest.mark.parametrize('remaining', [1, 10, 49])
def test_capacity_rollover_needs_new_primary_evidence(remaining):
    component, proposal = case(stable=(3, 4))
    component['base_event_candidates'][0].update(protected=False, body='x' * (1500 - 2 - remaining))
    component['track_ids'].append('b')
    component['memberships'][0].update(track_id='b', routing_role='bridge')
    component['context_edges'] = [{'unit_root_message_id': 3, 'track_id': 'a'}]
    plan = latest.normalize_event_curator_output(proposal, component)
    assert plan['events'] == []
    assert plan['defer_source_message_ids'] == [3, 4]
    assert plan['hard_skips'][0]['reason'] == 'capacity_boundary_unresolved'


@pytest.mark.parametrize('remaining', [1, 10])
@pytest.mark.parametrize('legacy_request', [False, True])
def test_unfinished_frozen_append_request_recovers_to_followup(settings, monkeypatch, remaining, legacy_request):
    old = _seed_base(settings, remaining)
    ingest(settings, 2)
    # Old request shape at the current runtime; actual older revisions still
    # require the existing explicit rebuild, exercised separately below.
    with monkeypatch.context() as legacy:
        legacy.setattr(latest, 'EVENT_APPEND_MIN_REMAINING_CHARS', 1)
        task = asyncio.run(p.advance(settings.database, include_recent=True))
        while task['role'] != 'event_writer':
            p.submit(settings.database, task['job_id'], output_for(task['role'], task['request']))
            task = asyncio.run(p.advance(settings.database, include_recent=True))
    with Store(settings.database, read_only=True) as store:
        frozen = json.loads(store.conn.execute('SELECT request_json FROM pipeline_jobs WHERE id=?',
                                               (task['job_id'],)).fetchone()[0])
    assert frozen['writer_mode'] == 'append' and frozen['append_remaining_chars'] == remaining
    if legacy_request:
        frozen.pop('writer_mode')
        frozen.pop('append_remaining_chars')
        frozen['event'].pop('append_only')
        frozen['writer_previous_body_only'] = 1
        with Store(settings.database) as store:
            store.conn.execute('UPDATE pipeline_jobs SET request_json=? WHERE id=?',
                               (json.dumps(frozen), task['job_id']))
    roles = []

    async def runner(role, request):
        roles.append(role)
        assert role == 'event_writer' and request['writer_mode'] == 'create'
        assert request['event']['base_event_ids'] == []
        assert {message['id'] for message in request['messages']} == {3, 4}
        with Store(settings.database, read_only=True) as store:
            stored = json.loads(store.conn.execute('SELECT request_json FROM pipeline_jobs WHERE id=?',
                                                   (task['job_id'],)).fetchone()[0])
            assert stored['writer_mode'] == 'create'
        return output_for(role, request)

    result = asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))
    assert result['events'] == 1 and result['processed_originals'] == 2
    assert roles == ['event_writer']
    with Store(settings.database, read_only=True) as store:
        assert dict(store.conn.execute('SELECT * FROM fact_events WHERE item_id=?',
                                       (old['item_id'],)).fetchone()) == old
        assert store.conn.execute("SELECT count(*) FROM fact_events WHERE status='active'").fetchone()[0] == 2


def test_old_runtime_needs_explicit_rebuild_before_near_full_followup(settings, monkeypatch):
    old = _seed_base(settings, 10)
    ingest(settings, 2)
    with monkeypatch.context() as legacy:
        legacy.setattr(latest, 'EVENT_APPEND_MIN_REMAINING_CHARS', 1)
        task = asyncio.run(p.advance(settings.database, include_recent=True))
        while task['role'] != 'event_writer':
            p.submit(settings.database, task['job_id'], output_for(task['role'], task['request']))
            task = asyncio.run(p.advance(settings.database, include_recent=True))
    with Store(settings.database) as store:
        batch_id = store.conn.execute('SELECT batch_id FROM pipeline_jobs WHERE id=?',
                                      (task['job_id'],)).fetchone()[0]
        data = json.loads(store.conn.execute('SELECT input_json FROM pipeline_batches WHERE id=?',
                                             (batch_id,)).fetchone()[0])
        data['runtime_revision'] = 'synthetic-before-capacity-fix'
        store.conn.execute('UPDATE pipeline_batches SET input_json=? WHERE id=?',
                           (json.dumps(data), batch_id))
        for row in store.conn.execute('SELECT id,request_json FROM pipeline_jobs WHERE batch_id=?',
                                      (batch_id,)).fetchall():
            request = json.loads(row['request_json'])
            request['runtime_revision'] = data['runtime_revision']
            store.conn.execute('UPDATE pipeline_jobs SET request_json=? WHERE id=?',
                               (json.dumps(request), row['id']))

    async def forbidden(*args):
        raise AssertionError('An old runtime must be held before model calls')

    held = asyncio.run(p.advance(settings.database, include_recent=True, runner=forbidden))
    assert held['status'] == 'needs_repair' and held['repair_kind'] == 'runtime_contract'
    asyncio.run(recovery.rebuild(settings.database, batch_id, 'REBUILD_PIPELINE_BATCH'))
    writers = []

    async def runner(role, request):
        if role == 'event_writer':
            writers.append(request)
            assert request['writer_mode'] == 'create'
            assert {message['id'] for message in request['messages']} == {3, 4}
        return output_for(role, request)

    result = asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))
    assert result['events'] == 1 and result['processed_originals'] == 2
    assert len(writers) == 1
    with Store(settings.database, read_only=True) as store:
        assert dict(store.conn.execute('SELECT * FROM fact_events WHERE item_id=?',
                                       (old['item_id'],)).fetchone()) == old
        assert store.conn.execute("SELECT count(*) FROM fact_events WHERE status='active'").fetchone()[0] == 2


def test_completed_small_frozen_append_is_not_replayed(settings, monkeypatch):
    old = _seed_base(settings, 1)
    ingest(settings, 2)
    with monkeypatch.context() as legacy:
        legacy.setattr(latest, 'EVENT_APPEND_MIN_REMAINING_CHARS', 1)
        task = asyncio.run(p.advance(settings.database, include_recent=True))
        while task['role'] != 'event_writer':
            p.submit(settings.database, task['job_id'], output_for(task['role'], task['request']))
            task = asyncio.run(p.advance(settings.database, include_recent=True))
        output = output_for('event_writer', task['request'])
        output['event_draft'] = '好'
        output['sentence_evidence'][0]['sentence'] = '好'
        p.submit(settings.database, task['job_id'], output)

    async def forbidden(*args):
        raise AssertionError('Accepted completed stages must not call the model again')

    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=forbidden))['events'] == 1
    with Store(settings.database, read_only=True) as store:
        current = store.conn.execute("SELECT body FROM fact_events WHERE status='active'").fetchone()[0]
        assert current == old['body'] + '\n\n好'
        assert store.conn.execute('SELECT count(*) FROM fact_events').fetchone()[0] == 2
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=forbidden))['status'] == 'current'
