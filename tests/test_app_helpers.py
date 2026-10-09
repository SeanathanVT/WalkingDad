import json
import queue
import re
import urllib.parse
from datetime import datetime, timedelta

import pytest

import app
import storage
from units import KM_TO_MI

_SCHEMA = {field: cast for field, _, cast, _ in app._SETTINGS_SCHEMA}


@pytest.mark.parametrize(("secs", "expected"), [
    (0, "0:00:00"),
    (59, "0:00:59"),
    (60, "0:01:00"),
    (3599, "0:59:59"),
    (3600, "1:00:00"),
    (36000 + 61, "10:01:01"),
    (125 * 3600, "125:00:00"),
    (61.99, "0:01:01"),
])
def test_format_seconds_to_hms(secs, expected):
    assert app.format_seconds_to_hms(secs) == expected


def test_kcal_estimate(monkeypatch):
    monkeypatch.setattr(app, "KCAL_PER_MILE", 80)
    assert app.kcal_estimate(2.5) == 200
    assert app.kcal_estimate(0) == 0


def test_extract_status_fields_dict_and_object():
    assert app._extract_status_fields({"dist": 5, "steps": 7, "speed": 30}) == (5, 7, 30)
    assert app._extract_status_fields({}) == (0, 0, 0)

    class Status:
        dist = 9
        speed = 25

    assert app._extract_status_fields(Status()) == (9, 0, 25)
    assert app._extract_status_fields(object()) == (0, 0, 0)


def test_clamp_waitress_threads():
    lo, hi = app._MIN_WAITRESS_THREADS, app._MAX_WAITRESS_THREADS
    assert app._clamp_waitress_threads(str(lo - 1), 8) == lo
    assert app._clamp_waitress_threads("-5", 8) == lo
    assert app._clamp_waitress_threads(str(lo), 8) == lo
    assert app._clamp_waitress_threads(str(hi), 8) == hi
    assert app._clamp_waitress_threads(str(hi + 1), 8) == hi
    assert app._clamp_waitress_threads(" 16 ", 8) == 16
    for bad in ("", "abc", "4.5"):
        with pytest.raises(ValueError, match="invalid literal"):
            app._clamp_waitress_threads(bad, 8)


def test_clamp_resume_grace_period():
    lo = app._MIN_RESUME_GRACE_PERIOD_SECONDS
    assert app._clamp_resume_grace_period(str(lo - 1), 10) == lo
    assert app._clamp_resume_grace_period("0", 10) == lo
    assert app._clamp_resume_grace_period(str(lo), 10) == lo
    assert app._clamp_resume_grace_period("100000", 10) == 100000
    for bad in ("", "x", "3.5"):
        with pytest.raises(ValueError, match="invalid literal"):
            app._clamp_resume_grace_period(bad, 10)


def test_settings_schema_casts():
    assert _SCHEMA["ble_device_name"]("  KS-X  ", "old") == "KS-X"
    assert _SCHEMA["ble_device_name"]("   ", "old") == ""
    assert _SCHEMA["host"]("  0.0.0.0 ", "127.0.0.1") == "0.0.0.0"
    assert _SCHEMA["host"]("   ", "127.0.0.1") == "127.0.0.1"
    assert _SCHEMA["apple_health_shortcut_name"]("", "Log Walk") == "Log Walk"
    assert _SCHEMA["stale_pause_timeout_minutes"]("-3", 30) == 0
    assert _SCHEMA["stale_pause_timeout_minutes"]("45", 30) == 45
    assert _SCHEMA["max_speed_kmh"]("6.5", 6.0) == 6.5
    assert _SCHEMA["kcal_per_mile"]("90", 80) == 90
    assert _SCHEMA["resume_grace_period_seconds"] is app._clamp_resume_grace_period
    assert _SCHEMA["waitress_threads"] is app._clamp_waitress_threads
    with pytest.raises(ValueError, match="invalid literal"):
        _SCHEMA["port"]("http", 5000)


