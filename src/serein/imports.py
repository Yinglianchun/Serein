"""Explicit file imports, with durable progress and original-content preservation.

Format/UUID contracts were checked against import_memory.py at ad60c5a.
This adapter uses the public raw archive and Scene store, never the old dehydrator.
"""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .core.store import Store, Conflict, digest, encode, now
from .deployment import identity
from .compat.germany.raw_events import RawEventStore


def timestamp(value, fallback):
    if value in (None,''):return fallback
    if isinstance(value,(int,float)) or (isinstance(value,str) and re.fullmatch(r'\d+(?:\.\d+)?',value)):
        numeric=float(value)
        return datetime.fromtimestamp(numeric/1000 if numeric>100_000_000_000 else numeric,timezone.utc).isoformat()
    parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).isoformat()


def text_content(value):
    if isinstance(value,str):return value
    if isinstance(value,dict):
        if isinstance(value.get('text'),str):return value['text']
        return text_content(value.get('parts',[]))
    if isinstance(value,list):return '\n'.join(part for item in value if (part:=text_content(item)))
    return ''


def markdown_messages(text,names):
    labels={name.lower():'user' for name in ('user','human','me','用户','人类','我',names['user_name'])}
    labels.update({name.lower():'assistant' for name in ('assistant','ai','claude','chatgpt','gpt','deepseek','助手',names['ai_name'])})
    pattern=re.compile(r'^\s*(?:>\s*)?(?:[-*+]\s*)?(?:#{1,6}\s*)?(?:\*\*)?([^:：\n]+?)(?:\*\*)?\s*[:：]\s*(.*)$')
    result=[];role='user';parts=[];fenced=False
    def flush():
        content='\n'.join(parts)
        if content.strip():result.append({'role':role,'content':content})
    for line in text.splitlines():
        if line.lstrip().startswith(('```','~~~')):fenced=not fenced
        match=pattern.match(line) if not fenced else None
        label=match.group(1).strip().strip('*').lower() if match else ''
        if label in labels:
            flush();role=labels[label];parts=[match.group(2).removeprefix('**').lstrip()]
        else:parts.append(line)
    flush()
    return result


def chatgpt_messages(conversation):
    mapping=conversation['mapping']
    if not isinstance(mapping,dict):raise ValueError('ChatGPT mapping 必须是对象')
    head=conversation.get('current_node')
    if not head:
        parents={node.get('parent') for node in mapping.values() if isinstance(node,dict)}
        leaves=[key for key in mapping if key not in parents]
        if len(leaves)!=1:raise ValueError('ChatGPT 有多个分支但没有 current_node，无法确定要导入的对话分支')
        head=leaves[0]
    chain=[];seen=set()
    while head:
        if head in seen or head not in mapping:raise ValueError('ChatGPT 对话分支损坏')
        seen.add(head);node=mapping[head]
        if node.get('message'):chain.append({**node['message'],'id':node['message'].get('id') or head})
        head=node.get('parent')
    return list(reversed(chain))


