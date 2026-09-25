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
    assert len(data['components']) == 2
    components = {item['track_ids'][0]: item for item in p.components(
        settings.database, {**data, 'joint_review': False}, data['routing_result'])}
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

    review={'events':[{'event_index':0,'reason':'Synthetic owned activity'}],
            'boundaries':[],'dispositions':[]}
    first_plan = {'events': [{'action': 'create', 'primary_track_id': first,
                              'base_event_ids': [], 'owned_unit_roots': [1, 3]}],
                  'skip_unit_roots': [], 'defer_unit_roots': [],'decision_review':review}
    second_plan = {'events': [{'action': 'create', 'primary_track_id': second,
                               'base_event_ids': [], 'owned_unit_roots': [2, 3, 4]}],
                   'skip_unit_roots': [], 'defer_unit_roots': [],'decision_review':review}
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


def _two_track_bridge_batch(settings):
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
    asyncio.run(p.advance(settings.database, include_recent=True))
    with Store(settings.database, read_only=True) as store:
        batch = dict(store.conn.execute(
            'SELECT * FROM pipeline_batches WHERE id=?', (task['request']['batch_id'],)
        ).fetchone())
        data = json.loads(batch['input_json'])
    assignments = data['routing_result']['assignments']
    first = assignments[0]['primary_track_id']
    second = assignments[1]['primary_track_id']
    components = {item['track_ids'][0]: item for item in p.components(
        settings.database, {**data, 'joint_review': False}, data['routing_result'])}
    return batch, data, first, second, components[first], components[second]


def test_joint_curator_requires_declared_bridge_evidence(settings):
    save_settings(settings.database, {'pipeline': {'joint_review_enabled': True}})
    batch, data, first, second, _, _ = _two_track_bridge_batch(settings)
    component = data['components'][0]
    assert set(component['track_ids']) == {first, second}
    proposal = {'events': [{'action': 'create', 'primary_track_id': first,
                            'base_event_ids': [], 'owned_unit_roots': [1, 2, 3, 4]}],
                'skip_unit_roots': [], 'defer_unit_roots': [],
                'decision_review': {'events': [{'event_index': 0, 'reason': 'One continued map check'}],
                                    'boundaries': [], 'dispositions': [],
                                    'continuations': [{'event_index': 0, 'left_track_id': first,
                                                       'right_track_id': second, 'bridge_unit_root': 3,
                                                       'reason': 'The second check continues the first',
                                                       'evidence': [
                                                           {'source_message_id': 3, 'quote': 'Synthetic turn 3'},
                                                           {'source_message_id': 2, 'quote': 'Synthetic turn 2'}]}]}}
    plan = latest.normalize_event_curator_output(proposal, component)
    assert len(plan['events']) == 1
    assert set(plan['events'][0]['source_message_ids']) == {1, 2, 3, 4}
    proposal['decision_review']['continuations'][0]['evidence'][1]['quote'] = 'missing quote'
    with pytest.raises(ValueError, match='verbatim'):
        latest.normalize_event_curator_output(proposal, component)


def test_bridge_deferral_on_one_corridor_blocks_global_source_settlement(settings):
    batch, data, first, second, first_component, second_component = _two_track_bridge_batch(settings)
    first_plan = latest.normalize_event_curator_output({
        'events': [{'action': 'create', 'primary_track_id': first,
                    'base_event_ids': [], 'owned_unit_roots': [1, 3]}],
        'skip_unit_roots': [], 'defer_unit_roots': [],
        'decision_review': {'events':[{'event_index':0,'reason':'Synthetic owned activity'}],
                            'boundaries':[],'dispositions':[]},
    }, first_component)

    second_component['base_event_candidates'] = [{
        'event_id': 'protected-base',
        'primary_track_id': second,
        'session_ids': [second_component['messages'][0]['session_id']],
        'source_message_ids': [2],
        'predecessor_event_ids': [],
        'active': True,
        'protected': True,
    }]
    second_plan = latest.normalize_event_curator_output({
        'events': [{'action': 'extend', 'primary_track_id': second,
                    'base_event_ids': ['protected-base'], 'owned_unit_roots': [3]}],
        'skip_unit_roots': [4], 'defer_unit_roots': [],
        'decision_review': {'events':[{'event_index':0,'reason':'Synthetic protected continuation'}],
                            'boundaries':[],'dispositions':[{'disposition':'skip','unit_roots':[4],
                                'reason':'Synthetic unrelated unit','parked_source_message_ids':[]}]},
    }, second_component)
    assert second_plan['events'] == []
    assert set(second_plan['defer_source_message_ids']) == {2, 3}

    written = {'title': 'Synthetic', 'event_draft': 'Synthetic Event',
               'recallable': True, 'evidence_sufficient': True}
    result = p.settle(settings.database, batch, data, data['routing_result'], [
        (first_component, first_plan, [(first_plan['events'][0], dict(written))]),
        (second_component, second_plan, []),
    ])
    assert result['deferred'] == 2
    with Store(settings.database, read_only=True) as store:
        outcomes = dict(store.conn.execute(
            'SELECT raw_id,outcome FROM raw_processing ORDER BY raw_id'
        ))
        assert store.conn.execute("SELECT count(*) FROM documents WHERE kind='event'").fetchone()[0] == 1
    assert 3 not in outcomes
    assert outcomes == {1: 'settled', 4: 'skipped'}


