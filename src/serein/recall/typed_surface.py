"""Mixed Scene/Event recall, with Germany's gates and evidence formatting.

The upstream candidate and admission functions are read-only. This adapter owns
storage translation; model configuration and actual delivery remain with callers.
"""

from array import array
from collections import Counter, OrderedDict
from contextlib import closing
from copy import copy
import json
import math
import re
from threading import Lock
import time
from pathlib import Path
from types import SimpleNamespace

from . import scene
from .germany.candidates import CandidateGateway
from .germany.memory_recall.typed_admission_shadow import evaluate_typed_admission_shadow
from .germany.memory_recall.typed_candidate_shadow import rerank_lane_with_freshness
from .index import Search, content_stamp, unit_vector
from .rendering import render
from .person_references import resolve_person_references
from .reranker_input import memory_document
from .legacy_indexes import lexical_index, cue_index
from .germany.memory_recall.fact_event_lexical_shadow import _source_hash as lexical_hash
from ..deployment import read_from_store


_VECTOR_CACHE_LIMIT = 4096
_VECTOR_CACHE: OrderedDict[str, array] = OrderedDict()
_VECTOR_CACHE_LOCK = Lock()


def _decode_vector(raw):
    """Decode a stored JSON vector once, keyed by its exact serialized value."""
    key = str(raw)
    with _VECTOR_CACHE_LOCK:
        cached = _VECTOR_CACHE.get(key)
        if cached is not None:
            _VECTOR_CACHE.move_to_end(key)
            return cached
    values = json.loads(key)
    if not isinstance(values, list) or any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        raise ValueError('Stored embedding is not a finite JSON number array')
    decoded = array('d', values)
    with _VECTOR_CACHE_LOCK:
        existing = _VECTOR_CACHE.get(key)
        if existing is not None:
            _VECTOR_CACHE.move_to_end(key)
            return existing
        _VECTOR_CACHE[key] = decoded
        _VECTOR_CACHE.move_to_end(key)
        while len(_VECTOR_CACHE) > _VECTOR_CACHE_LIMIT:
            _VECTOR_CACHE.popitem(last=False)
    return decoded


def _cosine(left, right):
    if len(left) != len(right) or not left:
        return None
    right_norm = math.sqrt(sum(value * value for value in right))
    if not right_norm:
        return None
    return sum(a * b for a, b in zip(left, right)) / right_norm


