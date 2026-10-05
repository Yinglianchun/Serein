"""Human-readable MCP views for memory, Narrative, diary and favorite reads."""

from typing import Any

from ..recall.dates import date_lines, memory_dates
from ..recall.source_refs import message_refs


def typed_id(result: dict[str, Any]) -> str:
    kind = str(result.get("kind") or "memory")
    identifier = str(result.get("id") or "")
    return identifier if identifier.startswith(kind + ":") else f"{kind}:{identifier}"


def version_lines(document: dict[str, Any]) -> list[str]:
    revision = document.get("body_revision", document.get("revision"))
    lines = [f"revision: {revision}"] if revision is not None else []
    current = document.get("revision")
    if current is not None and revision is not None and current != revision:
        lines.append(f"current_revision: {current}")
    updated = document.get("updated_at") or (document.get("metadata") or {}).get("updated_at")
    if updated:
        lines.append(f"updated_at: {updated}")
    return lines


def comment_lines(result: dict[str, Any]) -> list[str]:
    comments = [*(result.get("annotations") or []), *(result.get("comments") or [])]
    lines = [f"comments: {len(comments)}"]
    for number, comment in enumerate(comments, 1):
        author = str(comment.get("author") or comment.get("role") or "unknown")
        stamp = str(comment.get("createdAt") or comment.get("created_at") or "").strip()
        suffix = f" at={stamp}" if stamp else ""
        lines.extend((f"[comment {number}] author={author}{suffix}",
                      str(comment.get("content") or "").strip()))
    return lines


def source_lines(evidence: list[dict[str, Any]], *, with_evidence: bool) -> list[str]:
    lines = [f"bound_sources: {len(evidence)}"]
    for number, source in enumerate(evidence, 1):
        identity = (message_refs([source]) or [{}])[0]
        fields = [f"source_id={source.get('source_id') or 'unknown'}"]
        for name in ("source_system", "session_id", "thread_id", "conversation_id"):
            if identity.get(name):
                fields.append(f"{name}={identity[name]}")
        if identity.get("message_ids"):
            fields.append("message_id=" + ",".join(identity["message_ids"]))
        lines.append(f"[source {number}] " + " ".join(fields))
        if with_evidence:
            lines.extend(("text:", str(source.get("content") or "").strip()))
    if evidence and not with_evidence:
        lines.append("Use with_evidence=true to expand the numbered original sources.")
    return lines


def narrative_menu_lines(menu: dict[str, Any]) -> list[str]:
    key = str(menu.get("arc_key") or "")
    lines = [f"[narrative_menu key={key}]", f"title: {menu.get('title') or key}",
             f"material_count: {menu.get('material_count') or 0}"]
    for item in menu.get("materials") or []:
        day = f" {item['date']}" if item.get("date") else ""
        lines.append(f"[{item['index']}] {item['kind']}: {item.get('title') or item['id']}{day} (id={item['id']})")
    if menu.get("menu_truncated"):
        lines.append("menu_truncated: true")
    lines.extend((f'Use read_arc_materials(arc_key="{key}", picks=[numbers]) to read up to 5 items.',
                  "[/narrative_menu]"))
    return lines


def memory_text(result: dict[str, Any], *, with_evidence: bool) -> str:
    ref = typed_id(result)
    if not result.get("readable"):
        return f"kind: {result.get('kind') or 'memory'}\nstatus: {result.get('status') or 'unavailable'}\nid: {ref}"
    document = result.get("document") or {}
    kind = str(result.get("kind") or "memory")
    lines = [f"kind: {kind}", f"status: {result.get('status') or 'available'}", f"id: {ref}"]
    title = str(document.get("title") or "").strip()
    if title:
        lines.append(f"title: {title}")
    lines.extend(date_lines(memory_dates(document, kind)))
    lines.extend(version_lines(document))
    if document.get("author"):
        lines.append(f"author: {document['author']}")
    lines.extend(("body:", str(document.get("body_md") or "").strip()))
    lines.extend(comment_lines(result))
    if result.get("evidence_scope") == "unavailable_for_revision":
        lines.append("bound_sources: unavailable_for_revision")
    else:
        lines.extend(source_lines(result.get("evidence") or [], with_evidence=with_evidence))
    for menu in result.get("narrative_menus") or []:
        lines.extend(("", *narrative_menu_lines(menu)))
    return "\n".join(lines)


