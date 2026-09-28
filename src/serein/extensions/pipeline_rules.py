"""Bounded pure routing/unit rules adapted from the verified Bridge baseline.
See docs/public-feature-contracts.md for provenance and host differences.
"""
import re
from datetime import datetime, timedelta
from typing import Any
TRACK_ROUTING_ROLES = {"origin", "primary_activity", "landing", "bridge", "routine"}
TRACK_EVENT_POLICIES = {"default", "rolling_engineering"}


def flushable_dialogue_units(messages, *, now):
    """Completed exchanges up to a real silence boundary, separately per session."""
    sessions = {}
    for item in sorted(messages, key=lambda item: item['id']):
        sessions.setdefault(item['session_id'], []).append(item)
    ready = []
    silence = timedelta(minutes=20)
    for rows in sessions.values():
        units = dialogue_units(rows)
        last = -1
        for index, unit in enumerate(units):
            times = [datetime.fromisoformat(m['created_at'].replace('Z', '+00:00')) for m in unit]
            end = max(times)
            if index + 1 < len(units):
                following = min(datetime.fromisoformat(m['created_at'].replace('Z', '+00:00'))
                                for m in units[index + 1])
                paused = following - end >= silence
            else:
                paused = end <= now - silence
            if paused:
                last = index
        ready.extend(unit for unit in units[:last + 1] if dialogue_unit_is_complete(unit))
    return sorted(ready, key=lambda unit: unit[0]['id'])
