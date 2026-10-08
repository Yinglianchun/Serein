"""Optional conversation context; disabled features do not instantiate their stores."""
import json
import asyncio
import logging
import time
from uuid import uuid4
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from .deployment import read_settings, task_model, feature_enabled
from .core.store import Store, encode
from .chat_context import ClientContext


def persona_engine(database, task, state):
    from .compat.persona_engine import PersonaStateEngine
    from .model_runtime import TaskClient
    engine = PersonaStateEngine({'serein_database':database,'identity':state['identity'],
        'persona':{'enabled':task=='persona','conflict_nudge_enabled':task=='anti_retreat'}})
    engine.client = TaskClient(database,'persona')
    return engine


def conversation_turns(messages):
    """Adapt chat messages to the evaluator's user_text / assistant_text pairs."""
    context=ClientContext()
    turns=[]
    user=''
    for item in messages:
        if item.get('role')=='user':
            user=context._strip_external_context_from_user_text(context._coerce_message_text(item.get('content')))
        elif item.get('role')=='assistant' and not item.get('tool_calls'):
            answer=context._coerce_message_text(item.get('content'))
            if user and answer:
                turns.append({'user_text':user,'assistant_text':answer})
                user=''
    return turns[-8:]


def current_round(database, window_id):
    with Store(database) as store:
        row=store.conn.execute('SELECT value_json FROM background_state WHERE name=?',('feature_round:'+window_id,)).fetchone()
        if row is None:
            row=store.conn.execute('SELECT value_json FROM background_state WHERE name=?',('memo_round:'+window_id,)).fetchone()
    return json.loads(row[0]) if row else 0


def morning_dream(database, state, at=None):
    stamp = (at or datetime.now(ZoneInfo(state['clock']['timezone']))).astimezone(ZoneInfo(state['clock']['timezone']))
    if stamp.hour < 4:
        return '', {}
    day = stamp.date().isoformat()
    with Store(database, read_only=True) as store:
        saved = store.conn.execute("SELECT value_json FROM background_state WHERE name='dream_morning_day'").fetchone()
        if saved and json.loads(saved[0]) == day:
            return '', {}
        rows = store.conn.execute("SELECT * FROM historical_works WHERE kind='dream' AND id NOT IN (SELECT document_id FROM deletions)").fetchall()
    candidates = []
    for row in rows:
        meta = json.loads(row['metadata_json'])
        try:
            generated = datetime.fromisoformat(meta.get('generated_at', '').replace('Z','+00:00'))
            if generated.tzinfo is None:
                continue
        except (ValueError, TypeError):
            continue
        if not meta.get('surfaced') and stamp - timedelta(hours=24) <= generated <= stamp:
            candidates.append((generated, row['id'], row['body_md']))
    receipt = {'dream_morning_day':day}
    if not candidates:
        return '', receipt
    generated, key, body = max(candidates)
    receipt['dream_id'] = key
    return f'晨间梦境（想象内容，不是事实记忆） · {generated.isoformat()} · {key}\n{body}', receipt


async def prepare(database, window_id, query, messages):
    state = read_settings(database)
    features = state['features']
    parts, receipt = [], {'memo_ids':[],'round':current_round(database,window_id)+1,'user_query':query}
    if features['dream_morning']:
        text, dream_receipt = morning_dream(database, state)
        if text: parts.append(text)
        receipt.update(dream_receipt)
    if features['memos']:
        from .compat.memo_store import ReminderStore
        memos = ReminderStore({'serein_database':database})
        due = memos.due(session_id=window_id,channels=['gateway','session'],round_id=receipt['round'])
        receipt['memo_ids'] = [item['id'] for item in due]
        if due:
            parts.append('备忘 · 留给未来的话（原文资料）：\n'+json.dumps(
                [{'memo_id':item['id'],'title':item['title'],'content':item['content']} for item in due],ensure_ascii=False))
    if features['anti_retreat']:
        with Store(database,read_only=True) as store:
            row=store.conn.execute('SELECT value_json FROM background_state WHERE name=?',('anti_retreat:'+window_id,)).fetchone()
        pending=json.loads(row[0]).get('pending',{}) if row else {}
        if pending.get('round')==receipt['round'] and time.time()-pending.get('created_at',0)<600:
            parts.append(pending['nudge'])
            receipt['anti_retreat_id']=pending['id']
    return '\n\n'.join(part for part in parts if part), receipt