class Snapshot:
    def __init__(self, search, query, policy, embedding, cutoff, use_passages, settings):
        self.search, self.query = search, query
        self.objects, self.catalog, self.whole, self.passages = {}, {}, {}, {}
        self.suppressed = Counter()
        stored = search.conn.execute("SELECT value FROM settings WHERE key='embedding_profile'").fetchone()
        if stored is None or json.loads(stored[0]) != embedding['profile'] or embedding['query'] != query.text:
            raise ValueError('Query embedding must match the indexed profile and original query')
        dimension = json.loads(search.conn.execute("SELECT value FROM settings WHERE key='embedding_dimension'").fetchone()[0])
        vector = unit_vector(embedding['embedding'], dimension) if dimension is not None else []
        self.query_vector = vector
        self.cutoff = cutoff
        self.lexical=lexical_index(settings)
        self.cues=cue_index(settings,profile=embedding['profile'])
        indexed = {row['id']: dict(row) for row in search.conn.execute(
            "SELECT * FROM documents WHERE kind IN ('scene','event')")}
        canonical = search.reader.store.conn
        current_rows = canonical.execute(
            "SELECT d.*,r.number AS body_revision,r.title,r.body_md,r.body_sha256,r.metadata_json "
            "FROM documents d JOIN revisions r ON r.document_id=d.id AND r.number=d.revision "
            "WHERE d.kind IN ('scene','event')").fetchall()
        documents = {}
        for row in current_rows:
            doc = dict(row)
            doc['metadata'] = json.loads(doc.pop('metadata_json'))
            documents[doc['id']] = doc
        source_sets = {}
        for row in canonical.execute("SELECT document_id,source_id FROM evidence_bindings WHERE active=1"):
            source_sets.setdefault(row['document_id'], set()).add(row['source_id'])
        predecessors = {row[0] for row in canonical.execute("SELECT predecessor_id FROM event_replacements")}
        source_scenes = {}
        for document_id, sources in source_sets.items():
            doc = documents.get(document_id)
            if not doc or doc['kind'] != 'scene' or doc['lifecycle'] != 'active':
                continue
            for source_id in sources:
                source_scenes.setdefault(source_id, set()).add(document_id)
        covered = set()
        for document_id, doc in documents.items():
            sources = source_sets.get(document_id, set())
            if doc['kind'] != 'event' or not sources:
                continue
            possible = None
            for source_id in sources:
                possible = set(source_scenes.get(source_id, set())) if possible is None else possible & source_scenes.get(source_id, set())
                if not possible:
                    break
            if possible:
                covered.add(document_id)
        stamps = {}
        for document_id, indexed_row in indexed.items():
            doc = documents.get(document_id)
            if not doc:
                self.suppressed['missing_or_deleted'] += 1
                continue
            if doc['lifecycle'] != 'active':
                self.suppressed['lifecycle_not_active'] += 1
                continue
            if doc['manual_surface'] != 1:
                self.suppressed['manual_surface_not_enabled'] += 1
                continue
            if doc['kind'] == 'event' and doc['id'] in predecessors:
                self.suppressed['replaced_by_event'] += 1
                continue
            if doc['kind'] == 'event' and doc['id'] in covered:
                self.suppressed['covered_by_scene'] += 1
                continue
            if doc['id'] in query.exclude_ids or doc['kind'] + ':' + doc['id'] in query.exclude_ids:
                self.suppressed['excluded_by_request'] += 1
                continue
            if scene.domain_rejection(doc, query, policy):
                self.suppressed[doc['kind'] + '_domain_rejected'] += 1
                continue
            if content_stamp(doc) != indexed_row['stamp']:
                self.suppressed['stale_content_rebuild_required'] += 1
                continue
            obj = {'kind': doc['kind'], 'id': doc['id'], 'status': doc['lifecycle'], 'readable': True,
                   'document': doc, 'evidence': [], 'comments': [],
                   'surface_state': {'document_id': doc['id'], 'can_surface': True, 'reasons': [], 'covering_scene_ids': []}}
            self.objects[doc['id']] = obj
            stamps[doc['id']] = indexed_row['stamp']
            body = scene.evidence_text(doc) if doc['kind'] == 'scene' else doc['body_md']
            meta=doc['metadata']
            day=(meta.get('local_date') or meta.get('source_started_at') or '') if doc['kind']=='event' else (
                meta.get('date') or meta.get('created') or doc['created_at'] or meta.get('updated_at') or '')
            self.catalog[doc['id']] = {'owner_kind': doc['kind'], 'title': doc['title'], 'body': body,
                'memory_date': str(day), 'recallable': obj['surface_state']['can_surface']}
        for row in search.conn.execute('SELECT id,embedding FROM vectors'):
            if row['id'] in self.catalog:
                try:
                    stored_vector = _decode_vector(row['embedding'])
                except (TypeError, ValueError, json.JSONDecodeError):
                    self.suppressed['invalid_whole_vector'] += 1
                    continue
                if len(stored_vector) == len(vector):
                    self.whole[row['id']] = round(sum(a*b for a,b in zip(vector,stored_vector)),4)
        if use_passages and search.has_passages:
            for row in search.conn.execute('SELECT * FROM passages WHERE embedding IS NOT NULL'):
                if row['document_id'] not in stamps or row['stamp'] != stamps[row['document_id']]:
                    continue
                try:
                    stored_vector = _decode_vector(row['embedding'])
                except (TypeError, ValueError, json.JSONDecodeError):
                    self.suppressed['invalid_passage_vector'] += 1
                    continue
                if len(stored_vector) != len(vector):
                    continue
                self.passages.setdefault(row['document_id'], []).append({k:row[k] for k in
                    ('ordinal','start_offset','end_offset','text') } | {
                    'score': round(sum(a*b for a,b in zip(vector,stored_vector)),4)})
        for rows in self.passages.values():
            rows.sort(key=lambda row:(-row['score'],row['ordinal']))
        self.cue_allowed=set()
        if Path(self.cues.db_path).is_file():
            with closing(self.cues._connect()) as db:
                states={r['scene_id']:r['source_hash'] for r in db.execute('SELECT scene_id,source_hash FROM memory_cue_passage_scene_state')}
            owners=[{'id':key,'title':obj['document']['title'],'cues':obj['document']['metadata'].get('scene_cues')}
                    for key,obj in self.objects.items() if obj['document']['kind']=='scene']
            parts={('scene',key):sorted(rows,key=lambda r:r['ordinal']) for key,rows in self.passages.items()}
            self.cue_allowed={row['scene_id'] for row in self.cues._normalize_scenes(owners,parts)
                              if states.get(row['scene_id'])==self.cues._source_hash(**row)}
        self.lexical_allowed=set()
        if Path(self.lexical.db_path).is_file():
            with closing(self.lexical._connect()) as db:
                states={r['item_id']:r['source_hash'] for r in db.execute('SELECT item_id,source_hash FROM lexical_documents')}
            for key,obj in self.objects.items():
                doc=obj['document']
                if doc['kind']=='event' and states.get(key)==lexical_hash({**doc['metadata'],'item_id':key,
                        'item_type':'event','title':doc['title'],'body':doc['body_md']}):
                    self.lexical_allowed.add(key)

    def whole_search(self, kind, allowed, limit):
        rows = [{'owner_id':key, 'score':score} for key,score in self.whole.items()
                if self.catalog[key]['owner_kind']==kind and (allowed is None or key in allowed)
                and score >= self.cutoff]
        return sorted(rows,key=lambda row:(-row['score'],row['owner_id']))[:limit]

    def search_scene_whole_by_embedding(self, vector, *, scene_ids, top_k):
        return [{'scene_id':row['owner_id'],'score':row['score']} for row in self.whole_search('scene',scene_ids,top_k)]

    def search_by_embedding(self, vector, *, top_k, owner_kinds=None, passages_per_owner=2,
                            allowed_owner_ids=None, memory_kinds=None, allowed_memory_ids=None,
                            allowed_scene_ids=None):
        if memory_kinds is not None:
            return {'matches':[{'memory_id':row['owner_id'],'memory_kind':'event','score':row['score']}
                               for row in self.whole_search('event',allowed_memory_ids,top_k)]}
        if owner_kinds is None:
            allowed=self.cue_allowed if allowed_scene_ids is None else self.cue_allowed&allowed_scene_ids
            if not self.query_vector or not Path(self.cues.db_path).is_file():
                return {'status':'unavailable','reason':'index_missing_or_query_empty','matches':[]}
            model=str(getattr(self.cues.embedding_engine,'model','') or '')
            with closing(self.cues._connect()) as db:
                cue_rows=db.execute('SELECT * FROM memory_cue_passage_embeddings').fetchall()
            best={}
            for row in cue_rows:
                scene_id=str(row['scene_id'])
                if scene_id not in allowed or str(row['embedding_model']) != model or int(row['dimension']) != len(self.query_vector):
                    continue
                try:
                    score=_cosine(self.query_vector,_decode_vector(row['embedding']))
                except (TypeError,ValueError,json.JSONDecodeError):
                    self.suppressed['invalid_cue_vector'] += 1
                    continue
                if score is None: continue
                passage={'ordinal':int(row['passage_ordinal']),'start_offset':int(row['evidence_start_offset']),
                    'end_offset':int(row['evidence_end_offset']),'text':str(row['evidence_text']),
                    'evidence_start_offset':int(row['evidence_start_offset']),'evidence_end_offset':int(row['evidence_end_offset']),
                    'evidence_text':str(row['evidence_text']),'context_start_offset':int(row['passage_start_offset']),
                    'context_end_offset':int(row['passage_end_offset']),'context_text':str(row['passage_text']),
                    'score':round(score,4)}
                candidate={'owner_kind':'scene','owner_id':scene_id,'score':round(score,4),
                    'matched_cues':[str(row['cue'])],'binding_confidence':round(float(row['confidence']),4),
                    'passages':[passage],'candidate_only':True,'decision_applied':False}
                if scene_id not in best or score > best[scene_id]['score']:
                    best[scene_id]=candidate
            matches=sorted(best.values(),key=lambda row:(-row['score'],row['owner_id']))[:top_k]
            return {'status':'ok','candidate_count':len(best),'matches':matches,
                    'candidate_only':True,'decision_applied':False}
        rows = [{'owner_kind':self.catalog[key]['owner_kind'],'owner_id':key,'score':parts[0]['score'],
                 'passages':parts[:passages_per_owner]} for key,parts in self.passages.items()
                if self.catalog[key]['owner_kind'] in owner_kinds and parts[0]['score'] >= self.cutoff
                and (allowed_owner_ids is None or (self.catalog[key]['owner_kind'],key) in allowed_owner_ids)]
        return {'matches':sorted(rows,key=lambda row:(-row['score'],row['owner_kind'],row['owner_id']))[:top_k]}

    def search_lexical(self, text, *, top_k, memory_kinds, allowed_memory_ids=None):
        allowed=self.lexical_allowed if allowed_memory_ids is None else self.lexical_allowed&set(allowed_memory_ids)
        return self.lexical.search(text,top_k=top_k,memory_kinds=memory_kinds,allowed_memory_ids=allowed)


