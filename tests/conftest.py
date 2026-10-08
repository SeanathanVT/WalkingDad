import os

os.environ["WALKINGDAD_NO_STARTUP"] = "1"

import pytest

import app
import config
import storage

_RESET = {
    "connected": True, "connecting": False, "connection_failed": False,
    "ble_loop": None, "controller": None, "_device_ble_address": None, "_ble_command_lock": None,
    "_last_status_update_monotonic": 0.0,
    "_stats_monitor_task": None, "_idle_watchdog_task": None, "_belt_sequence_task": None,
    "_speed_change_task": None, "_auto_reconnect_task": None, "_belt_transitioning": False,
    "session_active": False, "belt_running": False, "_session_start_time": None, "_session_id": None,
    "_session_start_monotonic": 0.0, "_pending_restore": None, "_paused_since": None,
    "_resume_grace_deadline": 0, "_last_moving_packet_monotonic": None, "resume_speed_kmh": 2.0,
    "current_speed_kmh": 0.0, "current_distance_km": 0.0, "current_steps": 0, "current_calories": 0.0,
    "current_session_active_seconds": 0, "_last_dev_dist": 0, "_last_dev_steps": 0,
    "_shutting_down": False, "_server_stopping": False, "_last_health_status_change": None,
}
# /settings POST rewrites these on both modules; registering them restores them after each test.
_SETTINGS_CONSTS = [c for _, c, _, _ in app._SETTINGS_SCHEMA] + ["APPLE_HEALTH_EXPORT_ENABLED", "KEYBOARD_SHORTCUTS_ENABLED"]


@pytest.fixture
def app_state(monkeypatch, tmp_path):
    """Idle, connected app with every global reset, files and DB under tmp_path, and BLE scheduling recorded."""
    for name, value in _RESET.items():
        monkeypatch.setattr(app, name, value)
    for name in _SETTINGS_CONSTS:
        monkeypatch.setattr(config, name, getattr(config, name))
        if hasattr(app, name):
            monkeypatch.setattr(app, name, getattr(app, name))
    # Pinned, since config.py reads the maintainer's real data/config.json at import.
    monkeypatch.setattr(config, "KEYBOARD_SHORTCUTS_ENABLED", True)
    app.speed_history.clear()
    app._samples.reset()
    monkeypatch.setattr(app, "_sse_subscribers", [])
    monkeypatch.setattr(app, "SESSION_STATE_FILE", str(tmp_path / "session_state.json"))
    monkeypatch.setattr(app, "_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setattr(storage, "_db_path", None)
    storage.init_db(str(tmp_path / "test.db"))

    scheduled = []

    def record(coro, loop):
        scheduled.append(coro)

    monkeypatch.setattr(app.asyncio, "run_coroutine_threadsafe", record)
    yield scheduled
    for coro in scheduled:
        coro.close()


@pytest.fixture
def walking(monkeypatch):
    """A walking session past its grace window, with a controllable monotonic clock."""
    clock = [1000.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(app, "_save_session_state", lambda: None)
    monkeypatch.setattr(app, "_session_id", None)
    monkeypatch.setattr(app, "session_active", True)
    monkeypatch.setattr(app, "belt_running", True)
    monkeypatch.setattr(app, "_belt_transitioning", False)
    monkeypatch.setattr(app, "_resume_grace_deadline", 0)
    monkeypatch.setattr(app, "current_session_active_seconds", 0)
    monkeypatch.setattr(app, "current_speed_kmh", 0.0)
    monkeypatch.setattr(app, "current_distance_km", 0.0)
    monkeypatch.setattr(app, "current_steps", 0)
    monkeypatch.setattr(app, "_last_moving_packet_monotonic", None)
    monkeypatch.setattr(app, "_last_dev_dist", 0)
    monkeypatch.setattr(app, "_last_dev_steps", 0)
    monkeypatch.setattr(app, "resume_speed_kmh", 3.0)
    app.speed_history.clear()

    def packet(speed_kmh, after=1.0, dist=0, steps=0):
        clock[0] += after
        app.process_status_packet(dist, steps, int(speed_kmh * 10))

    return packet