def test_build_setup_shortcut_url():
    assert app._build_setup_shortcut_url() == app._APPLE_HEALTH_SHORTCUT_ICLOUD_LINK
    assert app._build_setup_shortcut_url().startswith("https://www.icloud.com/shortcuts/")


def test_build_log_shortcut_url(monkeypatch):
    monkeypatch.setattr(app, "APPLE_HEALTH_SHORTCUT_NAME", "Log Walk & Run")
    record = {
        "date": "2026-01-02", "start_time": "08:30:00", "duration_seconds": 600,
        "distance_km": 1.5, "distance_mi": 0.932, "calories": 70, "steps": 999,
    }
    url = app._build_log_shortcut_url(record, "http://192.168.1.5:5001/health_logged/abc")
    assert url.startswith("shortcuts://x-callback-url/run-shortcut?name=Log%20Walk%20%26%20Run&input=text&text=")
    text, success = url.split("&text=", 1)[1].split("&x-success=")
    assert success == "http%3A%2F%2F192.168.1.5%3A5001%2Fhealth_logged%2Fabc"
    assert " " not in text
    assert '"' not in text
    assert "{" not in text
    assert "08:30:00" in text
    payload = json.loads(urllib.parse.unquote(text))
    assert payload == record


def test_build_log_shortcut_url_keeps_colon_and_slash(monkeypatch):
    monkeypatch.setattr(app, "APPLE_HEALTH_SHORTCUT_NAME", "a:b/c")
    record = dict.fromkeys(("date", "start_time", "duration_seconds", "distance_km", "distance_mi", "calories", "steps"), 0)
    assert "name=a:b/c&" in app._build_log_shortcut_url(record, "http://x/")


class _FakeUDPSocket:
    def __init__(self, *args):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def connect(self, addr):
        pass

    def getsockname(self):
        return ("192.168.1.5", 40000)


@pytest.mark.parametrize(("host_url", "expected"), [
    ("http://localhost:5001/", "http://192.168.1.5:5001/"),
    ("http://127.0.0.1:5001/", "http://192.168.1.5:5001/"),
    ("http://[::1]:5001/", "http://192.168.1.5:5001/"),
    ("http://192.168.1.9:5001/", "http://192.168.1.9:5001/"),
])
def test_phone_reachable_base_url(monkeypatch, host_url, expected):
    monkeypatch.setattr(app.socket, "socket", _FakeUDPSocket)
    with app.app.test_request_context(base_url=host_url):
        assert app._phone_reachable_base_url() == expected


def test_phone_reachable_base_url_no_route(monkeypatch):
    class NoRoute(_FakeUDPSocket):
        def connect(self, addr):
            raise OSError("Network is unreachable")

    monkeypatch.setattr(app.socket, "socket", NoRoute)
    with app.app.test_request_context(base_url="http://localhost:5001/"):
        assert app._phone_reachable_base_url() == "http://localhost:5001/"


def test_inject_flags(app_state, monkeypatch):
    monkeypatch.setattr(app, "connected", False)
    monkeypatch.setattr(app, "connecting", True)
    monkeypatch.setattr(app, "connection_failed", True)
    monkeypatch.setattr(app, "APPLE_HEALTH_SHORTCUT_NAME", "My Shortcut")
    assert app.inject_flags() == {
        "connected": False, "connecting": True, "connection_failed": True,
        "apple_health_shortcut_name": "My Shortcut",
        "setup_shortcut_url": app._build_setup_shortcut_url(),
        "keyboard_shortcuts_enabled": True, "hotkey": app.hotkey, "belt_transitioning": False,
    }


def test_build_stats_payload(app_state, monkeypatch):
    monkeypatch.setattr(app, "session_active", True)
    monkeypatch.setattr(app, "belt_running", True)
    monkeypatch.setattr(app, "_belt_transitioning", True)
    monkeypatch.setattr(app, "_server_stopping", True)
    monkeypatch.setattr(app, "current_speed_kmh", 4.0)
    monkeypatch.setattr(app, "current_distance_km", 1.23456)
    monkeypatch.setattr(app, "current_steps", 1500)
    monkeypatch.setattr(app, "current_calories", 61.6)
    monkeypatch.setattr(app, "current_session_active_seconds", 3725.9)
    assert app._build_stats_payload() == {
        "is_connected": True,
        "connection_failed": False,
        "session_active": True,
        "is_running": True,
        "belt_transitioning": True,
        "speed": round(4.0 * KM_TO_MI, 1),
        "distance": round(1.23456 * KM_TO_MI, 2),
        "steps": 1500,
        "calories": 62,
        "time_active": "1:02:05",
        "stopping": True,
        "health_status_changed": None,
    }


