import json
import os
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import app
import config
import storage


@pytest.fixture
def client(app_state):
    return app.app.test_client()


@pytest.fixture
def no_loop(monkeypatch, app_state):
    def fail(coro, loop):
        coro.close()
        raise RuntimeError("no BLE loop")

    monkeypatch.setattr(app.asyncio, "run_coroutine_threadsafe", fail)


@pytest.fixture
def running(monkeypatch, app_state):
    monkeypatch.setattr(app, "session_active", True)
    monkeypatch.setattr(app, "belt_running", True)


@pytest.fixture
def paused(monkeypatch, app_state):
    monkeypatch.setattr(app, "session_active", True)
    monkeypatch.setattr(app, "belt_running", False)


def names(scheduled):
    return [c.__name__ for c in scheduled]


def redirects_root(resp):
    return resp.status_code == 302 and resp.headers["Location"].endswith("/")


def raw_pauses(session_id):
    conn = sqlite3.connect(storage._db_path)
    try:
        return conn.execute("SELECT reason, end_time FROM pauses WHERE session_id = ?", (session_id,)).fetchall()
    finally:
        conn.close()


def completed_session(start, steps=100):
    sid = storage.create_session(start.astimezone().isoformat(timespec="seconds"), "pad", None)
    storage.complete_session(sid, {
        "end_time": (start + timedelta(minutes=10)).astimezone().isoformat(timespec="seconds"),
        "elapsed_s": 600, "moving_s": 600, "distance_m": 1000, "steps": steps,
        "calories_kcal": 50, "avg_speed_mps": 1.0,
    })
    return sid


def pending_state(session_id=None, steps=1234):
    return {
        "session_active": True,
        "session_id": session_id,
        "session_start_time": (datetime.now() - timedelta(minutes=5)).isoformat(),
        "current_distance_km": 1.5,
        "current_steps": steps,
        "current_calories": 80.0,
        "current_session_active_seconds": 300,
        "resume_speed_kmh": 3.5,
        "last_dev_dist": 150,
        "last_dev_steps": steps,
    }


# ── GET / ──

def test_root_disconnected(client, monkeypatch):
    monkeypatch.setattr(app, "connected", False)
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"DISCONNECTED" in resp.data


def test_root_idle(client):
    assert b"Start New Session" in client.get("/").data


def test_root_active(client, running):
    assert b"Active Session" in client.get("/").data


def test_root_paused(client, paused):
    assert b"Session Paused" in client.get("/").data


def test_phone_viewport_and_manifest(client):
    html = client.get("/").get_data(as_text=True)
    assert 'name="viewport" content="width=device-width' in html
    assert 'rel="manifest"' in html
    assert client.get("/static/manifest.json").get_json()["display"] == "standalone"


def test_root_pending_restore(client, monkeypatch):
    monkeypatch.setattr(app, "_pending_restore", pending_state())
    data = client.get("/").data
    assert b"was interrupted" in data
    assert b"1,234 steps" in data


def test_start_not_connected(client, app_state, monkeypatch):
    monkeypatch.setattr(app, "connected", False)
    assert redirects_root(client.post("/start"))
    assert not app.session_active
    assert not storage.list_active_sessions()
    assert app_state == []


def test_start(client, app_state, monkeypatch):
    monkeypatch.setattr(app, "current_steps", 99)
    monkeypatch.setattr(app, "current_distance_km", 3.0)
    monkeypatch.setattr(app, "_pending_restore", pending_state())
    app.speed_history.append(4.0)
    assert redirects_root(client.post("/start"))
    assert app.session_active and app.belt_running
    assert (app.current_steps, app.current_distance_km, list(app.speed_history)) == (0, 0.0, [])
    assert app._pending_restore is None
    assert [s["id"] for s in storage.list_active_sessions()] == [app._session_id]
    assert os.path.exists(app.SESSION_STATE_FILE)
    assert names(app_state) == ["_start_belt_sequence"]


