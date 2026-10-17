"""Backlog, throughput and a conservative ETA for the event pipeline.

The progress report is intentionally read-only and never fabricates a
countdown. Counts are separated by processing scope:

* ``archived`` is the full raw-event total;
* only events admitted to processing (no closed import boundary, no
  unresolved image hold) belong to the processing scope, so archived-only
  history is never reported as a pending to-do;
* routing completion is never reported as whole-pipeline completion, and an
  ETA is suppressed immediately when the pipeline is paused or blocked.

An ETA is returned only when the recent samples are fresh, span a wide enough
window and show real progress; otherwise ``eta_reason`` explains why an
estimate would be misleading (cold start, pause, throttling, stall).
"""
from datetime import datetime, timedelta, timezone

from ..core.store import Store

# Sliding window used to measure throughput.
MIN_SAMPLES = 3
MIN_WINDOW_SECONDS = 60.0
WINDOW_SECONDS = 1800.0
# No successful advance within this age means the pipeline is paused/blocked.
STALE_SAMPLE_SECONDS = 300.0
# Keep the sample table bounded.
KEEP_RECENT_SAMPLES = 200


def create_samples_table(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pipeline_progress_samples(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sampled_at TEXT NOT NULL,
            stage TEXT NOT NULL,
            completed_count INTEGER NOT NULL,
            total_count INTEGER NOT NULL)"""
    )


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _admission_scope_clause(conn):
    """Restrict raw events to the admitted processing scope.

    Excludes events whose import boundary is still closed and events held
    pending image transcription. Uses the ``raw_events r`` alias.
    """
    clause = ""
    if _table_exists(conn, "pipeline_import_boundaries"):
        clause += (
            " AND NOT EXISTS (SELECT 1 FROM pipeline_import_boundaries b "
            "WHERE b.upload_id=json_extract(r.metadata_json,"
            "'$.import_upload_id') AND b.released=0)")
    if _table_exists(conn, "pipeline_image_holds"):
        clause += (
            " AND NOT EXISTS (SELECT 1 FROM pipeline_image_holds h "
            "WHERE h.raw_id=r.id AND h.event_hash=r.event_hash)")
    return clause


def counts(store):
    """Return archived totals separated from the processing backlog.

    Archived-only history (closed import boundary) and unresolved image holds
    are kept out of the processing scope, so they are never reported as a
    pending to-do. Routing and settlement are counted separately.
    """
    empty = {
        "archived": 0, "admitted": 0, "total": 0, "routed": 0,
        "processed": 0, "completed": 0, "pending": 0,
        "pending_unrouted": 0, "pending_unprocessed": 0,
    }
    if not _table_exists(store.conn, "raw_events"):
        return empty
    archived = store.conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
    scope = _admission_scope_clause(store.conn)
    admitted = store.conn.execute(
        "SELECT COUNT(*) FROM raw_events r WHERE 1=1" + scope
    ).fetchone()[0]
    # Routed = admitted raw events with a durable route.
    routed = 0
    if _table_exists(store.conn, "pipeline_route_provenance"):
        routed = store.conn.execute(
            "SELECT COUNT(DISTINCT r.id) FROM raw_events r WHERE 1=1"
            + scope + " AND (r.id IN (SELECT raw_id FROM pipeline_routes) "
            "OR r.id IN (SELECT raw_id FROM pipeline_route_provenance))"
        ).fetchone()[0]
    elif _table_exists(store.conn, "pipeline_routes"):
        routed = store.conn.execute(
            "SELECT COUNT(DISTINCT r.id) FROM raw_events r WHERE 1=1"
            + scope + " AND r.id IN (SELECT raw_id FROM pipeline_routes)"
        ).fetchone()[0]
    # Processed = admitted raw events recorded by Curator/Writer settlement
    # (settled or skipped). Deferred events are intentionally absent.
    processed = 0
    if _table_exists(store.conn, "raw_processing"):
        processed = store.conn.execute(
            "SELECT COUNT(DISTINCT r.id) FROM raw_events r WHERE 1=1"
            + scope + " AND r.id IN (SELECT raw_id FROM raw_processing)"
        ).fetchone()[0]
    routed = min(routed, admitted)
    processed = min(processed, admitted)
    return {
        "archived": archived,
        "admitted": admitted,
        "total": admitted,
        "routed": routed,
        "processed": processed,
        "pending_unrouted": admitted - routed,
        "pending_unprocessed": admitted - processed,
    }


def _completed_for(current):
    """Return (completed, pending) over the admitted scope, ignoring pauses.

    While routing is incomplete, completion tracks routing; once everything is
    routed, completion tracks downstream settlement so routing is never
    mistaken for whole-pipeline completion.
    """
    admitted = current["admitted"]
    if admitted == 0:
        return 0, 0
    if current["pending_unrouted"] > 0:
        return current["routed"], current["pending_unrouted"]
    if current["pending_unprocessed"] > 0:
        return current["processed"], current["pending_unprocessed"]
    return admitted, 0


def stage_for(current, result_status):
    """Stage over the admitted scope, honoring pause/block status."""
    if current["admitted"] == 0:
        return "empty"
    if result_status in ("paused", "blocked"):
        return "blocked"
    if current["pending_unrouted"] > 0:
        return "routing"
    if current["pending_unprocessed"] > 0:
        return "processing"
    return "done"


def record_sample(database, result_status=""):
    """Append one progress sample after an advance; safe to call on every run."""
    with Store(database) as store:
        create_samples_table(store.conn)
        current = counts(store)
        stage = stage_for(current, result_status)
        completed, _ = _completed_for(current)
        store.conn.execute(
            """INSERT INTO pipeline_progress_samples
                (sampled_at, stage, completed_count, total_count)
                VALUES (?,?,?,?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                stage,
                completed,
                current["admitted"],
            ),
        )
        store.conn.execute(
            """DELETE FROM pipeline_progress_samples WHERE id NOT IN (
                SELECT id FROM pipeline_progress_samples
                ORDER BY id DESC LIMIT ?)""",
            (KEEP_RECENT_SAMPLES,),
        )


