"""Continuation state matching the verified Bridge message-level Track router."""
import json
import re
from datetime import datetime, timedelta, timezone
from ..core.store import digest, encode
from . import pipeline_latest as latest


def scope_for(source, session):
    return digest(encode([source, session]))[:20]


def next_ordinal(scope, cards):
    numbers = [int(match[1]) for card in cards if
               (match := re.fullmatch(r'session_' + re.escape(str(scope)) + r'_track_([0-9]+)', card['track_id']))]
    return max(numbers, default=0) + 1


def load_tracks(store, source, session, before_id, message, *, lookback_days=3):
    """Show tracks with proven routed activity in the preceding N days.

    Public API clients need not supply a window identity. The current raw
    message supplies the clock; persisted route receipts supply activity.
    """
    if type(lookback_days) is not int or not 1<=lookback_days<=365:
        raise ValueError('Track lookback must be an integer between 1 and 365 days')
    current=store.conn.execute('SELECT created_at,metadata_json FROM raw_events WHERE id=? AND source=?',
                               (before_id,source)).fetchone()
    if current is None:raise ValueError('Track routing anchor is missing')
    reference=datetime.fromisoformat(current['created_at'].replace('Z','+00:00'))
    if reference.tzinfo is None:reference=reference.replace(tzinfo=timezone.utc)
    reference=reference.astimezone(timezone.utc)
    lower=reference-timedelta(days=lookback_days)
    metadata=json.loads(current['metadata_json'] or '{}')
    boundary=(metadata.get('runtime',''),metadata.get('workspace_root',''))
    anchors={}
    rows=store.conn.execute('SELECT r.*,p.route_json FROM pipeline_routes p JOIN raw_events r ON r.id=p.raw_id '
        'WHERE r.source=? AND r.id<? AND julianday(r.created_at)>=julianday(?) '
        'AND julianday(r.created_at)<=julianday(?) ORDER BY r.id DESC',
        (source,before_id,lower.isoformat(),reference.isoformat()))
    for row in rows:
        stamp=datetime.fromisoformat(row['created_at'].replace('Z','+00:00'))
        if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone.utc)
        if not lower<=stamp.astimezone(timezone.utc)<=reference:continue
        meta=json.loads(row['metadata_json'] or '{}')
        if (meta.get('runtime',''),meta.get('workspace_root',''))!=boundary:continue
        route=json.loads(row['route_json'])
        for track_id in [route['primary_track_id'],*route['context_track_ids']]:
            previous=anchors.get(track_id)
            if previous is None or (stamp,row['id'])>(previous[0],previous[1]['id']):
                anchors[track_id]=(stamp,row)
    scope=scope_for(source,session)
    cards=[]
    for row in store.conn.execute('SELECT id,card_json,scope FROM pipeline_tracks ORDER BY id'):
        anchor=anchors.get(row['id'])
        if anchor is None:continue
        card=json.loads(row['card_json'])
        card.setdefault('last_session_id',row['scope'])
        match=re.fullmatch(r'session_(.+)_track_[0-9]+',card['track_id'])
        card.setdefault('origin_session_id',match[1] if match else row['scope'])
        original=message(anchor[1])
        card['recent_source_message_ids']=[original['id']]
        card['recent_turns']=latest.transcript_payload([original])
        cards.append(card)
    all_ids=[{'track_id':row[0]} for row in store.conn.execute('SELECT id FROM pipeline_tracks')]
    return cards,next_ordinal(scope,all_ids)


def parked(cards):
    return [{**card, 'status': 'parked'} if card.get('status') == 'active' else dict(card) for card in cards]


def update_cards(cards, assignments, updates, messages, scope):
    current = {card['track_id']: card for card in parked(cards)}
    by_id = {m['id']: m for m in messages}
    for update in updates:
        key = update['track_id']
        primary = [a['source_message_id'] for a in assignments if a['primary_track_id'] == key]
        context = [a['source_message_id'] for a in assignments if key in a['context_track_ids']]
        anchor = (primary or context)[-1]
        previous = current.get(key, {})
        current[key] = {**previous, **update, 'origin_session_id': previous.get('origin_session_id', scope),
                        'last_session_id': scope, 'recent_source_message_ids': [anchor],
                        'recent_turns': latest.transcript_payload([by_id[anchor]])}
    return list(current.values())


def persist(conn, cards, scope, *, preserve_newer=False):
    for card in cards:
        if preserve_newer:
            row = conn.execute('SELECT card_json FROM pipeline_tracks WHERE id=?', (card['track_id'],)).fetchone()
            if row:
                previous = [key for key in json.loads(row[0]).get('recent_source_message_ids', []) if type(key) is int]
                incoming = [key for key in card.get('recent_source_message_ids', []) if type(key) is int]
                if previous and (not incoming or max(incoming) < max(previous)):
                    continue
        # Unused parked cards still belong to the last window that referenced
        # them. Saving a different batch must not move their continuation scope.
        conn.execute('INSERT INTO pipeline_tracks VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET '
                     'scope=excluded.scope,card_json=excluded.card_json',
                     (card['track_id'], card.get('last_session_id', scope), encode(card)))