def test_build_stats_payload_idle(app_state):
    payload = app._build_stats_payload()
    assert payload["speed"] == 0
    assert payload["distance"] == 0
    assert payload["calories"] == 0
    assert payload["time_active"] == "0:00:00"
    assert payload["is_running"] is False


def test_sse_frame():
    frame = app._sse_frame({"a": 1, "b": "x"})
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    assert frame.count("\n") == 2
    assert json.loads(frame[len("data: "):]) == {"a": 1, "b": "x"}


def _start_session(monkeypatch, minutes_ago=10):
    monkeypatch.setattr(app, "session_active", True)
    monkeypatch.setattr(app, "belt_running", True)
    monkeypatch.setattr(app, "_session_start_time", datetime.now() - timedelta(minutes=minutes_ago))


def test_build_session_state_snapshot(app_state, monkeypatch):
    start = datetime.fromisoformat("2026-01-02T08:30:00")
    for name, value in {
        "session_active": True, "_session_id": "sid", "_session_start_time": start,
        "current_distance_km": 1.5, "current_steps": 2000, "current_calories": 75.0,
        "current_session_active_seconds": 900, "resume_speed_kmh": 3.5,
        "_last_dev_dist": 150, "_last_dev_steps": 2000,
    }.items():
        monkeypatch.setattr(app, name, value)
    assert app._build_session_state_snapshot() == {
        "session_active": True, "session_id": "sid", "session_start_time": "2026-01-02T08:30:00",
        "current_distance_km": 1.5, "current_steps": 2000, "current_calories": 75.0,
        "current_session_active_seconds": 900, "resume_speed_kmh": 3.5,
        "last_dev_dist": 150, "last_dev_steps": 2000,
    }
    monkeypatch.setattr(app, "_session_start_time", None)
    assert app._build_session_state_snapshot()["session_start_time"] is None


def test_save_session_state_noop_when_inactive(app_state, tmp_path):
    app._save_session_state()
    assert list(tmp_path.glob("session_state.json*")) == []


def test_save_session_state_writes_atomically_and_flushes(app_state, monkeypatch, tmp_path):
    _start_session(monkeypatch)
    app._begin_db_session()
    app._record_sample()
    app._save_session_state()
    with open(app.SESSION_STATE_FILE) as f:
        assert json.load(f) == app._build_session_state_snapshot()
    assert not (tmp_path / "session_state.json.tmp").exists()
    assert len(storage.get_samples(app._session_id)) == 1


def test_save_session_state_survives_write_error(app_state, monkeypatch, tmp_path, caplog):
    _start_session(monkeypatch)
    monkeypatch.setattr(app, "SESSION_STATE_FILE", str(tmp_path / "missing" / "s.json"))
    app._save_session_state()
    assert "Failed to write session state" in caplog.text


def test_load_session_state(app_state):
    path = app.SESSION_STATE_FILE
    assert app._load_session_state() is None

    def write(text):
        with open(path, "w") as f:
            f.write(text)

    write("{not json")
    assert app._load_session_state() is None
    write("[1, 2]")
    assert app._load_session_state() is None
    write('{"session_active": false, "current_steps": 5}')
    assert app._load_session_state() is None
    write("{}")
    assert app._load_session_state() is None
    write('{"session_active": true, "current_steps": 5}')
    assert app._load_session_state() == {"session_active": True, "current_steps": 5}


def test_load_session_state_non_utf8_file(app_state):
    with open(app.SESSION_STATE_FILE, "wb") as f:
        f.write(b"\xff\xfe\x00garbage")
    assert app._load_session_state() is None


