"""Transport-sized chunks preserve complete reply envelopes and original IDs."""
from datetime import datetime
from .pipeline_rules import dialogue_units

DEFAULTS={'max_input_chars':12000,'max_prompt_chars':40000,'timeout_seconds':600}


def blocks(messages, max_chars=12000):
    result=[];current=[];chars=0;turns=0;previous=None
    for unit in dialogue_units(messages):
        known=all(m.get('metadata',{}).get('timestamp_source')!='import_time' for m in unit)
        start=datetime.fromisoformat(unit[0]['created_at'].replace('Z','+00:00')) if known else None
        size=sum(len(m.get('content',m.get('text',''))) for m in unit)
        silence=start is not None and previous is not None and (start-previous).total_seconds()>=1200
        if size>max_chars:
            # Reply envelopes are transport atoms. Never truncate one just to
            # satisfy a soft input target; isolate it so later material can
            # still be split normally and let the prompt budget be the final
            # model-facing guard.
            if current:
                result.append(current);current=[];chars=0;turns=0
            result.append(list(unit))
            previous=datetime.fromisoformat(unit[-1]['created_at'].replace('Z','+00:00')) if known else None
            continue
        if current and (silence or turns>=20 or chars+size>max_chars):
            result.append(current);current=[];chars=0;turns=0
        current.extend(unit);chars+=size;turns+=1
        previous=datetime.fromisoformat(unit[-1]['created_at'].replace('Z','+00:00')) if known else None
    if current:result.append(current)
    return result


def allowed_ids(request):
    component=request.get('component',{})
    messages=request.get('messages',component.get('messages',[]))
    return {'message_ids':[m['id'] for m in messages],
            'stable_unit_roots':[m['unit_root_message_id'] for m in component.get('memberships',[])
                                 if m['unit_root_message_id'] in {r['id'] for r in component.get('messages',[])}],
            'track_ids':component.get('track_ids',[t['track_id'] for t in request.get('active_tracks',[])]),
            'base_event_ids':[b['event_id'] for b in component.get('base_event_candidates',[])]}