def _parse(value):
    return datetime.fromisoformat(value)


def _read_samples(store):
    if not _table_exists(store.conn, "pipeline_progress_samples"):
        return []
    rows = store.conn.execute(
        """SELECT sampled_at, stage, completed_count, total_count
            FROM pipeline_progress_samples
            ORDER BY id DESC LIMIT ?""",
        (MIN_SAMPLES * 4,),
    ).fetchall()
    return [dict(row) for row in reversed(rows)]


def estimate(samples, pending, now):
    """Measure throughput over the sliding window; return a conservative ETA."""
    if not samples:
        return None, "insufficient_samples", 0
    latest = samples[-1]
    latest_time = _parse(latest["sampled_at"])
    if (now - latest_time).total_seconds() > STALE_SAMPLE_SECONDS:
        return None, "paused_or_throttled", 0
    # Walk back to the oldest sample still inside the window. Samples are
    # ascending in time, so the first one inside the window is the start.
    start = latest
    for item in samples:
        item_time = _parse(item["sampled_at"])
        if (latest_time - item_time).total_seconds() <= WINDOW_SECONDS:
            start = item
            break
    span = (latest_time - _parse(start["sampled_at"])).total_seconds()
    usable = [
        item for item in samples
        if _parse(start["sampled_at"]) <= _parse(item["sampled_at"]) <= latest_time
    ]
    if len(usable) < MIN_SAMPLES or span < MIN_WINDOW_SECONDS:
        return None, "insufficient_samples", 0
    completed_delta = latest["completed_count"] - start["completed_count"]
    if completed_delta <= 0 or span <= 0:
        return None, "stalled", 0
    rate = completed_delta / span  # routed messages per second
    if pending <= 0:
        return 0, None, rate
    return pending / rate, None, rate


def _humanize(seconds):
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {seconds} 秒"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时 {minutes} 分"
    days, hours = divmod(hours, 24)
    return f"{days} 天 {hours} 小时"


def progress_report(database, result_status=""):
    """Return a JSON-friendly backlog / throughput / ETA report."""
    now = datetime.now(timezone.utc)
    with Store(database, read_only=True) as store:
        current = counts(store)
        samples = _read_samples(store)
    latest = samples[-1] if samples else None
    stage = stage_for(current, result_status)
    # A freshly recorded blocked sample must be honored even if the caller did
    # not pass the result status: suppress the ETA immediately rather than
    # waiting for the sample to age out.
    if stage in ("routing", "processing") and latest is not None:
        if latest["stage"] == "blocked":
            stage = "blocked"
    completed, pending = _completed_for(current)
    if stage == "done":
        eta_seconds, reason, rate = 0, None, 0
    elif stage == "empty":
        eta_seconds, reason, rate = None, "insufficient_samples", 0
    elif stage == "blocked":
        eta_seconds, reason, rate = None, "paused_or_blocked", 0
    else:
        eta_seconds, reason, rate = estimate(samples, pending, now)
    percent = (
        round(completed * 100.0 / current["admitted"], 2)
        if current["admitted"]
        else 0.0
    )
    return {
        "stage": stage,
        "archived": current["archived"],
        "admitted": current["admitted"],
        "total": current["admitted"],
        "routed": current["routed"],
        "processed": current["processed"],
        "completed": completed,
        "pending": pending,
        "percent": percent,
        "throughput_per_hour": round(rate * 3600.0, 1) if rate > 0 else None,
        "samples": len(samples),
        "eta_seconds": round(eta_seconds, 1) if eta_seconds is not None else None,
        "eta_human": "已完成" if stage == "done"
        else (_humanize(eta_seconds) if eta_seconds is not None else None),
        "eta_reliable": eta_seconds is not None,
        "eta_reason": reason,
    }
