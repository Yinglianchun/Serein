"""Scene writes and frontend projections over canonical SQLite documents."""

import json
import re
from datetime import date as Date
from uuid import uuid4

from ..core.reader import Reader
from ..core.domains import normalize_domain
from ..core.store import Store, Conflict, digest, encode, now
from ..core.writer import Writer
from .germany.evidence import normalize_evidence_ref


def initialize_scene_ids(database):
    with Store(database) as store, store.transaction():
        store.conn.execute('CREATE TABLE IF NOT EXISTS scene_evidence_ids(id INTEGER PRIMARY KEY AUTOINCREMENT,binding_id TEXT NOT NULL UNIQUE)')
        for row in store.conn.execute("SELECT b.id,b.metadata_json FROM evidence_bindings b JOIN documents d ON d.id=b.document_id WHERE d.kind='scene'").fetchall():
            original=json.loads(row['metadata_json']).get('id')
            if type(original) is int:
                store.conn.execute('INSERT OR IGNORE INTO scene_evidence_ids VALUES (?,?)',(original,row['id']))
        map_evidence_ids(store)


def map_evidence_ids(store):
    store.conn.execute("INSERT OR IGNORE INTO scene_evidence_ids(binding_id) SELECT b.id FROM evidence_bindings b JOIN documents d ON d.id=b.document_id WHERE d.kind='scene' ORDER BY b.id")


def scene_payload(doc):
    meta={**normalize_domain(doc['metadata']),'id':doc['id'],'name':doc['title'],'updated_at':doc['updated_at'],
          'scene_revision':doc['revision'],'scene_status':doc['lifecycle'],'active':doc['lifecycle']=='active',
          'type':'archived' if doc['lifecycle']=='archived' else 'scene'}
    # Legacy BucketManager reads trimmed prose; imported Markdown retains its
    # original boundary whitespace in SQLite. Keep legacy relation hashes valid.
    return {'id':doc['id'],'content':doc['body_md'].strip(),'metadata':meta,'object_kind':'scene'}


def sources(refs):
    return [{'source_key':encode([ref['source_system'],ref['session_id'] or ref['thread_id'],ref['message_id']]),
             'content':ref['content'],'metadata':ref} for ref in (normalize_evidence_ref(raw) for raw in refs)]