def test_clear_session_state(app_state, tmp_path):
    app._clear_session_state()
    (tmp_path / "session_state.json").write_text("{}")
    app._clear_session_state()
    assert not (tmp_path / "session_state.json").exists()


def test_write_config_creates_new_file(app_state):
    app._write_config({"PORT": 5001})
    with open(app._CONFIG_FILE) as f:
        assert json.load(f) == {"PORT": 5001}


def test_write_config_merges_into_existing(app_state):
    with open(app._CONFIG_FILE, "w") as f:
        json.dump({"HOST": "h", "PORT": 1}, f)
    app._write_config({"PORT": 2, "SPEED_STEP": 0.5})
    with open(app._CONFIG_FILE) as f:
        assert json.load(f) == {"HOST": "h", "PORT": 2, "SPEED_STEP": 0.5}


def test_begin_db_session_creates_row(app_state, monkeypatch):
    monkeypatch.setattr(app.time, "monotonic", lambda: 10_000.0)
    _start_session(monkeypatch, minutes_ago=5)
    app._samples.add(1, 0, 0, 0, belt_running=True)
    app._begin_db_session()
    row = storage.get_session(app._session_id)
    assert row["status"] == "active"
    assert row["device_model"] == app.BLE_DEVICE_NAME
    assert datetime.fromisoformat(row["start_time"]) == app._session_start_time.astimezone().replace(microsecond=0)
    assert app._session_start_monotonic == pytest.approx(10_000 - 300, abs=1)
    assert app._samples.drain() == []


def test_begin_db_session_reuses_existing_id(app_state, monkeypatch):
    _start_session(monkeypatch)
    existing = storage.create_session("2026-01-01T08:00:00+00:00", "x", None)
    app._begin_db_session(existing)
    assert app._session_id == existing
    assert len(storage.list_active_sessions()) == 1


def test_begin_db_session_unknown_existing_id_creates_new(app_state, monkeypatch):
    _start_session(monkeypatch)
    app._begin_db_session("gone")
    assert app._session_id not in (None, "gone")
    assert storage.get_session(app._session_id) is not None


def test_begin_db_session_storage_failure_clears_id(app_state, monkeypatch):
    _start_session(monkeypatch)
    monkeypatch.setattr(app, "_session_id", "stale")

    def boom(*a, **k):
        raise RuntimeError

    monkeypatch.setattr(storage, "create_session", boom)
    app._begin_db_session()
    assert app._session_id is None


def test_record_sample_and_flush(app_state, monkeypatch):
    clock = [5000.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: clock[0])
    _start_session(monkeypatch, minutes_ago=0)
    app._begin_db_session()
    monkeypatch.setattr(app, "current_speed_kmh", 3.6)
    monkeypatch.setattr(app, "current_distance_km", 0.25)
    monkeypatch.setattr(app, "current_steps", 300)
    clock[0] += 2
    app._record_sample()
    monkeypatch.setattr(app, "belt_running", False)
    clock[0] += 5
    app._record_sample()
    app._flush_samples()
    rows = storage.get_samples(app._session_id)
    assert [(r["speed_mps"], r["distance_m"], r["steps"], r["belt_running"]) for r in rows] == [
        (pytest.approx(1.0), 250, 300, 1), (0.0, 250, 300, 0),
    ]
    assert rows[1]["t_ms"] - rows[0]["t_ms"] == 5000
    app._flush_samples()
    assert len(storage.get_samples(app._session_id)) == 2


def test_record_sample_noop_without_session(app_state, monkeypatch):
    app._record_sample()
    assert app._samples.drain() == []
    _start_session(monkeypatch)
    app._record_sample()
    assert app._samples.drain() == []


def test_flush_samples_without_session_id_drops_rows(app_state, monkeypatch):
    monkeypatch.setattr(storage, "append_samples", lambda *a: pytest.fail("should not write"))
    app._samples.add(1, 0, 0, 0, belt_running=True)
    app._flush_samples()
    assert app._samples.drain() == []