def test_start_already_active(client, app_state, running):
    assert redirects_root(client.post("/start"))
    assert app_state == []
    assert not storage.list_active_sessions()


def test_start_schedule_failure(client, no_loop):
    assert redirects_root(client.post("/start"))
    assert app.session_active
    assert not app.belt_running


# ── /pause, /pause_session ──

@pytest.mark.parametrize("path", ["/pause", "/pause_session"])
def test_pause(client, app_state, path):
    client.post("/start")
    app.speed_history.extend([2.5, 4.2])
    assert redirects_root(client.post(path))
    assert not app.belt_running
    assert app.resume_speed_kmh == 4.2
    assert raw_pauses(app._session_id) == [("manual", None)]
    with open(app.SESSION_STATE_FILE) as f:
        assert json.load(f)["resume_speed_kmh"] == 4.2
    assert names(app_state) == ["_start_belt_sequence", "_pause_belt_sequence"]


def test_pause_not_running(client, app_state, paused):
    assert redirects_root(client.post("/pause"))
    assert app_state == []


def test_pause_schedule_failure(client, running, no_loop):
    assert redirects_root(client.post("/pause"))
    assert not app.belt_running


# ── /resume, /resume_session ──

@pytest.mark.parametrize("path", ["/resume", "/resume_session"])
def test_resume(client, app_state, path):
    client.post("/start")
    client.post("/pause")
    assert redirects_root(client.post(path))
    assert app.belt_running
    assert raw_pauses(app._session_id)[0][1] is not None
    assert names(app_state)[-1] == "_resume_belt_sequence"


def test_resume_no_session(client, app_state):
    assert redirects_root(client.post("/resume"))
    assert not app.belt_running
    assert app_state == []


def test_resume_already_running(client, app_state, running):
    assert redirects_root(client.post("/resume"))
    assert app_state == []


def test_resume_schedule_failure(client, paused, no_loop):
    assert redirects_root(client.post("/resume"))
    assert not app.belt_running


# ── speed ──

@pytest.fixture
def speeds(monkeypatch):
    monkeypatch.setattr(app, "MIN_SPEED_KMH", 1.0)
    monkeypatch.setattr(app, "MAX_SPEED_KMH", 6.0)
    monkeypatch.setattr(app, "SPEED_STEP", 0.5)
    monkeypatch.setattr(app, "SLOW_WALK_SPEED_KMH", 2.0)


@pytest.mark.parametrize(("path", "current", "expected"), [
    ("/increase_speed", 3.0, 35),
    ("/increase_speed", 6.0, 60),
    ("/decrease_speed", 3.0, 25),
    ("/decrease_speed", 1.0, 10),
    ("/min_speed", 3.0, 10),
    ("/slow_speed", 3.0, 20),
    ("/max_speed", 3.0, 60),
])
def test_speed(client, app_state, running, speeds, monkeypatch, path, current, expected):
    monkeypatch.setattr(app, "current_speed_kmh", current)
    assert redirects_root(client.post(path))
    assert names(app_state) == ["_locked_change_speed"]
    assert app_state[0].cr_frame.f_locals["dev_speed"] == expected


@pytest.mark.parametrize("path", ["/increase_speed", "/decrease_speed", "/min_speed", "/slow_speed", "/max_speed"])
def test_speed_not_running(client, app_state, paused, path):
    assert redirects_root(client.post(path))
    assert app_state == []


def test_speed_schedule_failure(client, running, no_loop):
    assert redirects_root(client.post("/max_speed"))


# ── /end_session ──