def test_protected_append_requires_explicit_opt_in_and_new_stable_source(settings):
    _, _, _, second, _, component = _two_track_bridge_batch(settings)
    session = component['messages'][0]['session_id']
    component['context_messages'].append({**component['messages'][0], 'id': 10,
                                          'content': 'The notebook cover was loose.'})
    component['base_event_candidates'] = [{
        'event_id': 'earlier-notebook', 'primary_track_id': second,
        'session_ids': [session], 'source_message_ids': [10],
        'predecessor_event_ids': [], 'active': True, 'protected': True,
        'continuation_allowed': True,
    }]
    proposal = {'events': [{'action': 'extend', 'primary_track_id': second,
                            'base_event_ids': ['earlier-notebook'], 'owned_unit_roots': [4]}],
                'skip_unit_roots': [2, 3], 'defer_unit_roots': [],
                'decision_review': {'events': [{'event_index': 0, 'reason': 'New stitch follows repair'}],
                                    'boundaries': [], 'dispositions': [{
                                        'disposition': 'skip', 'unit_roots': [2, 3],
                                        'reason': 'Unrelated synthetic turns',
                                        'parked_source_message_ids': []}]}}
    without_opt_in = latest.normalize_event_curator_output(proposal, component)
    assert without_opt_in['events'] == []
    assert 4 in without_opt_in['defer_source_message_ids']
    component['append_protected'] = True
    with_opt_in = latest.normalize_event_curator_output(proposal, component)
    assert len(with_opt_in['events']) == 1
    assert with_opt_in['events'][0]['append_only'] is True
    assert set(with_opt_in['events'][0]['source_message_ids']) == {4, 10}


def test_bridge_settlement_beats_other_corridor_skip(settings):
    batch, data, first, second, first_component, second_component = _two_track_bridge_batch(settings)
    first_plan = latest.normalize_event_curator_output({
        'events': [{'action': 'create', 'primary_track_id': first,
                    'base_event_ids': [], 'owned_unit_roots': [1, 3]}],
        'skip_unit_roots': [], 'defer_unit_roots': [],
        'decision_review': {'events':[{'event_index':0,'reason':'Synthetic owned activity'}],
                            'boundaries':[],'dispositions':[]},
    }, first_component)
    second_plan = latest.normalize_event_curator_output({
        'events': [{'action': 'create', 'primary_track_id': second,
                    'base_event_ids': [], 'owned_unit_roots': [2, 4]}],
        'skip_unit_roots': [3], 'defer_unit_roots': [],
        'decision_review': {'events':[{'event_index':0,'reason':'Synthetic owned activity'}],
                            'boundaries':[],'dispositions':[{'disposition':'skip','unit_roots':[3],
                                'reason':'Synthetic bridge skipped here','parked_source_message_ids':[]}]},
    }, second_component)
    written = {'title': 'Synthetic', 'event_draft': 'Synthetic Event',
               'recallable': True, 'evidence_sufficient': True}
    result = p.settle(settings.database, batch, data, data['routing_result'], [
        (first_component, first_plan, [(first_plan['events'][0], dict(written))]),
        (second_component, second_plan, [(second_plan['events'][0], dict(written))]),
    ])
    assert result['deferred'] == 0
    with Store(settings.database, read_only=True) as store:
        outcomes = dict(store.conn.execute(
            'SELECT raw_id,outcome FROM raw_processing ORDER BY raw_id'
        ))
    assert outcomes[3] == 'settled'


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
    # A card without any durable routed activity is no longer a visible candidate.
    assert saved[unused['track_id']]['status'] == 'active'


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


def test_recent_route_boundaries_and_old_anchor_rehydration(settings):
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
        assert {c['track_id'] for c in cards} == {b_key}
    assert next(c for c in cards if c['track_id'] == b_key)['recent_turns'][0]['message_id'] == 2
    assert ordinal == 43