def delivered(database, window_id, receipt):
    if feature_enabled(database,'memos'):
        from .compat.memo_store import ReminderStore
        memos = ReminderStore({'serein_database':database})
        for key in receipt.get('memo_ids',[]):
            memos.mark_reminded(key,round_id=receipt['round'])
    with Store(database) as store,store.transaction(immediate=True):
        if receipt.get('dream_morning_day') and feature_enabled(database,'dream_morning'):
            day = receipt['dream_morning_day']
            saved = store.conn.execute("SELECT value_json FROM background_state WHERE name='dream_morning_day'").fetchone()
            if not saved or json.loads(saved[0]) != day:
                from .compat.dreams import Dreams
                from .core.store import now
                key = receipt.get('dream_id')
                row = store.conn.execute("SELECT * FROM historical_works WHERE id=? AND kind='dream' AND id NOT IN (SELECT document_id FROM deletions)", (key,)).fetchone() if key else None
                if row:
                    meta = json.loads(row['metadata_json'])
                    if not meta.get('surfaced'):
                        meta.update(surfaced=True, surfaced_at=now())
                        Dreams.save_record(store, meta, row['body_md'])
                        Dreams.log_event(store,'surfaced',{'dream_id':key,'generated_at':meta.get('generated_at'),'surfaced_at':meta['surfaced_at'],'reason':'morning'})
                store.conn.execute('INSERT INTO background_state VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json', ('dream_morning_day',encode(day)))
        row=store.conn.execute('SELECT value_json FROM background_state WHERE name=?',('anti_retreat:'+window_id,)).fetchone()
        if row:
            saved=json.loads(row[0]);pending=saved.get('pending',{})
            if receipt.get('anti_retreat_id') and pending.get('id')==receipt['anti_retreat_id']:
                saved.update(last_round=receipt['round'],last_at=time.time(),pending={})
            elif pending.get('round',0)<=receipt['round']:
                saved['pending']={}
            store.conn.execute('UPDATE background_state SET value_json=? WHERE name=?',(encode(saved),'anti_retreat:'+window_id))
        store.conn.execute('INSERT INTO background_state VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json',
            ('feature_round:'+window_id,encode(receipt.get('round',0))))


async def after_reply(database, window_id, query, message, messages, round_id=None, recalled_ids=None):
    state = read_settings(database)
    async def evaluate_persona():
        engine=persona_engine(database,'persona',state)
        if round_id is None or round_id % engine.evaluation_interval_rounds == 0:
            await engine.update_from_exchange(window_id,query,message,
                recalled_memory_ids=recalled_ids or [],recent_conversation_turns=conversation_turns(messages))
    tasks=[]
    if task_model(database,'persona'):
        if state['features']['persona']:tasks.append(evaluate_persona())
        if state['features']['anti_retreat']:
            tasks.append(evaluate_retreat(database,window_id,query,message,messages,round_id or current_round(database,window_id),state))
    for result in await asyncio.gather(*tasks,return_exceptions=True):
        if isinstance(result,Exception):
            logging.getLogger(__name__).warning('Post-reply evaluation failed: %s',type(result).__name__)


async def evaluate_retreat(database,window_id,query,message,messages,round_id,state):
    """One async detector per window; a signal is usable only on the following turn."""
    key='anti_retreat:'+window_id;token=uuid4().hex;stamp=time.time()
    with Store(database) as store,store.transaction(immediate=True):
        row=store.conn.execute('SELECT value_json FROM background_state WHERE name=?',(key,)).fetchone()
        saved=json.loads(row[0]) if row else {}
        if (saved.get('running_until',0)>stamp or saved.get('checked_round',-1)>=round_id
            or saved.get('pending',{}).get('round',0)>round_id
            or (saved.get('last_round') is not None and (round_id+1-saved['last_round']<6 or stamp-saved['last_at']<600))):return
        saved.update(token=token,running_until=stamp+120,checked_round=round_id,pending={})
        store.conn.execute('INSERT INTO background_state VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json',(key,encode(saved)))
    try:
        engine=persona_engine(database,'anti_retreat',state)
        history=[*conversation_turns(messages),{'user_text':query,'assistant_text':message}][-8:]
        result=await asyncio.wait_for(engine.detect_conflict_nudge(query,history),90)
        enabled=feature_enabled(database,'anti_retreat')
        next_round=current_round(database,window_id)+1
        with Store(database) as store,store.transaction(immediate=True):
            row=store.conn.execute('SELECT value_json FROM background_state WHERE name=?',(key,)).fetchone()
            saved=json.loads(row[0]) if row else {}
            if saved.get('token')==token:
                if enabled and next_round==round_id+1 and result.get('triggered'):
                    saved['pending']={'id':token,'round':round_id+1,'nudge':result['nudge'].replace('这一轮可能','上一轮可能',1),'created_at':time.time()}
                saved['running_until']=0
                store.conn.execute('UPDATE background_state SET value_json=? WHERE name=?',(encode(saved),key))
    finally:
        with Store(database) as store,store.transaction(immediate=True):
            store.conn.execute("UPDATE background_state SET value_json=json_set(value_json,'$.running_until',0) WHERE name=? AND json_extract(value_json,'$.token')=?",(key,token))