def recall_text(result: dict[str, Any], *, with_evidence: bool) -> str:
    notices = []
    fallback = result.get("manual_fallback")
    if fallback:
        notices.extend(("Manual fallback: lexical lookup (surface-eligible memories only).",
                        f"primary_status: {fallback['primary_status']}"))
        if fallback.get("primary_reason"):
            notices.append(f"primary_reason: {fallback['primary_reason']}")
        notices.append("These are lookup matches, not automatic relevance approval or an injection record.")
        for label, counts in (("primary_suppressed", fallback.get("primary_suppressed", {})),
                              ("lookup_suppressed", result.get("suppressed", {}))):
            if counts:
                notices.append(label + ": " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    if result.get("reranker_error"):
        notices.append(f"Recall incomplete: reranker_error={result['reranker_error']}; no fallback was attempted.")
    context = str(result.get("context") or "").strip()
    if context:
        lines = [*notices, context]
        for pool in (result.get("pools") or {}).values():
            for hit in pool.get("items") or []:
                obj = hit.get("object") or {}
                lines.extend(("", f"[memory_details ref={hit.get('kind')}:{hit.get('id')}]"))
                lines.extend(version_lines(obj.get("document") or {}))
                lines.extend(comment_lines(obj))
                lines.extend(source_lines(obj.get("evidence") or [], with_evidence=with_evidence))
                lines.append("[/memory_details]")
        return "\n".join(lines)
    status = str(result.get("status") or "no_match")
    if status == "use_narrative_reader":
        return "This request needs a Narrative lookup. Use find_arc(query=...) first."
    if status == "use_evidence_reader":
        return "This request needs an exact memory read. Use read_memory(identifier=..., with_evidence=true)."
    summary = "Recall incomplete." if result.get("reranker_error") else "No matching memory."
    if result.get("reason"):
        notices.append(f"reason: {result['reason']}")
    return "\n".join([*notices, summary, f"status: {status}"])


def find_arc_text(result: dict[str, Any]) -> str:
    items = result.get("items") or []
    if not items:
        return "No matching Narrative."
    lines = ["[narrative_menu]"]
    for number, item in enumerate(items, 1):
        arc = f" arc_key={item['arc_key']}" if item.get("arc_key") else ""
        lines.append(f"[{number}] id=narrative:{item['id']}{arc}\n    {item.get('title') or item['id']}")
    lines.extend(("Use read_memory(identifier=the Narrative id) for its body, or read_arc_materials(identifier=the Narrative id) for materials.",
                  "[/narrative_menu]"))
    return "\n".join(lines)


def arc_materials_text(result: dict[str, Any], *, with_evidence: bool) -> str:
    if result.get("status") not in {"active", "ok"}:
        return f"Narrative materials unavailable.\nstatus: {result.get('status') or 'not_found'}"
    lines = [f"narrative_id: narrative:{result.get('narrative_id')}" if result.get("narrative_id") else
             f"arc_key: {result.get('arc_key')}", "[materials]"]
    for number, item in enumerate(result.get("items") or [], 1):
        kind, identifier = item.get("kind") or "memory", item.get("id") or ""
        obj = item.get("object") or {}
        if not obj.get("readable"):
            lines.append(f"[{number}] id={kind}:{identifier} status={item.get('selection') or obj.get('status') or 'unavailable'}")
            continue
        document = obj.get("document") or {}
        lines.extend((f"[{number}] id={kind}:{identifier}", f"title: {document.get('title') or identifier}",
                      *date_lines(memory_dates(document, kind)), *version_lines(document)))
        if document.get("author"):
            lines.append(f"author: {document['author']}")
        lines.extend(("body:", str(document.get("body_md") or "").strip()))
        lines.extend(comment_lines(obj))
        lines.extend(source_lines(obj.get("evidence") or [], with_evidence=with_evidence))
    lines.append("[/materials]")
    if result.get("next_offset") is not None:
        lines.append(f"next_offset: {result['next_offset']}")
    return "\n".join(lines)


def diary_text(result: dict[str, Any]) -> str:
    items = result.get("diaries") or []
    lines = ["Diary", f"count: {len(items)}"]
    for item in items:
        kind = str(item.get("entry_type") or item.get("kind") or "diary")
        lines.extend(("", f"kind: {kind}", f"status: {item.get('visibility') or 'active'}",
                      f"id: {kind}:{item.get('id')}", f"title: {item.get('title') or ''}",
                      f"date: {item.get('date') or ''}", f"revision: {item.get('revision') or 1}"))
        if item.get("updated_at"):
            lines.append(f"updated_at: {item['updated_at']}")
        if item.get("created_at"):
            lines.append(f"created_at: {item['created_at']}")
        if item.get("author"):
            lines.append(f"author: {item['author']}")
        lines.extend(("body:", str(item.get("content") or "").strip(), *comment_lines(item)))
        source_id = str(item.get("source_id") or "").strip()
        if source_id:
            lines.extend(("bound_sources: 1", f"[source 1] source_id={source_id}"))
        else:
            lines.append("bound_sources: 0")
    return "\n".join(lines)


def favorites_text(result: dict[str, Any], *, with_evidence: bool) -> str:
    lines = ["[favorites]", f"count: {len(result.get('items') or [])}"]
    for item in result.get("items") or []:
        lines.extend(("", memory_text(item, with_evidence=with_evidence)))
    lines.extend((f"has_more: {str(bool(result.get('has_more'))).lower()}",
                  f"next_offset: {result.get('next_offset')}", "[/favorites]"))
    return "\n".join(lines)