def _pauses(session_id):
    conn = storage._connect(storage._db_path)
    try:
        return [tuple(r) for r in conn.execute(
            "SELECT start_time, end_time, reason FROM pauses WHERE session_id = ?", (session_id,),
        )]
    finally:
        conn.close()


def test_record_pause_and_resume(app_state, monkeypatch):
    sid = storage.create_session("2026-01-01T08:00:00+00:00", None, None)
    monkeypatch.setattr(app, "_session_id", sid)
    app._record_pause("manual", at="2026-01-01T08:05:00+00:00")
    assert _pauses(sid) == [("2026-01-01T08:05:00+00:00", None, "manual")]
    monkeypatch.setattr(app, "_now_iso", lambda: "2026-01-01T08:06:00+00:00")
    app._record_resume()
    assert _pauses(sid) == [("2026-01-01T08:05:00+00:00", "2026-01-01T08:06:00+00:00", "manual")]
    app._record_pause("auto")
    assert _pauses(sid)[-1] == ("2026-01-01T08:06:00+00:00", None, "auto")


def test_record_pause_resume_noop_without_session_id(app_state, monkeypatch):
    monkeypatch.setattr(storage, "add_pause", lambda *a: pytest.fail("should not write"))
    monkeypatch.setattr(storage, "end_pause", lambda *a: pytest.fail("should not write"))
    app._record_pause("manual")
    app._record_resume()


def test_last_sample_time(app_state, monkeypatch):
    assert app._last_sample_time() is None
    start = datetime(2026, 1, 2, 8, 0).astimezone()
    monkeypatch.setattr(app, "_session_start_time", start)
    sid = storage.create_session(start.isoformat(), None, None)
    monkeypatch.setattr(app, "_session_id", sid)
    assert app._last_sample_time() is None
    storage.append_samples(sid, [(1000, 1.0, 1, 1, 1, None), (65_500, 1.0, 60, 90, 1, None)])
    assert app._last_sample_time() == (start + timedelta(seconds=65)).isoformat()


def test_save_session(app_state, monkeypatch):
    _start_session(monkeypatch, minutes_ago=20)
    app._begin_db_session()
    sid = app._session_id
    monkeypatch.setattr(app, "current_distance_km", 1.2)
    monkeypatch.setattr(app, "current_steps", 1600)
    monkeypatch.setattr(app, "current_calories", 60.0)
    monkeypatch.setattr(app, "current_session_active_seconds", 800)
    app._save_session()
    assert app._session_id is None
    row = storage.get_session(sid)
    assert row["status"] == "completed"
    assert row["distance_m"] == pytest.approx(1200)
    assert row["steps"] == 1600
    assert row["calories_kcal"] == 60.0
    assert row["moving_s"] == 800
    assert row["avg_speed_mps"] == pytest.approx(1.5)
    assert row["elapsed_s"] == pytest.approx(1200, abs=2)
    assert row["has_samples"] == 1
    assert datetime.fromisoformat(row["end_time"]) <= datetime.now().astimezone()


def test_save_session_zero_moving_time_and_missing_row(app_state, monkeypatch):
    _start_session(monkeypatch)
    monkeypatch.setattr(app, "current_distance_km", 0.01)
    app._save_session()
    (row,) = storage.list_sessions()
    assert row["avg_speed_mps"] == pytest.approx(10.0)


def test_save_session_noop_when_inactive(app_state, monkeypatch):
    monkeypatch.setattr(app, "_session_id", "sid")
    app._save_session()
    assert app._session_id == "sid"
    assert storage.list_sessions() == []


def test_save_session_resets_id_on_storage_error(app_state, monkeypatch):
    _start_session(monkeypatch)
    app._begin_db_session()

    def boom(*a):
        raise RuntimeError

    monkeypatch.setattr(storage, "complete_session", boom)
    app._save_session()
    assert app._session_id is None


def _completed(start, distance_m=1000.0):
    sid = storage.create_session(start, None, None)
    storage.complete_session(sid, {
        "end_time": start, "elapsed_s": 600, "moving_s": 600, "distance_m": distance_m,
        "steps": 1000, "calories_kcal": 50, "avg_speed_mps": 1.0,
    })
    return sid


