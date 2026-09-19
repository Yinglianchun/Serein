import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from test_public_features import settings, ingest, output_for, synthetic_runner, raw_archive
from serein.core.store import Store, encode
from serein.extensions import pipeline as p
from serein.extensions import pipeline_tracks as tracks
from serein.extensions import pipeline_latest as latest
from serein.extensions.pipeline_rules import normalize_event_track_message_output
from serein.extensions.pipeline_rules import flushable_dialogue_units
from serein.deployment import save_settings


def card(key, **values):
    return {'track_id': key, 'subject': 'Synthetic', 'throughline': 'Continue synthetic work',
            'event_policy': 'default', 'status': 'active', **values}


def exchanges(count, start=None, session=1, first_id=1):
    start = start or datetime(2026, 9, 14, tzinfo=timezone.utc)
    return [{'id': first_id + i, 'source_event_id': str(first_id + i), 'session_id': session,
             'role': 'user' if i % 2 == 0 else 'assistant', 'text': 'Synthetic exchange '+str(i),
             'created_at': (start + timedelta(minutes=i // 2, seconds=i % 2)).isoformat()}
            for i in range(count * 2)]


def test_flush_waits_for_actual_silence_not_age_of_individual_rounds():
    messages = exchanges(30)
    last = datetime.fromisoformat(messages[-1]['created_at'])
    # Five early rounds are old enough, but the session has never paused.
    assert flushable_dialogue_units(messages, now=last + timedelta(minutes=1)) == []
    assert flushable_dialogue_units(messages, now=last + timedelta(minutes=20, seconds=-1)) == []
    assert len(flushable_dialogue_units(messages, now=last + timedelta(minutes=20))) == 30
    # A later active tail does not prevent routing an earlier, genuinely paused segment.
    tail = exchanges(2, start=last + timedelta(minutes=20), first_id=61)
    assert len(flushable_dialogue_units(messages + tail, now=datetime.fromisoformat(tail[-1]['created_at']))) == 30


def test_unanswered_tails_do_not_count_and_proactive_reply_completes_round():
    messages = exchanges(4)
    end = datetime.fromisoformat(messages[-1]['created_at'])
    messages += [{'id': 9, 'session_id': 1, 'role': 'user', 'text': 'Pending question',
                  'created_at': (end + timedelta(minutes=1)).isoformat()}]
    assert len(flushable_dialogue_units(messages, now=end + timedelta(hours=1))) == 4
    proactive = {'id': 10, 'session_id': 2, 'role': 'assistant', 'text': 'Synthetic wake',
                 'metadata': {'proactive': True}, 'created_at': end.isoformat()}
    assert flushable_dialogue_units([proactive], now=end + timedelta(hours=1)) == []
    reply = {**proactive, 'id': 11, 'role': 'user', 'metadata': {}, 'text': 'Synthetic reply'}
    assert len(flushable_dialogue_units([proactive, reply], now=end + timedelta(minutes=20))) == 1


def test_unanswered_proactive_message_reopens_silence_without_counting_a_round():
    messages = exchanges(5)
    end = datetime.fromisoformat(messages[-1]['created_at'])
    proactive = {'id': 11, 'session_id': 1, 'role': 'assistant', 'text': 'Synthetic wake',
                 'metadata': {'proactive': True},
                 'created_at': (end + timedelta(minutes=15)).isoformat()}
    # The five completed rounds are old enough by the original clock, but the
    # unanswered proactive message opens a fresh response window.
    assert flushable_dialogue_units(messages + [proactive],
                                    now=end + timedelta(minutes=20)) == []
    ready = flushable_dialogue_units(messages + [proactive],
                                     now=end + timedelta(minutes=35))
    assert len(ready) == 5
    assert all(proactive not in unit for unit in ready)


def test_daytime_gate_accumulates_within_session_and_keeps_originals(settings, monkeypatch):
    current = datetime(2026, 9, 14, 2, tzinfo=timezone.utc)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current.astimezone(tz)
    monkeypatch.setattr(p, 'datetime', Clock)
    save_settings(settings.database, {'models': [{'id': 'local', 'model': 'synthetic',
                       'base_url': 'http://127.0.0.1:9/v1'}], 'assignments': {'track_router': 'local'}})
    requests = []
    async def complete(model, payload):
        with Store(settings.database, read_only=True) as store:
            request = json.loads(store.conn.execute('SELECT request_json FROM pipeline_jobs '
                             'WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        requests.append(request)
        return {'choices': [{'message': {'content': json.dumps(output_for('track_router', request))}}]}
    monkeypatch.setattr('serein.model_runtime.complete', complete)
    archive = raw_archive(settings)
    def add(rows):
        archive.ingest([{k: v for k, v in row.items() if k != 'id'} for row in rows], source='test')
    add(exchanges(4, session='a'))
    add(exchanges(1, session='b', first_id=9))
    asyncio.run(p.flush_routes(settings.database))
    assert requests == []  # Four plus one across two sessions never becomes five.
    fifth = exchanges(1, start=current, session='a', first_id=11)
    add(fifth)
    end = datetime.fromisoformat(fifth[-1]['created_at'])
    current = end + timedelta(minutes=20, seconds=-1)
    asyncio.run(p.flush_routes(settings.database))
    assert requests == []  # Four old paused rounds remain buffered for the fifth.
    current = end + timedelta(minutes=20)
    asyncio.run(p.flush_routes(settings.database))
    assert sum(len(request['messages']) for request in requests) == 10
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT COUNT(*) FROM pipeline_routes').fetchone()[0] == 10
        assert store.conn.execute('SELECT COUNT(*) FROM raw_processing').fetchone()[0] == 0
        assert store.conn.execute("SELECT COUNT(*) FROM documents WHERE kind='event'").fetchone()[0] == 0
        cards = [json.loads(row[0]) for row in store.conn.execute('SELECT card_json FROM pipeline_tracks')]
        assert cards and all(card['recent_source_message_ids'] and card['recent_turns'] for card in cards)
    count = len(requests)
    asyncio.run(p.flush_routes(settings.database))
    assert len(requests) == count  # No duplicate routing after a successful flush.


def test_individual_message_routes_and_bridge_ownership(settings):
    raw_archive(settings).ingest([{'source_event_id': str(i), 'session_id': 'one',
                                  'role': 'user' if i % 2 else 'assistant', 'text': 'Synthetic turn '+str(i),
                                  'created_at': '2025-01-01T00:00:00Z'} for i in range(1, 5)], source='test')
    task = asyncio.run(p.advance(settings.database, include_recent=True))
    assert '组内所有消息必须选择同一个' not in task['request']['prompt']
    output = {'message_assignments': [
        {'source_message_id': 1, 'primary_track_ref': 'new:1', 'context_track_refs': [], 'routing_role': 'origin'},
        {'source_message_id': 2, 'primary_track_ref': 'new:2', 'context_track_refs': [], 'routing_role': 'landing'},
        {'source_message_id': 3, 'primary_track_ref': 'new:1', 'context_track_refs': ['new:2'], 'routing_role': 'bridge'},
        {'source_message_id': 4, 'primary_track_ref': 'new:2', 'context_track_refs': [], 'routing_role': 'primary_activity'}],
        'track_updates': [{'track_ref': ref, 'subject': ref, 'throughline': 'Synthetic continuation', 'status': 'active'}
                          for ref in ('new:1', 'new:2')]}
    p.submit(settings.database, task['job_id'], output)
    curator = asyncio.run(p.advance(settings.database, include_recent=True))
    assert '单一 primary Track' in curator['request']['prompt']
    with Store(settings.database, read_only=True) as store:
        data = json.loads(store.conn.execute(
            'SELECT input_json FROM pipeline_batches WHERE id=?',
            (task['request']['batch_id'],),
        ).fetchone()[0])
    assignments = data['routing_result']['assignments']
    first = assignments[0]['primary_track_id']
    second = assignments[1]['primary_track_id']
    assert first != second
    components = {item['track_ids'][0]: item for item in data['components']}
    assert set(components) == {first, second}

    first_component = components[first]
    second_component = components[second]
    assert [item['id'] for item in first_component['messages']] == [1, 3]
    assert [item['id'] for item in second_component['messages']] == [2, 3, 4]
    assert [item['track_id'] for item in first_component['track_cards']] == [first]
    assert [item['track_id'] for item in second_component['track_cards']] == [second]
    expected_edge = [{'unit_root_message_id': 3, 'track_id': second, 'relation': 'bridge'}]
    assert first_component['context_edges'] == expected_edge
    assert second_component['context_edges'] == expected_edge

    first_plan = {'events': [{'action': 'create', 'primary_track_id': first,
                              'base_event_ids': [], 'owned_unit_roots': [1, 3]}],
                  'skip_unit_roots': [], 'defer_unit_roots': []}
    second_plan = {'events': [{'action': 'create', 'primary_track_id': second,
                               'base_event_ids': [], 'owned_unit_roots': [2, 3, 4]}],
                   'skip_unit_roots': [], 'defer_unit_roots': []}
    normalized_first = latest.normalize_event_curator_output(first_plan, first_component)
    normalized_second = latest.normalize_event_curator_output(second_plan, second_component)
    assert normalized_first['events'][0]['source_message_ids'] == [1, 3]
    assert normalized_second['events'][0]['source_message_ids'] == [2, 3, 4]
    first_roles = {item['source_message_id']: item['activity_role']
                   for item in normalized_first['events'][0]['source_bindings']}
    second_roles = {item['source_message_id']: item['activity_role']
                    for item in normalized_second['events'][0]['source_bindings']}
    assert first_roles[3] == 'primary_activity'
    assert second_roles[3] == 'bridge'


def test_bridge_deferral_on_one_corridor_must_block_global_settlement(settings):
    raw_archive(settings).ingest([
        {'source_event_id': str(i), 'session_id': 'one',
         'role': 'user' if i % 2 else 'assistant', 'text': 'Synthetic turn '+str(i),
         'created_at': '2025-01-01T00:00:00Z'}
        for i in range(1, 5)
    ], source='test')
    task = asyncio.run(p.advance(settings.database, include_recent=True))
    router_output = {
        'message_assignments': [
            {'source_message_id': 1, 'primary_track_ref': 'new:1', 'context_track_refs': [], 'routing_role': 'origin'},
            {'source_message_id': 2, 'primary_track_ref': 'new:2', 'context_track_refs': [], 'routing_role': 'landing'},
            {'source_message_id': 3, 'primary_track_ref': 'new:1', 'context_track_refs': ['new:2'], 'routing_role': 'bridge'},
            {'source_message_id': 4, 'primary_track_ref': 'new:2', 'context_track_refs': [], 'routing_role': 'primary_activity'},
        ],
        'track_updates': [
            {'track_ref': ref, 'subject': ref, 'throughline': 'Synthetic continuation', 'status': 'active'}
            for ref in ('new:1', 'new:2')
        ],
    }
    p.submit(settings.database, task['job_id'], router_output)
    curator = asyncio.run(p.advance(settings.database, include_recent=True))
    with Store(settings.database, read_only=True) as store:
        batch = dict(store.conn.execute(
            'SELECT * FROM pipeline_batches WHERE id=?', (task['request']['batch_id'],)
        ).fetchone())
        data = json.loads(batch['input_json'])
    assignments = data['routing_result']['assignments']
    first = assignments[0]['primary_track_id']
    second = assignments[1]['primary_track_id']
    components = {item['track_ids'][0]: item for item in data['components']}
    first_component = components[first]
    second_component = components[second]

    first_plan = latest.normalize_event_curator_output({
        'events': [{'action': 'create', 'primary_track_id': first,
                    'base_event_ids': [], 'owned_unit_roots': [1, 3]}],
        'skip_unit_roots': [], 'defer_unit_roots': [],
    }, first_component)
    second_plan = latest.normalize_event_curator_output({
        'events': [{'action': 'create', 'primary_track_id': second,
                    'base_event_ids': [], 'owned_unit_roots': [2, 4]}],
        'skip_unit_roots': [], 'defer_unit_roots': [3],
    }, second_component)

    written = {'title': 'Synthetic', 'event_draft': 'Synthetic Event',
               'recallable': True, 'evidence_sufficient': True}
    plans = [
        (first_component, first_plan, [(first_plan['events'][0], dict(written))]),
        (second_component, second_plan, [(second_plan['events'][0], dict(written))]),
    ]
    result = p.settle(settings.database, batch, data, data['routing_result'], plans)
    assert result['deferred'] == 1
    with Store(settings.database, read_only=True) as store:
        outcomes = dict(store.conn.execute(
            'SELECT raw_id,outcome FROM raw_processing ORDER BY raw_id'
        ))
    # Public parity with haven_bridge: a bridge unit deferred by either Track
    # corridor must remain unsettled globally so that the deferred Track can
    # revisit it later.
    assert 3 not in outcomes


def test_track_anchor_continuation_and_parked_unused_state(settings):
    ingest(settings)
    asyncio.run(p.advance(settings.database, include_recent=True, runner=synthetic_runner))
    with Store(settings.database) as store:
        row = store.conn.execute('SELECT * FROM pipeline_tracks').fetchone()
        key, scope = row['id'], row['scope']
        saved = json.loads(row['card_json'])
        assert saved['recent_source_message_ids'] == [2]
        assert saved['origin_session_id'] == saved['last_session_id'] == scope
        unused = card('session_'+scope+'_track_0017', last_session_id=scope, origin_session_id=scope)
        tracks.persist(store.conn, [unused], scope)
    ingest(settings, 2)
    task = asyncio.run(p.advance(settings.database, include_recent=True))
    active = {c['track_id']: c for c in task['request']['active_tracks']}
    assert active[key]['recent_turns'][0]['message_id'] == 2
    assert all(c['status'] == 'parked' for c in active.values())
    asyncio.run(p.advance(settings.database, include_recent=True, runner=synthetic_runner))
    with Store(settings.database, read_only=True) as store:
        saved = {r['id']: json.loads(r['card_json']) for r in store.conn.execute('SELECT * FROM pipeline_tracks')}
    assert saved[key]['status'] == 'active'
    assert saved[key]['recent_source_message_ids'] == [4]
    assert saved[unused['track_id']]['status'] == 'parked'


def test_new_track_ordinal_uses_max_not_count_and_preserves_policy():
    old = [card('session_current_track_0042', event_policy='rolling_engineering'),
           card('session_previous_track_0999')]
    assert tracks.next_ordinal('current', old) == 43
    messages = [{'id': 1, 'role': 'user', 'content': 'Continue', 'session_id': 1}]
    output = {'message_assignments': [{'source_message_id': 1, 'primary_track_ref': old[0]['track_id'],
                                       'context_track_refs': [], 'routing_role': 'origin'}],
              'track_updates': [{'track_ref': old[0]['track_id'], 'subject': 'Updated', 'throughline': 'Same work',
                                 'event_policy': 'default', 'status': 'active'}]}
    assigned, updates, _ = normalize_event_track_message_output(output, messages, old, session_id='current', next_track_ordinal=43)
    assert updates[0]['event_policy'] == 'rolling_engineering'
    output['message_assignments'][0]['primary_track_ref'] = 'new:1'
    output['track_updates'][0]['track_ref'] = 'new:1'
    assigned, updates, ordinal = normalize_event_track_message_output(output, messages, old, session_id='current', next_track_ordinal=43)
    assert assigned[0]['primary_track_id'] == 'session_current_track_0043' and ordinal == 44


def test_previous_visible_window_boundaries_and_old_anchor_rehydration(settings):
    archive = raw_archive(settings)
    def add(session, number, workspace='one', metadata=None):
        archive.ingest([{'source_event_id': str(number), 'session_id': session, 'role': 'user',
                        'text': 'Synthetic '+str(number), 'created_at': '2025-01-01T00:00:00Z',
                        'metadata': {'runtime': 'synthetic', 'workspace_root': workspace, **(metadata or {})}}], source='test')
    add('a', 1); add('b', 2); add('a', 3); add('foreign', 4, workspace='two'); add('c', 5)
    p.initialize(settings.database)
    scopes = {s: tracks.scope_for('test', s) for s in ('a', 'b', 'foreign', 'c')}
    with Store(settings.database) as store:
        for session, scope in scopes.items():
            # Emulate rc65 cards without original-message anchors.
            tracks.persist(store.conn, [card('session_'+scope+'_track_0001')], scope)
        b_key = 'session_'+scopes['b']+'_track_0001'
        store.conn.execute('INSERT INTO pipeline_routes VALUES (?,?)', (2, encode({
            'source_message_id': 2, 'primary_track_id': b_key, 'context_track_ids': [], 'routing_role': 'origin'})))
        # A high ordinal whose last window moved must still reserve its ID.
        tracks.persist(store.conn, [card('session_'+scopes['c']+'_track_0042', last_session_id='elsewhere')], scopes['c'])
        cards, ordinal = tracks.load_tracks(store, 'test', 'c', 5, p.message)
    assert {c['track_id'] for c in cards} == {b_key, 'session_'+scopes['c']+'_track_0001'}
    assert next(c for c in cards if c['track_id'] == b_key)['recent_turns'][0]['message_id'] == 2
    assert ordinal == 43


def test_context_only_track_keeps_anchor_and_last_real_window():
    old = [card('old', origin_session_id='first', last_session_id='previous'),
           card('unused', origin_session_id='first', last_session_id='previous')]
    assignments = [{'source_message_id': 1, 'primary_track_id': 'new', 'context_track_ids': ['old'], 'routing_role': 'bridge'}]
    updates = [card('new'), card('old')]
    result = tracks.update_cards(old, assignments, updates, [{'id': 1, 'content': 'Bridge', 'role': 'user'}], 'current')
    by_id = {c['track_id']: c for c in result}
    assert by_id['old']['origin_session_id'] == 'first'
    assert by_id['old']['last_session_id'] == 'current'
    assert by_id['old']['recent_source_message_ids'] == [1]
    assert by_id['unused']['status'] == 'parked' and by_id['unused']['last_session_id'] == 'previous'


def test_old_pending_contract_is_retired_without_processing_raw_data(settings):
    ingest(settings)
    p.initialize(settings.database)
    batch = p.new_batch(settings.database, True)
    with Store(settings.database) as store:
        data = json.loads(batch['input_json']);data['contract'] = 'public-event-scene-context-v1'
        store.conn.execute('UPDATE pipeline_batches SET input_json=? WHERE id=?', (encode(data), batch['id']))
    p.initialize(settings.database)
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT status FROM pipeline_batches WHERE id=?', (batch['id'],)).fetchone()[0] == 'superseded_protocol'
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 0