def scope_members(reader):
    """Read active Arc membership in batches without materializing every object."""
    conn=reader.store.conn
    narratives={}
    for row in conn.execute(
        "SELECT d.id,d.revision,r.metadata_json FROM documents d JOIN revisions r "
        "ON r.document_id=d.id AND r.number=d.revision "
        "WHERE d.kind='narrative' AND d.lifecycle='active'"):
        meta=json.loads(row['metadata_json'])
        arc_key=str((meta.get('legacy_registry') or meta).get('arc_key') or '')
        if arc_key:
            narratives[row['id']]={'arc_key':arc_key,'revision':row['revision']}
    members={row['arc_key']:set() for row in narratives.values()}
    dispositions={}
    for row in conn.execute(
        "SELECT document_id,revision,kind,target_id,disposition FROM narrative_materials "
        "WHERE kind IN ('event','scene')"):
        owner=narratives.get(row['document_id'])
        if not owner or row['revision'] != owner['revision']:
            continue
        key=(row['document_id'],row['kind'],row['target_id'])
        dispositions.setdefault(key,set()).add(row['disposition'])
    active={(row['kind'],row['id']) for row in conn.execute(
        "SELECT kind,id FROM documents WHERE kind IN ('event','scene') AND lifecycle!='deleted'")}
    for (document_id,kind,target_id),values in dispositions.items():
        if 'excluded' not in values and values.intersection({'linked','appended'}) and (kind,target_id) in active:
            members[narratives[document_id]['arc_key']].add((kind,target_id))
    for row in conn.execute("SELECT arc_key,event_id FROM event_arc_links"):
        if row['arc_key'] in members and ('event',row['event_id']) in active:
            members[row['arc_key']].add(('event',row['event_id']))
    return members


