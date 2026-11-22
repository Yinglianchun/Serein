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


def seed_samples(value, offset_completed, now=None, stage="routing"):
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
                    stage,
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


def test_eta_reads_dense_existing_history(settings):
    add_raw(settings, 100)
    with Store(settings.database) as store:
        store.conn.executemany("INSERT INTO pipeline_routes VALUES (?, '{}')",
                               [(i,) for i in range(1, 61)])
    seed_samples(settings, [(2 * step - 120, step) for step in range(61)])
    before = settings.database.read_bytes()
    report = pp.progress_report(settings.database)
    assert settings.database.read_bytes() == before
    assert report["eta_reliable"] is True
    assert report["throughput_per_hour"] == 1800.0
    assert report["eta_seconds"] == 80.0


@pytest.mark.parametrize("spacing", [2, 0.25])
def test_fast_sampling_preserves_time_span_and_latest_progress(settings, monkeypatch, spacing):
    add_raw(settings, 100)
    start = datetime.now(timezone.utc)
    current = start

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current

    monkeypatch.setattr(pp, "datetime", Clock)
    for step in range(int(120 / spacing) + 1):
        elapsed = step * spacing
        current = start + timedelta(seconds=elapsed)
        if elapsed > 0 and elapsed % 2 == 0:
            with Store(settings.database) as store:
                store.conn.execute("INSERT INTO pipeline_routes VALUES (?, '{}')", (int(elapsed / 2),))
        pp.record_sample(settings.database)
    with Store(settings.database, read_only=True) as store:
        rows = store.conn.execute("SELECT * FROM pipeline_progress_samples ORDER BY id").fetchall()
    assert len(rows) <= pp.KEEP_RECENT_SAMPLES
    assert (datetime.fromisoformat(rows[-1]["sampled_at"])
            - datetime.fromisoformat(rows[0]["sampled_at"])).total_seconds() >= 60
    assert datetime.fromisoformat(rows[-1]["sampled_at"]) == current
    assert rows[-1]["completed_count"] == 60
    report = pp.progress_report(settings.database)
    assert report["eta_reliable"] is True
    assert report["throughput_per_hour"] == 1800.0
    assert report["eta_seconds"] == 80.0


def test_fast_stage_changes_preserve_pause_and_restart_measurement(settings):
    add_raw(settings, 100)
    seed_samples(settings, [(-120, 0), (-60, 1), (0, 2)])
    assert pp.progress_report(settings.database)["eta_reliable"] is True
    pp.record_sample(settings.database, "blocked")
    assert pp.progress_report(settings.database)["stage"] == "blocked"
    pp.record_sample(settings.database)
    pp.record_sample(settings.database)
    report = pp.progress_report(settings.database)
    assert report["stage"] == "routing"
    assert report["eta_reason"] == "insufficient_samples"
    with Store(settings.database, read_only=True) as store:
        stages = [row[0] for row in store.conn.execute(
            "SELECT stage FROM pipeline_progress_samples ORDER BY id")]
    assert stages == ["routing"] * 3 + ["blocked", "routing", "routing"]


def test_sample_retention_stays_bounded(settings):
    add_raw(settings, 4)
    seed_samples(settings, [(-60 * step, 0) for step in range(250, 0, -1)])
    pp.record_sample(settings.database)
    with Store(settings.database, read_only=True) as store:
        count = store.conn.execute("SELECT COUNT(*) FROM pipeline_progress_samples").fetchone()[0]
    assert count == pp.KEEP_RECENT_SAMPLES


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
    assert client.get("/v1/pipeline/progress").json() == data


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


def test_auto_boundary_excluded_until_explicitly_restored(settings):
    from serein.deployment import save_settings

    save_settings(settings.database, {"pipeline": {"auto_enabled": False}})
    add_raw(settings, 4)
    save_settings(settings.database, {"pipeline": {"auto_enabled": True}})
    assert p.new_batch(settings.database, True) is None
    client = TestClient(create_app(settings, token="test", live=True),
                        headers={"Authorization": "Bearer test"})
    before = settings.database.read_bytes()
    response = client.get("/api/pipeline/progress")
    assert response.status_code == 200
    assert settings.database.read_bytes() == before
    report = response.json()
    assert report["archived"] == 4
    assert report["admitted"] == report["processed"] == report["pending"] == 0
    assert report["stage"] == "empty"

    # New originals still enter the backlog; restoring the boundary admits
    # the old originals again without changing the archive total.
    add_raw(settings, 6)
    report = pp.progress_report(settings.database)
    assert report["archived"] == 6
    assert report["admitted"] == report["pending"] == 2
    assert report["stage"] == "routing"
    assert p.restore_auto_boundary(settings.database, "RESTORE_AUTO_BOUNDARY")["restored_originals"] == 4
    report = pp.progress_report(settings.database)
    assert report["archived"] == report["admitted"] == report["pending"] == 6


