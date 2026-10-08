import asyncio
import concurrent.futures
import logging
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

import app
import storage

STANDBY = ("switch_mode", app.WalkingPad.MODE_STANDBY)
MANUAL = ("switch_mode", app.WalkingPad.MODE_MANUAL)
FULL_WAKE = [STANDBY, MANUAL, ("start_belt",)]
_real_sleep = asyncio.sleep


class FakeClient:
    def __init__(self, connected=True, fail=False):
        self.is_connected = connected
        self.fail = fail
        self.disconnects = 0
        self.callback = None

    async def disconnect(self):
        self.disconnects += 1
        if self.fail:
            raise RuntimeError("disconnect failed")

    def set_disconn_callback(self, cb):
        self.callback = cb


class FakeController:
    """Records calls in order; `fail` names methods that raise; `hook(name)` runs on every call."""

    def __init__(self, fail=(), hook=None):
        self.calls = []
        self.fail = set(fail)
        self.hook = hook
        self.client = FakeClient()

    async def _call(self, name, *args):
        self.calls.append((name, *args))
        if self.hook:
            self.hook(name)
        if name in self.fail:
            raise RuntimeError(f"{name} failed")

    async def run(self, address):
        await self._call("run", address)

    async def switch_mode(self, mode):
        await self._call("switch_mode", mode)

    async def start_belt(self):
        await self._call("start_belt")

    async def stop_belt(self):
        await self._call("stop_belt")

    async def change_speed(self, speed):
        await self._call("change_speed", speed)

    async def ask_stats(self):
        await self._call("ask_stats")


class FakeLoop:
    def __init__(self, running=True):
        self.running = running
        self.scheduled = []

    def is_running(self):
        return self.running

    def is_closed(self):
        return False

    def stop(self):
        self.running = False

    def call_soon_threadsafe(self, fn, *args):
        self.scheduled.append(fn)
        fn(*args)


def reply(speed_kmh):
    """A hook that answers ask_stats() with a status packet at speed_kmh."""
    def hook(name):
        if name == "ask_stats":
            app.process_status_packet(0, 0, int(speed_kmh * 10))
    return hook


def run(make_coro):
    async def main():
        app._ble_command_lock = asyncio.Lock()
        return await make_coro()
    return asyncio.run(main())


@pytest.fixture
def sleeps(app_state, monkeypatch):
    """Instant asyncio.sleep (still yields); returns the requested delays."""
    delays = []

    async def fake(delay, result=None):
        delays.append(delay)
        await _real_sleep(0)
        return result

    monkeypatch.setattr(app.asyncio, "sleep", fake)
    return delays


def on_sleep(monkeypatch, action):
    """Instant asyncio.sleep that first calls action(n) with the 1-based call count."""
    count = [0]

    async def fake(delay, result=None):
        count[0] += 1
        action(count[0])
        await _real_sleep(0)
        return result

    monkeypatch.setattr(app.asyncio, "sleep", fake)


def disconnect_on_sleep(n):
    """on_sleep action: drop `connected` on the nth sleep, ending the idle watchdog."""
    def action(count):
        if count == n:
            app.connected = False
    return action


@pytest.fixture
def pad(sleeps, monkeypatch):
    fake = FakeController()
    monkeypatch.setattr(app, "controller", fake)
    return fake