def test_track_lookback_crosses_sessions_without_window_metadata(settings):
    archive=raw_archive(settings)
    for session,stamp in [('old','2026-09-19T11:59:59Z'),
                          ('first','2026-09-20T12:00:00Z'),
                          ('middle','2026-09-21T12:00:00Z'),
                          ('current','2026-09-23T12:00:00Z')]:
        archive.ingest([{'source_event_id':session,'session_id':session,'role':'user',
                         'text':'Synthetic '+session,'created_at':stamp}],source='test')
    p.initialize(settings.database)
    with Store(settings.database) as store:
        for raw in store.conn.execute('SELECT id,session_id FROM raw_events WHERE id<4'):
            key='session_'+tracks.scope_for('test',raw['session_id'])+'_track_0001'
            tracks.persist(store.conn,[card(key)],tracks.scope_for('test',raw['session_id']))
            store.conn.execute('INSERT INTO pipeline_routes VALUES (?,?)',(raw['id'],encode({
                'source_message_id':raw['id'],'primary_track_id':key,'context_track_ids':[],
                'routing_role':'origin'})))
        three_days,_=tracks.load_tracks(store,'test','current',4,p.message)
        one_day,_=tracks.load_tracks(store,'test','current',4,p.message,lookback_days=1)
        seven_days,_=tracks.load_tracks(store,'test','current',4,p.message,lookback_days=7)
    assert {c['recent_turns'][0]['text'] for c in three_days}=={
        'Synthetic first','Synthetic middle'}
    assert one_day==[]
    assert {c['recent_turns'][0]['text'] for c in seven_days}=={
        'Synthetic old','Synthetic first','Synthetic middle'}


def test_track_lookback_keeps_runtime_boundary_and_latest_activity(settings):
    archive=raw_archive(settings)
    for session,stamp,workspace in [
        ('first','2026-09-19T00:00:00Z','one'),
        ('foreign','2026-09-22T12:00:00Z','two'),
        ('first','2026-09-23T11:00:00Z','one'),
        ('current','2026-09-23T12:00:00Z','one')]:
        archive.ingest([{'source_event_id':session+stamp,'session_id':session,'role':'user',
                         'text':'Synthetic '+session+stamp,'created_at':stamp,
                         'metadata':{'runtime':'synthetic','workspace_root':workspace}}],source='test')
    p.initialize(settings.database)
    with Store(settings.database) as store:
        first='session_'+tracks.scope_for('test','first')+'_track_0001'
        foreign='session_'+tracks.scope_for('test','foreign')+'_track_0001'
        tracks.persist(store.conn,[card(first)],tracks.scope_for('test','first'))
        tracks.persist(store.conn,[card(foreign)],tracks.scope_for('test','foreign'))
        for raw_id,key in [(1,first),(2,foreign),(3,first)]:
            store.conn.execute('INSERT INTO pipeline_routes VALUES (?,?)',(raw_id,encode({
                'source_message_id':raw_id,'primary_track_id':key,'context_track_ids':[],
                'routing_role':'origin'})))
        cards,_=tracks.load_tracks(store,'test','current',4,p.message)
    assert [c['track_id'] for c in cards]==[first]
    assert cards[0]['recent_source_message_ids']==[3]


@pytest.mark.parametrize('days,visible',[(1,False),(3,True)])
def test_pipeline_setting_controls_router_track_visibility(settings,days,visible):
    save_settings(settings.database,{'pipeline':{'track_lookback_days':days}})
    raw_archive(settings).ingest([
        {'source_event_id':'old-user','session_id':'old','role':'user',
         'text':'Earlier synthetic request','created_at':'2026-09-21T12:00:00Z'},
        {'source_event_id':'old-answer','session_id':'old','role':'assistant',
         'text':'Earlier synthetic answer','created_at':'2026-09-21T12:00:01Z'},
        {'source_event_id':'new-user','session_id':'new','role':'user',
         'text':'New synthetic request','created_at':'2026-09-23T12:00:00Z'},
        {'source_event_id':'new-answer','session_id':'new','role':'assistant',
         'text':'New synthetic answer','created_at':'2026-09-23T12:00:01Z'},
    ],source='test')
    p.initialize(settings.database)
    old_scope=tracks.scope_for('test','old')
    key='session_'+old_scope+'_track_0001'
    with Store(settings.database) as store:
        tracks.persist(store.conn,[card(key)],old_scope)
        store.conn.execute('INSERT INTO pipeline_routes VALUES (?,?)',(2,encode({
            'source_message_id':2,'primary_track_id':key,'context_track_ids':[],
            'routing_role':'primary_activity'})))
        for raw_id in (1,2):
            store.conn.execute('INSERT INTO raw_processing VALUES (?,?,?)',(raw_id,'synthetic-earlier','settled'))
    batch=p.new_batch(settings.database,True,datetime(2026,9,23,13,tzinfo=timezone.utc))
    assert batch is not None
    data=json.loads(batch['input_json'])
    assert (key in {item['track_id'] for item in data['tracks']}) is visible


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


@pytest.mark.parametrize('old_contract', ['public-event-scene-context-v1',
                                          'public-event-message-tracks-v7'])
def test_old_pending_contract_is_retired_without_processing_raw_data(settings, old_contract):
    ingest(settings)
    p.initialize(settings.database)
    batch = p.new_batch(settings.database, True)
    with Store(settings.database) as store:
        data = json.loads(batch['input_json']);data['contract'] = old_contract
        store.conn.execute('UPDATE pipeline_batches SET input_json=? WHERE id=?', (encode(data), batch['id']))
    p.initialize(settings.database)
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT status FROM pipeline_batches WHERE id=?', (batch['id'],)).fetchone()[0] == 'superseded_protocol'
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 0