def mixed_candidates(snapshot, found, members, query):
    """Rank every eligible mixed vector; candidate admission is applied later."""
    scope_key=(found.get('entity_scope',{}).get('scope_anchor') or {}).get('arc_key')
    allowed=members.get(scope_key,set()) if scope_key else None
    rows=[]
    for key,doc in snapshot.catalog.items():
        owner=(doc['owner_kind'],key)
        if not doc['recallable'] or (allowed is not None and owner not in allowed):continue
        whole=snapshot.whole.get(key)
        parts=snapshot.passages.get(key,[])
        passage=parts[0]['score'] if parts else None
        scores=[score for score in (whole,passage) if score is not None]
        if not scores or max(scores)<snapshot.cutoff:continue
        score=max(scores)
        evidence=parts[:2] if passage is not None and (whole is None or passage>=whole) else [{
            'ordinal':0,'start_offset':0,'end_offset':len(doc['body']),'text':doc['body'],'score':whole}]
        components={k:v for k,v in (('whole',whole),('passage',passage)) if v is not None}
        sources=[]
        if whole is not None:sources.append(f'{owner[0]}_whole_embedding')
        if passage is not None:sources.append(f'{owner[0]}_passage_embedding')
        rows.append({'owner_kind':owner[0],'owner_id':key,'title':doc['title'],
                     'memory_date':doc['memory_date'],'score':score,'passages':evidence,
                     'score_components':components,'candidate_sources':sources,
                     'signal_scores':{f'{name}_cosine':value for name,value in components.items()}})
    return rerank_lane_with_freshness(rows,query=query.text)


