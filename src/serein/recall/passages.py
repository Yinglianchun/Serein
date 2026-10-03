"""Exact canonical body slices and their disposable document-space vectors."""

from collections import Counter
import json
from pathlib import Path
import re
import sqlite3
from contextlib import closing

from ..core.reader import Reader
from ..core.store import digest, encode
from ..adapters.embedding import EmbeddingClient
from .index import Search, content_stamp, unit_vector, refresh_index
from .scene import evidence_lines

MIN_OWNER_CHARS = 500


def needs_passages(document, min_chars=MIN_OWNER_CHARS):
    text = document['body_md']
    return sum(len(text[start:end].strip()) for start,end in regions(document)) > min_chars


def ensure_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS passages (
            document_id TEXT NOT NULL, ordinal INTEGER NOT NULL, stamp TEXT NOT NULL,
            start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL, text TEXT NOT NULL,
            embedding TEXT, dimension INTEGER, origin TEXT NOT NULL,
            PRIMARY KEY(document_id,ordinal));
        CREATE TABLE IF NOT EXISTS passage_owners(document_id TEXT PRIMARY KEY, stamp TEXT NOT NULL);
    """)


def regions(document):
    if document['kind'] != 'scene':
        return [(0, len(document['body_md']))]
    spans = []
    for start, end, _ in evidence_lines(document):
        if spans and spans[-1][1] == start:
            spans[-1] = (spans[-1][0], end)
        else:
            spans.append((start, end))
    return spans


def slices(document, maximum=240, overlap=40, *, min_chars=MIN_OWNER_CHARS):
    """Bounded sentence-aware windows; positions always refer to canonical prose."""
    text, spans = document['body_md'], []
    if not needs_passages(document, min_chars):
        return []
    maximum = min(maximum, min_chars)
    overlap = min(overlap, maximum // 6)
    for region_start, region_end in regions(document):
        start = region_start
        while start < region_end:
            while start < region_end and text[start].isspace(): start += 1
            if start == region_end: break
            end = min(start + maximum, region_end)
            if end < region_end:
                stops = [match.end() for match in re.finditer(r'[。！？!?；;\n]', text[start+maximum//2:end])]
                if stops: end = start + maximum//2 + stops[-1]
            stop = end
            while stop > start and text[stop-1].isspace(): stop -= 1
            if stop > start: spans.append((start, stop))
            if end == region_end: break
            start = max(start + 1, end - overlap)
    return spans if len(spans) > 1 else []


def full_coverage(document, spans):
    text = document['body_md']
    for first,last in regions(document):
        position = first
        for start,end in sorted(spans):
            if end <= position or start >= last: continue
            if start > position and text[position:start].strip(): return False
            position = max(position, end)
        if text[position:last].strip(): return False
    return True


def prepare_passages(settings, *, legacy=None, document_ids=None, client=None):
    """Keep valid old layouts when complete; otherwise build independent slices."""
    from ..configured_models import recall_settings
    from .policy import RecallPolicy
    from .passage_layouts import open_layouts, layout
    policy = RecallPolicy.from_config(recall_settings(settings))
    if not policy.passages_enabled:
        return {'status':'disabled'}
    if client is None and settings.embedding.get('tokenizer'):
        client = EmbeddingClient(settings.database, settings.index, **settings.embedding)
    window = getattr(client, 'window', None)
    grouped, counts = {}, Counter()
    if legacy:
        for row in legacy['rows']:
            grouped.setdefault(row['owner_id'], []).append(row)
    if document_ids is None:
        with Reader(settings.database) as reader:
            document_ids=[row[0] for row in reader.store.conn.execute("SELECT id FROM documents WHERE kind IN ('event','scene')")]
    document_ids=set(document_ids)
    refresh_index(settings.database,settings.index,document_ids)
    with Search(settings.database, settings.index) as search, closing(open_layouts(settings.database)) as layouts, layouts:
        stored = dict(search.conn.execute("SELECT key,value FROM settings WHERE key LIKE 'embedding_%'"))
        profile = json.loads(stored.get('embedding_profile', 'null'))
        dimension = json.loads(stored.get('embedding_dimension', 'null'))
        if legacy and legacy['profile'] != profile:
            raise ValueError('Legacy passage profile differs from the current index')
        conn = sqlite3.connect(Path(settings.index).resolve().as_uri()+'?mode=rw',uri=True)
        try:
            ensure_tables(conn)
            with conn:
                for key in sorted(document_ids):
                    obj = search.reader.read(key, with_evidence=False)
                    if not obj['readable']: continue
                    doc, valid = obj['document'], []
                    if doc['kind'] not in ('event','scene'): continue
                    stamp, body = content_stamp(doc), doc['body_md']
                    previous = conn.execute('SELECT stamp FROM passage_owners WHERE document_id=?',(doc['id'],)).fetchone()
                    missing=conn.execute('SELECT count(*) FROM passages WHERE document_id=? AND embedding IS NULL',(doc['id'],)).fetchone()[0]
                    if previous and previous[0] == stamp and (not legacy or missing==0):
                        counts['owners_reused_local'] += 1
                        continue
                    spans = layout(layouts, doc, policy.passage_min_chars)
                    if window:
                        title = doc['title'].strip()+'\n' if doc['kind']=='event' and doc['title'].strip() else ''
                        prefix = f"Instruct: {profile['document_instruction']}\nDocument: " if profile['document_instruction'] else ''
                        if spans or not window.fits(prefix + title + body):
                            spans = [(a+start,a+end) for a,b in regions(doc)
                                     for start,end in window.spans(body[a:b],prefix=prefix+title,max_chars=profile['max_chars'])]
                    if not spans:
                        conn.execute('DELETE FROM passages WHERE document_id=?',(doc['id'],))
                        conn.execute('INSERT OR REPLACE INTO passage_owners VALUES (?,?)',(doc['id'],stamp))
                        counts['owners_not_applicable'] += 1
                        continue
                    for old in grouped.get(doc['id'], []):
                        # Old Scene readers trimmed boundary whitespace; retain
                        # exact input bytes while mapping offsets to canonical prose.
                        source_body=body
                        shift=0
                        if doc['kind']=='scene' and old['content_hash']!=digest(body) and old['content_hash']==digest(body.strip()):
                            source_body=body.strip()
                            shift=len(body)-len(body.lstrip())
                        start,end=old['start_offset']+shift,old['end_offset']+shift
                        fingerprint = dict(schema=4,owner_kind=doc['kind'],owner_id=doc['id'],
                            title=doc['title'].strip() if doc['kind']=='event' else '',content=source_body,
                            model=profile['model'],document_instruction=profile['document_instruction'],passage_config=legacy['passage_config'])
                        if (old['owner_kind'] != doc['kind'] or old['content_hash'] != digest(source_body)
                            or old['source_hash'] != digest(json.dumps(fingerprint,ensure_ascii=False,sort_keys=True))
                            or old['model'] != profile['model'] or old['dimension'] != dimension
                            or not 0 <= start < end <= len(body) or body[start:end] != old['text']
                            or not any(a <= start < end <= b for a,b in regions(doc))):
                            counts['legacy_rows_rejected'] += 1
                            continue
                        try: vector = unit_vector(json.loads(old['embedding']),dimension)
                        except (ValueError,TypeError):
                            counts['legacy_rows_rejected'] += 1
                            continue
                        valid.append((start,end,encode(vector)))
                    if valid and full_coverage(doc,[(a,b) for a,b,_ in valid]):
                        planned=[(a,b,v,'legacy_verified') for a,b,v in valid]
                        counts['legacy_vectors_reused'] += len(planned)
                    else:
                        available={(a,b):v for a,b,v in valid}
                        planned=[(a,b,available.get((a,b)),'legacy_verified' if (a,b) in available else 'serein') for a,b in spans]
                        counts['legacy_vectors_reused'] += sum(v is not None for _,_,v,_ in planned)
                    conn.execute('DELETE FROM passages WHERE document_id=?',(doc['id'],))
                    conn.executemany('INSERT INTO passages VALUES (?,?,?,?,?,?,?,?,?)',
                        [(doc['id'],n,stamp,a,b,body[a:b],v,dimension if v else None,origin) for n,(a,b,v,origin) in enumerate(planned)])
                    conn.execute('INSERT OR REPLACE INTO passage_owners VALUES (?,?)',(doc['id'],stamp))
                    counts['owners_prepared'] += 1
                    counts['passages_planned'] += len(planned)
        finally: conn.close()
    return dict(counts)


def passage_coverage(settings):
    with Search(settings.database,settings.index) as search:
        if not search.has_passages: return {'status':'not_built'}
        rows=search.conn.execute('SELECT count(*),sum(embedding IS NOT NULL),sum(embedding IS NULL) FROM passages').fetchone()
        return {'passages':rows[0],'covered':rows[1] or 0,'missing':rows[2] or 0,
                'owners':search.conn.execute('SELECT count(DISTINCT document_id) FROM passages').fetchone()[0]}


def fill_passages(settings, *, client=None, batch_size=16, progress=None, document_ids=None):
    from ..configured_models import recall_settings
    from .policy import RecallPolicy
    if not RecallPolicy.from_config(recall_settings(settings)).passages_enabled:
        return {'status':'disabled','planned':0,'embedded':0,'requests':0,'canonical_writes':0}
    client=client or EmbeddingClient(settings.database,settings.index,**settings.embedding)
    document_ids=None if document_ids is None else set(document_ids)
    prepare_passages(settings,document_ids=document_ids,client=client)
    with Search(settings.database,settings.index) as search:
        stored=json.loads(search.conn.execute("SELECT value FROM settings WHERE key='embedding_profile'").fetchone()[0])
        if stored != client.profile: raise ValueError('Passage provider profile differs from index')
        pending=[]
        owners = sorted(document_ids) if document_ids is not None else [row[0] for row in search.conn.execute('SELECT DISTINCT document_id FROM passages WHERE embedding IS NULL')]
        rows = (row for key in owners for row in search.conn.execute('SELECT * FROM passages WHERE document_id=? AND embedding IS NULL ORDER BY ordinal',(key,)))
        for row in rows:
            obj=search.reader.read(row['document_id'],with_evidence=False)
            if not obj['readable'] or content_stamp(obj['document'])!=row['stamp']: continue
            doc=obj['document']
            text=doc['title'].strip()+'\n'+row['text'] if doc['kind']=='event' and doc['title'].strip() else row['text']
            pending.append({**dict(row),'input':text})
    report={'planned':len(pending),'embedded':0,'requests':0,'changed':0,'canonical_writes':0}
    if not 1<=batch_size<=64: raise ValueError('Invalid batch size')
    for offset in range(0,len(pending),batch_size):
        batch=pending[offset:offset+batch_size]
        vectors=client.documents([row['input'] for row in batch])
        if len(vectors)!=len(batch): raise ValueError('Passage vector count mismatch')
        with sqlite3.connect(Path(settings.index).resolve().as_uri()+'?mode=rw',uri=True) as conn, Reader(settings.database) as reader:
            for row,vector in zip(batch,vectors):
                obj=reader.read(row['document_id'],with_evidence=False)
                if not obj['readable'] or content_stamp(obj['document'])!=row['stamp']:
                    report['changed']+=1
                    continue
                cursor=conn.execute('UPDATE passages SET embedding=?,dimension=? WHERE document_id=? AND ordinal=? AND stamp=? AND text=? AND embedding IS NULL',
                    (encode(unit_vector(vector,client.dimension)),client.dimension,row['document_id'],row['ordinal'],row['stamp'],row['text']))
                report['embedded']+=cursor.rowcount
        report['requests']+=1
        if progress: progress(dict(report))
    return {**report,'coverage':passage_coverage(settings)}
