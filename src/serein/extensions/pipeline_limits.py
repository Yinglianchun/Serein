"""Transport-sized chunks preserve complete reply envelopes and original IDs."""
from datetime import datetime, timedelta, timezone
from .pipeline_rules import iter_dialogue_units

DEFAULTS={'max_input_chars':40000,'max_prompt_chars':200000,'timeout_seconds':600}


def settlement_window(unit):
    """The first 03:00 UTC+8 watermark containing the WHOLE envelope."""
    end=max(datetime.fromisoformat(m['created_at'].replace('Z','+00:00')) for m in unit)
    end=end.astimezone(timezone(timedelta(hours=8)))
    boundary=end.replace(hour=3,minute=0,second=0,microsecond=0)
    return boundary if end<=boundary else boundary+timedelta(days=1)


def routing_blocks(messages, max_chars=40000):
    """Freeze transport frames within settlement windows, never split a unit."""
    return [frame for frame, _ in frames_with_lookahead(messages, max_chars)]


def blocks(messages, max_chars=40000):
    return [frame for frame, _ in frames_with_lookahead(messages, max_chars, settlement=False)]


def frames_with_lookahead(messages, max_chars, *, settlement=True):
    """Yield a closed frame and the first whole envelope in the following frame."""
    current=[];chars=0;turns=0;previous=None;window=None
    for unit in iter_dialogue_units(messages):
        unit_window = settlement_window(unit) if settlement else None
        known=all(m.get('metadata',{}).get('timestamp_source')!='import_time' for m in unit)
        start=datetime.fromisoformat(unit[0]['created_at'].replace('Z','+00:00')) if known else None
        size=sum(len(m.get('content',m.get('text',''))) for m in unit)
        silence=start is not None and previous is not None and (start-previous).total_seconds()>=1200
        if current and (unit_window!=window or silence or turns>=20 or chars+size>max_chars):
            yield current, unit
            current=[];chars=0;turns=0
        current.extend(unit);chars+=size;turns+=1
        window=unit_window
        previous=datetime.fromisoformat(unit[-1]['created_at'].replace('Z','+00:00')) if known else None
    if current:
        yield current, []


def allowed_ids(request):
    component=request.get('component',{})
    messages=request.get('messages',component.get('messages',[]))
    stable_ids={int(m['id']) for m in component.get('messages',[])}
    return {'message_ids':[m['id'] for m in messages],
            'stable_unit_roots':[int(m['unit_root_message_id']) for m in component.get('memberships',[])
                                 if all(int(sid) in stable_ids for sid in
                                        (m.get('source_message_ids') or [m['unit_root_message_id']]))],
            'track_ids':component.get('track_ids',[t['track_id'] for t in request.get('active_tracks',[])]),
            'base_event_ids':[b['event_id'] for b in component.get('base_event_candidates',[])]}
