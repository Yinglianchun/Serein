"""Entity handles grounded in current bound snapshots, never Arc membership."""

from collections import Counter
import json
from pathlib import Path
import re
import sqlite3
import unicodedata

from ..core.store import digest, encode
from .index import Search, content_stamp
from .scene import domain_rejection
from ..tagging_entities import current_entities


def key(text):
    return unicodedata.normalize('NFKC',text).strip().casefold()


def pattern(text):
    escaped=re.escape(text)
    return (r'(?<![A-Za-z0-9_])'+escaped+r'(?![A-Za-z0-9_])') if text.isascii() else escaped


def evidence_stamp(evidence):
    return digest(encode(sorted({item['source_id'] for item in evidence})))


def ensure_tables(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS entity_terms(entity_key TEXT PRIMARY KEY,text TEXT NOT NULL,single_allowed INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS entity_observations(
            document_id TEXT NOT NULL,entity_key TEXT NOT NULL,text TEXT NOT NULL,stamp TEXT NOT NULL,
            evidence_stamp TEXT NOT NULL,supports_json TEXT NOT NULL,origin TEXT NOT NULL,
            PRIMARY KEY(document_id,entity_key));
        CREATE INDEX IF NOT EXISTS entity_handles ON entity_observations(entity_key,document_id);
    ''')


def rebuild_entities(settings, *, legacy_rows=(), document_ids=None):
    counts, previous = Counter(), set()
    conn=sqlite3.connect(Path(settings.index).resolve().as_uri()+'?mode=rw',uri=True)
    try:
        ensure_tables(conn)
        with conn, Search(settings.database,settings.index) as search:
            search.reader.store.conn.execute('BEGIN')
            for row in legacy_rows:
                if not row.get('scope_eligible'): continue
                text=row['entity_text'].strip()
                if len(text)<2: continue
                single=row['confidence_basis'] in ('explicit_work_title','known_arc_title')
                conn.execute('INSERT INTO entity_terms VALUES (?,?,?) ON CONFLICT(entity_key) DO UPDATE SET single_allowed=max(single_allowed,excluded.single_allowed)',(key(text),text,int(single)))
                previous.add((row['owner_id'],key(text)))
            terms={row[0]:{'text':row[1],'single':bool(row[2])} for row in conn.execute('SELECT * FROM entity_terms')}
            matcher=re.compile('|'.join(pattern(row['text']) for row in sorted(terms.values(),key=lambda row:-len(row['text']))),re.IGNORECASE) if terms else None
            document_ids=None if document_ids is None else set(document_ids)
            if document_ids is None:
                conn.execute('DELETE FROM entity_observations')
            else:
                conn.executemany('DELETE FROM entity_observations WHERE document_id=?',[(key,) for key in document_ids])
            for row in search.reader.store.conn.execute("SELECT id FROM documents WHERE kind IN ('event','scene') AND lifecycle!='deleted' ORDER BY id"):
                if document_ids is not None and row['id'] not in document_ids: continue
                obj=search.reader.read(row['id'],with_evidence=True)
                if not obj['readable']: continue
                evidence={item['source_id']:item for item in obj['evidence']}
                model_entities=current_entities(search.reader.store,obj['document'])
                if not evidence and not model_entities:
                    counts['owners_without_bound_evidence']+=1
                    continue
                counts['owners_with_bound_evidence']+=bool(evidence)
                counts['owners_with_body_entities']+=bool(model_entities) and not evidence
                found={}
                for entity in model_entities:
                    supports={}
                    for support in entity['supports']:
                        start,end=support['name_start'],support['name_end']
                        supports[(support['source_id'],start,end)]={**support,'start_offset':start,'end_offset':end}
                    found[key(entity['name'])]={'text':entity['name'],'explicit':True,'model_validated':True,'supports':supports}
                for source in evidence.values():
                    matches=[(m.start(),m.end(),m.group(),False) for m in matcher.finditer(source['content'])] if matcher else []
                    matches += [(m.start(1),m.end(1),m.group(1),True) for m in re.finditer(r'《([^》\n]{2,80})》',source['content'])]
                    for start,end,text,explicit in matches:
                        entry=found.setdefault(key(text),{'text':text,'explicit':False,'supports':{}})
                        entry['explicit'] |= explicit
                        entry['supports'][(source['source_id'],start,end)]={'source_id':source['source_id'],'start_offset':start,'end_offset':end}
                accepted=0
                for entity,entry in found.items():
                    supports=list(entry['supports'].values())
                    if len(supports)<2 and not entry['explicit'] and not terms.get(entity,{}).get('single'): continue
                    origin=('model_validated' if entry.get('model_validated') else
                            'legacy_revalidated' if (row['id'],entity) in previous else 'current_bound_sources')
                    conn.execute('INSERT INTO entity_observations VALUES (?,?,?,?,?,?,?)',
                        (row['id'],entity,entry['text'],content_stamp(obj['document']),evidence_stamp(evidence.values()),encode(supports[:8]),origin))
                    counts[origin]+=1
                    accepted+=1
                counts['owners_with_entities']+=bool(accepted)
            counts['vocabulary_terms']=len(terms)
    finally: conn.close()
    return {**dict(counts),'canonical_writes':0,'external_calls':0}


def candidates(search, query, policy, limit=50):
    if not search.has_entities: return []
    names=search.conn.execute('SELECT DISTINCT entity_key,text FROM entity_observations ORDER BY entity_key').fetchall()
    matched=[row['entity_key'] for row in names if re.search(pattern(row['text']),query.text,re.IGNORECASE)]
    found={}
    for entity in matched:
        for row in search.conn.execute('SELECT * FROM entity_observations WHERE entity_key=? ORDER BY document_id',(entity,)):
            obj=search.reader.read(row['document_id'],with_evidence=True)
            if not obj['readable'] or (query.mode=='surface' and not obj['surface_state']['can_surface']): continue
            doc=obj['document']
            if content_stamp(doc)!=row['stamp'] or evidence_stamp(obj['evidence'])!=row['evidence_stamp']: continue
            ref=doc['kind']+':'+doc['id']
            # Delivery cooldown belongs after winner selection; filtering here
            # could promote lower-ranked entity candidates into vacated slots.
            if ref in query.exclude_ids or doc['id'] in query.exclude_ids: continue
            if domain_rejection(doc,query,policy): continue
            if doc['id'] not in found and len(found)>=limit: continue
            hit=found.setdefault(doc['id'],{'id':doc['id'],'kind':doc['kind'],'score':None,'method':'entity','object':{**obj,'evidence':[]},'entity_handles':[]})
            hit['entity_handles'].append({'text':row['text'],'support_count':len(json.loads(row['supports_json']))})
    return list(found.values())
