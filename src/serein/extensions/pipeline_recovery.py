"""Restore routes from their frozen producer, never from a later window's card.

No model calls are made here. Rebuilding an unverifiable plan is a separate,
explicitly confirmed operation; originals, old jobs and published Events survive.
"""
from copy import deepcopy
import json
import re
from uuid import uuid4

from ..core.store import Store, Conflict, digest, encode, now
from ..deployment import read_from_store
from . import pipeline_tracks as tracks

MAX_HISTORY_BATCHES = 64
MAX_HISTORY_JOBS = 256
SOURCE_STATES = ('routed', 'done', 'superseded_input_budget', 'waiting_materials')
MESSAGE_KEYS = ('id', 'source', 'source_event_id', 'original_session_id',
                'session_id', 'role', 'content', 'created_at')
# rc65 before the replay-seed fix, with Windows CRLF or release LF files.
# Only its completed, compacted producers qualify; unfinished work stays held.
COMPACTED_ROUTER_REVISIONS = frozenset({
    '4fc0c763905e28e35af100115e6b265817662d38d896e0eec0cb3b20f3f3f679',
    '0a8a6382fe8f0e6e02e2f165c69c179cce0925669c59d30677f5a8e673099620',
})


def initialize(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS pipeline_route_provenance(
            raw_id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, route_json TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS pipeline_route_provenance_batch ON pipeline_route_provenance(batch_id);
        CREATE INDEX IF NOT EXISTS pipeline_batches_scope_status ON pipeline_batches(scope,status);
    ''')


def record_routes(conn, batch_id, assignments, *, preserve_existing=False):
    """Call inside the SAME transaction that saves the producer's snapshot."""
    for assignment in assignments:
        raw_id = assignment['source_message_id']
        value = encode(assignment)
        conn.execute('INSERT OR REPLACE INTO pipeline_routes VALUES (?,?)', (raw_id, value))
        if preserve_existing:
            existing=conn.execute('SELECT route_json FROM pipeline_route_provenance WHERE raw_id=?',(raw_id,)).fetchone()
            if existing and existing[0]==value:continue
        conn.execute('INSERT OR REPLACE INTO pipeline_route_provenance VALUES (?,?,?)',
                     (raw_id, batch_id, value))


def _message_key(message):
    return tuple(message.get(key) for key in MESSAGE_KEYS)


def _same_scope(data, messages):
    """Scope is a source/window pair, not the current location of a Track card."""
    return bool(messages) and all(isinstance(m, dict) and
        tracks.scope_for(m.get('source'), m.get('original_session_id')) == data['scope']
        for m in messages)


def history_reset_cutoff(store, scope, before_id):
    """A confirmed reset survives downstream state changes after accepted routing."""
    from . import pipeline as p
    rows = store.conn.execute("SELECT fresh.input_json,CAST(json_extract(old.result_json,"
        "'$.recovery_downgrade.history_before_id') AS INTEGER) AS cutoff FROM pipeline_batches old "
        "JOIN pipeline_batches fresh ON fresh.id=json_extract(old.result_json,'$.replacement_batch_id') "
        "WHERE old.scope=? AND fresh.scope=old.scope AND old.status='superseded_repair' "
        "AND json_extract(fresh.input_json,'$.routing_result._public_normalized')=1 "
        "AND CAST(json_extract(old.result_json,'$.recovery_downgrade.history_before_id') AS INTEGER)<=? "
        "AND json_extract(old.result_json,'$.recovery_downgrade.confirmed_by')='REBUILD_PIPELINE_BATCH' "
        "ORDER BY cutoff DESC", (scope, before_id))
    for row in rows:
        data = json.loads(row['input_json'])
        try:
            if data.get('scope') != scope or not _same_scope(data, data.get('routing_messages', [])):
                continue
            p.validate_routing_result(data, data['routing_result'])
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        # Skip/rebatch inputs deliberately omit this accepted snapshot. Merely
        # finishing an unrouted batch therefore cannot publish a history reset.
        return row['cutoff']
    return 0


def _frames(database, batch):
    """Replay accepted jobs against THEIR inputs/ordinals, without invoking job()."""
    from . import pipeline as p
    original = json.loads(batch['input_json'])
    legacy_compacted = (batch['status'] == 'done' and original.get('task_snapshot_compacted') is True
        and all(key not in original for key in ('tracks', 'recent', 'next_track_ordinal', 'input_policy')))
    jobs = p.router_jobs(database, batch)
    if len(jobs) > MAX_HISTORY_JOBS:
        raise p.RoutingRecoveryError('route history exceeds bounded job recovery limit')
    # The runtime revision protects unfinished execution. Published Router
    # receipts with a complete seed and per-request proofs can instead be
    # checked below against their own inputs, even after downstream code changes.
    published_replay = (batch['status'] in SOURCE_STATES
        and all(key in original for key in ('tracks', 'recent', 'next_track_ordinal', 'input_policy'))
        and bool(jobs) and all('routing_frame' in json.loads(row['request_json']) for row in jobs))
    current_revision = p.runtime_revision()
    if (original.get('contract') != p.CONTRACT
            or (original.get('runtime_revision') != current_revision
                and not published_replay
                and not (legacy_compacted and original.get('runtime_revision') in COMPACTED_ROUTER_REVISIONS))
            or original.get('scope') != batch['scope']):
        raise p.RoutingRecoveryError('route producer has an incompatible frozen contract: '
            f"producer={batch['id']}; status={batch['status']}; "
            f"contract={original.get('contract')!r}; expected_contract={p.CONTRACT!r}; "
            f"runtime_revision={original.get('runtime_revision')!r}; current_runtime_revision={current_revision!r}; "
            f"scope={original.get('scope')!r}; expected_scope={batch['scope']!r}")
    if not _same_scope(original, original.get('routing_messages', [])):
        raise p.RoutingRecoveryError('route producer messages do not belong to its frozen scope')
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
    state=list(original.get('tracks',[]));ordinal=original.get('next_track_ordinal',1)
    prior=list(original.get('recent',[]))
    for row in jobs:
        request = json.loads(row['request_json'])
        if request.get('batch_id') != batch['id']:
            raise p.RoutingRecoveryError('Router job belongs to another producer batch')
        messages = p._router_prefix(original, request, cursor)
        if legacy_compacted:
            if 'routing_frame' not in request:
                raise p.RoutingRecoveryError('compacted Router request has no frozen frame proof')
            # Old compaction lost the complete seed AND candidate policy. Saved
            # requests still prove their selected cards; retain every encountered
            # card so a hidden candidate must match when it returns later.
            if cursor == 0:
                ordinal=request['next_track_ordinal'];prior=list(request.get('recent_context',[]))
                if prior and not _same_scope(original,prior):
                    raise p.RoutingRecoveryError('Router recent context is outside its frozen scope')
            known={card['track_id']:card for card in state}
            with Store(database,read_only=True) as store:
                for card in request['active_tracks']:
                    if card['track_id'] not in known:
                        tracks.assert_before(store,[card],original['routing_messages'][0]['id'])
                        state.append(card);known[card['track_id']]=card
            selected=[known[card['track_id']] for card in request['active_tracks']]
            p.assert_router_state({'input_policy':{}},request,selected,ordinal,prior)
        else:
            p.assert_router_state(original,request,state,ordinal,prior)
        if 'routing_frame' in request:
            with Store(database,read_only=True) as store:
                tracks.assert_before(store,request['active_tracks'],messages[0]['id'])
        with Store(database,read_only=True) as store:
            for key,value in request.get('routing_frame',{}).get('source_hashes',{}).items():
                raw=store.conn.execute('SELECT event_hash FROM raw_events WHERE id=?',(int(key),)).fetchone()
                if raw is None or raw[0]!=value:
                    raise p.RoutingRecoveryError('frozen Router source hash changed: '+str(key))
        cursor += len(messages)
        if row['output_json'] is None:
            break  # An unfinished suffix is never rerun by recovery.
        output = json.loads(row['output_json'])
        p.validate(request, output)
        assignments, updates, ordinal = p.normalize_event_track_message_output(
            output, messages, request['active_tracks'], session_id=original['scope'],
            next_track_ordinal=request['next_track_ordinal'])
        with p.latest.identity_scope(request['identity']):
            cards = tracks.update_cards(state, assignments, updates,
                                        messages, original['scope'])
        state=cards;prior.extend(messages)
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
            pending_read=frozen.get('pending_material_read')
            if pending_read:
                original_ids={m['id'] for m in component['messages']}
                extra={m['id'] for m in frozen['messages']}-original_ids
                if extra and (frozen.get('adopted_pending_ids')!=[pending_read['id']]
                        or extra!={m['id'] for m in pending_read['messages']}):
                    raise ValueError('pending material ownership differs from the explicit adoption')
                bounded['messages']=[m for m in frozen['messages'] if m['id'] in original_ids]
                allowed_sessions=set(component.get('context_session_ids',[]))|{m['session_id'] for m in pending_read['messages']}
                if not set(frozen.get('context_session_ids',[])).issubset(allowed_sessions):raise ValueError('pending read has unrelated sessions')
                bounded['context_session_ids']=component.get('context_session_ids',[])
            if (p._component_signature([bounded]) != p._component_signature([component])
                    or [_message_key(m) for m in bounded['messages']] != [_message_key(m) for m in component['messages']]
                    or frozen.get('base_event_candidates', []) != component.get('base_event_candidates', [])
                    or bounded.get('context_session_ids', []) != component.get('context_session_ids', [])):
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


RECOVERABLE = ('pending', 'paused_failure', 'retry_wait', 'routing_only', 'needs_repair', 'waiting_materials')


def _recovery_scope(store, batch_id):
    from . import pipeline as p
    if not isinstance(batch_id, str) or not batch_id:
        raise ValueError('请选择需要处理的批次')
    batch = store.conn.execute('SELECT rowid AS queue_order,* FROM pipeline_batches WHERE id=?', (batch_id,)).fetchone()
    if batch is None or batch['status'] not in RECOVERABLE:
        raise Conflict('此批已不处于待处理状态，请刷新后确认')
    if not store.conn.execute('SELECT 1 FROM pipeline_batches b WHERE b.id=? AND '+p.SCOPE_HEAD, (batch_id,)).fetchone():
        raise Conflict('请先处理同一聊天中更早的批次')
    data = json.loads(batch['input_json'])
    routing = data.get('routing_messages', data.get('messages', []))
    stable = data.get('messages', routing)
    if not stable or not _same_scope(data, routing):
        raise Conflict('无法确认本批原话范围，请先检查批次记录')
    stable_ids = {m['id'] for m in stable}
    reserved=store.conn.execute("SELECT pm.id FROM pipeline_pending_materials pm,json_each(pm.source_ids_json) s WHERE pm.status IN ('open','running') AND pm.batch_id<>? AND s.value IN ("+','.join('?' for _ in stable_ids)+") LIMIT 1",(batch_id,*sorted(stable_ids))).fetchone()
    if reserved:raise Conflict('原话已由待办 '+str(reserved['id'])+' 保留，请先处理对应待办')
    if len(stable_ids) != len(stable) or not stable_ids.issubset({m['id'] for m in routing}):
        raise Conflict('本批原话编号不完整，不能继续操作')
    for unit in p.dialogue_units(routing):
        ids = {m['id'] for m in unit}
        if ids & stable_ids and (not ids.issubset(stable_ids) or not p.dialogue_unit_is_complete(unit)):
            raise Conflict('本批范围不包含完整问答，不能跳过或拆分')
    for frozen in routing:
        row = store.conn.execute('SELECT * FROM raw_events WHERE id=?', (frozen['id'],)).fetchone()
        if row is None or _message_key(p.message(row)) != _message_key(frozen):
            raise Conflict('原话已变化，请刷新并核对批次')
        if frozen['id'] in stable_ids and store.conn.execute('SELECT 1 FROM raw_processing WHERE raw_id=?', (frozen['id'],)).fetchone():
            raise Conflict('本批原话已有处理记录，不能重复操作')
    if store.conn.execute('SELECT 1 FROM raw_processing WHERE operation_id=?', (batch_id,)).fetchone():
        raise Conflict('本批已有结算记录，不能重新操作')
    if store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='fact_event_settlement_operations'").fetchone():
        if store.conn.execute('SELECT 1 FROM fact_event_settlement_operations WHERE operation_id=?', (batch_id,)).fetchone():
            raise Conflict('本批已有 Event 结算记录，不能重新操作')
    return batch, data, routing, stable


def _recovery_chunks(data, max_chars):
    from . import pipeline as p
    from .pipeline_limits import routing_blocks
    stable_ids = {m['id'] for m in data.get('messages', data['routing_messages'])}
    chunks = routing_blocks(data['routing_messages'], max_chars)
    result = []
    for index, chunk in enumerate(chunks):
        if index+1 < len(chunks):
            following = p.dialogue_units(chunks[index+1])[0]
            if not p.dialogue_unit_is_complete(following):
                chunk = [*chunk, *following]
        stable = [m for m in chunk if m['id'] in stable_ids]
        if stable:
            result.append((chunk, stable))
    return result


def recovery_preview(database, batch_id, max_input_chars=None):
    with Store(database, read_only=True) as store, store.transaction():
        batch, data, routing, stable = _recovery_scope(store, batch_id)
        chars = sum(len(m['content']) for m in stable)
        if max_input_chars is None:
            max_input_chars = max(2000, min(10000, chars//2))
        if type(max_input_chars) is not int or not 2000 <= max_input_chars <= 100000:
            raise ValueError('每批原话字符上限须为 2000 至 100000 的整数')
        chunks = _recovery_chunks({**data, 'routing_messages': routing}, max_input_chars)
        sizes = [sum(len(m['content']) for m in chunk) for chunk, _ in chunks]
        return {'batch_id': batch_id, 'scope': batch['scope'], 'originals': len(stable), 'original_chars': chars,
                'first_at': min(m['created_at'] for m in stable), 'last_at': max(m['created_at'] for m in stable),
                'max_input_chars': max_input_chars, 'batches': len(chunks), 'batch_chars': sizes,
                'oversized_batches': sum(size > max_input_chars for size in sizes), 'can_rebatch': len(chunks) > 1}


async def recover_batch(database, batch_id, action, confirm, max_input_chars=None):
    from ..work_tasks import execute
    expected = {'skip': 'SKIP_PIPELINE_BATCH', 'rebatch': 'REBATCH_PIPELINE_BATCH'}
    if action not in expected or confirm != expected[action]:
        raise ValueError('请明确确认本批操作；原话和已保存的 Event 保留')
    if action == 'rebatch' and (type(max_input_chars) is not int or not 2000 <= max_input_chars <= 100000):
        raise ValueError('每批原话字符上限须为 2000 至 100000 的整数')
    async def operation():
        return _rebuild(database, batch_id, recovery_action=action, max_input_chars=max_input_chars)
    return await execute(database, 'pipeline', operation)


def _rebuild(database, batch_id, *, recovery_action=None, max_input_chars=None, resume_pending_id=None):
    from . import pipeline as p
    p.initialize(database)
    with Store(database) as store, store.transaction(immediate=True):
        batch = store.conn.execute('SELECT rowid AS queue_order,* FROM pipeline_batches WHERE id=?',
                                   (batch_id,)).fetchone()
        if recovery_action:
            batch, data, _, _ = _recovery_scope(store, batch_id)
        elif batch is None or batch['status'] != 'needs_repair':
            raise Conflict('此批已不处于待修复状态，请刷新后确认')
        if resume_pending_id is not None and not store.conn.execute(
                "SELECT 1 FROM pipeline_pending_materials WHERE id=? AND batch_id=? AND status='open' AND kind='unrouted'",
                (resume_pending_id,batch_id)).fetchone():
            raise Conflict('待办已被领取或完成，请刷新')
        data = json.loads(batch['input_json'])
        split = _recovery_chunks(data, max_input_chars) if recovery_action == 'rebatch' else None
        if split is not None and len(split) < 2:
            raise ValueError('当前上限无法把本批拆成更小的完整问答；可降低上限，或选择跳过本批')
        legacy_contract = data.get('contract') != p.CONTRACT
        if legacy_contract and not recovery_action:
            _recovery_scope(store,batch_id)
        stable = [m['id'] for m in data.get('messages',data['routing_messages'])]
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
        fresh = {key: deepcopy(data[key]) for key in ('contract', 'runtime_revision', 'input_policy', 'messages', 'parked',
                 'routing_messages', 'tracks', 'scope', 'source', 'recent', 'day', 'recovery_downgrade', 'recovery_path') if key in data}
        fresh['contract'] = p.CONTRACT
        fresh['runtime_revision'] = p.runtime_revision()
        # Explicit rebuilding creates a new summary-first plan; old jobs and
        # their original reading policy remain available in the retired batch.
        fresh['input_policy'] = {**read_from_store(store)['pipeline'], 'curator_summary_first': True}
        fresh['tracks'] = [c for c in fresh.get('tracks',[]) if p._card_is_complete(c)]
        previous_result = json.loads(batch['result_json'] or '{}')
        history_error = data.get('routing_evidence_error') or previous_result.get('reason', '')
        if legacy_contract or data.get('routing_evidence_error') or 'No bounded historical pre-routing Track frame:' in history_error:
            # The existing explicit rebuild starts a new history segment for
            # this scope. Later windows must not reintroduce the same gap.
            fresh['tracks'] = []
            fresh['recent'] = []
            fresh['recovery_downgrade'] = {'reason': '旧执行方案不兼容，按已核对原话重新归线' if legacy_contract else history_error,
                'history_before_id': min(m['id'] for m in routing),
                'confirmed_by': 'REBUILD_PIPELINE_BATCH', 'source_batch_id': batch_id}
        fresh['next_track_ordinal'] = max(data.get('next_track_ordinal', 1),
                                         tracks.next_ordinal(data['scope'], reserved))
        fresh['ignore_route_cache'] = True
        fresh['rebuild_of'] = batch_id
        fresh['queue_order'] = data.get('queue_order', batch['queue_order'])
        if resume_pending_id is not None:
            fresh.update(resume_pending_id=resume_pending_id,pending_previous_batch_id=batch_id)
        route_only=batch_id.startswith('route:') and not recovery_action and resume_pending_id is None
        replacement = ('route:' if route_only else 'pipeline:')+digest(encode([batch_id, uuid4().hex]))
        audit = {'status': 'superseded_repair', 'replacement_batch_id': replacement,
                 'confirmed_at': now(), 'previous_result': previous_result,
                 'discarded_route_cache': cache}
        if fresh.get('recovery_downgrade'):
            audit['recovery_downgrade'] = fresh['recovery_downgrade']
        if recovery_action == 'skip':
            completed = {'status': 'processed', 'batch_id': replacement, 'completed_at': now(), 'events': 0,
                         'processed_originals': len(stable), 'skipped': len(stable), 'manual_skip': True, 'pending': 0}
            for raw_id in stable:
                store.conn.execute('INSERT INTO raw_processing VALUES (?,?,?)', (raw_id, replacement, 'manual_skip'))
            store.conn.execute("INSERT INTO pipeline_batches(id,scope,input_json,status,result_json) VALUES (?,?,?,'done',?)",
                               (replacement, data['scope'], encode(fresh), encode(completed)))
            audit.update(recovery_action='skip', source_message_ids=stable)
            result = {'status': 'skipped_batch', 'batch_id': batch_id, 'skipped_originals': len(stable)}
        elif split is not None:
            fresh['input_policy'] = {**read_from_store(store)['pipeline'], 'max_input_chars': max_input_chars}
            replacements = []
            for index, (chunk, owned) in enumerate(split):
                key = replacement if index == 0 else 'pipeline:'+uuid4().hex
                owned_ids = {m['id'] for m in owned}
                child = {**fresh, 'routing_messages': chunk, 'messages': owned,
                         'parked': [m for m in chunk if m['id'] not in owned_ids],
                         'rebatch_of': batch_id, 'rebatch_load_tracks': index > 0,
                         'recovery_path': fresh.get('recovery_path', '')+f'{index:08d}/'}
                store.conn.execute("INSERT INTO pipeline_batches(id,scope,input_json,status) VALUES (?,?,?,'pending')",
                                   (key, data['scope'], encode(child)))
                replacements.append(key)
            audit.update(recovery_action='rebatch', replacement_batch_ids=replacements, max_input_chars=max_input_chars)
            result = {'status': 'rebatched', 'batch_id': replacements[0], 'batch_ids': replacements,
                      'superseded_batch_id': batch_id, 'batches': len(replacements), 'originals': len(stable)}
        else:
            store.conn.execute('INSERT INTO pipeline_batches(id,scope,input_json,status) VALUES (?,?,?,?)',
                               (replacement, data['scope'], encode(fresh),'routing_only' if route_only else 'pending'))
            result = {'status': 'rebuilt', 'batch_id': replacement, 'superseded_batch_id': batch_id,
                      'note': '旧计划与模型结果已保留，尚未处理的原话将重新归线；已保存的 Event 不变。'}
        store.conn.execute("UPDATE pipeline_batches SET status='superseded_repair',result_json=? WHERE id=?",
                           (encode(audit), batch_id))
        if resume_pending_id is not None:
            store.conn.execute("UPDATE pipeline_pending_materials SET batch_id=?,status='running',updated_at=? WHERE id=?",
                               (replacement,now(),resume_pending_id))
        else:
            store.conn.execute("UPDATE pipeline_pending_materials SET status=?,updated_at=? WHERE batch_id=? AND status IN ('open','running')",
                               ('resolved_skipped' if recovery_action=='skip' else 'resolved_rebuilt',now(),batch_id))
        return result