def parse_file(content,filename,names,mode='auto',stamp=None):
    stamp=stamp or now();source_hash=digest(content);warnings=[]
    extension=Path(filename).suffix.lower()
    stripped=content.lstrip('\ufeff \r\n\t')
    if extension=='.jsonl':
        try:data=[json.loads(line) for line in stripped.splitlines() if line.strip()]
        except ValueError as exc:raise ValueError('JSONL 格式错误：'+str(exc)) from None
    elif extension=='.json' or stripped.startswith(('{','[')):
        try:data=json.loads(stripped)
        except ValueError as exc:raise ValueError('JSON 格式错误：'+str(exc)) from None
    elif extension in ('.md','.txt',''):
        data={'messages':markdown_messages(content,names)}
    else:raise ValueError('支持 .json、.jsonl、.md、.txt 文件')
    is_operit=isinstance(data,dict) and isinstance(data.get('memories'),list) and (
        'exportDate' in data or 'links' in data or any(isinstance(row,dict) and 'uuid' in row for row in data['memories']))
    if mode=='operit' and not is_operit:raise ValueError('文件不是 Operit 记忆备份')
    if is_operit:
        if mode=='conversation':raise ValueError('这是 Operit 记忆库，请选择自动识别或 Operit')
        entries=[]
        for index,row in enumerate(data['memories']):
            if not isinstance(row,dict) or not isinstance(row.get('content'),str) or not row['content'].strip():
                raise ValueError(f'Operit 第 {index+1} 条缺少正文，未开始导入')
            uuid=str(row.get('uuid') or '').strip()
            key='operit_'+digest(uuid or encode([row.get('title'),row['content'],row.get('createdAt')]))[:32]
            created=timestamp(row.get('createdAt'),stamp)
            entries.append({'id':key,'uuid':uuid,'title':str(row.get('title') or '').strip() or row['content'].strip()[:60],
                'body':row['content'],'created_at':created,'updated_at':timestamp(row.get('updatedAt'),created),'original':row})
        links=data.get('links',[])
        if links:warnings.append(f'保留备份中的 {len(links)} 条旧关联信息；它们不会直接变成已审核的 Scene 关系。')
        return {'format':'operit','entries':entries,'warnings':warnings,'links':links,'export_date':data.get('exportDate'),'source_hash':source_hash}
    if isinstance(data,dict) and 'conversations' in data:data=data['conversations']
    if isinstance(data,dict) and ('role' in data or 'sender' in data):data=[data]
    if isinstance(data,list) and data and all(isinstance(row,dict) and ('role' in row or 'sender' in row) for row in data):
        # JSONL may carry multiple sessions; preserve their explicit separation.
        groups={}
        for row in data:groups.setdefault(str(row.get('session_id') or row.get('conversation_id') or 'file'),[]).append(row)
        data=[{'messages':rows,**({'id':key} if key!='file' else {})} for key,rows in groups.items()]
    conversations=data if isinstance(data,list) else [data]
    entries=[];formats=set();skipped=0;missing_times=0
    for conversation in conversations:
        if not isinstance(conversation,dict):raise ValueError('对话条目必须是对象')
        if 'mapping' in conversation:messages=chatgpt_messages(conversation);fmt='chatgpt'
        elif 'chat_messages' in conversation:messages=conversation['chat_messages'];fmt='claude'
        elif 'messages' in conversation:messages=conversation['messages'];fmt='text' if extension in ('.txt','.md') else 'messages'
        elif 'events' in conversation:messages=conversation['events'];fmt='raw'
        else:raise ValueError('无法识别聊天记录，请提供 Claude、ChatGPT、messages/events 或带角色的文本导出')
        if not isinstance(messages,list):raise ValueError('messages 必须是列表')
        formats.add(fmt)
        session=str(conversation.get('uuid') or conversation.get('id') or conversation.get('conversation_id') or
                    conversation.get('session_id') or 'file-'+digest(encode(messages))[:24])
        for index,message in enumerate(messages):
            if not isinstance(message,dict):raise ValueError('消息必须是对象')
            author=message.get('author',{});author=author if isinstance(author,dict) else {}
            role=str(message.get('sender') or message.get('role') or author.get('role') or '').lower()
            role={'human':'user','ai':'assistant','bot':'assistant'}.get(role,role)
            if role not in ('user','assistant'):skipped+=1;continue
            value=message.get('text',message.get('content',''));text=text_content(value)
            if not text.strip():skipped+=1;continue
            original_time=message.get('created_at',message.get('create_time',message.get('timestamp',message.get('time'))))
            if original_time in (None,''):missing_times+=1
            message_id=str(message.get('uuid') or message.get('id') or message.get('source_event_id') or message.get('message_id') or f'position-{index}')
            entries.append({'source':'import-'+fmt,'source_event_id':digest(encode([session,message_id])),
                'session_id':session,'conversation_id':session,'role':role,'text':text,
                'created_at':timestamp(original_time,stamp),'metadata':{'original_message_id':message_id,
                    'original_timestamp':original_time,'timestamp_source':'export' if original_time not in (None,'') else 'import_time',
                    'original_message':message,'conversation_title':conversation.get('name',conversation.get('title','')),
                    'source_file':Path(filename).name}})
    if skipped:warnings.append(f'{skipped} 条系统、工具或无文字消息未作为对话导入；文字消息中的原始结构保留在元数据里。')
    if missing_times:warnings.append(f'{missing_times} 条消息没有时间，使用首次导入时间归档，并标注“原始时间未知”。')
    return {'format':'+'.join(sorted(formats)),'entries':entries,'warnings':warnings,'source_hash':source_hash}


