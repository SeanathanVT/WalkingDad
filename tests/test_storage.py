"""Tests query sqlite3 directly in places storage.py has no read API for (e.g. raw pause rows);
that's a test-code exception to the "no SQL outside storage.py" rule, which governs app code."""
import sqlite3
import threading

import pytest

import storage


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "test.db")
    storage.init_db(path)
    return path


def _raw_pauses(db, session_id):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT start_time, end_time, reason FROM pauses WHERE session_id = ?",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()


def test_init_db_idempotent(db):
    storage.init_db(db)  # second call must not raise
    conn = sqlite3.connect(db)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert {"meta", "sessions", "pauses", "samples"} <= tables
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        assert version == "2"
    finally:
        conn.close()


def test_create_session_returns_id_and_active_status(db):
    session_id = storage.create_session("2026-01-01T08:00:00", "KS-BLC2", "1.0", profile="alice")
    import uuid
    uuid.UUID(session_id)  # raises if not a valid UUID
    session = storage.get_session(session_id)
    assert session["status"] == "active"
    assert session["device_model"] == "KS-BLC2"
    assert session["app_version"] == "1.0"
    assert session["profile"] == "alice"


def test_create_session_default_profile(db):
    session_id = storage.create_session("2026-01-01T08:00:00", None, None)
    assert storage.get_session(session_id)["profile"] == "default"


def test_add_pause_and_end_pause(db):
    session_id = storage.create_session("2026-01-01T08:00:00", None, None)
    storage.add_pause(session_id, "2026-01-01T08:05:00", "manual")
    rows = _raw_pauses(db, session_id)
    assert len(rows) == 1
    assert rows[0][1] is None

    storage.end_pause(session_id, "2026-01-01T08:06:00")
    rows = _raw_pauses(db, session_id)
    assert rows[0][1] == "2026-01-01T08:06:00"
    assert rows[0][2] == "manual"


def test_end_pause_no_open_pause_is_noop(db):
    session_id = storage.create_session("2026-01-01T08:00:00", None, None)
    storage.end_pause(session_id, "2026-01-01T08:06:00")  # must not raise


def test_add_pause_unknown_session_raises_integrity_error(db):
    with pytest.raises(sqlite3.IntegrityError):
        storage.add_pause("nonexistent-id", "2026-01-01T08:00:00", "manual")


def test_append_samples_batch_insert(db):
    session_id = storage.create_session("2026-01-01T08:00:00", None, None)
    storage.append_samples(session_id, [
        (1000, 1.0, 1.0, 1, 1, None),
        (0, 0.0, 0.0, 0, 1, None),
    ])
    samples = storage.get_samples(session_id)
    assert [s["t_ms"] for s in samples] == [0, 1000]
    assert samples[1]["speed_mps"] == 1.0
    assert samples[1]["distance_m"] == 1.0
    assert samples[1]["steps"] == 1
    assert samples[1]["belt_running"] == 1


def test_append_samples_empty_list_noop(db):
    session_id = storage.create_session("2026-01-01T08:00:00", None, None)
    storage.append_samples(session_id, [])
    assert storage.get_samples(session_id) == []


def test_complete_session_sets_totals_and_status(db):
    session_id = storage.create_session("2026-01-01T08:00:00", None, None)
    storage.complete_session(session_id, {
        "end_time": "2026-01-01T08:05:00",
        "elapsed_s": 300,
        "moving_s": 280,
        "distance_m": 400,
        "steps": 600,
        "calories_kcal": 30,
        "calories_estimated": 1,
        "avg_speed_mps": 1.4,
        "max_speed_mps": 1.8,
    })
    session = storage.get_session(session_id)
    assert session["status"] == "completed"
    assert session["end_time"] == "2026-01-01T08:05:00"
    assert session["elapsed_s"] == 300
    assert session["moving_s"] == 280
    assert session["distance_m"] == 400
    assert session["steps"] == 600
    assert session["calories_kcal"] == 30
    assert session["calories_estimated"] == 1
    assert session["avg_speed_mps"] == 1.4
    assert session["max_speed_mps"] == 1.8


