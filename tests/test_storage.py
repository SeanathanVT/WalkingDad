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
        assert {"meta", "sessions", "pauses", "samples", "profiles"} <= tables
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        assert version == "3"
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


def test_list_active_sessions(db):
    active = storage.create_session("2026-01-01T08:00:00", None, None)
    done = storage.create_session("2026-01-01T09:00:00", None, None)
    storage.complete_session(done, {"end_time": "2026-01-01T09:05:00"})

    assert [s["id"] for s in storage.list_active_sessions()] == [active]


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


def test_add_pause_is_noop_while_one_is_open(db):
    session_id = storage.create_session("2026-01-01T08:00:00", None, None)
    storage.add_pause(session_id, "2026-01-01T08:05:00", "manual")
    storage.add_pause(session_id, "2026-01-01T08:05:02", "auto")
    assert _raw_pauses(db, session_id) == [("2026-01-01T08:05:00", None, "manual")]

    storage.end_pause(session_id, "2026-01-01T08:06:00")
    storage.add_pause(session_id, "2026-01-01T08:10:00", "auto")

    assert len(_raw_pauses(db, session_id)) == 2


def test_complete_session_derives_samples_fields(db):
    session_id = storage.create_session("2026-01-01T08:00:00", None, None)
    storage.append_samples(session_id, [
        (0, 1.0, 0.0, 0, 1, None),
        (1000, 1.6, 1.3, 2, 1, None),
        (2000, 0.0, 1.3, 2, 0, None),
    ])

    storage.complete_session(session_id, {"end_time": "2026-01-01T08:05:00"})

    session = storage.get_session(session_id)
    assert session["has_samples"] == 1
    assert session["max_speed_mps"] == 1.6


def test_complete_session_explicit_max_speed_wins_and_no_samples(db):
    session_id = storage.create_session("2026-01-01T08:00:00", None, None)

    storage.complete_session(session_id, {"end_time": "2026-01-01T08:05:00", "max_speed_mps": 2.0})

    session = storage.get_session(session_id)
    assert session["has_samples"] == 0
    assert session["max_speed_mps"] == 2.0


def test_complete_session_closes_open_pause_at_end_time(db):
    session_id = storage.create_session("2026-01-01T08:00:00", None, None)
    storage.add_pause(session_id, "2026-01-01T08:01:00", "manual")
    storage.end_pause(session_id, "2026-01-01T08:02:00")
    storage.add_pause(session_id, "2026-01-01T08:04:00", "auto")

    storage.complete_session(session_id, {"end_time": "2026-01-01T08:05:00"})

    assert _raw_pauses(db, session_id) == [
        ("2026-01-01T08:01:00", "2026-01-01T08:02:00", "manual"),
        ("2026-01-01T08:04:00", "2026-01-01T08:05:00", "auto"),
    ]


def test_set_health_status(db):
    session_id = storage.create_session("2026-01-01T08:00:00", None, None)
    assert storage.get_session(session_id)["health_logged"] == storage.HEALTH_PENDING

    storage.set_health_status(session_id, storage.HEALTH_DISMISSED)

    assert storage.get_session(session_id)["health_logged"] == storage.HEALTH_DISMISSED


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
        except Exception as exc:
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


def _q(path, sql, *args):
    conn = sqlite3.connect(path)
    try:
        with conn:
            return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _meta_version(path):
    return _q(path, "SELECT value FROM meta WHERE key = 'schema_version'")[0][0]


def _set_meta_version(path, value):
    _q(path, "UPDATE meta SET value = ? WHERE key = 'schema_version'", value)


def _tables(path):
    return {r[0] for r in _q(path, "SELECT name FROM sqlite_master WHERE type = 'table'")}


def _v2_db(path):
    storage.init_db(path)
    _q(path, "DROP INDEX idx_sessions_profile_start")
    _q(path, "DROP TABLE profiles")
    _set_meta_version(path, "2")


def test_init_db_upgrades_v2_with_backup(tmp_path):
    path, backups = str(tmp_path / "w.db"), tmp_path / "backups"
    _v2_db(path)
    sid = storage.create_session("2026-01-01T08:00:00", "KS-BLC2", "1.0")
    storage.init_db(path, str(backups))
    assert _meta_version(path) == "3"
    assert "profiles" in _tables(path)
    [backup] = backups.iterdir()
    assert backup.name.startswith("w.db.v2-")
    assert _meta_version(str(backup)) == "2"
    assert "profiles" not in _tables(str(backup))
    assert _q(str(backup), "SELECT id FROM sessions") == [(sid,)]


def test_init_db_failed_step_rolls_back(tmp_path, monkeypatch):
    path = str(tmp_path / "w.db")
    _v2_db(path)
    monkeypatch.setitem(storage._MIGRATIONS, 3, "CREATE TABLE profiles(x); SELECT * FROM no_such_table")
    with pytest.raises(storage.DataVersionError, match=r"schema v3 failed.*unchanged at v2.*w\.db\.v2-"):
        storage.init_db(path, str(tmp_path / "b"))
    assert _meta_version(path) == "2"
    assert "profiles" not in _tables(path)


def test_init_db_fresh_takes_no_backup(tmp_path):
    storage.init_db(str(tmp_path / "w.db"), str(tmp_path / "b"))
    assert not (tmp_path / "b").exists()


def test_init_db_current_takes_no_backup(db, tmp_path):
    storage.init_db(db, str(tmp_path / "b"))
    assert not (tmp_path / "b").exists()


def test_init_db_refuses_newer(tmp_path):
    path = tmp_path / "w.db"
    storage.init_db(str(path))
    _set_meta_version(str(path), "99")
    before = path.read_bytes()
    with pytest.raises(storage.DataVersionError,
                       match=r"written by schema v99; this version of WalkingDad understands up to v3\. Run a newer"):
        storage.init_db(str(path))
    assert path.read_bytes() == before