class ImportArchive(RawEventStore):
    def _normalize_event(self,raw,*,default_source,ingested_at):
        event,error=super()._normalize_event(raw,default_source=default_source,ingested_at=ingested_at)
        if event:
            # Retain ordinary whitespace, but preserve the archive's existing
            # removal of injected client context. The exact export is metadata.
            if event['text']==raw['text'].strip():event['text']=raw['text']
            event['event_hash']=self._event_hash(**{key:event[key] for key in
                ('source','source_event_id','role','text','created_at','conversation_id','session_id')})
        return event,error


def initialize_imports(database):
    with Store(database) as store:
        store.conn.executescript('''
            CREATE TABLE IF NOT EXISTS file_imports (id TEXT PRIMARY KEY,filename TEXT NOT NULL,format TEXT NOT NULL,
                payload_json TEXT NOT NULL,cursor INTEGER NOT NULL DEFAULT 0,inserted INTEGER NOT NULL DEFAULT 0,
                duplicate INTEGER NOT NULL DEFAULT 0,errors_json TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL,
                tagging INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS import_tag_jobs (document_id TEXT PRIMARY KEY,body_hash TEXT NOT NULL,upload_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS tagging_outbox (
                document_id TEXT PRIMARY KEY
            );
            CREATE TRIGGER IF NOT EXISTS tagging_revision_insert AFTER INSERT ON revisions BEGIN
                INSERT OR IGNORE INTO tagging_outbox(document_id)
                SELECT NEW.document_id WHERE EXISTS (
                    SELECT 1 FROM documents WHERE id=NEW.document_id AND kind IN ('event','scene'));
            END;
            CREATE TRIGGER IF NOT EXISTS tagging_binding_insert AFTER INSERT ON evidence_bindings BEGIN
                INSERT OR IGNORE INTO tagging_outbox(document_id)
                SELECT NEW.document_id WHERE EXISTS (
                    SELECT 1 FROM documents WHERE id=NEW.document_id AND kind IN ('event','scene'));
            END;
            CREATE TRIGGER IF NOT EXISTS tagging_binding_update AFTER UPDATE ON evidence_bindings BEGIN
                INSERT OR IGNORE INTO tagging_outbox(document_id)
                SELECT NEW.document_id WHERE EXISTS (
                    SELECT 1 FROM documents WHERE id=NEW.document_id AND kind IN ('event','scene'));
            END;
            CREATE TRIGGER IF NOT EXISTS tagging_binding_delete AFTER DELETE ON evidence_bindings BEGIN
                INSERT OR IGNORE INTO tagging_outbox(document_id)
                SELECT OLD.document_id WHERE EXISTS (
                    SELECT 1 FROM documents WHERE id=OLD.document_id AND kind IN ('event','scene'));
            END;
            CREATE TRIGGER IF NOT EXISTS tagging_document_lifecycle AFTER UPDATE OF lifecycle ON documents
            WHEN NEW.kind IN ('event','scene') BEGIN
                INSERT OR IGNORE INTO tagging_outbox(document_id) VALUES (NEW.id);
            END;
        ''')
        marker=store.conn.execute("SELECT 1 FROM background_state WHERE name='tagging_outbox_seeded_v1'").fetchone()
        if not marker:
            store.conn.execute("INSERT OR IGNORE INTO tagging_outbox(document_id) "
                "SELECT id FROM documents WHERE kind IN ('event','scene') AND lifecycle='active'")
            store.conn.execute("INSERT INTO background_state(name,value_json) VALUES ('tagging_outbox_seeded_v1','{\"status\":\"done\"}')")
        archive_imported_originals(store.conn)


