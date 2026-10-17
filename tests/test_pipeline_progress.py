"""Tests for event pipeline backlog, throughput and conservative ETA."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from serein.api.http import create_app
from serein.bootstrap import initialize
from serein.compat.raw_archive import raw_archive
from serein.config import Settings
from serein.core.store import Store
from serein.extensions import pipeline as p
from serein.extensions import pipeline_progress as pp


@pytest.fixture
def settings(tmp_path):
    value = Settings(tmp_path / "memory.db", tmp_path / "index.db", writable=True)
    initialize(value)
    p.initialize(value.database)
    return value


def add_raw(value, number):
    events = [
        {
            "source_event_id": f"e{i}",
            "session_id": "s",
            "role": "user",
            "text": f"消息{i}",
            "created_at": f"2026-09-01T00:00:{i:02d}Z",
        }
        for i in range(number)
    ]
    raw_archive(value).ingest(events, source="test")


def seed_samples(value, offset_completed, now=None):
    """Insert (offset_seconds, completed) pairs; offsets relative to now."""
    now = now or datetime.now(timezone.utc)
    with Store(value.database) as store:
        pp.create_samples_table(store.conn)
        total = store.conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
        for offset, completed in offset_completed:
            store.conn.execute(
                """INSERT INTO pipeline_progress_samples
                    (sampled_at, stage, completed_count, total_count)
                    VALUES (?,?,?,?)""",
                (
                    (now + timedelta(seconds=offset)).isoformat(),
                    "routing",
                    completed,
                    total,
                ),
            )


def test_empty_counts_and_stage(settings):
    report = pp.progress_report(settings.database)
    assert report["stage"] == "empty"
    assert report["total"] == 0 and report["pending"] == 0
    assert report["eta_reliable"] is False
    assert report["eta_reason"] == "insufficient_samples"


def test_counts_partial(settings):
    add_raw(settings, 100)
    with Store(settings.database) as store:
        ids = [r[0] for r in store.conn.execute(
            "SELECT id FROM raw_events ORDER BY id LIMIT 40")]
        store.conn.executemany(
            "INSERT OR REPLACE INTO pipeline_routes (raw_id, route_json) VALUES (?, '{}')",
            [(i,) for i in ids],
        )
    report = pp.progress_report(settings.database)
    assert report["total"] == 100
    assert report["completed"] == 40
    assert report["pending"] == 60
    assert report["percent"] == 40.0
    assert report["stage"] == "routing"


def test_record_sample_appends(settings):
    add_raw(settings, 10)
    pp.record_sample(settings.database, "processed")
    with Store(settings.database) as store:
        rows = store.conn.execute(
            "SELECT stage, completed_count, total_count FROM pipeline_progress_samples"
        ).fetchall()
    assert len(rows) == 1
    assert dict(rows[0]) == {
        "stage": "routing",
        "completed_count": 0,
        "total_count": 10,
    }


def test_eta_reliable_when_steady(settings):
    add_raw(settings, 100)
    with Store(settings.database) as store:
        ids = [r[0] for r in store.conn.execute(
            "SELECT id FROM raw_events ORDER BY id LIMIT 40")]
        store.conn.executemany(
            "INSERT OR REPLACE INTO pipeline_routes (raw_id, route_json) VALUES (?, '{}')",
            [(i,) for i in ids],
        )
    # 5 samples, 10 more routed every 60 s -> 600/hour.
    seed_samples(settings, [(-240, 0), (-180, 10), (-120, 20),
                            (-60, 30), (0, 40)])
    report = pp.progress_report(settings.database)
    assert report["eta_reliable"] is True
    assert report["eta_reason"] is None
    assert report["throughput_per_hour"] == 600.0
    assert report["eta_seconds"] == 360.0


def test_eta_cold_start_with_few_samples(settings):
    add_raw(settings, 100)
    seed_samples(settings, [(0, 10), (-60, 20)])
    report = pp.progress_report(settings.database)
    assert report["eta_reliable"] is False
    assert report["eta_reason"] == "insufficient_samples"


def test_eta_paused_or_throttled_when_stale(settings):
    add_raw(settings, 100)
    now = datetime.now(timezone.utc)
    # Newest sample is 10 minutes old.
    seed_samples(settings, [(-840, 10), (-780, 20), (-720, 30),
                            (-660, 40), (-600, 50)], now=now)
    report = pp.progress_report(settings.database)
    assert report["eta_reliable"] is False
    assert report["eta_reason"] == "paused_or_throttled"


def test_eta_stalled_when_no_progress(settings):
    add_raw(settings, 100)
    # Enough fresh samples, but completed count never changes.
    seed_samples(settings, [(-240, 20), (-180, 20), (-120, 20),
                            (-60, 20), (0, 20)])
    report = pp.progress_report(settings.database)
    assert report["eta_reliable"] is False
    assert report["eta_reason"] == "stalled"


def test_eta_unreliable_for_short_window(settings):
    add_raw(settings, 100)
    # 3 samples but the whole span is 10 seconds.
    seed_samples(settings, [(-10, 10), (-5, 20), (0, 30)])
    report = pp.progress_report(settings.database)
    assert report["eta_reliable"] is False
    assert report["eta_reason"] == "insufficient_samples"


def test_done_report_has_zero_eta(settings):
    add_raw(settings, 10)
    with Store(settings.database) as store:
        ids = [r[0] for r in store.conn.execute("SELECT id FROM raw_events")]
        store.conn.executemany(
            "INSERT OR REPLACE INTO pipeline_routes (raw_id, route_json) VALUES (?, '{}')",
            [(i,) for i in ids],
        )
        # Done requires downstream Curator/Writer settlement, not only routing.
        store.conn.executemany(
            "INSERT OR IGNORE INTO raw_processing (raw_id, operation_id, outcome) "
            "VALUES (?, ?, 'settled')",
            [(i, "b1") for i in ids],
        )
    report = pp.progress_report(settings.database, "processed")
    assert report["stage"] == "done"
    assert report["pending"] == 0 and report["percent"] == 100.0
    assert report["eta_seconds"] == 0
    assert report["eta_reliable"] is True


def test_advance_records_a_sample(settings):
    add_raw(settings, 3)
    # No stable units / no configured models -> advance returns current and
    # still records a progress sample through the hook.
    asyncio.run(p.advance(settings.database))
    with Store(settings.database) as store:
        count = store.conn.execute(
            "SELECT COUNT(*) FROM pipeline_progress_samples"
        ).fetchone()[0]
    assert count >= 1


def test_progress_api_endpoint(settings):
    add_raw(settings, 100)
    with Store(settings.database) as store:
        ids = [r[0] for r in store.conn.execute(
            "SELECT id FROM raw_events ORDER BY id LIMIT 40")]
        store.conn.executemany(
            "INSERT OR REPLACE INTO pipeline_routes (raw_id, route_json) VALUES (?, '{}')",
            [(i,) for i in ids],
        )
    seed_samples(settings, [(-240, 0), (-180, 10), (-120, 20),
                            (-60, 30), (0, 40)])
    client = TestClient(
        create_app(settings, token="test", live=True),
        headers={"Authorization": "Bearer test"},
    )
    response = client.get("/api/pipeline/progress")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 100 and data["completed"] == 40
    assert data["eta_reliable"] is True
    assert data["eta_human"] == "6 分 0 秒"


# --- Review fixes: archived-only scope, routing vs settlement, pause ETA ---


def test_archived_only_history_is_not_pending(settings):
    # 4 events archived but the import boundary is still closed: nothing has
    # been admitted to processing, so there is no pending to-do.
    add_raw(settings, 4)
    with Store(settings.database) as store:
        store.conn.execute(
            "UPDATE raw_events SET metadata_json=? WHERE metadata_json='{}'",
            ('{"import_upload_id":"up1"}',),
        )
        store.conn.execute(
            "INSERT INTO pipeline_import_boundaries (upload_id, released) "
            "VALUES (?,0)",
            ("up1",),
        )
    report = pp.progress_report(settings.database)
    assert report["archived"] == 4
    assert report["admitted"] == 0
    assert report["total"] == 0
    assert report["pending"] == 0
    assert report["stage"] == "empty"


def test_image_held_events_kept_out_of_scope(settings):
    add_raw(settings, 4)
    with Store(settings.database) as store:
        row = store.conn.execute(
            "SELECT id, event_hash FROM raw_events LIMIT 1").fetchone()
        store.conn.execute(
            "INSERT OR IGNORE INTO pipeline_image_holds "
            "(raw_id, sha256, event_hash) VALUES (?,?,?)",
            (row["id"], "x1", row["event_hash"]),
        )
    report = pp.progress_report(settings.database)
    assert report["archived"] == 4
    assert report["admitted"] == 3
    assert report["pending"] == 3


def test_routed_not_done_without_settlement(settings):
    # Daytime routing may be persisted before Curator/Writer settlement.
    add_raw(settings, 4)
    with Store(settings.database) as store:
        ids = [r[0] for r in store.conn.execute("SELECT id FROM raw_events")]
        store.conn.executemany(
            "INSERT OR REPLACE INTO pipeline_routes (raw_id, route_json) "
            "VALUES (?, '{}')",
            [(i,) for i in ids],
        )
    report = pp.progress_report(settings.database, "processed")
    assert report["stage"] == "processing"
    assert report["routed"] == 4 and report["processed"] == 0
    assert report["pending"] == 4
    assert report["percent"] == 0.0
    assert report["eta_reliable"] is False


def test_eta_suppressed_immediately_when_paused(settings):
    add_raw(settings, 6)
    with Store(settings.database) as store:
        ids = [r[0] for r in store.conn.execute(
            "SELECT id FROM raw_events ORDER BY id LIMIT 2")]
        store.conn.executemany(
            "INSERT OR REPLACE INTO pipeline_routes (raw_id, route_json) "
            "VALUES (?, '{}')",
            [(i,) for i in ids],
        )
    # Fresh samples showing steady progress over 180 s.
    seed_samples(settings, [(-180, 0), (-120, 1), (-60, 1), (0, 2)])
    report = pp.progress_report(settings.database, "paused")
    assert report["stage"] == "blocked"
    assert report["eta_reliable"] is False
    assert report["eta_reason"] == "paused_or_blocked"
    assert report["eta_seconds"] is None


def test_eta_suppressed_by_fresh_blocked_sample(settings):
    # Newest sample is fresh but already records the blocked stage; the status
    # argument is not passed. The ETA must still be suppressed immediately.
    add_raw(settings, 6)
    with Store(settings.database) as store:
        ids = [r[0] for r in store.conn.execute(
            "SELECT id FROM raw_events ORDER BY id LIMIT 2")]
        store.conn.executemany(
            "INSERT OR REPLACE INTO pipeline_routes (raw_id, route_json) "
            "VALUES (?, '{}')",
            [(i,) for i in ids],
        )
    now = datetime.now(timezone.utc)
    with Store(settings.database) as store:
        pp.create_samples_table(store.conn)
        total = store.conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
        for offset, stage, completed in [
                (-180, "routing", 0), (-120, "routing", 1),
                (-60, "routing", 2), (0, "blocked", 2)]:
            store.conn.execute(
                """INSERT INTO pipeline_progress_samples
                    (sampled_at, stage, completed_count, total_count)
                    VALUES (?,?,?,?)""",
                ((now + timedelta(seconds=offset)).isoformat(),
                 stage, completed, total),
            )
    report = pp.progress_report(settings.database)
    assert report["stage"] == "blocked"
    assert report["eta_reliable"] is False
    assert report["eta_reason"] == "paused_or_blocked"