def test_end_session(client, app_state, monkeypatch):
    client.post("/start")
    monkeypatch.setattr(app, "current_steps", 500)
    monkeypatch.setattr(app, "current_distance_km", 0.4)
    monkeypatch.setattr(app, "current_session_active_seconds", 300)
    assert redirects_root(client.post("/end_session"))
    history = storage.list_sessions()
    assert [(s["status"], s["steps"]) for s in history] == [("completed", 500)]
    assert not app.session_active and not app.belt_running
    assert (app.current_steps, app.current_distance_km, app.current_session_active_seconds) == (0, 0.0, 0)
    assert app._session_id is None
    assert not os.path.exists(app.SESSION_STATE_FILE)


def test_end_session_stale_while_running_is_noop(app_state, running):
    app._end_session(stale=True)
    assert app.session_active and app.belt_running
    assert app_state == []
    assert storage.list_sessions() == []


# ── /restore_session, /discard_session ──

def test_restore_session(client, monkeypatch):
    sid = storage.create_session(datetime.now().astimezone().isoformat(timespec="seconds"), "pad", None)
    monkeypatch.setattr(app, "_pending_restore", pending_state(sid))
    assert redirects_root(client.post("/restore_session"))
    assert app.session_active and not app.belt_running
    assert app._pending_restore is None
    assert app._session_id == sid
    assert (app.current_steps, app.current_distance_km, app.resume_speed_kmh) == (1234, 1.5, 3.5)
    assert (app._last_dev_dist, app._last_dev_steps) == (150, 1234)
    assert raw_pauses(sid) == [("shutdown", None)]
    assert os.path.exists(app.SESSION_STATE_FILE)


def test_restore_session_not_connected(client, monkeypatch):
    monkeypatch.setattr(app, "connected", False)
    monkeypatch.setattr(app, "_pending_restore", pending_state())
    assert redirects_root(client.post("/restore_session"))
    assert not app.session_active
    assert app._pending_restore is not None


def test_restore_session_already_active(client, running, monkeypatch):
    monkeypatch.setattr(app, "_pending_restore", pending_state())
    assert redirects_root(client.post("/restore_session"))
    assert app._pending_restore is not None
    assert app.current_steps == 0


def test_restore_session_nothing_pending(client):
    assert redirects_root(client.post("/restore_session"))
    assert not app.session_active


def test_discard_session(client, monkeypatch):
    sid = storage.create_session(datetime.now().astimezone().isoformat(timespec="seconds"), "pad", None)
    monkeypatch.setattr(app, "_pending_restore", pending_state(sid))
    with open(app.SESSION_STATE_FILE, "w") as f:
        json.dump(pending_state(sid), f)
    assert redirects_root(client.post("/discard_session"))
    assert app._pending_restore is None
    assert storage.get_session(sid) is None
    assert not os.path.exists(app.SESSION_STATE_FILE)


def test_discard_session_delete_fails(client, monkeypatch):
    def boom(_):
        raise RuntimeError

    monkeypatch.setattr(storage, "delete_session", boom)
    monkeypatch.setattr(app, "_pending_restore", pending_state("sid"))
    with open(app.SESSION_STATE_FILE, "w") as f:
        f.write("{}")
    assert redirects_root(client.post("/discard_session"))
    assert app._pending_restore is None
    assert not os.path.exists(app.SESSION_STATE_FILE)


def test_discard_session_with_nothing_pending(client, monkeypatch):
    deleted = []
    monkeypatch.setattr(storage, "delete_session", deleted.append)
    assert redirects_root(client.post("/discard_session"))
    assert deleted == []


def test_discard_session_while_active(client, running, monkeypatch):
    monkeypatch.setattr(app, "_pending_restore", pending_state())
    with open(app.SESSION_STATE_FILE, "w") as f:
        f.write("{}")
    assert redirects_root(client.post("/discard_session"))
    assert app._pending_restore is not None
    assert os.path.exists(app.SESSION_STATE_FILE)


# ── history / export ──

def test_export_csv(client, monkeypatch):
    client.post("/start")
    monkeypatch.setattr(app, "current_steps", 777)
    client.post("/end_session")
    resp = client.get("/export_csv")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/csv")
    assert resp.headers["Content-Disposition"] == "attachment; filename=walkingdad_history.csv"
    header, row = resp.data.decode().splitlines()
    assert header.startswith("date,start_time,end_time,duration_seconds")
    assert row.split(",")[6] == "777"