def test_init_db_rejects_non_integer_version(tmp_path):
    path = str(tmp_path / "w.db")
    storage.init_db(path)
    _set_meta_version(path, "abc")
    with pytest.raises(storage.DataVersionError, match="abc"):
        storage.init_db(path)


def test_init_db_backup_dir_independent_of_db_location(tmp_path):
    (tmp_path / "elsewhere").mkdir()
    path = str(tmp_path / "elsewhere" / "w.db")
    _v2_db(path)
    storage.init_db(path, str(tmp_path / "data" / "backups"))
    assert len(list((tmp_path / "data" / "backups").iterdir())) == 1


def test_create_session_records_schema_version(db):
    sid = storage.create_session("2026-01-01T08:00:00", "KS-BLC2", "1.0")
    assert _q(db, "SELECT schema_version FROM sessions WHERE id = ?", sid) == [(storage.SCHEMA_VERSION,)]


def test_create_and_get_profile(db):
    pid = storage.create_profile("Sean", height_m=1.778, weight_kg=81.6)
    p = storage.get_profile(pid)
    assert (p["name"], p["height_m"], p["weight_kg"]) == ("Sean", 1.778, 81.6)
    assert p["created_at"]
    assert storage.get_profile("nope") is None


def test_profile_stats_optional(db):
    p = storage.get_profile(storage.create_profile("Kim"))
    assert p["height_m"] is None and p["weight_kg"] is None


def test_profile_name_unique_case_insensitive(db):
    storage.create_profile("Sean")
    with pytest.raises(sqlite3.IntegrityError):
        storage.create_profile("sEAN")


def test_list_profiles_sorted_case_insensitive(db):
    for name in ("bob", "Alice", "carol"):
        storage.create_profile(name)
    assert [p["name"] for p in storage.list_profiles()] == ["Alice", "bob", "carol"]


def test_update_profile(db):
    pid = storage.create_profile("Sean")
    assert storage.update_profile(pid, "SEAN", 1.8, 80.0)  # its own name in a new case is not a conflict
    p = storage.get_profile(pid)
    assert (p["name"], p["height_m"], p["weight_kg"]) == ("SEAN", 1.8, 80.0)
    assert storage.update_profile("nope", "X", None, None) is False


def test_update_profile_name_taken(db):
    storage.create_profile("Sean")
    pid = storage.create_profile("Kim")
    with pytest.raises(sqlite3.IntegrityError):
        storage.update_profile(pid, "sean", None, None)


def test_delete_profile_unassigns_its_sessions(db):
    pid = storage.create_profile("Sean")
    sid = storage.create_session("2026-01-01T08:00:00", None, None, profile=pid)
    storage.delete_profile(pid)
    assert storage.get_profile(pid) is None
    assert storage.get_session(sid)["profile"] == storage.UNASSIGNED


def test_count_and_assign_unassigned(db):
    pid = storage.create_profile("Sean")
    other = storage.create_profile("Kim")
    for start in ("2026-01-01T08:00:00", "2026-01-02T08:00:00"):
        storage.complete_session(storage.create_session(start, None, None), {"end_time": start})
    kims = storage.create_session("2026-01-03T08:00:00", None, None, profile=other)
    assert storage.count_unassigned() == 2
    assert storage.assign_unassigned(pid) == 2
    assert storage.count_unassigned() == 0
    assert storage.get_session(kims)["profile"] == other


def test_unassigned_counts_and_claims_only_completed_sessions(db):
    pid = storage.create_profile("Sean")
    done = storage.create_session("2026-01-01T08:00:00", None, None)
    storage.complete_session(done, {"end_time": "2026-01-01T08:10:00"})
    interrupted = storage.create_session("2026-01-02T08:00:00", None, None)
    assert storage.count_unassigned() == 1
    assert storage.assign_unassigned(pid) == 1
    assert storage.get_session(interrupted)["profile"] == storage.UNASSIGNED



def test_set_session_profile_only_completed(db):
    pid = storage.create_profile("Sean")
    done = storage.create_session("2026-01-01T08:00:00", None, None)
    storage.complete_session(done, {"end_time": "2026-01-01T08:10:00"})
    active = storage.create_session("2026-01-02T08:00:00", None, None)
    assert storage.set_session_profile(done, pid) is True
    assert storage.get_session(done)["profile"] == pid
    assert storage.set_session_profile(active, pid) is False
    assert storage.set_session_profile("nope", pid) is False


def test_init_db_backup_failure_is_a_clean_error(tmp_path):
    path = str(tmp_path / "w.db")
    _v2_db(path)
    blocker = tmp_path / "backups"
    blocker.write_text("not a directory")
    with pytest.raises(storage.DataVersionError, match=r"backing up .*w\.db failed"):
        storage.init_db(path, str(blocker))
    assert _meta_version(path) == "2"


@pytest.mark.parametrize("value", ["1", "0", "-3"])
def test_init_db_rejects_version_below_oldest_shipped(tmp_path, value):
    path = str(tmp_path / "w.db")
    storage.init_db(path)
    _set_meta_version(path, value)
    with pytest.raises(storage.DataVersionError, match=f"unreadable schema version '{value}'"):
        storage.init_db(path, str(tmp_path / "b"))
    assert not (tmp_path / "b").exists()


def test_upgraded_schema_matches_fresh(tmp_path):
    fresh, upgraded = str(tmp_path / "fresh.db"), str(tmp_path / "up.db")
    storage.init_db(fresh)
    _v2_db(upgraded)
    storage.init_db(upgraded, str(tmp_path / "b"))
    schema = "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    assert _q(upgraded, schema) == _q(fresh, schema)