class Scenes:
    def __init__(self,database):
        self.database=database

    def read(self,scene_id):
        with Reader(self.database) as reader:
            obj=reader.read(scene_id,kind='scene',with_evidence=False)
            return scene_payload(obj['document']) if obj['readable'] else {'status':'not_found','id':scene_id}

    def evidence(self,scene_id):
        with Reader(self.database) as reader:
            obj=reader.read(scene_id,kind='scene')
            if not obj['readable']:
                return {'status':'invalid','scene_id':scene_id,'reason':obj['status']}
            rows=[]
            for ref in obj['evidence']:
                public=reader.store.conn.execute('SELECT id FROM scene_evidence_ids WHERE binding_id=?',(ref['binding_id'],)).fetchone()
                source=reader.store.conn.execute('SELECT metadata_json FROM sources WHERE id=?',(ref['source_id'],)).fetchone()
                rows.append({**json.loads(source[0]),**ref['metadata'],'id':public[0],'scene_id':scene_id,
                             'content':ref['content'],'content_sha256':ref['content_sha256']})
            return {'status':'ok','scene_id':scene_id,'evidence_status':'bound' if rows else 'unbound','evidence_refs':rows}

    def write(self,content,cues,title='',date='',domain='',evidence_refs=None):
        cues=self._cues(cues)
        if date: Date.fromisoformat(date)
        meta={'object_kind':'scene','memory_value_source':'authored_scene','write_contract':'write-scene-v1',
              'scene_cues':cues,'date':date,'canonical_domain':domain or 'general','domain':[domain or 'general'],
              'scene_status':'active','active':True,'created':now()}
        with Writer(self.database) as writer,writer.store.transaction():
            result=writer.execute('scene_create:'+uuid4().hex,'save',{'kind':'scene','title':title or content[:40],
                'body_md':content,'metadata':meta,'sources':sources(evidence_refs or [])})
            map_evidence_ids(writer.store)
        return f"已保存。\n[scene_id:{result['id']}]\n[evidence_status:{'bound' if evidence_refs else 'unbound'}]\n[evidence_bound_count:{len(evidence_refs or [])}]"

    @staticmethod
    def _cues(value):
        values=re.split(r'[,|\n，;；]',value) if isinstance(value,str) else value
        if not isinstance(values,list): raise ValueError('cues must be a list')
        values=[part for v in values for part in re.split(r'[;；]',str(v))]
        values=list(dict.fromkeys(str(v).strip() for v in values if str(v).strip()))
        if not 1<=len(values)<=8 or any(len(v)>80 for v in values): raise ValueError('Use 1..8 cues of at most 80 characters')
        return values

    def edit(self,scene_id,expected_updated_at,*,title=None,content=None,cues=None,date=None,status=None,metadata=None):
        with Writer(self.database) as writer,writer.store.transaction():
            # Acquire the write lock before reading the version supplied by UI.
            writer.store.conn.execute('UPDATE documents SET revision=revision WHERE 0')
            doc=writer.store.read(scene_id)
            if not doc or doc['kind']!='scene' or doc['lifecycle']=='deleted':
                return {'status':'not_found','scene_id':scene_id}
            if doc['updated_at']!=expected_updated_at:
                return {'status':'conflict','scene_id':scene_id,'updated_at':doc['updated_at']}
            if doc['metadata'].get('source_record_immutable'): raise ValueError('Immutable source record')
            meta={**doc['metadata'],**(metadata or {})}
            if cues is not None:meta['scene_cues']=self._cues(cues)
            if date is not None:Date.fromisoformat(date);meta['date']=date
            if status is not None:
                if status not in ('active','archived','deleted'):raise ValueError('Invalid Scene status')
                meta.update(scene_status=status,active=status=='active',type='archived' if status=='archived' else 'scene')
            body=doc['body_md'] if content is None else content
            heading=doc['title'] if title is None else title
            if not body.strip() or not heading.strip():raise ValueError('Title and body are required')
            if meta==doc['metadata'] and body==doc['body_md'] and heading==doc['title']:
                return {'status':'unchanged','scene':scene_payload(doc),'updated_at':doc['updated_at']}
            stamp=now();meta.update(updated_at=stamp,name=heading,scene_revision=doc['revision']+1)
            writer.store.revise(scene_id,expected_revision=doc['revision'],title=heading,body_md=body,metadata=meta)
            if status is not None:writer.store.set_lifecycle(scene_id,status)
            writer._dirty(scene_id)
            updated=writer.store.read(scene_id)
            # Metadata tokens returned to existing clients must be exactly the
            # same token the subsequent optimistic write checks.
            return {'status':'updated','scene_id':scene_id,'updated_at':updated['updated_at'],'scene':scene_payload(updated)}

    def bind(self,scene_id,evidence_refs,bound_by='user'):
        with Writer(self.database) as writer,writer.store.transaction():
            doc=writer.store.read(scene_id)
            if not doc or doc['kind']!='scene' or doc['lifecycle']!='active':raise ValueError('Scene is not active')
            writer.execute('scene_bind:'+uuid4().hex,'evidence',{'document_id':scene_id,'expected_revision':doc['revision'],
                            'bind':sources(evidence_refs),'actor':bound_by})
            map_evidence_ids(writer.store)
        return {**self.evidence(scene_id),'status':'bound','bound_count':len(evidence_refs)}

    def unbind(self,scene_id,evidence_ids,unbound_by='user'):
        with Writer(self.database) as writer,writer.store.transaction():
            doc=writer.store.read(scene_id)
            if not doc or doc['kind']!='scene':raise ValueError('Scene not found')
            bindings=[]
            for key in evidence_ids:
                row=writer.store.conn.execute('SELECT binding_id FROM scene_evidence_ids WHERE id=?',(key,)).fetchone()
                if not row:raise ValueError('Evidence binding not found')
                bindings.append(row[0])
            writer.execute('scene_unbind:'+uuid4().hex,'evidence',{'document_id':scene_id,'expected_revision':doc['revision'],
                           'unbind':bindings,'actor':unbound_by})
        return {**self.evidence(scene_id),'status':'unbound','unbound_count':len(bindings)}

    def handoff(self,limit=500):
        with Reader(self.database) as reader:
            items=[]
            for row in reader.store.conn.execute("SELECT id FROM documents WHERE kind='scene' AND lifecycle='active'"):
                obj=reader.read(row[0]);doc=obj['document'];meta=doc['metadata']
                refs=[r for r in obj['evidence'] if json.loads(r['source_key'])[0]=='assistant_bridge']
                at=str(meta.get('date') or meta.get('event_date') or meta.get('created') or doc['created_at'])
                items.append({'id':doc['id'],'title':doc['title'],'layer':'scene','content':doc['body_md'].strip(),'date':at[:10],'created_at':at,
                    'source_message_ids':list(dict.fromkeys(str(r['metadata'].get('message_id') or json.loads(r['source_key'])[2]) for r in refs)),
                    'source_session_ids':list(dict.fromkeys(str(r['metadata'].get('session_id') or json.loads(r['source_key'])[1]) for r in refs))})
            items.sort(key=lambda item:(item['created_at'],item['id']),reverse=True)
            return {'status':'ok','lifecycle':'active_canonical','items':items[:limit],'count':len(items)}
