"""Scene cue handles, published domain choices, and body evidence projection."""

import re
import json

from .index import tokens
from ..core.domains import canonical_domain
from ..deployment import read_from_store


CONTEXT_SECTIONS = {"comment", "followup", "affect_anchor", "favorite_reason", "评论", "年轮", "后续", "待办", "喜欢它的原因"}


def evidence_text(document):
    return "\n".join(line for _, _, line in evidence_lines(document))


def evidence_lines(document):
    """Retained canonical line offsets, shared by whole-body and passage reads."""
    excluded_level, offset = None, 0
    for raw in document["body_md"].splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            level, name = len(heading[1]), heading[2].casefold().replace(" ", "_").replace("-", "")
            if excluded_level is not None and level <= excluded_level:
                excluded_level = None
            if name in CONTEXT_SECTIONS and excluded_level is None:
                excluded_level = level
        if excluded_level is None:
            yield offset, offset + len(raw), line
        offset += len(raw)


def domain_rejection(document, query, policy):
    domain = canonical_domain(document["metadata"]).casefold()
    rule = policy.domain_rules.get(document['kind'], {}).get(domain, policy.domains.get(domain, "normal"))
    if rule == "excluded":
        return "domain_excluded"
    if rule == 'explicit_only':
        cues = document['metadata'].get('scene_cues') or []
        if isinstance(cues, str):
            cues = [cues]
        explicit = query.names_title(document['title']) or any(
            query.names_title(cue) for cue in cues if isinstance(cue, str)) or bool(
                re.search(r'(?<![a-zA-Z0-9_:-])' + re.escape(document['id']) + r'(?![a-zA-Z0-9_:-])', query.text))
        if not explicit:
            return 'domain_explicit_only'
    return None


def cue_matches(document, query):
    cues = document["metadata"].get("scene_cues") or []
    if isinstance(cues, str):
        cues = [cues]
    # Read authored strings only; dictionary metadata is not evidence prose.
    cue_text = " ".join(cue for cue in cues if isinstance(cue, str)) if isinstance(cues, list) else ""
    terms = tokens(query.search_text)
    return bool(terms) and set(terms) <= set(tokens(cue_text))


def related_candidates(reader, seed_ids, query, policy, limit=1, *, include_delivered=False):
    """One hop over confirmed edges; relationships are handles, not admission."""
    if not read_from_store(reader.store)['features']['association']:
        return []
    seeds, found = set(seed_ids), {}
    for seed in sorted(seeds):
        origin = reader.read(seed, with_evidence=False)
        if not origin["readable"] or not origin["surface_state"]["can_surface"]:
            continue
        if origin['kind'] not in {'event', 'scene'}:
            continue
        if domain_rejection(origin["document"], query, policy):
            continue
        rows = reader.store.conn.execute(
            "SELECT * FROM scene_relations WHERE active=1 AND lifecycle='active' "
            "AND (source_scene_id=? OR target_scene_id=?) ORDER BY id", (seed, seed))
        for row in rows:
            metadata=json.loads(row['metadata_json'])
            if metadata.get('linker_version') in {'legacy-edge-import-v1','legacy-edge-import-v2'}:
                from ..compat.germany.scene_linker import SceneEdgeStore
                from ..compat.scenes import scene_payload
                owners=[reader.store.read(row[name+'_scene_id']) for name in ('source','target')]
                if SceneEdgeStore._current_edge_error(metadata,*(scene_payload(doc) if doc else None for doc in owners)):continue
            target = row["target_scene_id"] if row["source_scene_id"] == seed else row["source_scene_id"]
            if target in seeds or target in found:
                continue
            obj = reader.read(target, with_evidence=False)
            if not obj["readable"] or not obj["surface_state"]["can_surface"]:
                continue
            if obj['kind'] not in {'event', 'scene'}:
                continue
            ref = obj['kind'] + ':' + target
            if target in query.exclude_ids or ref in query.exclude_ids or (not include_delivered and query.mode == "surface" and ref in query.delivered_ids):
                continue
            if domain_rejection(obj["document"], query, policy):
                continue
            imported=metadata.get('linker_version') in {'legacy-edge-import-v1','legacy-edge-import-v2'}
            found[target] = {"id": target, "kind": obj['kind'], "edge_id": row["id"], "seed_id": seed,
                             "disposition": "candidate", "reason": "legacy_relationship_not_evidence" if imported else "confirmed_relationship_not_evidence"}
            if len(found) == limit:
                return list(found.values())
    return list(found.values())