def archive_imported_originals(conn, upload_id=None):
    """Keep imported-history exclusion as a compact, reversible upload boundary."""
    conn.execute('CREATE TABLE IF NOT EXISTS raw_processing(raw_id INTEGER PRIMARY KEY,operation_id TEXT NOT NULL,outcome TEXT NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS pipeline_import_boundaries('
                 'upload_id TEXT PRIMARY KEY,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,'
                 'released INTEGER NOT NULL DEFAULT 0)')
    columns={row[1] for row in conn.execute('PRAGMA table_info(pipeline_import_boundaries)')}
    if 'released' not in columns:
        conn.execute('ALTER TABLE pipeline_import_boundaries ADD COLUMN released INTEGER NOT NULL DEFAULT 0')
    tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'file_imports','raw_events'}<=tables:return
    # Migrate the old per-message markers before removing them. One upload boundary
    # replaces hundreds or thousands of archived_only rows and can later be released
    # without rewriting the raw archive.
    conn.execute("""INSERT OR IGNORE INTO pipeline_import_boundaries(upload_id)
        SELECT DISTINCT substr(operation_id,13) FROM raw_processing
        WHERE outcome='archived_only' AND operation_id LIKE 'file-import:%'""")
    conn.execute("""DELETE FROM raw_processing
        WHERE outcome='archived_only' AND operation_id LIKE 'file-import:%'""")
    if upload_id:
        conn.execute('INSERT OR IGNORE INTO pipeline_import_boundaries(upload_id,released) VALUES (?,0)',
                     (str(upload_id),))


def release_imported_originals(database, upload_id):
    """Opt one completed conversation import back into automatic Event processing."""
    identifier=str(upload_id or '').strip()
    if not identifier:
        raise ValueError('导入任务 ID 不能为空')
    initialize_imports(database)
    with Store(database) as store,store.transaction(immediate=True):
        row=store.conn.execute("SELECT id,format,cursor,json_array_length(payload_json,'$.entries') total FROM file_imports WHERE id=?",(identifier,)).fetchone()
        if row is None:
            raise ValueError('找不到导入任务')
        if row['format']=='operit':
            raise ValueError('Operit 记忆导入不属于原话 Event 整理')
        if int(row['cursor'])!=int(row['total']):
            raise ValueError('请先完成这份聊天记录的导入')
        originals=store.conn.execute("SELECT count(*) FROM raw_events WHERE json_extract(metadata_json,'$.import_upload_id')=?",(identifier,)).fetchone()[0]
        released=store.conn.execute('UPDATE pipeline_import_boundaries SET released=1 WHERE upload_id=? AND released=0',(identifier,)).rowcount
    return {'status':'released' if released else 'unchanged','upload_id':identifier,
            'originals':int(originals or 0),'released':bool(released)}


def report(row):
    data=json.loads(row['payload_json']);total=len(data['entries']);cursor=row['cursor'];errors=json.loads(row['errors_json'])
    return {'id':row['id'],'filename':row['filename'],'format':row['format'],'total':total,'processed':cursor,
        'inserted':row['inserted'],'duplicate':row['duplicate'],'failed':len(errors),'errors':errors[:20],
        'status':'completed' if cursor==total else 'ready' if not cursor else 'paused',
        'warnings':data['warnings'],'tagging':bool(row['tagging']),
        'sessions':len({item['session_id'] for item in data['entries']}) if row['format']!='operit' else 0,
        'preview':[{'role':item.get('role'),'title':item.get('title'),
                    'text':item.get('text',item.get('body',''))[:500]} for item in data['entries'][:3]]}


def stage(database,content,filename,mode,tagging):
    initialize_imports(database)
    key='upload:'+digest(content)
    with Store(database) as store:
        row=store.conn.execute('SELECT * FROM file_imports WHERE id=?',(key,)).fetchone()
    if row:
        if (mode=='operit' and row['format']!='operit') or (mode=='conversation' and row['format']=='operit'):
            raise ValueError('已识别的文件格式与所选类型不同')
        return report(row)
    data=parse_file(content,filename,identity(database),mode)
    if not data['entries']:raise ValueError('文件中没有可导入的文字条目')
    with Store(database) as store,store.transaction(immediate=True):
        store.conn.execute('INSERT OR IGNORE INTO file_imports (id,filename,format,payload_json,created_at,tagging) VALUES (?,?,?,?,?,?)',
            (key,Path(filename).name,data['format'],encode(data),now(),int(tagging and data['format']=='operit')))
        return report(store.conn.execute('SELECT * FROM file_imports WHERE id=?',(key,)).fetchone())