def test_load_session_history(app_state):
    _completed("2026-01-01T08:00:00+00:00")
    newest = _completed("2026-01-03T08:00:00+00:00", 2000)
    storage.create_session("2026-01-04T08:00:00+00:00", None, None)
    history = app._load_session_history()
    assert [h["date"] for h in history] == ["2026-01-03", "2026-01-01"]
    assert history[0]["id"] == newest
    assert history[0]["distance_km"] == 2.0
    assert len(app._load_session_history(limit=1)) == 1


def test_load_session_history_storage_error(app_state, monkeypatch):
    def boom(**k):
        raise RuntimeError

    monkeypatch.setattr(storage, "list_sessions", boom)
    assert app._load_session_history() == []


def test_clear_session_history_keeps_active(app_state):
    _completed("2026-01-01T08:00:00+00:00")
    active = storage.create_session("2026-01-02T08:00:00+00:00", None, None)
    app._clear_session_history()
    assert storage.list_sessions() == []
    assert storage.get_session(active) is not None


def test_dismiss_health_export_marks_most_recent(app_state):
    older = _completed("2026-01-01T08:00:00+00:00")
    newer = _completed("2026-01-02T08:00:00+00:00")
    app._dismiss_health_export()
    assert storage.get_session(newer)["health_logged"] == storage.HEALTH_DISMISSED
    assert storage.get_session(older)["health_logged"] == storage.HEALTH_PENDING
    assert app._last_health_status_change == {"id": newer, "status": storage.HEALTH_DISMISSED}


def test_dismiss_health_export_empty_history(app_state):
    app._dismiss_health_export()
    assert storage.list_sessions() == []


def test_sweep_orphaned_sessions(app_state):
    start = "2026-01-01T08:00:00+00:00"
    with_samples = storage.create_session(start, None, None)
    storage.append_samples(with_samples, [(1000, 1.0, 1.0, 2, 1, None), (2000, 1.0, 500.0, 600, 1, None)])
    empty = storage.create_session(start, None, None)
    keep = storage.create_session(start, None, None)
    app._sweep_orphaned_sessions(keep)
    row = storage.get_session(with_samples)
    assert row["status"] == "completed"
    assert row["distance_m"] == 500.0
    assert row["steps"] == 600
    assert row["end_time"] == "2026-01-01T08:00:02+00:00"
    assert row["calories_kcal"] == pytest.approx(app.KCAL_PER_MILE * 0.5 * KM_TO_MI)
    assert storage.get_session(empty) is None
    assert storage.get_session(keep)["status"] == "active"


def test_sweep_orphaned_sessions_none_keep_id(app_state):
    sid = storage.create_session("2026-01-01T08:00:00+00:00", None, None)
    app._sweep_orphaned_sessions(None)
    assert storage.get_session(sid) is None


def test_is_shutting_down(app_state, monkeypatch):
    assert app._is_shutting_down() is False
    monkeypatch.setattr(app, "_shutting_down", True)
    assert app._is_shutting_down() is True


def test_now_iso_format():
    now = app._now_iso()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", now)
    assert abs((datetime.fromisoformat(now) - datetime.now().astimezone()).total_seconds()) < 2


class _Loop:
    def __init__(self, closed):
        self.closed = closed

    def is_closed(self):
        return self.closed


def test_atexit_cleanup_noop_paths(app_state, monkeypatch):
    monkeypatch.setattr(app, "_shutting_down", True)
    monkeypatch.setattr(app, "ble_loop", _Loop(closed=False))
    app._atexit_cleanup()
    monkeypatch.setattr(app, "_shutting_down", False)
    monkeypatch.setattr(app, "ble_loop", None)
    app._atexit_cleanup()
    monkeypatch.setattr(app, "ble_loop", _Loop(closed=True))
    app._atexit_cleanup()
    assert app_state == []


