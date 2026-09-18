"""Explicit, checkpointed cue repair for already imported Ombre Scenes."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
from uuid import uuid4

from ..core.domains import canonical_domain
from ..core.store import Store, encode
from ..deployment import identity, read_settings, task_model
from ..import_tagging import TAGGING_PROMPT, tagging_failure, tagging_output
from ..model_runtime import complete, non_thinking_options
from ..tagging_cues import CUE_PROMPT, validate_cues
from ..tagging_entities import VERSION, prompt_materials, snapshot, validate
from .workflow import checkpoint


FULL_PROMPT=TAGGING_PROMPT.replace('不改正文、不生成经历或召回 cue。','不改正文、不生成经历。')+'\n'+CUE_PROMPT
CUE_ONLY_PROMPT='只返回 JSON {"cues":[]}，不要返回解释或其他字段。\n'+CUE_PROMPT


def _names_by_document(database):
    result={}
    root=Path(database).parent/'migrations'
    if not root.is_dir():return result
    for mapping in root.glob('*/id-map.json'):
        ledger=mapping.parent/'ledger.sqlite'
        if not ledger.is_file():continue
        try:
            ids=json.loads(mapping.read_text('utf-8'))
            with closing(sqlite3.connect(ledger.resolve().as_uri()+'?mode=ro',uri=True)) as conn:
                row=conn.execute("SELECT value FROM config WHERE key='options'").fetchone()
            options=json.loads(row[0]) if row else {}
            names=[options.get('user_name',''),options.get('ai_name',''),*(options.get('aliases') or [])]
            for document_id in ids.values():result[document_id]=[name for name in names if name]
        except (OSError,ValueError,sqlite3.Error,TypeError):
            continue
    return result


def collect(database):
    mapped_names=_names_by_document(database)
    current_identity=identity(database)
    fallback=[current_identity.get('user_name',''),current_identity.get('ai_name',''),
              *(current_identity.get('user_aliases') or [])]
    fallback=[name for name in fallback if isinstance(name,str) and name]
    items=[]
    with Store(database,read_only=True) as store:
        for row in store.conn.execute("SELECT id FROM documents WHERE kind='scene' AND lifecycle='active' ORDER BY id"):
            doc=store.read(row[0]);meta=doc['metadata']
            if meta.get('import_format')!='ombre-legacy':continue
            if meta.get('scene_cues') or meta.get('legacy_cues_rebuilt'):continue
            _,stamp=snapshot(store,doc)
            items.append({'document_id':doc['id'],'revision':doc['revision'],'stamp':stamp,
                'mode':'full_tagging' if meta.get('legacy_tagging_pending') or not meta.get('legacy_tagging_completed') else 'cues_only',
                'forbidden_names':mapped_names.get(doc['id'],fallback)})
    return items


def preview(database):
    items=collect(database)
    return {'candidates':len(items),'full_tagging':sum(item['mode']=='full_tagging' for item in items),
            'cues_only':sum(item['mode']=='cues_only' for item in items),
            'document_ids':[item['document_id'] for item in items[:20]]}


def _backup(database,directory):
    directory.mkdir(parents=True,exist_ok=False)
    target=directory/'before-cue-repair.db';temporary=target.with_suffix('.pending')
    with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True)) as src, \
            closing(sqlite3.connect(temporary)) as dst:
        src.backup(dst)
        if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('补 cues 前备份未通过完整性检查')
    temporary.replace(target)
    return target


def _ledger(database):
    path=Path(database).parent/'migrations'/'cue-repair.sqlite'
    conn=sqlite3.connect(path,isolation_level=None);conn.row_factory=sqlite3.Row
    conn.execute('''CREATE TABLE IF NOT EXISTS jobs(
        document_id TEXT PRIMARY KEY,stamp TEXT NOT NULL,mode TEXT NOT NULL,status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '')''')
    return conn


async def run(settings):
    model=task_model(settings.database,'operit_tagging')
    if not model:raise ValueError('尚未配置打标模型，不能补 cues')
    catalog=read_settings(settings.database)['tagging']['domains']
    items=collect(settings.database)
    directory=Path(settings.database).parent/'migrations'/'cue-repair'/uuid4().hex
    saved_backup=_backup(settings.database,directory)
    report={'backup':str(saved_backup),'total':len(items),'completed':0,'full_tagging':0,'cues_only':0,'empty':0}
    report_path=directory/'report.json'
    ledger=_ledger(settings.database)
    try:
        for position,item in enumerate(items):
            checkpoint('cue_repair',position,len(items))
            previous=ledger.execute('SELECT * FROM jobs WHERE document_id=?',(item['document_id'],)).fetchone()
            feedback=previous['error'] if previous and previous['stamp']==item['stamp'] else ''
            with Store(settings.database,read_only=True) as store:
                doc=store.read(item['document_id']);materials,current_stamp=snapshot(store,doc)
            if not doc or doc['revision']!=item['revision'] or current_stamp!=item['stamp']:
                raise ValueError('待补 cues 的 Scene 已改变，请重新预览')
            sent=prompt_materials(materials)
            payload={'title':doc['title'],'content':doc['body_md'],'kind':'scene',
                     'forbidden_names':item['forbidden_names']}
            if item['mode']=='full_tagging':payload.update(domains=catalog,materials=sent)
            if feedback:payload['validation_feedback']='上次结果未通过校验：'+feedback+'。请修正后重新返回完整 JSON。'
            try:
                response=await complete(model,{'messages':[
                    {'role':'system','content':FULL_PROMPT if item['mode']=='full_tagging' else CUE_ONLY_PROMPT},
                    {'role':'user','content':encode(payload)}],
                    'response_format':{'type':'json_object'},
                    **non_thinking_options(model)})
                output=tagging_output(response)
                cues=validate_cues(output.get('cues'),item['forbidden_names'])
                entities=[];rejected=0;domain=None;valid_domain=False
                if item['mode']=='full_tagging':
                    domain=output.get('domain')
                    valid_domain='domain' in output and (domain is None or isinstance(domain,str) and domain in {d['key'] for d in catalog})
                    entities,rejected=validate(output.get('entities'),sent)
                with Store(settings.database) as store,store.transaction(immediate=True):
                    current=store.read(doc['id'])
                    if (not current or current['revision']!=doc['revision'] or snapshot(store,current)[1]!=item['stamp']
                            or current['metadata'].get('scene_cues')):
                        raise ValueError('模型处理期间 Scene 已改变，未覆盖')
                    meta={**current['metadata'],'scene_cues':cues,'legacy_cues_rebuilt':True,'legacy_generate_cues':True}
                    if item['mode']=='full_tagging':
                        meta.update(legacy_tagging_completed=True,legacy_tagging_pending=False,
                            tagged_entities=entities,entity_input_hash=item['stamp'],entity_extraction_version=VERSION,
                            entity_rejected_count=rejected,operit_tagging_status='done',operit_tagging_model=model['model'])
                        if valid_domain and canonical_domain(current['metadata'])=='general':
                            meta.update(canonical_domain=domain or 'general',domain=[domain] if domain else [])
                    store.revise(doc['id'],expected_revision=current['revision'],title=current['title'],body_md=current['body_md'],metadata=meta)
                    store.conn.execute('INSERT INTO index_outbox(document_id) VALUES (?)',(doc['id'],))
                ledger.execute("INSERT INTO jobs VALUES (?,?,?,'done',1,'') ON CONFLICT(document_id) DO UPDATE SET stamp=excluded.stamp,mode=excluded.mode,status='done',attempts=jobs.attempts+1,error=''",
                               (doc['id'],item['stamp'],item['mode']))
            except Exception as exc:
                error=tagging_failure(exc)
                ledger.execute("INSERT INTO jobs VALUES (?,?,?,'failed',1,?) ON CONFLICT(document_id) DO UPDATE SET stamp=excluded.stamp,mode=excluded.mode,status='failed',attempts=jobs.attempts+1,error=excluded.error",
                               (doc['id'],item['stamp'],item['mode'],error))
                raise ValueError('补 cues 失败（'+error+'），本次请求可能已计费；已停止自动重试，下次从此条继续：'+doc['id']) from None
            report['completed']+=1;report[item['mode']]+=1
            if not cues:report['empty']+=1
            report_path.write_text(encode(report),'utf-8')
            print(f"补 cues {position+1}/{len(items)}",flush=True)
        checkpoint('cue_repair',len(items),len(items))
    finally:
        report_path.write_text(encode(report),'utf-8');ledger.close()
    return {**report,'report':str(report_path)}
