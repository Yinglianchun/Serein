"""Restore routes from their frozen producer, never from a later window's card.

No model calls are made here. Rebuilding an unverifiable plan is a separate,
explicitly confirmed operation; originals, old jobs and published Events survive.
"""
from copy import deepcopy
import json
import re
from uuid import uuid4

from ..core.store import Store, Conflict, digest, encode, now
from . import pipeline_tracks as tracks

MAX_HISTORY_BATCHES = 64
MAX_HISTORY_JOBS = 256
SOURCE_STATES = ('routed', 'done', 'superseded_input_budget')
MESSAGE_KEYS = ('id', 'source', 'source_event_id', 'original_session_id',
                'session_id', 'role', 'content', 'created_at')


def initialize(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS pipeline_route_provenance(
            raw_id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, route_json TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS pipeline_route_provenance_batch ON pipeline_route_provenance(batch_id);
        CREATE INDEX IF NOT EXISTS pipeline_batches_scope_status ON pipeline_batches(scope,status);
    ''')


def record_routes(conn, batch_id, assignments):
    """Call inside the SAME transaction that saves the producer's snapshot."""
    for assignment in assignments:
        raw_id = assignment['source_message_id']
        value = encode(assignment)
        conn.execute('INSERT OR REPLACE INTO pipeline_routes VALUES (?,?)', (raw_id, value))
        conn.execute('INSERT OR REPLACE INTO pipeline_route_provenance VALUES (?,?,?)',
                     (raw_id, batch_id, value))


def _message_key(message):
    return tuple(message.get(key) for key in MESSAGE_KEYS)


def _same_scope(data, messages):
    """Scope is a source/window pair, not the current location of a Track card."""
    return bool(messages) and all(isinstance(m, dict) and
        tracks.scope_for(m.get('source'), m.get('original_session_id')) == data['scope']
        for m in messages)


def _frames(database, batch):
    """Replay accepted jobs against THEIR inputs/ordinals, without invoking job()."""
    from . import pipeline as p
    original = json.loads(batch['input_json'])
    if original.get('contract') != p.CONTRACT or original.get('scope') != batch['scope']:
        raise p.RoutingRecoveryError('route producer has an incompatible frozen contract')
    if not _same_scope(original, original.get('routing_messages', [])):
        raise p.RoutingRecoveryError('route producer messages do not belong to its frozen scope')
    jobs = p.router_jobs(database, batch)
    if len(jobs) > MAX_HISTORY_JOBS:
        raise p.RoutingRecoveryError('route history exceeds bounded job recovery limit')
    if not jobs:
        result = original.get('routing_result')
        if result is None:
            return []
        p.validate_routing_result(original, result)
        upper = max(m['id'] for m in original['routing_messages'])
        for card in result['tracks']:
            anchors = card.get('recent_source_message_ids', [])
            if not isinstance(anchors, list) or any(type(key) is not int or key > upper for key in anchors):
                raise p.RoutingRecoveryError('producer snapshot contains invalid or future Track anchors')
        return [{'batch_id': batch['id'], 'job_id': None, 'messages': original['routing_messages'],
                 'assignments': result['assignments'], 'tracks': result['tracks'],
                 'next_track_ordinal': result['next_track_ordinal']}]
    frames = []
    cursor = 0
    for row in jobs:
        request = json.loads(row['request_json'])
        if request.get('batch_id') != batch['id']:
            raise p.RoutingRecoveryError('Router job belongs to another producer batch')
        messages = p._router_prefix(original, request, cursor)
        cursor += len(messages)
        if row['output_json'] is None:
            break  # An unfinished suffix is never rerun by recovery.
        output = json.loads(row['output_json'])
        p.validate(request, output)
        assignments, updates, ordinal = p.normalize_event_track_message_output(
            output, messages, request['active_tracks'], session_id=original['scope'],
            next_track_ordinal=request['next_track_ordinal'])
        with p.latest.identity_scope(request['identity']):
            cards = tracks.update_cards(request['active_tracks'], assignments, updates,
                                        messages, original['scope'])
        used = {key for a in assignments for key in [a['primary_track_id'], *a['context_track_ids']]}
        frames.append({'batch_id': batch['id'], 'job_id': row['id'], 'messages': messages,
                       'assignments': assignments, 'tracks': [c for c in cards if c['track_id'] in used],
                       'next_track_ordinal': ordinal})
    return frames


def recover_cached_routes(database, data, assignments):
    """Return a fully proved historical result, or None for legacy cache fallback.

    A producer may span several input chunks. Use its accepted per-job frames,
    not its end-of-day card when that card has seen messages after this batch.
    Legacy producers are searched only within this scope and the exact raw IDs.
    Ambiguous or invalid explicit provenance fails closed.
    """
    from . import pipeline as p
    expected = {m['id']: m for m in data['routing_messages']}
    wanted = {a['source_message_id']: a for a in assignments}
    upper = max(expected)
    if not _same_scope(data, list(expected.values())):
        raise p.RoutingRecoveryError('frozen messages do not belong to their declared scope')
    linked = {}
    candidates = {}
    with Store(database, read_only=True) as store:
        ids = list(expected)
        for offset in range(0, len(ids), 400):
            chunk = ids[offset:offset+400]
            marks = ','.join('?' for _ in chunk)
            for row in store.conn.execute(
                    'SELECT * FROM pipeline_route_provenance WHERE raw_id IN ('+marks+')', chunk):
                if row['route_json'] != encode(wanted[row['raw_id']]):
                    raise p.RoutingRecoveryError('cached route disagrees with its recorded producer')
                linked[row['raw_id']] = row['batch_id']
            # Old versions had no provenance table. Discover bounded overlapping
            # producer batches, then prove each result using its saved request.
            unlinked = [key for key in chunk if key not in linked]
            if not unlinked:
                continue
            marks = ','.join('?' for _ in unlinked)
            rows = store.conn.execute(
                "SELECT b.* FROM pipeline_batches b WHERE b.scope=? "
                "AND b.status IN ('routed','done','superseded_input_budget') "
                "AND json_valid(b.input_json) AND EXISTS (SELECT 1 "
                "FROM json_each(b.input_json,'$.routing_messages') m WHERE m.type='object' "
                "AND json_extract(m.value,'$.id') IN ("+marks+')) ORDER BY b.rowid DESC LIMIT ?',
                (data['scope'], *unlinked, MAX_HISTORY_BATCHES+1)).fetchall()
            candidates.update((row['id'], dict(row)) for row in rows)
        for key in set(linked.values()):
            row = store.conn.execute('SELECT * FROM pipeline_batches WHERE id=?', (key,)).fetchone()
            if row is None or row['scope'] != data['scope'] or row['status'] not in SOURCE_STATES:
                raise p.RoutingRecoveryError('cached route producer is missing or outside frozen scope')
            candidates[key] = dict(row)
    if len(candidates) > MAX_HISTORY_BATCHES:
        raise p.RoutingRecoveryError('route history exceeds bounded producer recovery limit')
    possibilities = {key: {} for key in expected}
    for batch in candidates.values():
        try:
            frames = _frames(database, batch)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
            if batch['id'] in linked.values():
                raise p.RoutingRecoveryError('cannot verify recorded route producer: '+str(error)) from error
            continue  # Unlinked malformed history is not evidence.
        for frame in frames:
            # Never trim future anchors off a card whose prose was generated
            # using future material. Such a frame is not usable for this batch.
            if max(m['id'] for m in frame['messages']) > upper:
                continue
            source_messages = {m['id']: m for m in frame['messages']}
            overlap = expected.keys() & source_messages.keys()
            actual = {a['source_message_id']: a for a in frame['assignments']}
            if ([key for key in expected if key in overlap] !=
                    [key for key in source_messages if key in overlap]):
                continue
            if not overlap or any(_message_key(expected[key]) != _message_key(source_messages[key])
                                  or actual.get(key) != wanted[key] for key in overlap):
                continue
            # The identity of both endpoints of a bridge is part of actual.
            for key in overlap:
                if key in linked and linked[key] != batch['id']:
                    continue
                signature = encode({'messages': [_message_key(m) for m in frame['messages']],
                                    'assignments': frame['assignments'], 'tracks': frame['tracks'],
                                    'next_track_ordinal': frame['next_track_ordinal']})
                possibilities[key][signature] = frame
    if any(len(values) > 1 for values in possibilities.values()):
        raise p.RoutingRecoveryError('ambiguous historical routes; explicit plan rebuild required')
    if any(not values for values in possibilities.values()):
        if linked:
            raise p.RoutingRecoveryError('recorded route history cannot prove the frozen input range')
        return None
    selected = {}
    for values in possibilities.values():
        frame = next(iter(values.values()))
        selected[(frame['batch_id'], frame['job_id'])] = frame
    cards = {}
    for frame in sorted(selected.values(), key=lambda f: max(m['id'] for m in f['messages'])):
        cards.update((c['track_id'], c) for c in frame['tracks'])
    used = p._assignment_tracks(data, assignments)
    result = {'_public_normalized': True, 'assignments': assignments,
              'tracks': [cards[key] for key in used], 'track_state_updates': [cards[key] for key in used],
              'next_track_ordinal': max(data.get('next_track_ordinal', 1),
                                       *(f['next_track_ordinal'] for f in selected.values())),
              'recovered_route_sources': [{'batch_id': f['batch_id'], 'job_id': f['job_id'],
                                          'source_message_ids': [m['id'] for m in f['messages']]}
                                         for f in selected.values()]}
    p.validate_routing_result(data, result)
    return result


def assert_downstream_snapshot(database, batch, data):
    """A saved job cannot silently attach to freshly constructed ownership."""
    from . import pipeline as p
    with Store(database, read_only=True) as store:
        downstream = store.conn.execute(
            "SELECT role,request_json FROM pipeline_jobs WHERE batch_id=? AND "
            "(role LIKE 'event_curator:%' OR role LIKE 'event_writer:%')", (batch['id'],)).fetchall()
    if downstream and 'components' not in data:
        raise p.RoutingRecoveryError('downstream jobs have no frozen component snapshot; rebuild explicitly')
    for row in downstream:
        try:
            match = re.fullmatch(r'(?:event_curator|event_writer):([0-9]+)(?::[0-9]+)?(?::context)?', row['role'])
            if match is None:
                raise ValueError('unrecognized downstream role')
            component = data['components'][int(match[1])]
            frozen = json.loads(row['request_json'])['component']
            # Bounded context reads may add reading-only memberships. They may
            # not change any original membership or frozen predecessor choice.
            roots = {u['unit_root_message_id'] for u in component['memberships']}
            bounded = {**frozen, 'memberships': [u for u in frozen['memberships']
                                                if u['unit_root_message_id'] in roots]}
            if (p._component_signature([bounded]) != p._component_signature([component])
                    or [_message_key(m) for m in frozen['messages']] != [_message_key(m) for m in component['messages']]
                    or frozen.get('base_event_candidates', []) != component.get('base_event_candidates', [])
                    or frozen.get('context_session_ids', []) != component.get('context_session_ids', [])):
                raise ValueError('frozen ownership, bridge endpoints or predecessors differ')
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
            raise p.RoutingRecoveryError('downstream job disagrees with frozen component: '+str(error)) from error


async def rebuild(database, batch_id, confirm):
    from ..work_tasks import execute
    if not isinstance(batch_id, str) or not batch_id or confirm != 'REBUILD_PIPELINE_BATCH':
        raise ValueError('请明确确认作废此批计划并重新归线；原话与旧结果仍保留')
    async def operation():
        return _rebuild(database, batch_id)
    return await execute(database, 'pipeline', operation)


def _rebuild(database, batch_id):
    from . import pipeline as p
    p.initialize(database)
    with Store(database) as store, store.transaction(immediate=True):
        batch = store.conn.execute('SELECT rowid AS queue_order,* FROM pipeline_batches WHERE id=?',
                                   (batch_id,)).fetchone()
        if batch is None or batch['status'] != 'needs_repair':
            raise Conflict('此批已不处于待修复状态，请刷新后确认')
        data = json.loads(batch['input_json'])
        if data.get('contract') != p.CONTRACT:
            raise Conflict('旧批次契约不兼容，不能自动重建')
        stable = [m['id'] for m in data['messages']]
        routing = data['routing_messages']
        if not stable or not _same_scope(data, routing):
            raise Conflict('冻结原话范围无法验证，不能自动重建')
        if store.conn.execute('SELECT 1 FROM raw_processing WHERE operation_id=? LIMIT 1', (batch_id,)).fetchone():
            raise Conflict('此批已有结算记录，不能作废重建')
        if store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='fact_event_settlement_operations'").fetchone():
            if store.conn.execute('SELECT 1 FROM fact_event_settlement_operations WHERE operation_id=?', (batch_id,)).fetchone():
                raise Conflict('此批已有 Event 结算收据，不能作废重建')
        cache = []
        reserved = [{'track_id': c['track_id']} for c in data.get('tracks', [])
                    if isinstance(c, dict) and isinstance(c.get('track_id'), str)]
        reserved.extend({'track_id': row[0]} for row in store.conn.execute('SELECT id FROM pipeline_tracks UNION SELECT track_id FROM pipeline_track_events'))
        for frozen in routing:
            row = store.conn.execute('SELECT * FROM raw_events WHERE id=?', (frozen['id'],)).fetchone()
            if row is None or _message_key(p.message(row)) != _message_key(frozen):
                raise Conflict('原话已变化，不能静默重建冻结计划')
            processed = store.conn.execute('SELECT 1 FROM raw_processing WHERE raw_id=?', (frozen['id'],)).fetchone()
            if processed and frozen['id'] in stable:
                raise Conflict('本批原话已由其他操作处理，不能重复重建')
            if processed:
                continue
            row = store.conn.execute('SELECT * FROM pipeline_routes WHERE raw_id=?', (frozen['id'],)).fetchone()
            provenance = store.conn.execute('SELECT * FROM pipeline_route_provenance WHERE raw_id=?', (frozen['id'],)).fetchone()
            if row or provenance:
                cache.append({'raw_id': frozen['id'], 'route_json': row['route_json'] if row else None,
                              'provenance': dict(provenance) if provenance else None})
                try:
                    route = json.loads(row['route_json'] if row else provenance['route_json'])
                    reserved.extend({'track_id': value} for value in
                                    [route['primary_track_id'], *route['context_track_ids']]
                                    if isinstance(value, str))
                except (KeyError, TypeError, ValueError):
                    pass
            store.conn.execute('DELETE FROM pipeline_routes WHERE raw_id=?', (frozen['id'],))
            store.conn.execute('DELETE FROM pipeline_route_provenance WHERE raw_id=?', (frozen['id'],))
        # Retain only the original pre-routing input, never old ownership/plans.
        fresh = {key: deepcopy(data[key]) for key in ('contract', 'input_policy', 'messages', 'parked',
                 'routing_messages', 'tracks', 'scope', 'source', 'recent', 'day') if key in data}
        fresh['tracks'] = [c for c in fresh['tracks'] if p._card_is_complete(c)]
        fresh['next_track_ordinal'] = max(data.get('next_track_ordinal', 1),
                                         tracks.next_ordinal(data['scope'], reserved))
        fresh['ignore_route_cache'] = True
        fresh['rebuild_of'] = batch_id
        fresh['queue_order'] = data.get('queue_order', batch['queue_order'])
        replacement = 'pipeline:'+digest(encode([batch_id, uuid4().hex]))
        audit = {'status': 'superseded_repair', 'replacement_batch_id': replacement,
                 'confirmed_at': now(), 'previous_result': json.loads(batch['result_json'] or '{}'),
                 'discarded_route_cache': cache}
        store.conn.execute("UPDATE pipeline_batches SET status='superseded_repair',result_json=? WHERE id=?",
                           (encode(audit), batch_id))
        store.conn.execute('INSERT INTO pipeline_batches(id,scope,input_json) VALUES (?,?,?)',
                           (replacement, data['scope'], encode(fresh)))
        return {'status': 'rebuilt', 'batch_id': replacement, 'superseded_batch_id': batch_id,
                'note': '旧计划与模型结果已保留，尚未处理的原话将重新归线；已保存的 Event 不变。'}