_RECALL_INTENT = re.compile(r'还记得|记得|想起|回忆|上次(?:聊|说|看|读|做)')


def _channel_rows(found, lane, channel):
    return list((((found.get('lanes') or {}).get(lane) or {}).get(channel) or {}).get('matches') or [])


def _entity_matches(index, query, owner_keys):
    if index is None or not hasattr(index,'owner_query_matches'):
        return []
    output=[]
    keys=sorted(owner_keys)
    for offset in range(0,len(keys),300):
        output.extend(index.owner_query_matches(query,owner_keys=set(keys[offset:offset+300]),limit=100))
    return output


def select_candidate_pool(snapshot, found, ranked, entity_matches, query, *, limit=20,
                          body_threshold=.50, cue_threshold=.55):
    """Keep six mixed vectors, then require cheap evidence for bounded expansion."""
    cue_by_owner={str(row.get('owner_id') or ''):row for row in _channel_rows(found,'scene','cue_search')
                  if float(row.get('score') or 0)>=cue_threshold}
    lexical_by_owner={str(row.get('owner_id') or ''):row for row in _channel_rows(found,'event','lexical_search')
                      if row.get('specific_terms')}
    entities={}
    for match in entity_matches:
        key=(str(match.get('owner_kind') or ''),str(match.get('owner_id') or ''))
        entities.setdefault(key,[]).append(match)
    recall_intent=bool(_RECALL_INTENT.search(query.text) or str((found.get('entity_scope') or {}).get('intent') or '')=='recall_reference')
    chosen=[];seen=set();counts=Counter()

    def prepare(row, reason):
        row=copy(row); key=(row['owner_kind'],row['owner_id'])
        cue=cue_by_owner.get(row['owner_id']) if row['owner_kind']=='scene' else None
        lexical=lexical_by_owner.get(row['owner_id']) if row['owner_kind']=='event' else None
        matches=entities.get(key,[])
        sources=list(row.get('candidate_sources') or [])
        signals=dict(row.get('signal_scores') or {})
        reasons=[reason]
        if cue:
            sources.append('scene_cue_candidate');signals['cue_cosine']=round(float(cue['score']),4)
            row['matched_cues']=list(cue.get('matched_cues') or [])
        if lexical:
            sources.append('event_lexical_candidate');signals['lexical_score']=round(float(lexical.get('score') or 0),4)
            row['specific_terms']=list(lexical.get('specific_terms') or [])
        if matches:
            sources.append('observed_entity');row['entity_handles']=matches
            signals['full_entity_matches']=len(matches)
        row['candidate_sources']=list(dict.fromkeys(sources))
        row['signal_scores']=signals
        row['entry_reasons']=reasons
        return row

    for rank,row in enumerate(ranked[:20],1):
        key=(row['owner_kind'],row['owner_id'])
        if rank<=6:
            chosen.append(prepare(row,'base_vector_rank'));seen.add(key);counts['base_pool']+=1
            continue
        components=row.get('score_components') or {}
        strong=max((float(value) for value in components.values()),default=-2)>=body_threshold
        cue=key[0]=='scene' and key[1] in cue_by_owner
        lexical=key[0]=='event' and key[1] in lexical_by_owner
        entity=recall_intent and bool(entities.get(key))
        reason=None
        if strong:reason='body_or_passage_floor'
        elif cue and counts['cue_expansion']<3:reason='semantic_cue'
        elif lexical and counts['keyword_expansion']<3:reason='event_specific_keyword'
        elif entity and counts['entity_expansion']<3:reason='full_entity_with_recall_intent'
        if reason:
            chosen.append(prepare(row,reason));seen.add(key);counts['tail_eligible']+=1
            if reason=='semantic_cue':counts['cue_expansion']+=1
            elif reason=='event_specific_keyword':counts['keyword_expansion']+=1
            elif reason=='full_entity_with_recall_intent':counts['entity_expansion']+=1
        else:
            counts['tail_rejected']+=1

    def add_expansion(rows, owner_kind, reason, counter):
        if counts[counter]>=3 or len(chosen)>=limit:return
        ranked_by_key={(row['owner_kind'],row['owner_id']):row for row in ranked}
        for raw in rows:
            key=(owner_kind,str(raw.get('owner_id') or ''))
            row=ranked_by_key.get(key)
            if not row or key in seen:continue
            chosen.append(prepare(row,reason));seen.add(key);counts[counter]+=1
            if len(chosen)>=limit or counts[counter]>=3:return

    add_expansion(sorted(cue_by_owner.values(),key=lambda row:(-float(row['score']),str(row.get('owner_id') or ''))),
                  'scene','semantic_cue','cue_expansion')
    add_expansion(_channel_rows(found,'event','lexical_search'),'event','event_specific_keyword','keyword_expansion')
    if recall_intent:
        for owner_kind in ('scene','event'):
            entity_rows=[{'owner_id':key[1]} for key in entities if key[0]==owner_kind]
            add_expansion(entity_rows,owner_kind,'full_entity_with_recall_intent','entity_expansion')
            if counts['entity_expansion']>=3:break
    return chosen[:limit],counts


