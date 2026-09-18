"""Optional automatic Event-to-Arc material linking.

The linker reads the settled Event body and a bounded set of body-free Arc cards.
It never reads original messages or Narrative prose and never creates an Arc.
"""

import asyncio
import json
import logging
import re

from .compat.events import project_events
from .compat.narratives import Narratives
from .core.store import Store, encode, now
from .deployment import task_model


SYSTEM_PROMPT = """你只判断一条已经正式落库的 Event 是否属于某条已有 Arc。
Arc 是长期主题、作品、项目或里程碑的材料目录，不是 Event 边界，也不是叙事正文。
你只能阅读 Event 的标题、正文，以及程序先按关键词找到的 Arc 卡；不会提供聊天原话或叙事卷正文。
只有 Event 的主要经历明确推进某条 Arc 时才能 attach。偶然提及、比喻、工具名、同一人物或泛泛相似都必须 unassigned。
只能选择候选中的一个 arc_key；不得创建 Arc、修改 Event 或撰写叙事卷。只返回合法 JSON。"""


def initialize(database):
    with Store(database) as store:
        store.conn.executescript("""
            CREATE TABLE IF NOT EXISTS pipeline_arc_links (
                event_id TEXT PRIMARY KEY,
                event_fingerprint TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                candidate_json TEXT NOT NULL DEFAULT '[]',
                decision_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)


def enqueue(conn, event_id, fingerprint):
    timestamp=now()
    conn.execute("INSERT INTO pipeline_arc_links(event_id,event_fingerprint,created_at,updated_at) VALUES (?,?,?,?) "
                 "ON CONFLICT(event_id) DO UPDATE SET event_fingerprint=excluded.event_fingerprint,status='pending',"
                 "attempts=0,candidate_json='[]',decision_json='{}',error='',updated_at=excluded.updated_at "
                 "WHERE pipeline_arc_links.event_fingerprint!=excluded.event_fingerprint",
                 (event_id,fingerprint,timestamp,timestamp))


def _compact(value):
    return re.sub(r"[^\w\u3400-\u9fff]+", "", str(value or "").casefold())


def candidate_arcs(store, event, *, limit=8):
    """Keyword-prefilter active Arc cards without reading either source originals or roll prose."""
    haystack=_compact(str(event.get('title') or '')+'\n'+str(event.get('body_md') or ''))
    if not haystack:return []
    fields=(('title',8),('title_aliases',7),('primary_entities',5),('supporting_entities',4),('query_cues',6),('intent_tags',3))
    rows=[]
    for arc in Narratives(store).list(query='',limit=100).get('items',[]):
        if arc.get('integrity_status')!='ok' or str(arc.get('lifecycle') or 'active')!='active' or not arc.get('arc_key'):
            continue
        matches=[];score=0
        for field,weight in fields:
            values=arc.get(field) if isinstance(arc.get(field),list) else [arc.get(field)]
            for value in values or []:
                term=_compact(value)
                if len(term)>=2 and term in haystack:
                    matches.append(str(value));score+=weight+min(len(term),12)/12
        if matches:
            rows.append({'arc_key':str(arc['arc_key']),'narrative_id':str(arc.get('narrative_id') or ''),
                'title':str(arc.get('title') or ''),'title_aliases':list(arc.get('title_aliases') or []),
                'primary_entities':list(arc.get('primary_entities') or []),
                'supporting_entities':list(arc.get('supporting_entities') or []),
                'query_cues':list(arc.get('query_cues') or []),'intent_tags':list(arc.get('intent_tags') or []),
                'matched_keywords':list(dict.fromkeys(matches))[:8],'match_score':round(score,3)})
    rows.sort(key=lambda row:(-row['match_score'],row['title'],row['arc_key']))
    return rows[:max(1,min(int(limit),12))]


def normalize_decision(output, candidates):
    if not isinstance(output,dict) or set(output)!={'action','arc_key','reason'}:
        raise ValueError('Arc Linker 必须只返回 action、arc_key 和 reason')
    action=str(output.get('action') or '').strip();arc_key=str(output.get('arc_key') or '').strip()
    reason=' '.join(str(output.get('reason') or '').split())[:600]
    allowed={item['arc_key'] for item in candidates}
    if action not in ('attach','unassigned') or not reason:
        raise ValueError('Arc Linker 返回了无效决定')
    if (action=='attach' and arc_key not in allowed) or (action=='unassigned' and arc_key):
        raise ValueError('Arc Linker 返回了候选外的 Arc')
    return {'action':action,'arc_key':arc_key,'reason':reason}


async def _model_decision(settings,event,candidates,invoke_model=None):
    payload={'event':{'event_id':event['id'],'title':event['title'],'body':event['body_md']},'candidate_arcs':candidates}
    if invoke_model is not None:
        return normalize_decision(await invoke_model(payload),candidates)
    from .model_runtime import complete
    model=task_model(settings.database,'arc_linker')
    if not model:raise ValueError('请先选择 Event · Arc 归档模型')
    response=await complete(model,{'messages':[{'role':'system','content':SYSTEM_PROMPT},
        {'role':'user','content':json.dumps(payload,ensure_ascii=False)}],
        'response_format':{'type':'json_object'}})
    content=response['choices'][0]['message']['content']
    fenced=re.fullmatch(r'\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*',content,re.S|re.I)
    return normalize_decision(json.loads(fenced[1] if fenced else content),candidates)


def _finish(database,row,candidates,decision):
    with Store(database) as store,store.transaction(immediate=True):
        queued=store.conn.execute('SELECT * FROM pipeline_arc_links WHERE event_id=?',(row['event_id'],)).fetchone()
        fact=store.conn.execute("SELECT fingerprint,status,item_type FROM fact_events WHERE item_id=?",(row['event_id'],)).fetchone()
        current=store.read(row['event_id'])
        if (not queued or queued['status']!='pending'
                or queued['event_fingerprint']!=row['event_fingerprint']):
            return {'status':'unchanged','event_id':row['event_id']}
        if (not fact or fact['item_type']!='event' or fact['status']!='active' or fact['fingerprint']!=row['event_fingerprint']
                or not current or current['lifecycle']!='active'):
            decision={'action':'unassigned','arc_key':'','reason':'Event 已变化或不再有效'}
        else:
            store.conn.execute('DELETE FROM fact_event_arc_links WHERE event_id=? AND event_fingerprint!=?',
                (row['event_id'],row['event_fingerprint']))
        status='unassigned'
        if decision['action']=='attach':
            existing=[str(item[0]) for item in store.conn.execute(
                'SELECT arc_key FROM fact_event_arc_links WHERE event_id=?',(row['event_id'],))]
            if existing and decision['arc_key'] not in existing:
                decision={'action':'unassigned','arc_key':'','reason':'Event 已归入另一条 Arc'}
            elif not existing:
                arc=Narratives(store).read_by_arc_key(decision['arc_key'])
                if arc.get('status')!='ok' or str(arc.get('lifecycle') or 'active')!='active':
                    raise ValueError('候选 Arc 已变化，请重新判断')
                if row['event_id'] in set(map(str,arc.get('excluded_event_ids') or [])):
                    decision={'action':'unassigned','arc_key':'','reason':'该 Event 已从候选 Arc 明确撤出'}
                else:
                    store.conn.execute('INSERT INTO fact_event_arc_links VALUES (?,?,?,?)',
                        (decision['arc_key'],row['event_id'],row['event_fingerprint'],now()))
                    project_events(store.conn)
            if decision['action']=='attach':status='linked'
        store.conn.execute('UPDATE pipeline_arc_links SET status=?,attempts=attempts+1,candidate_json=?,decision_json=?,error=?,updated_at=? WHERE event_id=?',
            (status,encode(candidates),encode(decision),'',now(),row['event_id']))
        return {'status':status,'event_id':row['event_id'],'arc_key':decision.get('arc_key','')}


async def process(settings, *, invoke_model=None):
    initialize(settings.database)
    with Store(settings.database,read_only=True) as store:
        row=store.conn.execute("SELECT * FROM pipeline_arc_links WHERE status='pending' ORDER BY created_at,event_id LIMIT 1").fetchone()
        if not row:return {'status':'waiting','processed':0}
        event=store.read(row['event_id'])
        if not event:
            candidates=[];decision={'action':'unassigned','arc_key':'','reason':'Event 不存在'}
        else:
            candidates=candidate_arcs(store,event)
            decision=({'action':'unassigned','arc_key':'','reason':'没有命中已有 Arc 的检索词'} if not candidates
                      else await _model_decision(settings,event,candidates,invoke_model))
    return {**_finish(settings.database,dict(row),candidates,decision),'processed':1}


async def run(settings):
    while True:
        try:await process(settings)
        except Exception as error:
            logging.getLogger(__name__).exception('Arc linking failed; Event remains unbound')
            with Store(settings.database) as store:
                row=store.conn.execute("SELECT event_id FROM pipeline_arc_links WHERE status='pending' ORDER BY created_at,event_id LIMIT 1").fetchone()
                if row:store.conn.execute("UPDATE pipeline_arc_links SET status='error',attempts=attempts+1,error=?,updated_at=? WHERE event_id=?",
                    (str(error)[:500],now(),row[0]))
        await asyncio.sleep(15)
