import asyncio
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

from ..core.store import Store, digest, encode
from ..deployment import read_settings, save_settings, task_model
from ..tagging_entities import snapshot, prompt_materials, validate, VERSION
from ..import_tagging import TAGGING_PROMPT, tagging_output, tagging_failure
from ..model_runtime import complete, non_thinking_options

from ..tagging_cues import CUE_PROMPT, validate_cues

MIGRATION_TAGGING_PROMPT=TAGGING_PROMPT.replace('不改正文、不生成经历或召回 cue。','不改正文、不生成经历。')+'\n'+CUE_PROMPT


def migration_options(options):
    value={**options,'generate_cues':options.get('generate_cues',True)}
    if type(value['generate_cues']) is not bool:raise ValueError('generate_cues 必须为布尔值')
    return value

class MigrationPaused(Exception):pass


def checkpoint(stage,completed,total):
    from ..work_tasks import ACTIVE,progress,status
    active=ACTIVE.get()
    if not active:return
    progress(stage=stage,completed=completed,total=total)
    if status(active[0],active[1]).get('stop_requested'):raise MigrationPaused()


class Migration:
    def __init__(self,settings,plan,options):
        options=migration_options(options)
        self.settings=settings;self.plan=plan;self.options=options
        self.root=settings.database.parent/'migrations'/plan['fingerprint'][:24]
        self.root.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.root/'ledger.sqlite',isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''CREATE TABLE IF NOT EXISTS jobs(key TEXT PRIMARY KEY,stage TEXT,status TEXT,result TEXT,attempts INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY,value TEXT);''')
        registry=settings.database.parent/'migrations'/'sources.json'
        registered=json.loads(registry.read_text('utf-8')) if registry.exists() else {}
        origin=str(options.get('source_path') or Path(plan['root']).resolve())
        if origin in registered and registered[origin]!=plan['fingerprint']:
            self.db.close()
            raise ValueError('源库与先前扫描内容不同，已停止以避免重复导入；请使用原快照续跑')
        registered[origin]=plan['fingerprint']
        temporary=registry.with_suffix('.tmp');temporary.write_text(encode(registered),'utf-8');temporary.replace(registry)
        old=self.db.execute("SELECT value FROM config WHERE key='options'").fetchone()
        if old and {k:v for k,v in json.loads(old[0]).items() if k!='generate_cues'}!={k:v for k,v in options.items() if k!='generate_cues'}:
            self.db.close()
            raise ValueError('此次名字／别名与暂停前不同，请使用原配置续跑')
        self.db.execute("INSERT OR REPLACE INTO config VALUES ('options',?)",(encode(options),))
        self.ids={item['old_id']:'scene_legacy_'+digest(plan['fingerprint']+item['old_id'])[:24] for item in plan['items'] if item['kind']=='scene'}
        (self.root/'id-map.json').write_text(encode(self.ids),'utf-8')
        (self.root/'scan-report.json').write_text(encode({'summary':plan['summary'],'skipped':plan.get('skipped',[])}),'utf-8')

    def close(self):self.db.close()

    def freeze_configuration(self):
        state=read_settings(self.settings.database)
        # Keys may be renewed after an interruption; model inputs and domains must stay stable.
        value={'domains':state['tagging']['domains'],'models':{task:{k:v for k,v in (task_model(self.settings.database,task) or {}).items() if k!='api_key'}
            for task in ('operit_tagging','embedding','reranker')}}
        old=self.db.execute("SELECT value FROM config WHERE key='processing'").fetchone()
        if old and json.loads(old[0])!=value:raise ValueError('续跑时请保留已确认的主域及模型连接；可更新密钥，不能混用不同提取或向量配置')
        self.db.execute("INSERT OR IGNORE INTO config VALUES ('processing',?)",(encode(value),))

    def done(self,key,stage):
        row=self.db.execute('SELECT status FROM jobs WHERE key=? AND stage=?',(stage+':'+key,stage)).fetchone()
        return row and row[0] in ('done','skipped','empty')

    def mark(self,key,stage,status,result):
        self.db.execute('INSERT INTO jobs(key,stage,status,result,attempts) VALUES (?,?,?,?,1) '
            'ON CONFLICT(key) DO UPDATE SET status=excluded.status,result=excluded.result,attempts=attempts+1',
            (stage+':'+key,stage,status,encode(result)))

    def backup(self):
        path=self.root/'before-import.db'
        if path.exists():return
        temporary=path.with_suffix('.pending')
        with closing(sqlite3.connect(self.settings.database)) as src,closing(sqlite3.connect(temporary)) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('迁移前备份未通过完整性检查')
        temporary.replace(path)

    def import_bodies(self):
        from ..compat.diaries import Diaries
        diaries=Diaries(self.settings.database,initialize=True)
        for position,item in enumerate(self.plan['items']):
            checkpoint('bodies',position,len(self.plan['items']))
            old_id=item['old_id']
            if self.done(old_id,'body'):continue
            if item['kind']=='diary':
                day=item.get('date') or item['created'][:10]
                from datetime import date
                try:date.fromisoformat(day)
                except ValueError:day=''
                result=diaries.create(content=item['body'],title=item['title'],date=day,author='ai',
                    source_id='ombre-legacy:'+digest(self.plan['fingerprint']+old_id),preserve_content=True)
                self.mark(old_id,'body','done',{'kind':'diary'})
                continue
            key=self.ids[old_id]
            with Store(self.settings.database) as store,store.transaction(immediate=True):
                doc=store.read(key)
                if doc:
                    if doc['body_md']!=item['body'] or doc['title']!=item['title']:
                        raise ValueError('已导入的正文发生编辑，未覆盖：'+old_id)
                    if not (doc['metadata'].get('legacy_tagging_completed') or doc['metadata'].get('legacy_cues_rebuilt')) and not doc['metadata'].get('legacy_tagging_pending'):
                        store.revise(key,expected_revision=doc['revision'],title=doc['title'],body_md=doc['body_md'],
                            metadata={**doc['metadata'],'legacy_tagging_pending':True})
                else:
                    meta={'object_kind':'scene','memory_value_source':'authored_scene','write_contract':'legacy-ombre-scene-v1',
                        'import_format':'ombre-legacy','legacy_id':old_id,'import_source_hash':item['source_hash'],'legacy_tagging_pending':True,
                        'canonical_domain':'general','domain':['general'],'scene_cues':[],
                        'date':item.get('date',''),'created':item.get('legacy_created','')}
                    store.create(key,'scene',item['title'],item['body'],metadata=meta,
                        lifecycle='archived' if item['archived'] else 'active',manual_surface=not item['archived'],
                        created_at=item.get('legacy_created') or None)
                    store.conn.execute('INSERT INTO index_outbox(document_id) VALUES (?)',(key,))
            from ..recall.passage_layouts import prepare_layouts
            prepare_layouts(self.settings, [key])
            self.mark(old_id,'body','done',{'kind':'scene','id':key})
        from .comments import import_comments
        self.mark('all','comments','done',import_comments(self.settings.database,self.plan))

    def import_history(self):
        from .history import scan_history, import_history
        if 'history' not in self.plan:
            raise ValueError('此预览来自旧版本，请重新预览同一备份以补入历史数据')
        source=scan_history(self.plan['root'])
        if source['fingerprint']!=self.plan['history']['fingerprint']:
            raise ValueError('扫描后历史数据发生变化，请停止旧服务并重新扫描')
        result=import_history(self.settings.database,source,self.plan['fingerprint'])
        self.mark('all','history','done',result)
        from .originals import scan_originals, import_originals
        if 'originals' not in self.plan:raise ValueError('请重新预览同一备份以检查旧原文库与日期')
        originals=scan_originals(self.plan['root'])
        if originals['fingerprint']!=self.plan['originals']['fingerprint']:raise ValueError('扫描后旧原文库发生变化，请重新预览')
        raw_result=import_originals(self.settings.database,originals)
        self.mark('all','originals','done',raw_result)
        from .dates import repair_dates
        self.mark('all','dates','done',repair_dates(self.settings.database,self.plan))
        return result

    def import_companion(self):
        from .companion import scan_companion, import_companion
        source=scan_companion(self.plan['root'])
        if source['fingerprint']!=self.plan['companion']['fingerprint']:
            raise ValueError('扫描后旧 Persona／备忘发生变化，请停止旧服务并重新扫描')
        snapshot=self.root/'companion-source.json'
        if snapshot.exists() and json.loads(snapshot.read_text('utf-8'))['fingerprint']!=source['fingerprint']:
            raise ValueError('旧 Persona／备忘来源已改变，请使用原快照续跑')
        temporary=snapshot.with_suffix('.pending')
        temporary.write_text(encode(source),'utf-8');temporary.replace(snapshot)
        result=import_companion(self.settings.database,source,
            str(self.options.get('source_path') or Path(self.plan['root']).resolve()))
        self.mark('all','companion','done',result)
        return result

    async def tag(self,item,feedback=''):
        if self.db.execute("SELECT 1 FROM config WHERE key='processing'").fetchone():self.freeze_configuration()
        key=self.ids[item['old_id']]
        state=read_settings(self.settings.database);catalog=state['tagging']['domains']
        model=task_model(self.settings.database,'operit_tagging')
        if not model:raise ValueError('尚未配置打标模型')
        with Store(self.settings.database,read_only=True) as store:
            doc=store.read(key);materials,stamp=snapshot(store,doc)
            if (doc['metadata'].get('legacy_tagging_completed') or doc['metadata'].get('legacy_cues_rebuilt')) and doc['metadata'].get('entity_input_hash')==stamp:return
        sent=prompt_materials(materials)
        generate_cues=self.options['generate_cues']
        names=[self.options['user_name'],self.options['ai_name'],*self.options.get('aliases',[])]
        if not generate_cues and feedback.startswith(('cue','缺少 cues')):feedback=''
        response=await complete(model,{'messages':[{'role':'system','content':MIGRATION_TAGGING_PROMPT if generate_cues else TAGGING_PROMPT},
            {'role':'user','content':encode({'title':doc['title'],'content':doc['body_md'],'kind':'scene',
                'domains':catalog,'materials':sent,**({'forbidden_names':names} if generate_cues else {}),
                **({'validation_feedback':'上次结果未通过校验：'+feedback+'。请修正后重新返回完整 JSON。'+('cues 不得包含禁用名字，正文与实体仍按原文处理。' if generate_cues else '')} if feedback else {})})}],
            'response_format':{'type':'json_object'},
            **non_thinking_options(model)})
        output=tagging_output(response)
        domain=output.get('domain')
        valid_domain=domain is None or isinstance(domain,str) and domain in {d['key'] for d in catalog}
        entities,rejected=validate(output.get('entities'),sent)
        cues=validate_cues(output.get('cues'),names) if generate_cues else None
        with Store(self.settings.database) as store,store.transaction(immediate=True):
            current=store.read(key)
            if current['revision']!=doc['revision'] or snapshot(store,current)[1]!=stamp:raise ValueError('提取期间记忆已改变')
            meta={**current['metadata'],'legacy_tagging_completed':True,'legacy_generate_cues':generate_cues,
                'legacy_tagging_pending':False,
                'tagged_entities':entities,'entity_input_hash':stamp,'entity_extraction_version':VERSION,
                'entity_rejected_count':rejected,'operit_tagging_status':'done','operit_tagging_model':model['model']}
            if generate_cues:meta.update(scene_cues=cues,legacy_cues_rebuilt=True)
            if valid_domain:meta.update(canonical_domain=domain or 'general',domain=[domain] if domain else [])
            store.revise(key,expected_revision=current['revision'],title=current['title'],body_md=current['body_md'],metadata=meta)
            store.conn.execute('INSERT INTO index_outbox(document_id) VALUES (?)',(key,))

    async def tag_all(self):
        for position,item in enumerate(self.plan['items'],1):
            checkpoint('tagging',position-1,len(self.plan['items']))
            if item['kind']!='scene' or self.done(item['old_id'],'tag'):continue
            if item['archived']:
                self.mark(item['old_id'],'tag','skipped',{'reason':'archived'});continue
            print(f"{'打标／实体／cues' if self.options['generate_cues'] else '打标／实体'} {position}/{len(self.plan['items'])}",flush=True)
            previous=self.db.execute('SELECT result FROM jobs WHERE key=?',('tag:'+item['old_id'],)).fetchone()
            feedback=json.loads(previous[0]).get('reason','') if previous else ''
            try:
                await self.tag(item,feedback);self.mark(item['old_id'],'tag','done',{'generate_cues':self.options['generate_cues']})
            except Exception as exc:
                feedback=tagging_failure(exc)
                self.mark(item['old_id'],'tag','failed',{'error':type(exc).__name__,'reason':feedback})
                raise ValueError('打标失败（'+feedback+'），本次请求可能已计费，已停止自动重试；进度已保存，可续跑：'+item['old_id']) from None

    async def edges(self):
        from ..compat.background import SceneReader
        from .edges import save_edge, VERSION, initialize_edges
        from .repair import backup
        if not (self.root/'before-edge-repair.db').exists():backup(self.settings.database,self.root)
        initialize_edges(self.settings.database)
        reader=SceneReader(self.settings.database)
        for position,old in enumerate(self.plan['edges'],1):
            checkpoint('edges',position-1,len(self.plan['edges']))
            edge_key=digest(encode(old))
            previous=self.db.execute('SELECT result FROM jobs WHERE key=?',('edge:'+edge_key,)).fetchone()
            if previous and json.loads(previous[0]).get('version')==VERSION and self.done(edge_key,'edge'):continue
            left=self.ids.get(str(old['source']));right=self.ids.get(str(old['target']))
            with Store(self.settings.database,read_only=True) as store:
                docs=[store.read(key) if key else None for key in (left,right)]
            policies={d['key']:d['policy'] for d in read_settings(self.settings.database)['tagging']['domains']}
            error=''
            if any(not d or d['lifecycle']!='active' or d['manual_surface']==0 or policies.get(d['metadata'].get('canonical_domain'))=='excluded' for d in docs):
                error='缺端点、归档、不浮现或排除主域'
            print(f"旧边转换 {position}/{len(self.plan['edges'])}",flush=True)
            try:
                saved=save_edge(self.settings.database,await reader.get(left) if docs[0] else None,
                                await reader.get(right) if docs[1] else None,old,endpoint_reason=error)
                self.mark(edge_key,'edge',saved['status'],saved)
            except Exception as exc:
                self.mark(edge_key,'edge','failed',{'error':type(exc).__name__})
                raise ValueError('关系程序转换失败，进度已保存，可续跑') from None

    def report(self):
        result={stage:dict(Counter(r['status'] for r in self.db.execute('SELECT status FROM jobs WHERE stage=?',(stage,)))) for stage in ('body','history','originals','dates','comments','companion','tag','edge','vectors','cue_bindings')}
        result['details']={r['stage']:json.loads(r['result']) for r in self.db.execute("SELECT stage,result FROM jobs WHERE stage IN ('history','originals','dates','comments','companion','vectors','cue_bindings')")}
        result['edge_records']=[json.loads(r[0]) for r in self.db.execute("SELECT result FROM jobs WHERE stage='edge'")]
        (self.root/'report.json').write_text(encode(result),'utf-8')
        return result