def test_clear_history(client):
    completed_session(datetime.now())
    resp = client.post("/clear_history")
    assert resp.get_json() == {"status": "cleared"}
    assert storage.list_sessions() == []


def test_dismiss_health_export(client):
    sid = completed_session(datetime.now())
    resp = client.post("/dismiss_health_export")
    assert resp.get_json() == {"status": "dismissed"}
    assert storage.get_session(sid)["health_logged"] == storage.HEALTH_DISMISSED


def test_health_logged_callback(client, monkeypatch):
    monkeypatch.setattr(app, "APPLE_HEALTH_EXPORT_ENABLED", True)
    sid = completed_session(datetime.now())
    html = client.get("/").get_data(as_text=True)
    assert "x-success=" in html and f'data-session-id="{sid}"' in html

    assert redirects_root(client.get(f"/health_logged/{sid}"))
    assert storage.get_session(sid)["health_logged"] == storage.HEALTH_LOGGED
    assert app._build_stats_payload()["health_status_changed"] == {"id": sid, "status": storage.HEALTH_LOGGED}
    assert 'id="health-export-banner"' not in client.get("/").get_data(as_text=True)


@pytest.mark.parametrize("enabled", [True, False])
def test_history_log_buttons_only_for_unlogged(client, monkeypatch, enabled):
    monkeypatch.setattr(app, "APPLE_HEALTH_EXPORT_ENABLED", enabled)
    logged = completed_session(datetime.now() - timedelta(days=1))
    dismissed = completed_session(datetime.now())
    storage.set_health_status(logged, storage.HEALTH_LOGGED)
    storage.set_health_status(dismissed, storage.HEALTH_DISMISSED)
    html = client.get("/").get_data(as_text=True)
    assert ("health_logged%2F" + dismissed in html) is enabled
    assert "health_logged%2F" + logged not in html
    assert ('class="bi bi-heart-fill health-icon"' in html) is enabled


def test_delete_session(client):
    sid = completed_session(datetime.now())
    storage.append_samples(sid, [(0, 1.0, 1.0, 1, 1, None)])
    resp = client.post(f"/delete_session/{sid}")
    assert resp.get_json() == {"status": "deleted"}
    assert storage.get_session(sid) is None
    assert storage.get_samples(sid) == []


def test_delete_session_refuses_in_progress(client):
    sid = storage.create_session(datetime.now().astimezone().isoformat(timespec="seconds"), "pad", None)
    assert client.post(f"/delete_session/{sid}").status_code == 404
    assert client.post("/delete_session/no-such-id").status_code == 404
    assert storage.get_session(sid) is not None


def test_dismiss_targets_banner_session(client):
    shown = completed_session(datetime.now() - timedelta(hours=1))
    newer = completed_session(datetime.now())  # Ended on another device while the banner was open.
    client.post("/dismiss_health_export", data={"session_id": shown})
    assert storage.get_session(shown)["health_logged"] == storage.HEALTH_DISMISSED
    assert storage.get_session(newer)["health_logged"] == storage.HEALTH_PENDING


def test_health_logged_rejects_unknown_and_in_progress(client):
    active = storage.create_session(datetime.now().astimezone().isoformat(timespec="seconds"), "pad", None)
    assert client.get(f"/health_logged/{active}").status_code == 404
    assert client.get("/health_logged/no-such-id").status_code == 404
    assert storage.get_session(active)["health_logged"] == storage.HEALTH_PENDING
    assert app._build_stats_payload()["health_status_changed"] is None


def test_dismissed_session_does_not_reprompt(client, monkeypatch):
    monkeypatch.setattr(app, "APPLE_HEALTH_EXPORT_ENABLED", True)
    completed_session(datetime.now())
    client.post("/dismiss_health_export")
    assert 'id="health-export-banner"' not in client.get("/").get_data(as_text=True)