def dialogue_units(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    units: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_has_assistant = False
    current_starts_proactive = False
    current_has_user = False
    for message in messages:
        role = message["role"]
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        is_proactive = role == "assistant" and bool(
            metadata.get("proactive")
            or metadata.get("autonomy")
            or metadata.get("memory_event_source")
        )
        # Multiple proactive messages remain one pending exchange until the
        # user's first reply, regardless of silence between them.
        if is_proactive and current and not (current_starts_proactive and not current_has_user):
            units.append(current)
            current = []
            current_has_assistant = False
            current_starts_proactive = False
            current_has_user = False
        if role == "user" and current and current_has_assistant and not (
            current_starts_proactive and not current_has_user
        ):
            units.append(current)
            current = []
            current_has_assistant = False
            current_starts_proactive = False
            current_has_user = False
        if not current:
            current_starts_proactive = is_proactive
        current.append(message)
        if role == "user":
            current_has_user = True
        if role == "assistant":
            current_has_assistant = True
    if current:
        units.append(current)
    return units

def dialogue_unit_is_complete(unit: list[dict[str, Any]]) -> bool:
    if not unit:
        return False
    first = unit[0]
    first_metadata = first.get("metadata") if isinstance(first.get("metadata"), dict) else {}
    starts_proactive = first.get("role") == "assistant" and bool(
        first_metadata.get("proactive")
        or first_metadata.get("autonomy")
        or first_metadata.get("memory_event_source")
    )
    if starts_proactive:
        # A proactive/free-activity message is not independently complete.  The
        # user's first reply completes the pending interaction; a later assistant
        # landing joins when present, but is not required before routing.
        return any(item.get("role") == "user" for item in unit[1:])
    if first.get("role") == "assistant":
        # This is a late answer/landing whose initiating messages may already be
        # in the durable ledger.  Buffering it would block every later message
        # in the session; route it with the existing Track cards as context.
        return True
    if first.get("role") != "user":
        return False
    return any(item.get("role") == "assistant" for item in unit[1:])

def normalize_event_track_message_output(
    output: dict[str, Any],
    messages: list[dict[str, Any]],
    active_tracks: list[dict[str, Any]],
    *,
    session_id: int,
    next_track_ordinal: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    payload_keys = set(output).difference(
        {"_splitter_provider", "_splitter_model", "_splitter_provider_index", "_codex_job"}
    )
    expected_keys = {"message_assignments", "track_updates"}
    if not expected_keys.issubset(payload_keys):
        raise ValueError(
            "Track Router top-level fields missing: "
            f"{sorted(expected_keys - payload_keys)}"
        )
    raw_assignments = output.get("message_assignments")
    raw_updates = output.get("track_updates")
    if not isinstance(raw_assignments, list) or not isinstance(raw_updates, list):
        raise ValueError("Track Router must return message_assignments and track_updates")
    expected_ids = [int(item["id"]) for item in messages]
    existing = {
        str(track.get("track_id") or ""): track
        for track in active_tracks
        if str(track.get("track_id") or "")
    }
    assignments: list[dict[str, Any]] = []
    used_refs: list[str] = []
    required_fields = {
        "source_message_id",
        "primary_track_ref",
        "context_track_refs",
        "routing_role",
    }
    for index, raw in enumerate(raw_assignments):
        if not isinstance(raw, dict):
            raise ValueError(f"Track Router message assignment #{index + 1} must be an object")
        if not required_fields.issubset(raw):
            raise ValueError(
                f"Track Router message assignment #{index + 1} fields missing: "
                f"{sorted(required_fields - set(raw))}"
            )
        source_id = raw.get("source_message_id")
        primary_ref = str(raw.get("primary_track_ref") or "").strip()
        raw_context_refs = raw.get("context_track_refs")
        routing_role = str(raw.get("routing_role") or "").strip()
        if type(source_id) is not int or index >= len(expected_ids) or source_id != expected_ids[index]:
            expected_id = expected_ids[index] if index < len(expected_ids) else None
            raise ValueError(
                f"Track Router assignment #{index + 1} source_message_id={source_id!r}; "
                f"expected={expected_id!r} in source message order"
            )
        if primary_ref not in existing and not re.fullmatch(r"new:[1-9][0-9]*", primary_ref):
            raise ValueError("Track Router referenced an unknown primary Track")
        if not isinstance(raw_context_refs, list):
            raise ValueError("Track Router context_track_refs must be a list")
        context_refs: list[str] = []
        for value in raw_context_refs:
            ref = str(value or "").strip()
            if (
                not ref
                or ref == primary_ref
                or ref in context_refs
                or (ref not in existing and not re.fullmatch(r"new:[1-9][0-9]*", ref))
            ):
                raise ValueError("Track Router returned an invalid context Track")
            context_refs.append(ref)
        if routing_role not in TRACK_ROUTING_ROLES:
            raise ValueError("Track Router returned an invalid routing role")
        if bool(context_refs) != (routing_role == "bridge"):
            raise ValueError("Track Router bridge role must match declared context Tracks")
        assignments.append(
            {
                "source_message_id": source_id,
                "primary_track_ref": primary_ref,
                "context_track_refs": context_refs,
                "routing_role": routing_role,
            }
        )
        used_refs.extend([primary_ref, *context_refs])
    if len(assignments) != len(expected_ids):
        raise ValueError(f"Track Router omitted source_message_ids={expected_ids[len(assignments):]}")

    update_by_ref: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_updates):
        if not isinstance(raw, dict):
            raise ValueError(f"Track Router update #{index + 1} must be an object")
        required_update_fields = {"track_ref", "subject", "throughline", "status"}
        if not required_update_fields.issubset(raw):
            raise ValueError(
                f"Track Router update #{index + 1} fields missing: "
                f"{sorted(required_update_fields - set(raw))}"
            )
        track_ref = str(raw.get("track_ref") or "").strip()
        subject = " ".join(str(raw.get("subject") or "").split())
        throughline = " ".join(str(raw.get("throughline") or "").split())
        event_policy = str(raw.get("event_policy") or "").strip()
        status = str(raw.get("status") or "").strip()
        if track_ref in update_by_ref or (
            track_ref not in existing and not re.fullmatch(r"new:[1-9][0-9]*", track_ref)
        ):
            raise ValueError("Track Router updated an invalid or repeated Track")
        if not subject or len(subject) > 160 or not throughline or len(throughline) > 600:
            raise ValueError("Track Router update needs bounded subject and throughline")
        if status not in {"active", "parked"}:
            raise ValueError("Track Router update has invalid status")
        existing_policy = str((existing.get(track_ref) or {}).get("event_policy") or "default")
        if not event_policy:
            event_policy = existing_policy
        if event_policy not in TRACK_EVENT_POLICIES:
            raise ValueError("Track Router update has invalid event_policy")
        if existing_policy == "rolling_engineering":
            event_policy = existing_policy
        update_by_ref[track_ref] = {
            "subject": subject,
            "throughline": throughline,
            "event_policy": event_policy,
            "status": status,
        }
    used_ref_set = set(used_refs)
    if set(update_by_ref) != used_ref_set:
        raise ValueError(
            "Track Router updates must exactly cover all used Tracks: "
            f"missing={sorted(used_ref_set - set(update_by_ref))}, unused={sorted(set(update_by_ref) - used_ref_set)}"
        )

    new_refs = sorted(
        {ref for ref in used_refs if ref not in existing},
        key=lambda value: int(value.split(":", 1)[1]),
    )
    ref_to_track_id = {ref: ref for ref in existing}
    ordinal = next_track_ordinal
    for ref in new_refs:
        ref_to_track_id[ref] = f"session_{session_id}_track_{ordinal:04d}"
        ordinal += 1
    return (
        [
            {
                "source_message_id": item["source_message_id"],
                "primary_track_id": ref_to_track_id[item["primary_track_ref"]],
                "context_track_ids": [ref_to_track_id[ref] for ref in item["context_track_refs"]],
                "routing_role": item["routing_role"],
            }
            for item in assignments
        ],
        [
            {"track_id": ref_to_track_id[ref], **update_by_ref[ref]}
            for ref in dict.fromkeys(used_refs)
        ],
        ordinal,
    )