def add_related_candidate(snapshot, ranked, rows, query, policy):
    """Give one reviewed Event/Scene neighbor a scoring chance, with no bonus."""
    links=scene.related_candidates(snapshot.search.reader,
        [row['owner_id'] for row in rows], query, policy,
        limit=None, include_delivered=True)
    by_id={link['id']:link for link in links}
    owners={(row['owner_kind'],row['owner_id']) for row in rows}
    for row in ranked:
        if row['owner_id'] in by_id and (row['owner_kind'],row['owner_id']) not in owners:
            # Keep its own body/vector score. The edge is provenance, not a bonus.
            return [*rows, {**row, 'relation_candidate':by_id[row['owner_id']]}]
    return rows


def run(engine, query, result, gate, decision, embedding, *, cutoff, limit, use_passages,
        with_evidence, body_char_limit, delivered_menu_keys, recall_ablation, deadline_at=None,
        strategy='mixed'):
    with Search(engine.settings.database,engine.settings.index) as search:
        association_enabled=read_from_store(search.reader.store)['features']['association']
        snapshot=Snapshot(search,query,engine.policy,embedding,cutoff,use_passages,engine.settings)
        upstream=CandidateGateway()
        upstream.__dict__.update(copy(gate.engine.__dict__))
        entity_index=None
        if upstream.observed_entity_shadow_index is None:
            del upstream.observed_entity_shadow_index
        else:
            index=upstream.observed_entity_shadow_index
            entity_index=index
            upstream.observed_entity_shadow_index=SimpleNamespace(resolve_query=index.resolve_query,
                owner_query_matches=lambda *args,**kwargs:[],link_candidates=lambda *args:[])
        upstream.passage_candidate_shadow_enabled=True
        upstream._passage_candidate_shadow_catalog=snapshot.catalog
        upstream._passage_candidate_shadow_arc_members=scope_members(search.reader)
        upstream._passage_candidate_shadow_arc_cards={}
        upstream.embedding_engine=snapshot
        upstream.passage_shadow_index=snapshot
        upstream.fact_event_semantic_index=snapshot
        upstream.cue_passage_shadow_index=snapshot
        upstream.fact_event_lexical_shadow_index=SimpleNamespace(search=snapshot.search_lexical)
        if recall_ablation=='without_cues':
            upstream.cue_passage_shadow_index=SimpleNamespace(search_by_embedding=lambda *a,**kw:{'matches':[]})
        if recall_ablation=='without_embedding':
            snapshot.whole.clear();snapshot.passages.clear()
        found=upstream._passage_candidate_shadow_debug(query.text,embedding['embedding'])
        # The old path is retained only for controlled replay, not exposed as a
        # live API option. Production intentionally uses the mixed policy.
        if found.get('status')=='not_retrieved':
            ranked=[];all_entity_matches=[];rows=[];entry_counts=Counter()
        else:
            ranked=mixed_candidates(snapshot,found,upstream._passage_candidate_shadow_arc_members,query) if strategy=='mixed' else found.get('candidates',[])
            all_entity_matches=_entity_matches(entity_index,query.text,
                {(row['owner_kind'],row['owner_id']) for row in ranked})
            rows,entry_counts=select_candidate_pool(snapshot,found,ranked,all_entity_matches,query,
                body_threshold=engine.policy.body_candidate_threshold,
                cue_threshold=engine.policy.cue_candidate_threshold) if strategy=='mixed' else (ranked,Counter())
        scope=found.get('entity_scope',{})
        result['candidate_policy']={**found.get('policy',{}),'selection_strategy':strategy}
        if strategy=='mixed':
            result['candidate_policy'].update(lane_quotas=None,cross_lane_score_comparison=True,
                freshness_rerank='bounded_across_mixed_pool',pool_limit=21 if association_enabled else 20,
                direct_pool_limit=20,base_vector_pool_limit=6,relation_pool_limit=int(association_enabled),
                association_enabled=association_enabled,final_order='reranker_descending',vector_floor=None if cutoff == -1 else cutoff,
                tail_body_or_passage_floor=engine.policy.body_candidate_threshold,
                cue_semantic_floor=engine.policy.cue_candidate_threshold,
                expansion_limits={'cue':3,'keyword':3,'entity':3},cue_contributes_score=False,
                keyword_contributes_score=False,entity_contributes_score=False)
        result['candidate_retrieval']={k:found[k] for k in ('status','reason','candidate_count','entity_scope') if k in found}
        result['candidate_retrieval'].update(vector_ranked_count=len(ranked),base_pool_count=entry_counts['base_pool'],
            tail_eligible_count=entry_counts['tail_eligible'],tail_rejected_count=entry_counts['tail_rejected'],
            cue_expansion_count=entry_counts['cue_expansion'],keyword_expansion_count=entry_counts['keyword_expansion'],
            entity_expansion_count=entry_counts['entity_expansion'],actual_candidate_count=len(rows),
            candidate_count=len(rows),snapshot_suppressed=dict(snapshot.suppressed))
        row_keys={(r['owner_kind'],r['owner_id']) for r in rows}
        owner_matches=[match for match in all_entity_matches
                       if (str(match.get('owner_kind') or ''),str(match.get('owner_id') or '')) in row_keys]
        surface=upstream._typed_surface_reranker_gate(query.text,scope,gate.debug(decision,user_utterance=query.user_utterance),
                    candidates=rows,owner_entity_matches=owner_matches)
        result['surface_reranker_gate']={**surface,'entity_scope':scope}
        if surface.get('applied') or found.get('status')=='not_retrieved':
            return {**result,'status':'skipped','reason':found.get('reason') or surface['reason']}
        if strategy=='mixed' and association_enabled:
            rows=add_related_candidate(snapshot,ranked,rows,query,engine.policy)
            if len(rows)>result['candidate_retrieval']['actual_candidate_count']:
                rows[-1]['candidate_sources']=list(dict.fromkeys([*(rows[-1].get('candidate_sources') or []),'reviewed_scene_relation']))
                rows[-1]['entry_reasons']=['reviewed_scene_relation']
                rows[-1].setdefault('signal_scores',{})['relation_candidate']=1
            result['candidate_retrieval']['candidate_count']=len(rows)
            result['candidate_retrieval']['actual_candidate_count']=len(rows)
        rows=[row for row in rows if row['owner_kind']!='event' or snapshot.catalog[row['owner_id']]['recallable']]
        admission_scope=scope
        if strategy=='mixed':
            admission_scope={**scope,'operator':'none'}
        rerank_query=resolve_person_references(query.text,upstream.identity) if query.user_utterance else query.text
        admission=evaluate_typed_admission_shadow(query.text,admission_scope,rows,direct_threshold=engine.policy.direct_threshold)
        scores={}
        if admission['mode']=='direct_evidence_rerank' and rows and engine.reranker:
            remaining=deadline_at-time.monotonic() if deadline_at is not None else None
            if remaining is not None and remaining < 1.8:
                return {**result,'status':'skipped','reason':'hook_deadline_before_reranker','admission':admission}
            documents=[{'ref':upstream._typed_owner_ref(row),'title':'', 'body':'',
                        'rerank_text':memory_document(snapshot.objects[row['owner_id']]['document'],
                            snapshot.passages.get(row['owner_id'], []))} for row in rows]
            from ..adapters.reranker import RerankerClient, RerankerProviderError
            try:
                if remaining is not None and isinstance(engine.reranker,RerankerClient):
                    import httpx
                    with httpx.Client(timeout=max(.05,remaining)) as transport:
                        scores=engine.reranker(rerank_query,documents,client=transport)
                else:
                    scores=engine.reranker(rerank_query,documents)
            except RerankerProviderError as exc:
                result['reranker_error']=exc.code
            except ValueError:
                result['reranker_error']='provider_score_unavailable'
            admission=evaluate_typed_admission_shadow(query.text,admission_scope,rows,rerank_scores=scores,
                                                       direct_threshold=engine.policy.direct_threshold)
        result['admission']={**admission,'rerank_query':rerank_query}
        result['candidates']=admission['candidates']
        result['candidate_scores']=[{'ref':upstream._typed_owner_ref(row),'title':row['title'],
            'vector_score':row['score'],'score_channels':row.get('score_components',{}),
            'candidate_sources':row.get('candidate_sources',[]),'signal_scores':row.get('signal_scores',{}),
            'entry_reasons':row.get('entry_reasons',[]),
            'freshness':row.get('freshness'),'candidate_rank_score':row.get('rerank_score'),
            'time_bonus':round((row.get('rerank_score') or 0)-(row.get('score') or 0),4) if row.get('score') is not None else None,
            'reranker_score':scores.get(upstream._typed_owner_ref(row)),
            'candidate_origin':'relation' if row.get('relation_candidate') else 'direct',
            'relation_candidate':row.get('relation_candidate')} for row in rows]
        admitted=set(admission['selected_refs'])
        maximum=min(limit,engine.policy.max_cards)
        if admission['mode']=='timeline_scope_material':
            admitted=set(admission['material_refs'])
            selected=sorted((r for r in rows if upstream._typed_owner_ref(r) in admitted),
                            key=lambda r:(r['memory_date'],upstream._typed_owner_ref(r)))[-maximum:]
        else:
            eligible=[r for r in rows if upstream._typed_owner_ref(r) in admitted]
            if strategy=='mixed' and admission['mode']=='direct_evidence_rerank':
                eligible.sort(key=lambda r:-scores[upstream._typed_owner_ref(r)])
            selected=eligible[:maximum]
        result['pre_cooldown_selected_refs']=[upstream._typed_owner_ref(row) for row in selected]
        suppressed=Counter();hits=[]
        reasons={row['ref']:row['reason'] for row in admission['candidates']}
        for row in selected:
            ref=upstream._typed_owner_ref(row)
            if ref in query.delivered_ids:
                suppressed['already_delivered']+=1;continue
            obj=search.reader.read(row['owner_id'],with_evidence=with_evidence)
            if not obj['readable'] or not obj['surface_state']['can_surface']:
                suppressed['state_changed_before_read']+=1;continue
            if obj['document']['revision']!=snapshot.objects[row['owner_id']]['document']['revision']:
                suppressed['revision_changed_before_read']+=1;continue
            hits.append({'id':row['owner_id'],'kind':row['owner_kind'],'object':obj,'score':row['score'],
                         'method':'cosine','admission':reasons[ref],'freshness':row.get('freshness'),
                         'rerank_score':scores.get(ref),'score_channels':row.get('score_components',{})})
        result['selected_refs']=[hit['kind']+':'+hit['id'] for hit in hits]
        result['pools']={kind:{'method':'cosine','items':[h for h in hits if h['kind']==kind]} for kind in ('event','scene')}
        result['suppressed']=dict(suppressed)
        result['related_candidates']=scene.related_candidates(search.reader,[h['id'] for h in hits],query,engine.policy)
        result.update(render(hits,reader=search.reader,body_char_limit=body_char_limit,
                    scope_arc_key=(scope.get('scope_anchor') or {}).get('arc_key',''),delivered_menu_keys=delivered_menu_keys))
        return {**result,'status':'matched' if hits else 'no_match'}
