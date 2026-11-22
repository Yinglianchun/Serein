"""Gateway recall cooldown and delivery acknowledgements."""
from datetime import datetime, timedelta, timezone
import json

from .core.store import Store, encode, now

COOLDOWN_LIMIT = 200
COOLDOWN_STATE = 'gateway_recall_cooldown'


def _cooldown(store):
    row = store.conn.execute('SELECT value_json FROM background_state WHERE name=?', (COOLDOWN_STATE,)).fetchone()
    if row:
        return json.loads(row[0])
    # Recover the last deliveries from pre-upgrade gateway history. The newest
    # legacy round is round zero, so older records carry negative positions.
    records = []
    rows = store.conn.execute("SELECT payload FROM host_deliveries WHERE "
        "json_extract(payload,'$.reported_by')='serein_chat_proxy' ORDER BY id DESC")
    for distance, row in enumerate(rows):
        payload = json.loads(row[0])
        records.extend([key, -distance, payload.get('created_at')] for key in reversed(payload['delivered_ids']))
        if len(records) >= COOLDOWN_LIMIT:
            break
    return {'round': 0, 'records': list(reversed(records[:COOLDOWN_LIMIT]))}


def recent_deliveries(database, window_id=None, *, mode='rounds', rounds=5, hours=24):
    with Store(database, read_only=True) as store:
        state = _cooldown(store)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return list(dict.fromkeys(
        key for key, turn, stamp in reversed(state['records'])
        if (state['round'] - turn < rounds if mode == 'rounds' else stamp and stamp > cutoff)
    ))


def record_delivery(database, window_id, receipt_id, ids, *, query='', observation_id=None,
                    memory_items=(), count_round=True, audit=True):
    with Store(database) as store, store.transaction(immediate=True):
        stamp = now()
        if count_round or ids:
            state = _cooldown(store)
            state['round'] += int(count_round)
            # A tool response can deliver cards before its final reply counts the turn.
            turn = state['round'] if count_round else state['round'] + 1
            additions = [[key, turn, stamp] for key in ids]
            state['records'] = (state['records'] + additions)[-COOLDOWN_LIMIT:]
            store.conn.execute('INSERT OR REPLACE INTO background_state VALUES (?,?)', (COOLDOWN_STATE, encode(state)))
        if audit:
            payload = {'receipt_id': receipt_id, 'window_id': window_id, 'delivered_ids': ids,
                       'reported_by': 'serein_chat_proxy', 'delivery_target': 'upstream_model', 'created_at': stamp,
                       'query':query, 'observation_id':observation_id, 'gateway_memory_items':list(memory_items)}
            store.conn.execute('INSERT INTO host_deliveries(receipt_id,payload) VALUES (?,?)', (receipt_id, encode(payload)))