@pytest.fixture
def disconnects(monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_handle_disconnect", calls.append)
    return calls


# ── small helpers ────────────────────────────────────────────────────────
@pytest.mark.parametrize(("client", "expected"), [
    (FakeClient(), 1),
    (FakeClient(connected=False), 0),
    (FakeClient(fail=True), 1),
])
def test_safe_disconnect_client(app_state, client, expected):
    run(lambda: app._safe_disconnect_client(client, "test"))
    assert client.disconnects == expected


def test_safe_disconnect_client_none(app_state):
    run(lambda: app._safe_disconnect_client(None, "test"))


def test_run_locked_returns_factory_result(app_state):
    async def make():
        assert app._ble_command_lock.locked()
        return "ok"

    assert run(lambda: app._run_locked(make)) == "ok"


def test_run_locked_acquire_timeout_skips_factory(app_state):
    made = []

    async def body():
        await app._ble_command_lock.acquire()
        with pytest.raises(asyncio.TimeoutError):
            await app._run_locked(lambda: made.append(1), timeout=0.01)

    run(body)
    assert made == []


def test_run_locked_releases_after_factory_raises(app_state):
    async def boom():
        raise ValueError("boom")

    async def body():
        with pytest.raises(ValueError, match="boom"):
            await app._run_locked(boom)
        assert not app._ble_command_lock.locked()

    run(body)


def test_cancel_task(app_state):
    async def body():
        await app._cancel_task(None)
        done = asyncio.create_task(_real_sleep(0))
        await done
        await app._cancel_task(done)
        assert not done.cancelled()
        running = asyncio.create_task(asyncio.Event().wait())
        await _real_sleep(0)
        await app._cancel_task(running)
        assert running.cancelled()

    run(body)


def test_cancel_task_keeps_callers_own_cancellation(app_state):
    async def swallows():  # like every belt sequence
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass

    async def body():
        target = asyncio.create_task(swallows())
        await _real_sleep(0)
        caller = asyncio.create_task(app._cancel_task(target))
        await _real_sleep(0)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller

    run(body)


def test_check_staleness_and_disconnect(app_state, monkeypatch, disconnects):
    monkeypatch.setattr(app.time, "monotonic", lambda: 1000.0)
    app._last_status_update_monotonic = 1000.0 - app._STALE_STATUS_TIMEOUT_SECONDS
    assert app._check_staleness_and_disconnect("test") is False
    assert disconnects == []
    app._last_status_update_monotonic -= 1
    assert app._check_staleness_and_disconnect("test") is True
    assert disconnects == [None]


# ── wake sequences ───────────────────────────────────────────────────────
def test_full_wake_sequence_order(pad):
    run(app._full_wake_sequence)
    assert pad.calls == FULL_WAKE


def test_try_light_wake_confirmed(pad):
    pad.hook = reply(3.0)
    assert run(app._try_light_wake) is True
    assert pad.calls == [("start_belt",), ("ask_stats",)]


@pytest.mark.parametrize(("speed_before", "hook"), [
    (3.0, None),  # stale nonzero speed from before the probe must not count
    (0.0, reply(0.0)),  # fresh packet, but belt not moving
])
def test_try_light_wake_unconfirmed(pad, speed_before, hook):
    app.current_speed_kmh = speed_before
    pad.hook = hook
    assert run(app._try_light_wake) is False
    assert pad.calls.count(("ask_stats",)) == app._LIGHT_WAKE_PROBE_ATTEMPTS


def test_try_light_wake_swallows_start_belt_error(pad):
    pad.fail = {"start_belt"}
    pad.hook = reply(3.0)
    assert run(app._try_light_wake) is True


def test_wake_and_start_belt_full(pad):
    run(lambda: app._wake_and_start_belt(3.5))
    assert pad.calls == [*FULL_WAKE, ("change_speed", int(3.5 * 10))]


def test_wake_and_start_belt_no_speed(pad):
    run(app._wake_and_start_belt)
    assert pad.calls == FULL_WAKE


def test_wake_and_start_belt_light_wake(pad):
    pad.hook = reply(2.5)
    run(lambda: app._wake_and_start_belt(2.5, try_light_wake=True))
    assert pad.calls == [("start_belt",), ("ask_stats",), ("change_speed", 25)]


def test_wake_and_start_belt_light_wake_falls_back(pad):
    run(lambda: app._wake_and_start_belt(2.5, try_light_wake=True))
    probe = [("start_belt",)] + [("ask_stats",)] * app._LIGHT_WAKE_PROBE_ATTEMPTS
    assert pad.calls == [*probe, *FULL_WAKE, ("change_speed", 25)]


def test_locked_change_speed(pad, disconnects):
    run(lambda: app._locked_change_speed(30))
    assert pad.calls == [("change_speed", 30)]
    assert disconnects == []


def test_locked_change_speed_failure_disconnects(pad, disconnects):
    pad.fail = {"change_speed"}
    run(lambda: app._locked_change_speed(30))
    assert disconnects == [None]


# ── _handle_disconnect ───────────────────────────────────────────────────
def test_handle_disconnect_without_loop(app_state):
    app.belt_running = True
    app._handle_disconnect(None)
    assert (app.connected, app.connecting, app.connection_failed, app.belt_running) == (False, False, True, False)
    assert app_state == []


def test_handle_disconnect_schedules_one_reconnect(app_state):
    app.ble_loop = FakeLoop()
    app._handle_disconnect(None)
    app._handle_disconnect(None)
    assert (app.connected, app.connecting, app.connection_failed) == (False, True, False)
    assert [c.__name__ for c in app_state] == ["_auto_reconnect"]


def test_handle_disconnect_no_reconnect_while_shutting_down(app_state):
    app.ble_loop = FakeLoop()
    app._shutting_down = True
    app._handle_disconnect(None)
    assert (app.connecting, app.connection_failed) == (False, True)
    assert app_state == []


def test_handle_disconnect_ignores_stale_client(app_state):
    app.controller = FakeController()
    app._handle_disconnect(FakeClient())
    assert app.connected is True
    app._handle_disconnect(app.controller.client)
    assert app.connected is False


def test_handle_disconnect_records_auto_pause(app_state, monkeypatch):
    pauses = []
    monkeypatch.setattr(app, "_record_pause", pauses.append)
    app.session_active = True
    app._handle_disconnect(None)
    assert pauses == ["auto"]


def test_handle_disconnect_cancels_tasks(app_state):
    async def body():
        tasks = [asyncio.create_task(asyncio.Event().wait()) for _ in range(4)]
        app._stats_monitor_task, app._idle_watchdog_task, app._belt_sequence_task, app._speed_change_task = tasks
        await _real_sleep(0)
        app._handle_disconnect(None)
        await asyncio.gather(*tasks, return_exceptions=True)
        assert all(t.cancelled() for t in tasks)

    run(body)


# ── reconnect / scan / connect ───────────────────────────────────────────
def _fake_connect(monkeypatch, results, on_call=None):
    attempts = []

    async def connect():
        attempts.append(1)
        if on_call:
            on_call()
        result = results[len(attempts) - 1]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(app, "_connect_to_pad", connect)
    return attempts


def test_auto_reconnect_succeeds_after_failures(sleeps, monkeypatch):
    app.connected, app.connecting = False, True
    attempts = _fake_connect(monkeypatch, [False, RuntimeError("scan"), True])
    run(app._auto_reconnect)
    assert len(attempts) == 3
    assert (app.connected, app.connecting, app.connection_failed) == (True, False, False)


def test_auto_reconnect_gives_up(sleeps, monkeypatch):
    app.connected, app.connecting = False, True
    attempts = _fake_connect(monkeypatch, [False] * app._MAX_RECONNECT_ATTEMPTS)
    run(app._auto_reconnect)
    assert len(attempts) == app._MAX_RECONNECT_ATTEMPTS
    assert (app.connected, app.connecting, app.connection_failed) == (False, False, True)
    assert sleeps == [
        min(app._RECONNECT_BASE_DELAY_SECONDS * 2 ** (n - 1), app._RECONNECT_MAX_DELAY_SECONDS)
        for n in range(1, app._MAX_RECONNECT_ATTEMPTS)
    ]


def test_auto_reconnect_exits_when_shutting_down(sleeps, monkeypatch):
    app._shutting_down = True
    attempts = _fake_connect(monkeypatch, [])
    run(app._auto_reconnect)
    assert attempts == []


def test_auto_reconnect_stops_after_shutdown_begins(sleeps, monkeypatch):
    attempts = _fake_connect(monkeypatch, [False], on_call=lambda: setattr(app, "_shutting_down", True))
    run(app._auto_reconnect)
    assert len(attempts) == 1
    assert app.connection_failed is False


def _fake_scanner(monkeypatch, devices, fail=False):
    class Scanner:
        discovered_devices = devices

        async def __aenter__(self):
            if fail:
                raise RuntimeError("adapter off")
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(app, "BleakScanner", Scanner)


_NAMED = SimpleNamespace(address="AA", name=f"{app.BLE_DEVICE_NAME} X21")
_KNOWN = SimpleNamespace(address="BB", name=None)
_OTHER = SimpleNamespace(address="CC", name="Headphones")


@pytest.mark.parametrize(("devices", "known", "fail", "expected"), [
    ([_NAMED, _KNOWN], "BB", False, _KNOWN),
    ([_OTHER, _NAMED], "ZZ", False, _NAMED),
    ([_OTHER, _NAMED], None, False, _NAMED),
    ([_OTHER, _KNOWN], "ZZ", False, None),
    ([_NAMED], None, True, None),
])
def test_scan_for_device(sleeps, monkeypatch, devices, known, fail, expected):
    _fake_scanner(monkeypatch, devices, fail)
    app._device_ble_address = known
    assert run(lambda: app._scan_for_device(timeout=1)) is expected


def _new_controller(monkeypatch):
    """_connect_to_pad() finds _NAMED and builds the returned FakeController."""
    new = FakeController()

    async def scan(timeout):
        return _NAMED

    async def idle():
        pass

    monkeypatch.setattr(app, "_scan_for_device", scan)
    monkeypatch.setattr(app, "Controller", lambda: new)
    monkeypatch.setattr(app, "_idle_connection_watchdog", idle)
    return new


def test_connect_to_pad(sleeps, monkeypatch):
    old = FakeController()
    new = _new_controller(monkeypatch)
    app.controller = old

    assert run(app._connect_to_pad) is True
    assert old.client.disconnects == 1
    assert app.controller is new
    assert app._device_ble_address == _NAMED.address
    assert new.calls == [("run", _NAMED.address), MANUAL]
    assert new.client.callback is app._handle_disconnect
    assert app._idle_watchdog_task is not None
    new.on_cur_status_received(None, {"dist": 0, "steps": 0, "speed": 25})
    assert app.current_speed_kmh == 2.5


def test_connect_to_pad_device_not_found(sleeps, monkeypatch):
    scans = []

    async def scan(timeout):
        scans.append(timeout)

    monkeypatch.setattr(app, "_scan_for_device", scan)
    app._device_ble_address = "AA"
    assert run(app._connect_to_pad) is False
    assert len(scans) == 3
    assert app._device_ble_address is None


# ── background loops ─────────────────────────────────────────────────────
def test_stats_monitor_polls_until_belt_stops(pad, disconnects):
    app.belt_running = True

    def hook(name):
        if len(pad.calls) == 3:
            app.belt_running = False

    pad.hook = hook
    run(app._stats_monitor)
    assert pad.calls == [("ask_stats",)] * 3
    assert disconnects == []


def test_stats_monitor_breaks_on_staleness(pad, disconnects, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: clock[0])
    app.belt_running = True

    def hook(name):
        clock[0] += app._STALE_STATUS_TIMEOUT_SECONDS + 1

    pad.hook = hook
    run(app._stats_monitor)
    assert pad.calls == [("ask_stats",)]
    assert disconnects == [None]
    assert app.belt_running is True


def test_idle_watchdog_exits_when_not_connected(pad):
    app.connected = False
    run(app._idle_connection_watchdog)
    assert pad.calls == []


def test_idle_watchdog_detects_dead_client(pad, disconnects):
    pad.client.is_connected = False
    run(app._idle_connection_watchdog)
    assert disconnects == [None]
    assert pad.calls == []


def _begin_session():
    app._session_start_time = datetime.now()
    app._begin_db_session()
    app.session_active = True
    return app._session_id


def test_graceful_shutdown(pad):
    session_id = _begin_session()
    app.belt_running = True
    run(app._graceful_shutdown)
    assert pad.calls == [("stop_belt",), STANDBY]
    assert pad.client.disconnects == 1
    assert storage.get_session(session_id)["status"] == "completed"
    assert (app.connected, app.session_active, app.belt_running) == (False, False, False)


def test_graceful_shutdown_resets_flags_on_error(pad):
    pad.fail = {"stop_belt"}
    _begin_session()
    app.belt_running = True
    run(app._graceful_shutdown)
    assert pad.calls == [("stop_belt",)]
    assert (app.connected, app.session_active, app.belt_running) == (False, False, False)


def test_graceful_shutdown_idle(pad, caplog):
    run(app._graceful_shutdown)
    assert pad.calls == [STANDBY]
    assert pad.client.disconnects == 1
    assert storage.list_sessions() == []
    assert storage.list_active_sessions() == []
    assert "Graceful shutdown error" not in caplog.text


def test_graceful_shutdown_without_controller_still_saves(sleeps, caplog):
    session_id = _begin_session()
    app.belt_running = True
    run(app._graceful_shutdown)
    assert storage.get_session(session_id)["status"] == "completed"
    assert (app.connected, app.session_active, app.belt_running) == (False, False, False)
    assert "Graceful shutdown error" not in caplog.text


def test_graceful_shutdown_controller_without_client(pad, caplog):
    pad.client = None
    with caplog.at_level(logging.INFO):
        run(app._graceful_shutdown)
    assert pad.calls == [STANDBY]
    assert "Disconnecting BLE client" not in caplog.text
    assert "Graceful shutdown error" not in caplog.text


def test_end_session_while_paused_sends_no_stop(app_state, pad, disconnects):
    session_id = _begin_session()
    app.ble_loop = FakeLoop()
    app.app.test_client().post("/end_session")
    [sequence] = app_state
    app_state.clear()
    run(lambda: sequence)
    assert pad.calls == []
    assert disconnects == []
    assert app._belt_transitioning is False
    assert storage.get_session(session_id)["status"] == "completed"


# ── belt sequences scheduled by the routes ───────────────────────────────
_ROUTES = {
    "/start": ({}, FULL_WAKE, "start_belt", True),
    "/pause": ({"session_active": True, "belt_running": True}, [("stop_belt",)], "stop_belt", False),
    "/resume": (
        {"session_active": True, "resume_speed_kmh": 3.0},
        [("start_belt",), ("ask_stats",), ("change_speed", 30)], "change_speed", True,
    ),
    "/end_session": (
        {"session_active": True, "belt_running": True, "_session_start_time": datetime.now(), "ble_loop": FakeLoop()},
        [("stop_belt",)], "stop_belt", False,
    ),
}


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("route", list(_ROUTES))
def test_route_belt_sequence(app_state, pad, disconnects, monkeypatch, route, fail):
    state, expected, failing, restamps = _ROUTES[route]
    for name, value in state.items():
        setattr(app, name, value)

    async def monitor():
        pass

    monkeypatch.setattr(app, "_stats_monitor", monitor)
    app.app.test_client().post(route)
    [sequence] = app_state
    app_state.clear()

    transitioning = []

    def hook(name):
        transitioning.append(app._belt_transitioning)
        reply(3.0)(name)

    pad.hook = hook
    if fail:
        pad.fail = {failing}
    app._resume_grace_deadline = 0
    before = time.time()
    run(lambda: sequence)

    assert transitioning and all(transitioning)
    assert app._belt_transitioning is False
    assert app._belt_sequence_task is None
    if restamps:
        assert app._resume_grace_deadline >= before + app.RESUME_GRACE_PERIOD_SECONDS
    if fail:
        assert disconnects == [None]
        assert app.belt_running is False
    else:
        assert pad.calls == expected
        assert disconnects == []
        if restamps:
            assert app._stats_monitor_task is not None


def test_speed_change_registers_before_waiting_on_predecessor(app_state, pad):
    async def body():
        await app._ble_command_lock.acquire()
        first = asyncio.create_task(app._locked_change_speed(30))
        await _real_sleep(0)
        second = asyncio.create_task(app._locked_change_speed(36))
        await _real_sleep(0)
        assert app._speed_change_task is second  # so a disconnect at this point cancels it
        second.cancel()
        await asyncio.wait({first, second})
        app._ble_command_lock.release()
        assert second.cancelled() and first.done()

    run(body)
    assert pad.calls == []


def test_cancel_task_on_another_loop_cancels_there(app_state):
    other = asyncio.new_event_loop()
    stray = other.create_task(asyncio.Event().wait())

    async def body():
        # Bounded: waiting across threads would hang (nothing runs `other`).
        await asyncio.wait_for(app._cancel_task(stray), timeout=1)

    run(body)
    other.run_until_complete(asyncio.wait({stray}))
    assert stray.cancelled()
    other.close()
    closed = asyncio.new_event_loop()
    dead = closed.create_task(asyncio.Event().wait())
    closed.close()
    run(lambda: app._cancel_task(dead))  # closed loop: nothing to do, no error
    dead.get_coro().close()  # never ran; avoids a "never awaited" warning


def test_end_stops_belt_when_it_cancels_an_unfinished_pause(app_state, pad, disconnects):
    app.session_active, app.belt_running = True, True
    app._session_start_time = datetime.now()
    app.ble_loop = FakeLoop()
    client = app.app.test_client()
    client.post("/pause")
    client.post("/end_session")  # was_running is False: the pause already flipped it
    pause_seq, end_seq = app_state
    app_state.clear()

    async def body():
        await app._ble_command_lock.acquire()  # pause is stuck before its stop_belt
        pause = asyncio.create_task(pause_seq)
        await _real_sleep(0)
        end = asyncio.create_task(end_seq)
        await _real_sleep(0)
        app._ble_command_lock.release()
        await end
        assert pause.done()

    run(body)
    assert pad.calls == [("stop_belt",)]
    assert app._belt_transitioning is False




def test_superseded_sequence_leaves_newer_transition(app_state, pad, disconnects, monkeypatch):
    async def monitor():
        pass

    monkeypatch.setattr(app, "_stats_monitor", monitor)
    app.session_active, app.belt_running, app.resume_speed_kmh = True, True, 3.0
    client = app.app.test_client()
    client.post("/pause")
    client.post("/resume")
    pause_seq, resume_seq = app_state
    app_state.clear()
    pad.hook = reply(3.0)
    run(lambda: pause_seq)
    assert app._belt_transitioning is True  # resume is still queued
    run(lambda: resume_seq)
    assert app._belt_transitioning is False


@pytest.mark.parametrize("route", ["/start", "/pause", "/resume"])
def test_route_belt_sequence_cancelled_waiting_for_lock(app_state, pad, disconnects, route):
    state = _ROUTES[route][0]
    for name, value in state.items():
        setattr(app, name, value)
    app.app.test_client().post(route)
    [sequence] = app_state
    app_state.clear()
    belt_running = app.belt_running

    async def body():
        await app._ble_command_lock.acquire()  # e.g. held by an in-flight stats poll
        task = asyncio.create_task(sequence)
        await _real_sleep(0)
        assert app._belt_transitioning is True
        task.cancel()
        await task
        assert not task.cancelled()  # the sequence swallows its own cancellation

    run(body)
    assert app._belt_transitioning is False
    assert app._belt_sequence_task is None
    assert app._stats_monitor_task is None
    assert pad.calls == []
    assert disconnects == []
    assert app.belt_running is belt_running


# ── more connect paths ───────────────────────────────────────────────────
def _boom(*args):
    raise RuntimeError("boom")


@pytest.mark.parametrize(("api", "fails"), [
    ("set_disconn_callback", False),
    ("set_disconnected_callback", False),
    ("set_disconn_callback", True),
    ("set_disconnected_callback", True),
    (None, False),
])
def test_connect_to_pad_disconnect_callback_api(sleeps, monkeypatch, api, fails):
    new = _new_controller(monkeypatch)
    registered = []
    new.client = SimpleNamespace(is_connected=True)
    if api:
        setattr(new.client, api, _boom if fails else registered.append)
    assert run(app._connect_to_pad) is True
    assert registered == ([app._handle_disconnect] if api and not fails else [])
    assert new.calls == [("run", _NAMED.address), MANUAL]


def test_connect_to_pad_without_client_skips_callback(sleeps, monkeypatch, caplog):
    new = _new_controller(monkeypatch)
    new.client = None
    with caplog.at_level(logging.DEBUG):
        assert run(app._connect_to_pad) is True
    assert new.calls == [("run", _NAMED.address), MANUAL]
    assert "Disconnect callback not available" not in caplog.text


@pytest.mark.parametrize("fails", [False, True])
def test_connect_to_pad_enables_notifications(sleeps, monkeypatch, fails):
    new = _new_controller(monkeypatch)

    async def enable_notifications():
        await new._call("enable_notifications")
        if fails:
            raise RuntimeError("notify unsupported")

    new.enable_notifications = enable_notifications
    assert run(app._connect_to_pad) is True
    assert new.calls[-1] == ("enable_notifications",)


def test_connect_to_pad_status_handler_swallows_errors(sleeps, monkeypatch, caplog):
    new = _new_controller(monkeypatch)
    run(app._connect_to_pad)
    monkeypatch.setattr(app, "process_status_packet", _boom)
    new.on_cur_status_received(None, {"dist": 0, "steps": 0, "speed": 25})
    assert "_handle_status_update error: boom" in caplog.text


def test_try_light_wake_keeps_probing_after_ask_stats_errors(pad):
    pad.fail = {"ask_stats"}
    assert run(app._try_light_wake) is False
    assert pad.calls.count(("ask_stats",)) == app._LIGHT_WAKE_PROBE_ATTEMPTS


def test_auto_reconnect_stops_when_shutdown_begins_during_backoff(app_state, monkeypatch):
    on_sleep(monkeypatch, lambda n: setattr(app, "_shutting_down", True))
    app.connected, app.connecting = False, True
    attempts = _fake_connect(monkeypatch, [False, True])
    run(app._auto_reconnect)
    assert len(attempts) == 1
    assert (app.connected, app.connection_failed) == (False, False)


def test_handle_disconnect_reconnect_scheduling_fails(app_state, monkeypatch):
    def fail(coro, loop):
        coro.close()
        raise RuntimeError("loop closed")

    monkeypatch.setattr(app.asyncio, "run_coroutine_threadsafe", fail)
    app.ble_loop = FakeLoop()
    app._handle_disconnect(None)
    assert (app.connected, app.connecting, app.connection_failed) == (False, False, True)


def test_handle_disconnect_cancels_tasks_on_their_own_loop(app_state):
    newer = FakeLoop()  # e.g. a manual reconnect already replaced ble_loop

    async def body():
        tasks = [asyncio.create_task(asyncio.Event().wait()) for _ in range(2)]
        app._stats_monitor_task, app._belt_sequence_task = tasks
        await _real_sleep(0)
        app.ble_loop = newer
        app._handle_disconnect(None)
        await asyncio.gather(*tasks, return_exceptions=True)
        assert newer.scheduled == []
        assert all(t.cancelled() for t in tasks)

    run(body)


# ── more background-loop branches ────────────────────────────────────────
def test_idle_watchdog_defers_to_stats_monitor_while_belt_running(pad, disconnects, monkeypatch):
    on_sleep(monkeypatch, disconnect_on_sleep(3))
    app.belt_running = True
    pad.client.is_connected = False  # would trigger a disconnect if checked
    run(app._idle_connection_watchdog)
    assert pad.calls == []
    assert disconnects == []


def test_idle_watchdog_pings_when_is_connected_raises(pad, disconnects, monkeypatch):
    class BrokenClient:
        @property
        def is_connected(self):
            raise RuntimeError("backend gone")

    on_sleep(monkeypatch, disconnect_on_sleep(2))
    pad.client = BrokenClient()
    app._last_status_update_monotonic = time.monotonic()
    run(app._idle_connection_watchdog)
    assert pad.calls == [("ask_stats",)]
    assert disconnects == []


def test_idle_watchdog_breaks_when_stale(pad, disconnects, monkeypatch):
    monkeypatch.setattr(app.time, "monotonic", lambda: 1000.0)
    app._last_status_update_monotonic = 1000.0 - app._IDLE_STALE_STATUS_TIMEOUT_SECONDS - 1
    run(app._idle_connection_watchdog)  # connected stays True: only the break can end it
    assert disconnects == [None]
    assert pad.calls == []


def test_idle_watchdog_retries_after_ping_error(pad, disconnects, monkeypatch):
    on_sleep(monkeypatch, disconnect_on_sleep(3))
    pad.fail = {"ask_stats"}
    app._last_status_update_monotonic = time.monotonic()
    run(app._idle_connection_watchdog)
    assert pad.calls == [("ask_stats",)] * 2
    assert disconnects == []


def test_stats_monitor_processes_poll_reply(pad, disconnects, monkeypatch):
    packets = []
    monkeypatch.setattr(app, "process_status_packet", lambda *a: packets.append(a))
    app.belt_running = True

    async def ask_stats():
        app.belt_running = False
        return {"dist": 5, "steps": 7, "speed": 25}

    pad.ask_stats = ask_stats
    run(app._stats_monitor)
    assert packets == [(5, 7, 25)]


@pytest.mark.parametrize(("exc", "logged"), [
    (asyncio.TimeoutError(), "Status poll timed out"),
    (RuntimeError("gatt"), "ask_stats error: gatt"),
])
def test_stats_monitor_survives_poll_errors(pad, disconnects, monkeypatch, caplog, exc, logged):
    app.belt_running = True
    polls = []

    async def ask_stats():
        polls.append(1)
        if len(polls) == 3:
            app.belt_running = False
        raise exc

    pad.ask_stats = ask_stats
    run(app._stats_monitor)
    assert len(polls) == 3
    assert caplog.text.count(logged) == 3
    assert disconnects == []


def test_stats_monitor_saves_state_periodically(pad, disconnects, monkeypatch):
    saves = []
    monkeypatch.setattr(app, "_save_session_state", lambda: saves.append(len(pad.calls)))
    app.belt_running = True
    ticks = 2 * app._SESSION_STATE_SAVE_INTERVAL_SECONDS + 1

    def hook(name):
        if len(pad.calls) == ticks:
            app.belt_running = False

    pad.hook = hook
    run(app._stats_monitor)
    n = app._SESSION_STATE_SAVE_INTERVAL_SECONDS
    assert saves == [n, 2 * n]


def test_stats_monitor_cancelled_during_sleep_exits_cleanly(pad, monkeypatch):
    app.belt_running = True

    async def body():
        parked = asyncio.Event()

        async def park(delay, result=None):
            parked.set()
            await asyncio.get_running_loop().create_future()

        monkeypatch.setattr(app.asyncio, "sleep", park)
        task = asyncio.create_task(app._stats_monitor())
        await parked.wait()
        task.cancel()
        await task
        assert not task.cancelled()

    run(body)
    assert pad.calls == [("ask_stats",)]


def test_stats_monitor_logs_unexpected_error(pad, monkeypatch, caplog):
    monkeypatch.setattr(app, "_check_staleness_and_disconnect", _boom)
    app.belt_running = True
    run(app._stats_monitor)
    assert "Stats monitor error: boom" in caplog.text
    assert pad.calls == [("ask_stats",)]


# ── BLE thread lifecycle ─────────────────────────────────────────────────
@pytest.fixture
def ble_thread(app_state, monkeypatch):
    """Runs _ble_thread() inline with _connect_to_pad replaced by `connect`."""
    def start(connect):
        monkeypatch.setattr(app, "_connect_to_pad", connect)
        app.connected, app.connecting = False, True
        app._ble_thread()
    yield start
    asyncio.set_event_loop(None)


async def _refuse():
    return False


async def _crash():
    raise RuntimeError("adapter off")


@pytest.mark.parametrize("connect", [_refuse, _crash])
def test_ble_thread_connect_failure(ble_thread, connect):
    ble_thread(connect)
    assert (app.connected, app.connecting, app.connection_failed) == (False, False, True)
    assert app.ble_loop.is_closed()


def test_ble_thread_event_loop_creation_fails(ble_thread, monkeypatch):
    monkeypatch.setattr(app.asyncio, "new_event_loop", _boom)
    ble_thread(_crash)
    assert (app.connecting, app.connection_failed) == (False, True)
    assert app.ble_loop is None


def _connect_then(during_run):
    """A successful connect that leaves a pending task and runs during_run(loop) once run_forever starts."""
    leftovers = []

    async def connect():
        loop = asyncio.get_running_loop()
        leftovers.append(loop.create_task(asyncio.Event().wait()))
        # Nested so it lands after run_until_complete() returns, inside run_forever().
        loop.call_soon(loop.call_soon, during_run, loop)
        return True

    return connect, leftovers


def test_ble_thread_runs_until_stopped(ble_thread):
    app._begin_belt_transition()  # e.g. queued on the previous loop, cancelled before it ran
    seen = []

    def during_run(loop):
        seen.append((app.connected, app.connecting, app.ble_loop is loop, app._ble_command_lock))
        loop.stop()

    connect, leftovers = _connect_then(during_run)
    ble_thread(connect)
    [(connected, connecting, current, lock)] = seen
    assert (connected, connecting, current) == (True, False, True)
    assert isinstance(lock, asyncio.Lock) and not lock.locked()
    assert app._belt_transitioning is False
    assert app.connected is False
    assert app.ble_loop.is_closed()
    assert leftovers[0].cancelled()


def test_ble_thread_resets_transition_before_publishing_loop(ble_thread, monkeypatch):
    old = app.ble_loop = FakeLoop()
    seen = []
    reset = app._end_belt_transition
    monkeypatch.setattr(app, "_end_belt_transition", lambda gen=None: (seen.append(app.ble_loop), reset(gen)))
    ble_thread(_refuse)
    assert seen == [old]


def test_ble_thread_superseded_cleanup_keeps_newer_state(ble_thread):
    newer = FakeLoop()

    def during_run(loop):
        app.ble_loop = newer
        loop.stop()

    connect, _ = _connect_then(during_run)
    ble_thread(connect)
    assert app.connected is True
    assert app.ble_loop is newer


@pytest.fixture
def threads(app_state, monkeypatch):
    started = []

    class Thread:
        def __init__(self, target, daemon):
            self.target, self.daemon = target, daemon

        def start(self):
            started.append(self)

    monkeypatch.setattr(app.threading, "Thread", Thread)
    return started


@pytest.mark.parametrize(("connected", "connecting"), [(True, False), (False, True)])
def test_start_ble_thread_guard(threads, connected, connecting):
    app.connected, app.connecting, app.connection_failed = connected, connecting, True
    app._start_ble_thread()
    assert threads == []
    assert (app.connecting, app.connection_failed) == (connecting, True)


@pytest.mark.parametrize("running", [True, False])
def test_start_ble_thread(threads, running):
    old = FakeLoop(running=running)
    app.ble_loop = old
    app.connected, app.connection_failed = False, True
    app._start_ble_thread()
    assert (app.connecting, app.connection_failed) == (True, False)
    assert old.scheduled == ([old.stop] if running else [])
    [thread] = threads
    assert (thread.target, thread.daemon) == (app._ble_thread, True)


# ── signal shutdown ──────────────────────────────────────────────────────
class _Exit(Exception):
    pass


@pytest.fixture
def signal_exit(app_state, monkeypatch):
    """Patches sleep/_exit (which raises _Exit) and makes scheduled coroutines resolve to `result`."""
    calls = []
    outcome = {"result": None}

    def schedule(coro, loop):
        app_state.append(coro)
        future = concurrent.futures.Future()
        if isinstance(outcome["result"], Exception):
            future.set_exception(outcome["result"])
        else:
            future.set_result(outcome["result"])
        return future

    def exit_(code):
        calls.append(("exit", code))
        raise _Exit

    monkeypatch.setattr(app.asyncio, "run_coroutine_threadsafe", schedule)
    monkeypatch.setattr(app.time, "sleep", lambda s: calls.append(("sleep", s)))
    monkeypatch.setattr(app.os, "_exit", exit_)
    return calls, outcome


def test_signal_shutdown_ignores_duplicate(signal_exit):
    calls, _ = signal_exit
    app._shutting_down = True
    app._handle_signal_shutdown(15, None)
    assert calls == []
    assert app._server_stopping is False


def test_signal_shutdown_without_loop(signal_exit, app_state):
    calls, _ = signal_exit
    with pytest.raises(_Exit):
        app._handle_signal_shutdown(15, None)
    assert calls == [("sleep", 2), ("exit", 0)]
    assert (app._shutting_down, app._server_stopping) == (True, True)
    assert app_state == []


@pytest.mark.parametrize("fails", [False, True])
def test_signal_shutdown_with_loop(signal_exit, app_state, fails):
    calls, outcome = signal_exit
    loop = FakeLoop()
    if fails:
        outcome["result"] = RuntimeError("stuck")
        loop.call_soon_threadsafe = _boom
    app.ble_loop = loop
    with pytest.raises(_Exit):
        app._handle_signal_shutdown(15, None)
    assert [c.__name__ for c in app_state] == ["_graceful_shutdown"]
    assert loop.running is fails
    assert calls == [("sleep", 2), ("exit", 0)]
    assert app._server_stopping is True