def test_complete_session_partial_summary_leaves_nulls(db):
    session_id = storage.create_session("2026-01-01T08:00:00", None, None)
    storage.complete_session(session_id, {"end_time": "2026-01-01T08:05:00", "distance_m": 400})
    session = storage.get_session(session_id)
    assert session["status"] == "completed"
    assert session["distance_m"] == 400
    assert session["max_speed_mps"] is None


def test_delete_session_cascades(db):
    session_id = storage.create_session("2026-01-01T08:00:00", None, None)
    storage.add_pause(session_id, "2026-01-01T08:01:00", "manual")
    storage.append_samples(session_id, [(0, 1.0, 1.0, 1, 1, None)])

    storage.delete_session(session_id)

    assert storage.get_session(session_id) is None
    assert storage.get_samples(session_id) == []
    assert _raw_pauses(db, session_id) == []


def test_list_sessions_only_completed_newest_first(db):
    older = storage.create_session("2026-01-01T08:00:00", None, None)
    newer = storage.create_session("2026-01-02T08:00:00", None, None)
    active = storage.create_session("2026-01-03T08:00:00", None, None)
    storage.complete_session(older, {"end_time": "2026-01-01T08:05:00"})
    storage.complete_session(newer, {"end_time": "2026-01-02T08:05:00"})

    sessions = storage.list_sessions()

    assert [s["id"] for s in sessions] == [newer, older]
    assert active not in [s["id"] for s in sessions]


def test_list_sessions_limit(db):
    for i in range(3):
        sid = storage.create_session(f"2026-01-0{i + 1}T08:00:00", None, None)
        storage.complete_session(sid, {"end_time": f"2026-01-0{i + 1}T08:05:00"})

    assert len(storage.list_sessions(limit=2)) == 2


def test_list_sessions_profile_filter(db):
    a = storage.create_session("2026-01-01T08:00:00", None, None, profile="a")
    b = storage.create_session("2026-01-01T09:00:00", None, None, profile="b")
    storage.complete_session(a, {"end_time": "2026-01-01T08:05:00"})
    storage.complete_session(b, {"end_time": "2026-01-01T09:05:00"})

    sessions = storage.list_sessions(profile="a")

    assert [s["id"] for s in sessions] == [a]


def test_get_session_missing_returns_none(db):
    assert storage.get_session("nonexistent-id") is None


def test_get_samples_missing_session_returns_empty_list(db):
    assert storage.get_samples("nonexistent-id") == []


def test_clear_history_all(db):
    completed = storage.create_session("2026-01-01T08:00:00", None, None)
    storage.append_samples(completed, [(0, 1.0, 1.0, 1, 1, None)])
    storage.complete_session(completed, {"end_time": "2026-01-01T08:05:00"})
    active = storage.create_session("2026-01-02T08:00:00", None, None)

    storage.clear_history()

    assert storage.get_session(completed) is None
    assert storage.get_samples(completed) == []
    assert storage.get_session(active) is not None


def test_clear_history_by_profile(db):
    a = storage.create_session("2026-01-01T08:00:00", None, None, profile="a")
    b = storage.create_session("2026-01-01T09:00:00", None, None, profile="b")
    storage.complete_session(a, {"end_time": "2026-01-01T08:05:00"})
    storage.complete_session(b, {"end_time": "2026-01-01T09:05:00"})

    storage.clear_history(profile="a")

    assert storage.get_session(a) is None
    assert storage.get_session(b) is not None


def test_mark_health_logged(db):
    session_id = storage.create_session("2026-01-01T08:00:00", None, None)
    assert storage.get_session(session_id)["health_logged"] == 0

    storage.mark_health_logged(session_id)

    assert storage.get_session(session_id)["health_logged"] == 1


def test_concurrent_writes(db):
    errors = []

    def worker():
        try:
            session_id = storage.create_session("2026-01-01T08:00:00", None, None)
            for batch_start in range(0, 20, 5):
                storage.append_samples(session_id, [
                    (t, 1.0, float(t), t, 1, None)
                    for t in range(batch_start * 1000, (batch_start + 5) * 1000, 1000)
                ])
            storage.complete_session(session_id, {"end_time": "2026-01-01T08:05:00"})
        except Exception as exc:  # noqa: BLE001 - collected across threads, re-raised below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors

    sessions = storage.list_sessions()
    assert len(sessions) == 5
    total_samples = sum(len(storage.get_samples(s["id"])) for s in sessions)
    assert total_samples == 5 * 20