def test_legacy_archived_only_rows_do_not_obscure_settled_or_skipped_work(settings):
    add_raw(settings, 4)
    with Store(settings.database) as store:
        ids = [r[0] for r in store.conn.execute("SELECT id FROM raw_events ORDER BY id")]
        store.conn.executemany(
            "INSERT INTO raw_processing VALUES (?, 'legacy-originals:synthetic', 'archived_only')",
            [(i,) for i in ids[:2]],
        )
        store.conn.executemany(
            "INSERT INTO raw_processing VALUES (?, 'b1', ?)",
            [(ids[2], "settled"), (ids[3], "skipped")],
        )
        store.conn.executemany("INSERT INTO pipeline_routes VALUES (?, '{}')", [(i,) for i in ids[2:]])
    report = pp.progress_report(settings.database)
    assert report["archived"] == 4
    assert report["admitted"] == report["routed"] == report["processed"] == 2
    assert report["pending"] == 0
    assert report["stage"] == "done"
    assert report["percent"] == 100.0
    assert report["eta_seconds"] == 0


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


def test_real_task_pause_suppresses_api_eta_without_writing(settings):
    from serein import work_tasks
    add_raw(settings, 6)
    seed_samples(settings, [(-180, 0), (-120, 1), (-60, 1), (0, 2)])
    work_tasks.enqueue(settings.database, "pipeline")
    work_tasks.pause(settings.database, "pipeline")
    client = TestClient(create_app(settings, token="test", live=True),
                        headers={"Authorization": "Bearer test"})
    before = settings.database.read_bytes()
    response = client.get("/api/pipeline/progress")
    assert response.status_code == 200
    assert settings.database.read_bytes() == before
    assert response.json()["stage"] == "blocked"
    assert response.json()["eta_seconds"] is None


def test_processing_needs_its_own_samples(settings):
    add_raw(settings, 10)
    with Store(settings.database) as store:
        ids = [r[0] for r in store.conn.execute("SELECT id FROM raw_events ORDER BY id")]
        store.conn.executemany("INSERT INTO pipeline_routes VALUES (?, '{}')", [(i,) for i in ids])
        store.conn.executemany("INSERT INTO raw_processing (raw_id, operation_id, outcome) VALUES (?, 'b1', 'settled')",
                               [(i,) for i in ids[:2]])
    now = datetime.now(timezone.utc)
    seed_samples(settings, [(-300, 0), (-240, 4), (-180, 8)], now=now)
    seed_samples(settings, [(-120, 0)], now=now, stage="processing")
    report = pp.progress_report(settings.database)
    assert report["stage"] == "processing"
    assert report["eta_reason"] == "insufficient_samples"
    assert report["samples"] == 1
    seed_samples(settings, [(-60, 1), (0, 2)], now=now, stage="processing")
    report = pp.progress_report(settings.database)
    assert report["throughput_per_hour"] == 60.0
    assert report["eta_seconds"] == 480.0


def test_returning_stage_does_not_reuse_older_segment(settings):
    add_raw(settings, 10)
    now = datetime.now(timezone.utc)
    seed_samples(settings, [(-240, 0), (-180, 2), (-120, 4)], now=now)
    seed_samples(settings, [(-60, 1)], now=now, stage="processing")
    seed_samples(settings, [(0, 5)], now=now)
    report = pp.progress_report(settings.database)
    assert report["eta_reason"] == "insufficient_samples"
    assert report["samples"] == 1


@pytest.mark.parametrize("state,fields", [
    ("running", {"stop_requested": True, "lease_until": 4102444800}),
    ("running", {"lease_until": 1}),
    ("needs_repair", {}),
    ("awaiting_agent", {}),
    ("failed", {}),
])
def test_waiting_or_interrupted_work_has_no_eta_and_get_is_read_only(settings, state, fields):
    import json
    add_raw(settings, 6)
    seed_samples(settings, [(-180, 0), (-120, 1), (-60, 1), (0, 2)])
    task = {"id": "pipeline", "status": state, **fields}
    with Store(settings.database) as store:
        store.conn.execute("INSERT INTO background_state VALUES ('work:pipeline', ?)", (json.dumps(task),))
    before = settings.database.read_bytes()
    report = pp.progress_report(settings.database)
    assert settings.database.read_bytes() == before
    assert report["stage"] == "blocked"
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
