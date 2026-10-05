"""Recall orchestration. The application entry point delegates here."""

from collections import Counter
from dataclasses import replace

from ..core.reader import Reader
from .index import Search
from .policy import RecallPolicy
from .query import Query
from . import admission, arc, event, scene


class Recall:
    def __init__(self, settings, *, reranker=None):
        self.settings = settings
        self.policy = RecallPolicy.from_config(settings.recall)
        from ..core.store import Store
        import json
        with Store(settings.database,read_only=True) as store:
            saved=store.conn.execute("SELECT value_json FROM background_state WHERE name='deployment_settings'").fetchone()
        tagging=json.loads(saved[0]).get('tagging',{}) if saved else {}
        domains=tagging.get('domains',[])
        self.policy = replace(self.policy, domains={**self.policy.domains,
            **{item['key']:item.get('policy','normal') for item in domains}},
            domain_rules={**self.policy.domain_rules, **tagging.get('policies',{})})
        self.reranker = reranker
        if self.reranker is None and settings.reranker:
            from ..adapters.reranker import RerankerClient
            self.reranker = RerankerClient(**settings.reranker)

    def run(self, text, *, mode="surface", limit=5, with_evidence=False, method="lexical", min_cosine=None,
            topic=None, intent="direct", exclude_ids=(), delivered_ids=(), use_passages=None,
            body_char_limit=1200, delivered_menu_keys=(), recall_ablation='normal', deadline_at=None,
            user_utterance=False, surface_only=False):
        if recall_ablation not in ('normal','without_cues','without_embedding'):
            raise ValueError('Unsupported recall ablation')
        if method not in {"lexical", "semantic"} or not 1 <= limit <= 100:
            raise ValueError("Invalid recall method or limit")
        if surface_only and (mode != "lookup" or method != "lexical"):
            raise ValueError("surface_only is reserved for lexical lookup")
        query = Query(text, topic, intent, mode, tuple(exclude_ids), tuple(delivered_ids), user_utterance)
        # Manual fallback relaxes evidence admission, never visibility. Discovery
        # and final reads retain the same lifecycle/access surface restrictions.
        discovery_query = replace(query, mode="surface") if surface_only else query
        visibility_mode = "surface" if surface_only else mode
        result = {"query": text, "topic": query.search_text, "intent": intent, "method": method,
                  "status": "no_match", "pools": {}, "selected_refs": [], "candidates": [],
                  "injected": False, "suppressed": {}, "selection_scope": "retrieved_candidates"}
        from .rendering import render
        result.update(render([]))
        route = arc.handoff(intent)
        if route:
            return {**result, **route}
        if not query.search_text:
            return {**result, "status": "empty_query"}
        options = {}
        if method == "semantic":
            if not self.settings.embedding:
                raise ValueError("Semantic query provider is not configured")
            if min_cosine is None or not -1 <= min_cosine <= 1:
                raise ValueError("Semantic retrieval requires min_cosine in [-1,1]")
            from ..adapters.embedding import EmbeddingClient
            client = EmbeddingClient(self.settings.database, self.settings.index, **self.settings.embedding)
            # Preserve the full original query, including the entity name.
            if deadline_at is None:
                embedded=client.query(text)
            else:
                import httpx,time
                with httpx.Client(timeout=max(.05,deadline_at-time.monotonic())) as transport:
                    embedded=client.query(text,client=transport)
            options = {"query_embedding": embedded, "min_cosine": min_cosine}
        gate, decision = None, None
        if method == "semantic" and mode == "surface" and intent == "direct" and self.policy.routing_file:
            from .routing import route_query
            from .surface_gate import load_gate
            from ..compat.publication import recall_policy_data
            published,domains=recall_policy_data(self.settings)
            if domains is not None:
                self.policy=replace(self.policy,domains={**self.policy.domains,**domains})
            decision = route_query(self.policy.routing_file, options["query_embedding"],data=published)
            from ..deployment import identity
            names = identity(self.settings.database)
            gate = load_gate(self.policy.germany_policy_file, names['user_name'], names['ai_name'])
            result["routing"] = decision
            result["pre_candidate_gate"] = gate.before_candidates(text, decision, user_utterance=user_utterance)
            if result["pre_candidate_gate"]["applied"]:
                return {**result, "status": "skipped", "reason": result["pre_candidate_gate"]["reason"]}
            from .typed_surface import run
            # Automatic surface recall ranks valid vectors without a hard floor;
            # the final route action controls search, and body reranking admits cards.
            # Explicit lookup keeps the caller's min_cosine above.
            return run(self, query, result, gate, decision, options['query_embedding'],
                       cutoff=-1.0, limit=limit,
                       use_passages=self.policy.passages_enabled and use_passages is not False,
                       with_evidence=with_evidence, body_char_limit=body_char_limit,
                       delivered_menu_keys=delivered_menu_keys, recall_ablation=recall_ablation,deadline_at=deadline_at)
        pools, candidates = {}, []
        with Search(self.settings.database, self.settings.index) as search:
            for kind in ("event", "scene"):
                pool = search.search(text if method == "semantic" else query.search_text, kind=kind, mode=visibility_mode,
                                     limit=self.policy.candidate_limit, with_evidence=False,
                                     use_passages=self.policy.passages_enabled and use_passages is not False,
                                     **({} if recall_ablation=='without_embedding' else options))
                pools[kind] = {**pool, "items": []}
                candidates.extend({**hit, "method": pool["method"]} for hit in pool["items"])
            if recall_ablation!='without_cues':
                known = {hit["id"] for hit in candidates}
                scene_count = sum(hit["kind"] == "scene" for hit in candidates)
                for row in search.reader.store.conn.execute("SELECT id FROM documents WHERE kind='scene' AND lifecycle='active' ORDER BY id"):
                    if scene_count >= self.policy.candidate_limit:
                        break
                    if row["id"] in known:
                        continue
                    obj = search.reader.read(row["id"], with_evidence=False)
                    if obj["readable"] and (visibility_mode == "lookup" or obj["surface_state"]["can_surface"]) and scene.cue_matches(obj["document"], query):
                        candidates.append({"id": row["id"], "kind": "scene", "object": obj, "score": None, "method": "cue"})
                        scene_count += 1
            from .entities import candidates as entity_candidates
            known={hit['id']:hit for hit in candidates}
            for hit in entity_candidates(search,discovery_query,self.policy,self.policy.candidate_limit):
                if hit['id'] in known:
                    known[hit['id']]['entity_handles']=hit['entity_handles']
                else: candidates.append(hit)
        if gate:
            result["surface_reranker_gate"] = gate.after_candidates(text, decision, candidates, user_utterance=user_utterance)
            if result["surface_reranker_gate"]["applied"]:
                return {**result, "status": "skipped", "suppressed": {"query_does_not_need_memory": len(candidates)}}
        rejected, admitted = Counter(), {"event": [], "scene": []}
        scored = [hit for hit in candidates if mode == "surface" and method=='semantic' and hit['method']!='entity'
                  and admission.decide(hit, query, self.policy)[0] == "candidate"
                  and not scene.domain_rejection(hit["object"]["document"], query, self.policy)]
        evidence = [{"ref": f"{hit['kind']}:{hit['id']}", "title": hit["object"]["document"]["title"],
                     "body": scene.evidence_text(hit["object"]["document"]) if hit["kind"] == "scene" else hit["object"]["document"]["body_md"]}
                    for hit in scored]
        scores = self.reranker(text, evidence) if self.reranker and scored else {}
        for hit in candidates:
            document = hit["object"]["document"]
            reason = scene.domain_rejection(document, query, self.policy)
            disposition, reason = ("reject", reason) if reason else admission.decide(
                hit, query, self.policy, scores.get(f"{hit['kind']}:{hit['id']}"))
            if disposition in {"direct", "lookup"}:
                admitted[hit["kind"]].append({**event.annotate(hit), "admission": reason})
            else:
                rejected[reason] += 1
                result["candidates"].append({"id": hit["id"], "kind": hit["kind"], "disposition": disposition,
                                             "reason": reason, "retrieval_method": hit["method"],
                                             **({'entity_handles':hit['entity_handles']} if hit.get('entity_handles') else {})})
        # Interleave ranked lanes; never sort Event scores against Scene scores.
        merged = []
        for position in range(max(map(len, admitted.values()), default=0)):
            for kind in ("event", "scene"):
                if position < len(admitted[kind]):
                    merged.append(admitted[kind][position])
        ordered = arc.order_materials(merged, intent)
        if intent in {"latest", "progress", "timeline"}:
            rejected["not_selected_by_date"] += len(merged) - len(ordered)
        maximum = min(limit, self.policy.max_cards) if mode == "surface" else limit
        chosen = ordered[:maximum]
        with Reader(self.settings.database) as reader:
            for hit in chosen:
                # Germany suppresses delivered winners without filling their
                # slots with less relevant lower-ranked candidates.
                if mode=='surface' and f"{hit['kind']}:{hit['id']}" in query.delivered_ids:
                    rejected['already_delivered'] += 1
                    continue
                # Materialization always uses current canonical access state.
                obj = reader.read(hit["id"], with_evidence=with_evidence)
                if not obj["readable"] or (visibility_mode == "surface" and not obj["surface_state"]["can_surface"]):
                    rejected["state_changed_before_read"] += 1
                    continue
                if obj["document"]["revision"] != hit["object"]["document"]["revision"]:
                    rejected["revision_changed_before_read"] += 1
                    continue
                hit["object"] = obj
                pools[hit["kind"]]["items"].append(hit)
                result["selected_refs"].append(f"{hit['kind']}:{hit['id']}")
            result["related_candidates"] = scene.related_candidates(reader, [hit['id'] for pool in pools.values() for hit in pool['items']], query, self.policy)
            by_ref = {f"{hit['kind']}:{hit['id']}": hit for pool in pools.values() for hit in pool['items']}
            scope = result.get('surface_reranker_gate', {}).get('entity_scope', {})
            scope_key = (scope.get('scope_anchor') or {}).get('arc_key', '')
            # Fallback never expands into an Arc menu with broader lookup visibility.
            result.update(render([by_ref[ref] for ref in result['selected_refs']], reader=None if surface_only else reader, scope_arc_key=scope_key,
                                 body_char_limit=body_char_limit,delivered_menu_keys=delivered_menu_keys))
        for pool in pools.values():
            pool["status"] = "matched" if pool["items"] else "no_match"
        return {**result, "pools": pools, "suppressed": dict(rejected),
                "status": "matched" if result["selected_refs"] else "no_match"}

    def find_arc(self, text, limit=5):
        keywords = list(dict.fromkeys(text.split()))
        matches, ranks = {}, Counter()
        match_mode = 'all_terms'
        with Search(self.settings.database, self.settings.index) as search:
            result = search.search(text, kind="narrative", mode="lookup", limit=limit)
            # Models often supply alternative titles/topics separated by spaces.
            # Preserve strict results; broaden only an otherwise empty explicit lookup.
            if not result['items'] and len(keywords) > 1:
                if len(keywords) > 12:
                    raise ValueError('Use at most 12 space-separated Narrative keywords')
                match_mode = 'keyword_fallback'
                found = {}
                for keyword in keywords:
                    page = search.search(keyword, kind='narrative', mode='lookup', limit=100)
                    for rank, hit in enumerate(page['items'], 1):
                        found[hit['id']] = hit
                        matches.setdefault(hit['id'], []).append(keyword)
                        ranks[hit['id']] += 1 / (60 + rank)
                result['items'] = sorted(found.values(), key=lambda hit: (
                    -len(matches[hit['id']]), -ranks[hit['id']], hit['id']))[:limit]
        cards = []
        for hit in result["items"]:
            doc = hit["object"]["document"]
            cards.append({"id": hit["id"], "kind": "narrative", "title": doc["title"],
                          "revision": doc["revision"], "status": hit["object"]["status"],
                          "arc_key": (doc["metadata"].get("legacy_registry") or doc["metadata"]).get("arc_key"),
                          **({'matched_keywords': matches[hit['id']]} if hit['id'] in matches else {}),
                          "next_tools": ["read_arc_materials", "read_memory"]})
        return {"query": text, "status": "matched" if cards else "no_match", "items": cards,
                "match_mode": match_mode,
                "scope_only": True, "narrative_body_included": False, "injected": False}