def test_atexit_cleanup_swallows_errors(app_state, monkeypatch):
    monkeypatch.setattr(app, "ble_loop", _Loop(closed=False))
    app._atexit_cleanup()  # the recorder returns None, so .result() raises AttributeError
    assert len(app_state) == 1
    assert app_state[0].cr_code is app._graceful_shutdown.__code__


class _Stop(Exception):
    pass


def _run_one_tick(monkeypatch):
    calls = []

    def sleep(_):
        calls.append(1)
        if len(calls) == 2:
            raise _Stop

    monkeypatch.setattr(app.time, "sleep", sleep)
    with pytest.raises(_Stop):
        app._sse_broadcast_loop()


def test_sse_broadcast_replaces_stale_item(app_state, monkeypatch):
    stale, fresh = queue.Queue(maxsize=1), queue.Queue(maxsize=1)
    stale.put("stale")
    app._sse_subscribers.extend([stale, fresh])
    monkeypatch.setattr(app, "current_steps", 42)
    _run_one_tick(monkeypatch)
    expected = app._sse_frame(app._build_stats_payload())
    for q in (stale, fresh):
        assert q.get_nowait() == expected
        assert q.empty()
    assert json.loads(expected[len("data: "):])["steps"] == 42


def test_sse_broadcast_survives_stale_pause_error(app_state, monkeypatch):
    def boom():
        raise RuntimeError

    monkeypatch.setattr(app, "_end_session_if_stale_pause", boom)
    q = queue.Queue()
    app._sse_subscribers.append(q)
    _run_one_tick(monkeypatch)
    assert q.get_nowait() == app._sse_frame(app._build_stats_payload())


def test_sse_broadcast_survives_failed_tick(app_state, monkeypatch, caplog):
    real_payload, sleeps = app._build_stats_payload, []

    def payload():
        if len(sleeps) == 1:
            raise RuntimeError("bad tick")
        return real_payload()

    def sleep(_):
        sleeps.append(1)
        if len(sleeps) == 3:
            raise _Stop

    monkeypatch.setattr(app, "_build_stats_payload", payload)
    monkeypatch.setattr(app.time, "sleep", sleep)
    q = queue.Queue()
    app._sse_subscribers.append(q)
    with pytest.raises(_Stop):
        app._sse_broadcast_loop()
    assert "SSE broadcast tick failed (continuing): bad tick" in caplog.text
    assert q.get_nowait() == app._sse_frame(real_payload())
    assert q.empty()


def _boom(*a, **k):
    raise RuntimeError("db down")


def _oserror(*a):
    raise OSError("read-only")


@pytest.mark.parametrize(("call", "target", "attr", "expected"), [
    (app._flush_samples, storage, "append_samples", None),
    (lambda: app._record_pause("manual"), storage, "add_pause", None),
    (app._last_sample_time, storage, "get_samples", None),
    (lambda: app._sweep_orphaned_sessions(None), storage, "list_active_sessions", None),
    (app._record_resume, storage, "end_pause", None),
    (app._load_session_history, storage, "list_sessions", []),
    (app._clear_session_history, storage, "clear_history", None),
    (app._dismiss_health_export, storage, "list_sessions", None),
    (app._clear_session_state, app.os, "remove", None),
    (app._begin_db_session, storage, "create_session", None),
])
def test_storage_failures_are_logged_not_raised(app_state, monkeypatch, caplog, call, target, attr, expected):
    _start_session(monkeypatch)
    monkeypatch.setattr(app, "_session_id", "sid")
    app._samples.add(1, 0, 0, 0, belt_running=True)
    with open(app.SESSION_STATE_FILE, "w") as f:
        f.write("{}")
    monkeypatch.setattr(target, attr, _boom if target is storage else _oserror)
    assert call() == expected
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_save_session_without_db_row(app_state, monkeypatch, caplog):
    _start_session(monkeypatch)
    monkeypatch.setattr(storage, "create_session", _boom)
    app._begin_db_session()
    app._save_session()
    assert app._session_id is None
    assert "Session not saved: no database row" in caplog.text
    assert storage.list_sessions() == []
    assert storage.list_active_sessions() == []