# ── /settings ──

def test_settings_get(client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert b"Settings" in resp.data


def test_settings_post(client, monkeypatch):
    monkeypatch.setattr(app, "APPLE_HEALTH_EXPORT_ENABLED", True)
    new_max = config.MAX_SPEED_KMH - 0.5
    resp = client.post("/settings", data={
        "max_speed_kmh": str(new_max),
        "kcal_per_mile": "not-a-number",
        "port": str(config.PORT + 1),
    })
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/?saved=1")
    with open(app._CONFIG_FILE) as f:
        written = json.load(f)
    assert written["max_speed_kmh"] == new_max
    assert written["apple_health_export_enabled"] is False
    assert config.MAX_SPEED_KMH == app.MAX_SPEED_KMH == new_max
    assert written["kcal_per_mile"] == config.KCAL_PER_MILE == app.KCAL_PER_MILE
    assert config.PORT == written["port"]
    assert config.APPLE_HEALTH_EXPORT_ENABLED is False
    assert app.APPLE_HEALTH_EXPORT_ENABLED is False


@pytest.mark.parametrize(("days_ago", "dismissed"), [(0, False), (2, True)])
def test_settings_enable_health_export_predismiss(client, monkeypatch, days_ago, dismissed):
    monkeypatch.setattr(app, "APPLE_HEALTH_EXPORT_ENABLED", False)
    sid = completed_session(datetime.now() - timedelta(days=days_ago))
    client.post("/settings", data={"apple_health_export_enabled": "on"})
    assert app.APPLE_HEALTH_EXPORT_ENABLED is True
    assert bool(storage.get_session(sid)["health_logged"]) is dismissed


def test_settings_already_enabled_no_predismiss(client, monkeypatch):
    monkeypatch.setattr(app, "APPLE_HEALTH_EXPORT_ENABLED", True)
    sid = completed_session(datetime.now() - timedelta(days=2))
    client.post("/settings", data={"apple_health_export_enabled": "on"})
    assert not storage.get_session(sid)["health_logged"]


# ── /reconnect ──

@pytest.mark.parametrize(("connected", "connecting", "called"), [
    (False, False, True), (True, False, False), (False, True, False),
])
def test_reconnect(client, monkeypatch, connected, connecting, called):
    calls = []
    monkeypatch.setattr(app, "_start_ble_thread", lambda: calls.append(1))
    monkeypatch.setattr(app, "connected", connected)
    monkeypatch.setattr(app, "connecting", connecting)
    assert redirects_root(client.post("/reconnect"))
    assert bool(calls) is called


def test_reconnect_rejects_get(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_start_ble_thread", lambda: calls.append(1))
    monkeypatch.setattr(app, "connected", False)
    assert client.get("/reconnect").status_code == 405
    assert calls == []


@pytest.mark.parametrize("connection_failed", [True, False])
def test_connecting_page_reconnects_by_post(client, monkeypatch, connection_failed):
    monkeypatch.setattr(app, "connected", False)
    monkeypatch.setattr(app, "connection_failed", connection_failed)
    html = client.get("/").get_data(as_text=True)
    assert '<form method="post" action="/reconnect"' in html
    assert 'href="/reconnect"' not in html


# ── /stats, /stats_stream ──

def test_stats(client, running, monkeypatch):
    monkeypatch.setattr(app, "current_steps", 42)
    resp = client.get("/stats")
    assert resp.headers["Cache-Control"] == "no-store"
    body = resp.get_json()
    assert body["is_connected"] and body["session_active"] and body["is_running"]
    assert body["steps"] == 42


def test_stats_stream(client):
    resp = client.get("/stats_stream", buffered=False)
    assert resp.mimetype == "text/event-stream"
    first = next(iter(resp.response))
    first = first.decode() if isinstance(first, bytes) else first
    assert first.startswith("data: ")
    assert json.loads(first[len("data: "):])["is_connected"] is True
    assert len(app._sse_subscribers) == 1
    resp.close()
    assert app._sse_subscribers == []


def test_stats_stream_close_after_subscriber_already_removed(client):
    resp = client.get("/stats_stream", buffered=False)
    next(iter(resp.response))
    app._sse_subscribers.clear()
    resp.close()
    assert app._sse_subscribers == []


def test_stats_stream_keepalive_and_frames(client, monkeypatch):
    monkeypatch.setattr(app, "_SSE_KEEPALIVE_TIMEOUT_SECONDS", 0.01)
    resp = client.get("/stats_stream", buffered=False)
    chunks = (c.decode() if isinstance(c, bytes) else c for c in resp.response)
    next(chunks)
    assert next(chunks) == ": keepalive\n\n"
    app._sse_subscribers[0].put("data: {}\n\n")
    assert next(chunks) == "data: {}\n\n"
    resp.close()


# ── /shutdown ──

def test_shutdown(client, monkeypatch):
    threads, exits = [], []

    class FakeThread:
        def __init__(self, target, daemon):
            threads.append(target)

        def start(self):
            pass

    monkeypatch.setattr(app.threading, "Thread", FakeThread)
    monkeypatch.setattr(app.os, "_exit", exits.append)
    resp = client.post("/shutdown")
    assert resp.get_json() == {"status": "shutting_down"}
    assert app._shutting_down and app._server_stopping
    assert len(threads) == 1
    assert client.post("/shutdown").get_json() == {"status": "shutting_down"}
    assert len(threads) == 1
    assert exits == []


class _BleLoop:
    def __init__(self):
        self.calls = []

    def is_closed(self):
        return False

    def is_running(self):
        return True

    def stop(self):
        pass

    def call_soon_threadsafe(self, fn):
        self.calls.append(fn)


@pytest.mark.parametrize("fails", [False, True])
def test_shutdown_stops_ble_loop(client, monkeypatch, fails):
    loop, scheduled, timeouts = _BleLoop(), [], []

    class Future:
        def result(self, timeout):
            timeouts.append(timeout)
            if fails:
                raise TimeoutError

    def schedule(coro, ble_loop):
        assert ble_loop is loop
        scheduled.append(coro.__name__)
        coro.close()
        return Future()

    class FakeThread:
        def __init__(self, target, daemon):
            pass

        def start(self):
            pass

    monkeypatch.setattr(app.asyncio, "run_coroutine_threadsafe", schedule)
    monkeypatch.setattr(app.threading, "Thread", FakeThread)
    monkeypatch.setattr(app, "ble_loop", loop)
    assert client.post("/shutdown").get_json() == {"status": "shutting_down"}
    assert scheduled == ["_graceful_shutdown"]
    assert timeouts == [10]
    assert loop.calls == [loop.stop]


class _IdleLoop(_BleLoop):
    def is_running(self):
        return False


class _StuckLoop(_BleLoop):
    def call_soon_threadsafe(self, fn):
        self.calls.append(fn)
        raise RuntimeError("loop closed")


@pytest.mark.parametrize(("loop_cls", "stop_requested"), [(_IdleLoop, False), (_StuckLoop, True)])
def test_shutdown_loop_stop_edge_cases(client, monkeypatch, loop_cls, stop_requested):
    loop = loop_cls()

    def schedule(coro, ble_loop):
        coro.close()
        return SimpleNamespace(result=lambda timeout: None)

    monkeypatch.setattr(app.asyncio, "run_coroutine_threadsafe", schedule)
    monkeypatch.setattr(app.threading, "Thread", lambda target, daemon: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(app, "ble_loop", loop)
    assert client.post("/shutdown").get_json() == {"status": "shutting_down"}
    assert loop.calls == ([loop.stop] if stop_requested else [])