def import_scene(database,item,upload,data):
    with Store(database) as store,store.transaction(immediate=True):
        old=store.read(item['id'])
        if old:
            if old['body_md']!=item['body']:raise Conflict('同一 Operit UUID 的正文不同，未覆盖现有记忆')
            return 'duplicate'
        meta={'object_kind':'scene','memory_value_source':'imported_original','import_format':'operit','operit_uuid':item['uuid'],
            'operit_original':item['original'],'import_source_hash':data['source_hash'],'import_source_file':upload['filename'],
            'operit_export_date':data.get('export_date'),'canonical_domain':'general','domain':['general'],
            'scene_cues':[],
            'date':datetime.fromisoformat(item['created_at']).astimezone(timezone(timedelta(hours=8))).date().isoformat(),
            'scene_status':'active','active':True}
        store.create(item['id'],'scene',item['title'],item['body'],metadata=meta,
            created_at=item['created_at'],updated_at=item['updated_at'])
        source=store.add_source(encode(['operit_memory',item['uuid'] or item['id']]),item['body'],
            metadata={'type':'operit_memory','item_id':item['uuid'],'source_file':upload['filename'],
                      'source_system':'operit','message_id':item['uuid'] or item['id'],'session_id':'',
                      'created_at':item['created_at'],'original':item['original']})
        store.bind(item['id'],source,actor='import')
        from .compat.scenes import map_evidence_ids
        map_evidence_ids(store)
        store.conn.execute('INSERT INTO index_outbox(document_id) VALUES (?)',(item['id'],))
        if upload['tagging']:store.conn.execute('INSERT OR IGNORE INTO import_tag_jobs(document_id,body_hash,upload_id) VALUES (?,?,?)',
            (item['id'],digest(item['body']),upload['id']))
    return 'inserted'


def advance_import(settings,identifier):
    initialize_imports(settings.database)
    with Store(settings.database,read_only=True) as store:
        row=store.conn.execute('SELECT * FROM file_imports WHERE id=?',(identifier,)).fetchone()
    if row is None:raise ValueError('找不到导入任务')
    data=json.loads(row['payload_json']);start=row['cursor'];errors=json.loads(row['errors_json']);counts={'inserted':0,'duplicate':0}
    archive=ImportArchive({'raw_events':{'db_path':str(settings.database)}}) if row['format']!='operit' else None
    batch=data['entries'][start:start+25]
    for offset,item in enumerate(batch):
        try:
            if archive:
                item={**item,'metadata':{**item['metadata'],'import_upload_id':identifier}}
                with Store(settings.database,read_only=True) as store:
                    old=store.conn.execute('SELECT text,role,created_at FROM raw_events WHERE source=? AND source_event_id=?',
                        (item['source'],item['source_event_id'])).fetchone()
                normalized,error=archive._normalize_event(item,default_source=item['source'],ingested_at=now())
                if normalized is None:raise ValueError('原话不符合归档要求：'+error)
                if old and (old['text']!=normalized['text'] or old['role']!=item['role']):
                    raise Conflict('同一原始消息 ID 的内容不同，未覆盖已存原话')
                if old:status='duplicate'
                else:
                    result=archive.ingest([item],source=item['source'])
                    if result['rejected']:raise ValueError('原话不符合归档要求')
                    status='inserted' if result['inserted'] else 'duplicate'
            else:
                status=import_scene(settings.database,item,row,data)
                from .recall.passage_layouts import prepare_layouts
                prepare_layouts(settings, [item['id']])
            counts[status]+=1
        except (ValueError,KeyError) as exc:errors.append({'entry':start+offset+1,'message':str(exc)[:250]})
    with Store(settings.database) as store,store.transaction(immediate=True):
        archive_imported_originals(store.conn,identifier)
        store.conn.execute('UPDATE file_imports SET cursor=?,inserted=inserted+?,duplicate=duplicate+?,errors_json=? WHERE id=? AND cursor=?',
            (start+len(batch),counts['inserted'],counts['duplicate'],encode(errors),identifier,start))
        return report(store.conn.execute('SELECT * FROM file_imports WHERE id=?',(identifier,)).fetchone())


def list_imports(database):
    initialize_imports(database)
    with Store(database,read_only=True) as store:
        return [report(row) for row in store.conn.execute('SELECT * FROM file_imports ORDER BY created_at DESC,rowid DESC LIMIT 10')]
