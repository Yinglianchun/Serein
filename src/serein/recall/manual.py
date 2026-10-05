"""One bounded retry for manual MCP reads, never automatic gateway recall."""


def fallback_lookup(engine, query, primary, options):
    """Keep one engine/configuration snapshot and preserve all primary failures."""
    if (options.get("mode", "surface") != "surface" or options.get("intent", "direct") != "direct"
            or primary.get("selected_refs") or primary.get("context")
            or primary.get("reranker_error") or primary.get("error")):
        return primary
    status = primary.get("status")
    route_skip = (status == "skipped" and primary.get("reason") == "published_skip_route"
                  and primary.get("routing", {}).get("action") == "skip")
    if status != "no_match" and not route_skip:
        return primary
    result = engine.run(query, **{**options, "mode": "lookup", "method": "lexical",
                                 "min_cosine": None, "surface_only": True})
    return {**result, "manual_fallback": {
        "method": "lexical", "mode": "lookup", "visibility": "surface_eligible",
        "primary_status": status, "primary_method": primary.get("method"),
        "primary_reason": primary.get("reason"),
        "primary_suppressed": primary.get("suppressed", {}),
    }}
