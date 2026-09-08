import asyncio
import atexit
import csv
import io
import json
import logging
import os
import queue
import signal
import threading
import time
from collections import deque
from datetime import datetime

from bleak import BleakScanner
from flask import Flask, render_template, redirect, url_for, jsonify, make_response, request, Response
from ph4_walkingpad.pad import Controller, WalkingPad

import config
from config import (
    BLE_DEVICE_NAME, KCAL_PER_MILE, MAX_SPEED_KMH, MIN_SPEED_KMH,
    SPEED_STEP, SLOW_WALK_SPEED_KMH, RESUME_GRACE_PERIOD_SECONDS,
    HISTORY_DISPLAY_LIMIT, APPLE_HEALTH_SHORTCUT_NAME,
)

# ── Logging Setup ────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
)

# ── Conversion constants ─────────────────────────────────────────────────
KM_TO_MI = 0.621371


def kcal_estimate(miles: float) -> float:
    return KCAL_PER_MILE * miles


def format_seconds_to_hms(total_seconds: int) -> str:
    """Converts total seconds to H:MM:SS string format."""
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours}:{minutes:02}:{seconds:02}"


# ── Flask & global state ────────────────────────────────────────────────
app = Flask(__name__)

connected = connecting = connection_failed = False
# Protects _start_ble_thread()'s connected/connecting check-then-set: without
# it, two near-simultaneous callers (e.g. a double /reconnect) can both pass
# the guard before either sets connecting=True, spawning two overlapping
# _ble_thread() invocations that silently clobber ble_loop/controller/
# _ble_command_lock out from under each other's in-flight coroutines.
_start_ble_thread_lock = threading.Lock()
ble_loop: asyncio.AbstractEventLoop | None = None
controller: Controller | None = None
_device_ble_address: str | None = None
_resume_grace_deadline = 0
speed_history = deque(maxlen=15)
_stats_monitor_task: asyncio.Task | None = None  # Track the stats monitor task
_idle_watchdog_task: asyncio.Task | None = None  # Track the paused/idle connection watchdog
_belt_sequence_task: asyncio.Task | None = None  # Track an in-flight start/resume/pause belt sequence
_belt_transitioning = False  # True while a belt sequence is in flight; exposed in /stats for UI
_history_lock = threading.Lock()  # Protect session_history.json reads/writes
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session_history.json")
_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

_session_state_file_lock = threading.Lock()  # Protect session_state.json reads/writes
SESSION_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session_state.json")
_SESSION_STATE_SAVE_INTERVAL_SECONDS = 5
# ask_stats() only sends a request. The ph4_walkingpad library never returns the reply
# synchronously; the real data always lands via the on_cur_status_received notification
# callback (also routed through process_status_packet), independent of ask_stats(). So
# "is the connection alive" is judged by recency of ANY successful process_status_packet()
# call, not by ask_stats()'s own return value/exceptions.
_STALE_STATUS_TIMEOUT_SECONDS = 15
_last_status_update_monotonic = 0.0
# _stats_monitor()'s staleness check only runs while belt_running, so it's blind
# during a paused session or idle connection. The device only emits a status
# notification in response to an explicit ask_stats() request -- it does not
# push anything on its own -- so nothing else keeps _last_status_update_monotonic
# fresh during those periods. _idle_connection_watchdog() pings periodically
# whenever connected but no active stats monitor is running, purely to catch a
# dead link before the user notices only when they try to Resume.
_IDLE_WATCHDOG_PING_INTERVAL_SECONDS = 10
# _stats_monitor() pairs the 15s threshold above with a 1s poll interval, so a
# genuinely dead connection still gets ~15 chances to respond before it trips
# -- one missed/delayed reply barely matters. The idle watchdog's 10s interval
# would give the SAME 15s threshold only one chance, so a single slow reply
# would force-disconnect an otherwise healthy paused session. Detection speed
# matters far less while idle/paused (nobody's watching live stats), so this
# gets its own, much more forgiving threshold instead of sharing the active
# one -- roughly 4-5 missed pings before disconnecting.
_IDLE_STALE_STATUS_TIMEOUT_SECONDS = 45
# Serializes every controller read/write -- the idle watchdog's ask_stats()
# ping, belt sequences' start_belt()/stop_belt()/change_speed() commands, the
# active stats monitor's ask_stats() poll, and the manual speed-adjustment
# routes' change_speed() calls -- since each of these runs as a separate
# coroutine/task on the same ble_loop and would otherwise be free to
# interleave mid-write/read on the same BLE link. Cheaper and more robust
# than cancelling/recreating the watchdog task around every belt sequence
# (which only runs once, at connect time, and has no restart path once
# cancelled).
#
# Recreated fresh in _ble_thread() for every connection attempt, NOT created
# once here at import time: asyncio.Lock() permanently binds to whichever
# event loop first contends it, and _ble_thread() creates a brand-new event
# loop on every connection/reconnect. Reusing one Lock instance across a
# reconnect's new loop raises "RuntimeError: <Lock> is bound to a different
# event loop" the next time it's actually contended.
_ble_command_lock: asyncio.Lock | None = None
# Every controller.*() call in this file is bounded by one of the two
# timeouts below -- there is no unbounded await on the BLE link anywhere.
# That's what actually guarantees _ble_command_lock can never be held
# forever: two of its consumers (_locked_change_speed(), _end_belt_sequence())
# are fire-and-forget coroutines with no task variable anything else could
# cancel, so a stuck GATT operation in either would otherwise wedge the lock
# permanently with no recovery path. Values are generous above a healthy
# device's normal response time (a handful of 0.5s sleeps between steps),
# not tuned for snappiness.
_BLE_READ_TIMEOUT_SECONDS = 2  # ask_stats() -- a request/reply round trip
_BLE_WRITE_TIMEOUT_SECONDS = 5  # everything else: connect, mode/speed/belt commands
# Bounds how long the idle watchdog / stats monitor will wait to *acquire*
# _ble_command_lock (see _run_locked()). Belt-and-suspenders on top of the
# write-side timeouts above: even though no write can hang forever, one can
# still legitimately take up to _BLE_WRITE_TIMEOUT_SECONDS, and without this
# bound a caller waiting on the lock during that window would stall its own
# staleness check too. Same order of magnitude as a write timeout. Giving up
# here is cheap for these two callers -- they just skip one cycle and retry.
_BLE_COMMAND_LOCK_TIMEOUT_SECONDS = 5
# _run_locked()'s acquisition bound for everything that ISN'T a background
# read loop: belt sequences, speed changes, graceful shutdown. Deliberately
# much larger than _BLE_COMMAND_LOCK_TIMEOUT_SECONDS above -- giving up here
# is NOT cheap (it means declaring the connection dead via _handle_disconnect()
# and disrupting the user's session), so it must not fire just because some
# OTHER legitimate sequence is still using the link. Sized comfortably above
# the worst case any single sequence can take: Resume's light-wake probe
# (6.0s) falling back to the full wake sequence (3 x 5.5s = 16.5s) plus a
# final speed-set (5.5s) is ~28s in the worst case if every step lands near
# its own timeout.
_BLE_SEQUENCE_LOCK_TIMEOUT_SECONDS = 40
# Light-wake probe (Resume only, see _try_light_wake()): after a bare
# start_belt() with no mode toggle, poll ask_stats() up to this many times,
# this far apart, checking for a status update confirming nonzero speed
# before giving up and falling back to the full STANDBY/MANUAL toggle.
#
# These values do NOT need to stay under RESUME_GRACE_PERIOD_SECONDS --
# that was true before _belt_transitioning existed, but auto-pause is now
# suppressed for the entire span of any belt sequence regardless of how
# long it runs (see process_status_packet()'s AUTO-PAUSE LOGIC comment), so
# a long probe carries no false-auto-pause risk. Sized instead from real
# on-device measurement: one logged trial showed the device's own `speed`
# field lagging ~4.1s behind an accepted start_belt() (its `state` field
# reacted within ~100ms, `speed` did not catch up until several status
# packets later) -- the previous 3-attempt/0.5s-interval/1s-timeout probe
# (6.0s worst case) gave up at 3.5s, calling it unconfirmed 0.6s before the
# next reply would have confirmed it, and the STANDBY-first fallback then
# stopped a belt that had already started moving before restarting it.
# Retuned with real margin over that one data point rather than just
# raising the ceiling slightly -- if these prove excessively generous
# once more trials come in, they can be tightened later; erring long here
# only costs a longer spinner, where erring short costs a visible stop/
# restart. Also widened the interval to 1.0s to better match the ~1s
# status-reporting cadence observed in that log (0.5s meant some attempts
# were checking before new data could plausibly have arrived).
_LIGHT_WAKE_PROBE_ATTEMPTS = 6
_LIGHT_WAKE_PROBE_INTERVAL_SECONDS = 1.0
_LIGHT_WAKE_PROBE_TIMEOUT_SECONDS = 1.5
_pending_restore: dict | None = None  # Loaded at startup; cleared once restored or discarded

session_active = belt_running = False
_session_start_time: datetime | None = None
_session_state_lock = threading.Lock()  # Protect session_active/belt_running check-then-set transitions
_shutting_down = False
_server_stopping = False  # Flag for UI to detect Ctrl+C / signal shutdown
_shutting_down_lock = threading.Lock()  # Protect shutdown state mutations
resume_speed_kmh = 2.0  # default if none yet

current_speed_kmh = current_distance_km = 0.0
current_steps = 0
current_calories = 0.0
current_session_active_seconds = 0

_last_dev_dist = _last_dev_steps = 0

# ── SSE broadcaster state ────────────────────────────────────────────────
_sse_subscribers: list[queue.Queue] = []
_sse_subscribers_lock = threading.Lock()  # Protect _sse_subscribers list mutations, mirrors _history_lock convention
_SSE_BROADCAST_INTERVAL_SECONDS = 1
# Bounds how long a subscriber's generator can block on q.get() with nothing
# to send. Well above the normal ~1s broadcast cadence, so this only ever
# fires if a tick is genuinely missed -- at which point sending a comment
# line forces a real write attempt on the socket, surfacing a dead/blackholed
# connection sooner than blocking indefinitely would.
_SSE_KEEPALIVE_TIMEOUT_SECONDS = 20


# ── Session History Persistence ────────────────────────────────────────

def _build_session_record() -> dict:
    """Build a session record dict from current global state."""
    start = _session_start_time
    end = datetime.now()
    distance_mi = current_distance_km * KM_TO_MI
    duration = max(current_session_active_seconds, 1)  # avoid div-by-zero
    avg_speed_kmh = current_distance_km / (duration / 3600.0)
    avg_speed_mph = avg_speed_kmh * KM_TO_MI

    return {
        "date": start.strftime("%Y-%m-%d"),
        "start_time": start.strftime("%H:%M:%S"),
        "end_time": end.strftime("%H:%M:%S"),
        "duration_seconds": current_session_active_seconds,
        "distance_km": round(current_distance_km, 3),
        "distance_mi": round(distance_mi, 3),
        "steps": current_steps,
        "calories": round(current_calories),
        "avg_speed_kmh": round(avg_speed_kmh, 1),
        "avg_speed_mph": round(avg_speed_mph, 1),
        "health_logged": False,
    }


def _save_session():
    """Append the current session to session_history.json (thread-safe, atomic)."""
    if not session_active:
        return

    record = _build_session_record()

    with _history_lock:
        history = []
        try:
            if os.path.exists(HISTORY_FILE):
                with open(HISTORY_FILE, "r") as f:
                    history = json.load(f)
                if not isinstance(history, list):
                    history = []
        except (json.JSONDecodeError, IOError) as exc:
            logging.warning(f"Failed to read {HISTORY_FILE}, starting fresh: {exc}")
            history = []

        history.append(record)

        # Atomic (temp file + rename), matching _save_session_state(): a
        # crash mid-write must never leave session_history.json truncated or
        # otherwise corrupted -- that history is otherwise unrecoverable.
        tmp_path = f"{HISTORY_FILE}.tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(history, f, indent=2)
            os.replace(tmp_path, HISTORY_FILE)
            logging.info(f"Session saved to {HISTORY_FILE} ({len(history)} total sessions)")
        except IOError as exc:
            logging.error(f"Failed to write session history: {exc}")


def _load_session_history(limit: int | None = None) -> list:
    """Load session history from disk, most recent first (thread-safe). Pass limit=None for all."""
    with _history_lock:
        if not os.path.exists(HISTORY_FILE):
            return []
        try:
            with open(HISTORY_FILE, "r") as f:
                history = json.load(f)
            if not isinstance(history, list):
                return []
            result = list(reversed(history))
            return result[:limit] if limit is not None else result
        except (json.JSONDecodeError, IOError) as exc:
            logging.warning(f"Failed to read session history: {exc}")
            return []


def _clear_session_history():
    """Delete or truncate the session history file (thread-safe, atomic)."""
    with _history_lock:
        tmp_path = f"{HISTORY_FILE}.tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump([], f)
            os.replace(tmp_path, HISTORY_FILE)
            logging.info("Session history cleared")
        except IOError as exc:
            logging.error(f"Failed to clear session history: {exc}")


def _dismiss_health_export():
    """Mark the most recent session as no longer pending an Apple Health export
    (thread-safe, atomic). session_history.json stores oldest-first, so the most
    recent record is the last element -- not history[0], which is only true of
    _load_session_history()'s reversed-for-display copy."""
    with _history_lock:
        if not os.path.exists(HISTORY_FILE):
            return
        try:
            with open(HISTORY_FILE, "r") as f:
                history = json.load(f)
            if not isinstance(history, list) or not history:
                return
            history[-1]["health_logged"] = True
            tmp_path = f"{HISTORY_FILE}.tmp"
            with open(tmp_path, "w") as f:
                json.dump(history, f, indent=2)
            os.replace(tmp_path, HISTORY_FILE)
        except (json.JSONDecodeError, IOError) as exc:
            logging.warning(f"Failed to dismiss health export flag: {exc}")


# ── Session State Persistence (crash/restart recovery) ──────────────────

def _build_session_state_snapshot() -> dict:
    """Build a dict of the in-progress session's live state for crash recovery."""
    return {
        "session_active": session_active,
        "session_start_time": _session_start_time.isoformat() if _session_start_time else None,
        "current_distance_km": current_distance_km,
        "current_steps": current_steps,
        "current_calories": current_calories,
        "current_session_active_seconds": current_session_active_seconds,
        "resume_speed_kmh": resume_speed_kmh,
        "last_dev_dist": _last_dev_dist,
        "last_dev_steps": _last_dev_steps,
    }


def _save_session_state():
    """Write the current in-progress session to session_state.json (thread-safe, atomic)."""
    if not session_active:
        return
    snapshot = _build_session_state_snapshot()
    with _session_state_file_lock:
        tmp_path = f"{SESSION_STATE_FILE}.tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(snapshot, f, indent=2)
            os.replace(tmp_path, SESSION_STATE_FILE)
        except IOError as exc:
            logging.error(f"Failed to write session state: {exc}")


def _load_session_state() -> dict | None:
    """Load a saved in-progress session, if any (thread-safe). Returns None if absent/invalid."""
    with _session_state_file_lock:
        if not os.path.exists(SESSION_STATE_FILE):
            return None
        try:
            with open(SESSION_STATE_FILE, "r") as f:
                state = json.load(f)
            if not isinstance(state, dict) or not state.get("session_active"):
                return None
            return state
        except (json.JSONDecodeError, IOError) as exc:
            logging.warning(f"Failed to read session state, ignoring: {exc}")
            return None


def _clear_session_state():
    """Remove session_state.json once a session ends cleanly or is resolved (thread-safe)."""
    with _session_state_file_lock:
        try:
            if os.path.exists(SESSION_STATE_FILE):
                os.remove(SESSION_STATE_FILE)
        except IOError as exc:
            logging.error(f"Failed to clear session state: {exc}")


def _write_config(updates: dict) -> None:
    """Merge updates into config.json, creating the file if it doesn't exist."""
    existing = {}
    if os.path.isfile(_CONFIG_FILE):
        with open(_CONFIG_FILE) as f:
            existing = json.load(f)
    existing.update(updates)
    with open(_CONFIG_FILE, "w") as f:
        json.dump(existing, f, indent=2)


_pending_restore = _load_session_state()  # Check for an interrupted session at startup


# ── Context processor so templates always know flags ────────────────────
@app.context_processor
def inject_flags():
    return dict(
        connected=connected, connecting=connecting, connection_failed=connection_failed,
        apple_health_shortcut_name=APPLE_HEALTH_SHORTCUT_NAME,
    )


# ── BLE helpers ─────────────────────────────────────────────────────────
async def _scan_for_device(timeout: int = 10):
    try:
        async with BleakScanner() as scanner:
            await asyncio.sleep(timeout)
            devices = scanner.discovered_devices

            # First try to find by known address
            if _device_ble_address:
                for dev in devices:
                    if dev.address == _device_ble_address:
                        logging.info(f"Found device by known address: {_device_ble_address}")
                        return dev

            # Then try to find by name
            for dev in devices:
                if dev.name and BLE_DEVICE_NAME in dev.name:
                    logging.info(f"Found {BLE_DEVICE_NAME} device: {dev.name} ({dev.address})")
                    return dev

            logging.debug(f"Discovered {len(devices)} devices, none matched {BLE_DEVICE_NAME}")
            return None
    except Exception as exc:
        logging.warning(f"Scanner error: {exc}")
        return None


async def _connect_to_pad() -> bool:
    global controller, _device_ble_address, _idle_watchdog_task, _last_status_update_monotonic
    dev = None
    max_retries = 3
    retry_count = 0

    while retry_count < max_retries and not dev:
        if retry_count > 0:
            wait_time = min(2 ** retry_count, 10)
            logging.info(f"Retry {retry_count}/{max_retries} in {wait_time}s...")
            await asyncio.sleep(wait_time)

        if _device_ble_address:
            logging.info(f"Scanning for known device: {_device_ble_address}")
        else:
            logging.info(f"Scanning for device by name '{BLE_DEVICE_NAME}'...")

        dev = await _scan_for_device(timeout=10)

        if not dev:
            retry_count += 1
            if retry_count < max_retries:
                logging.warning(f"Device not found, retrying... ({retry_count}/{max_retries})")

    if not dev:
        logging.error(f"Could not find {BLE_DEVICE_NAME} after retries. Ensure it is on and in range.")
        _device_ble_address = None
        return False

    _device_ble_address = dev.address
    logging.info(f"Device found! Address: {_device_ble_address}")

    controller = Controller()
    await asyncio.wait_for(controller.run(dev.address), timeout=_BLE_WRITE_TIMEOUT_SECONDS)

    # Try to set disconnect callback (API varies by Bleak version)
    if hasattr(controller, "client") and controller.client:
        if hasattr(controller.client, "set_disconn_callback"):
            # Newer Bleak API
            try:
                controller.client.set_disconn_callback(_handle_disconnect)
            except Exception as exc:
                logging.warning(f"Could not set disconnect callback: {exc}")
        elif hasattr(controller.client, "set_disconnected_callback"):
            # Older Bleak API
            try:
                controller.client.set_disconnected_callback(_handle_disconnect)
            except Exception as exc:
                logging.warning(f"Could not set disconnect callback: {exc}")
        else:
            logging.debug("Disconnect callback not available in this Bleak version")

    await asyncio.wait_for(controller.switch_mode(WalkingPad.MODE_MANUAL), timeout=_BLE_WRITE_TIMEOUT_SECONDS)

    def _handle_status_update(_sender, status):
        try:
            dist, steps, speed = _extract_status_fields(status)
            process_status_packet(dist, steps, speed)
            logging.debug(f"Push d={dist} s={steps} v={speed}")
        except Exception as exc:
            logging.warning(f"_handle_status_update error: {exc}")

    controller.on_cur_status_received = _handle_status_update

    if hasattr(controller, "enable_notifications"):
        try:
            await asyncio.wait_for(controller.enable_notifications(), timeout=_BLE_WRITE_TIMEOUT_SECONDS)
        except Exception as exc:
            logging.warning(f"enable_notifications failed: {exc}")

    _last_status_update_monotonic = time.monotonic()  # fresh baseline for this connection
    _idle_watchdog_task = asyncio.create_task(_idle_connection_watchdog())
    return True


def _extract_status_fields(status) -> tuple:
    """Extract (distance, steps, speed) from a status dict or object."""
    if isinstance(status, dict):
        return status.get("dist", 0), status.get("steps", 0), status.get("speed", 0)
    return getattr(status, "dist", 0), getattr(status, "steps", 0), getattr(status, "speed", 0)


def process_status_packet(dev_dist: float, dev_steps: int, dev_speed: float):
    """Update cumulative stats from raw values AND handle auto-pause.

    Called from both the active poll (_stats_monitor -> ask_stats) and the
    passive BLE notification callback (_handle_status_update), either path
    landing here means the connection is alive.
    """
    global belt_running, resume_speed_kmh, _resume_grace_deadline
    global current_speed_kmh, current_distance_km, current_steps, current_calories
    global _last_dev_dist, _last_dev_steps, _last_status_update_monotonic

    new_reported_speed_kmh = dev_speed / 10.0
    just_auto_paused = False

    # Continuously populate the speed history with stable, non-zero speeds.
    if belt_running and new_reported_speed_kmh > MIN_SPEED_KMH:
        speed_history.append(new_reported_speed_kmh)

    # AUTO-PAUSE LOGIC
    # _resume_grace_deadline alone isn't a reliable bound on a belt
    # sequence's duration: _full_wake_sequence()/_try_light_wake() are each
    # built from asyncio.wait_for()-bounded steps, so a genuinely degraded
    # connection can legitimately take longer than RESUME_GRACE_PERIOD_SECONDS
    # to resolve without ever raising (each step just runs close to its own
    # timeout). _belt_transitioning is True for the entire span of any belt
    # sequence regardless of how long it takes, so ANDing it in here only
    # ever narrows when auto-pause can fire relative to before -- it can't
    # weaken real detection, since normal walking (the only time this needs
    # to catch someone actually stepping off) always has
    # _belt_transitioning=False.
    if time.time() > _resume_grace_deadline and not _belt_transitioning:
        if belt_running and new_reported_speed_kmh == 0 and current_speed_kmh > 0:
            logging.info("Belt has stopped unexpectedly. Auto-pausing session.")

            # Use the OLDEST speed from history to ignore the deceleration phase.
            if speed_history:
                resume_speed_kmh = speed_history[0] # Use the first (oldest) item
            else:
                # Fallback if pause happens too quickly after starting
                resume_speed_kmh = MIN_SPEED_KMH

            belt_running = False
            just_auto_paused = True

    # Cumulative stats accumulation
    if dev_dist < _last_dev_dist:
        _last_dev_dist = 0
    current_distance_km += (dev_dist - _last_dev_dist) / 100.0
    _last_dev_dist = dev_dist

    if dev_steps < _last_dev_steps:
        _last_dev_steps = 0
    current_steps += dev_steps - _last_dev_steps
    _last_dev_steps = dev_steps

    current_speed_kmh = new_reported_speed_kmh
    current_calories = kcal_estimate(current_distance_km * KM_TO_MI)

    # Stamped last, after all accumulation above has succeeded, so a
    # mid-function exception (e.g. malformed packet data) can't mark the
    # connection falsely "alive" and mask a real failure from the staleness
    # watchdog in _stats_monitor().
    _last_status_update_monotonic = time.monotonic()

    # Persist immediately on auto-pause, same guarantee as the manual /pause route.
    if just_auto_paused:
        _save_session_state()


async def _graceful_shutdown():
    """Safely stop the treadmill, cancel monitors, and disconnect BLE before exit."""
    global connected, belt_running, session_active
    try:
        # Step 0.5: Save in-progress session to history before cleanup
        if session_active:
            _save_session()
            _clear_session_state()

        # Step 1: Stop belt if running
        if belt_running and controller:
            logging.info("Stopping belt for graceful shutdown...")
            # Deliberately NOT _BLE_SEQUENCE_LOCK_TIMEOUT_SECONDS here, despite
            # this being a belt-affecting call like the others that use it:
            # every caller of _graceful_shutdown() (signal handler, /shutdown,
            # atexit) wraps it in its own external timeout (5-10s) and treats
            # a timeout as "log and continue exiting" rather than "wait it
            # out" -- the process force-exits shortly after regardless. Using
            # the long sequence timeout here would just mean losing gracefully
            # to the *external* timeout instead, without actually getting more
            # cleanup done. _run_locked()'s cheap-to-give-up default fits this
            # best-effort context, same as the background read loops.
            await _run_locked(
                lambda: asyncio.wait_for(controller.stop_belt(), timeout=_BLE_WRITE_TIMEOUT_SECONDS)
            )
            belt_running = False
            await asyncio.sleep(0.5)

        # Step 2: Cancel stats monitor and idle watchdog tasks
        logging.info("Cancelling stats monitor for shutdown")
        await _cancel_task(_stats_monitor_task)
        await _cancel_task(_idle_watchdog_task)

        # Step 3: Switch device to standby mode
        if controller:
            logging.info("Switching device to standby mode...")
            # See step 1's comment -- same reasoning applies here.
            await _run_locked(
                lambda: asyncio.wait_for(controller.switch_mode(WalkingPad.MODE_STANDBY), timeout=_BLE_WRITE_TIMEOUT_SECONDS)
            )
            await asyncio.sleep(0.5)

        # Step 4: Disconnect BLE client gracefully
        if controller and hasattr(controller, 'client') and controller.client:
            logging.info("Disconnecting BLE client...")
            try:
                await asyncio.wait_for(controller.client.disconnect(), timeout=_BLE_WRITE_TIMEOUT_SECONDS)
            except Exception as exc:
                logging.warning(f"BLE disconnect error (non-fatal): {exc}")
    except Exception as exc:
        logging.error(f"Graceful shutdown error (continuing exit): {exc}")
    finally:
        connected = False
        session_active = False
        belt_running = False
        logging.info("Device cleanup complete")


def _check_staleness_and_disconnect(context: str, threshold: float = _STALE_STATUS_TIMEOUT_SECONDS) -> bool:
    """Shared by _stats_monitor() and _idle_connection_watchdog(): compare
    _last_status_update_monotonic against a staleness threshold (defaulting
    to _STALE_STATUS_TIMEOUT_SECONDS; the idle watchdog passes its own, wider
    one), and if stale, log and disconnect. Returns True if it disconnected
    (the caller should stop its loop), False otherwise.
    """
    if time.monotonic() - _last_status_update_monotonic <= threshold:
        return False
    logging.error(
        f"No status update in over {threshold}s {context}; "
        "treating connection as dead."
    )
    _handle_disconnect(None)
    return True


async def _run_locked(make_coro, timeout: float = _BLE_COMMAND_LOCK_TIMEOUT_SECONDS):
    """Call `make_coro()` and await the result under _ble_command_lock, with
    a bounded acquisition.

    The sole way anything in this file touches _ble_command_lock -- every
    controller.*() call site uses this, not a bare `async with`. That's only
    safe to do uniformly because every controller.*() call is itself
    individually timeout-bounded (see _BLE_READ_TIMEOUT_SECONDS/
    _BLE_WRITE_TIMEOUT_SECONDS's module comment): no lock holder can hold it
    forever, so bounding acquisition here never risks giving up on a
    genuinely-still-working connection, only ever on one that's actually
    stuck. `timeout` defaults to _BLE_COMMAND_LOCK_TIMEOUT_SECONDS for the
    background read loops (_stats_monitor(), _idle_connection_watchdog())
    where giving up is cheap -- skip this cycle, retry next one. Belt
    sequences and speed changes pass _BLE_SEQUENCE_LOCK_TIMEOUT_SECONDS
    instead: for them, giving up means declaring the connection dead and
    disrupting the user's session, so it must not fire just because some
    other legitimate sequence is still using the link.

    Takes a zero-arg factory rather than an already-built coroutine: a bare
    coroutine argument (e.g. controller.ask_stats()) is constructed the
    moment the caller writes the call, before _run_locked ever runs -- so if
    lock acquisition times out, that coroutine was created but never
    awaited, which logs a "coroutine was never awaited" RuntimeWarning right
    when the lock is already contended and clean diagnostic signal matters
    most. Deferring construction to make_coro() means nothing is created
    until the lock is actually held. For a multi-statement body, define a
    local `async def` and pass the function itself (not a call to it) --
    it's already a valid zero-arg coroutine factory.

    Snapshots the lock into a local before acquiring, and releases that same
    local afterward -- never re-reads the bare global at release time.
    _ble_command_lock is reassigned to a fresh Lock() on every reconnect (see
    its module-level comment); a caller suspended here across a reconnect
    must release the lock it actually acquired, not whatever the global has
    since moved on to (which could belong to an unrelated, active
    connection).
    """
    lock = _ble_command_lock
    acquired = False
    try:
        await asyncio.wait_for(lock.acquire(), timeout=timeout)
        acquired = True
        return await make_coro()
    finally:
        if acquired:
            lock.release()


async def _idle_connection_watchdog():
    """Keep _last_status_update_monotonic fresh whenever _stats_monitor() isn't
    running (paused session or idle/no session), so a dead BLE link is caught
    within a bounded time instead of only surfacing the next time the user
    tries to Resume.

    Runs for the lifetime of a single connection (started once per successful
    connect in _connect_to_pad(), cancelled on disconnect/shutdown).
    """
    while True:
        await asyncio.sleep(_IDLE_WATCHDOG_PING_INTERVAL_SECONDS)
        if not connected:
            break  # disconnected via another path; nothing left for this task to do

        if belt_running:
            continue  # _stats_monitor() owns liveness now; keep waiting for it to finish

        # Cheap, no-wire-traffic check first: bleak caches the transport's own
        # connection state, so if it already knows the link is dead we don't
        # need to wait for the staleness timeout to catch up, and we skip a
        # real ask_stats() write into a connection that's already gone.
        try:
            if hasattr(controller, "client") and controller.client and not controller.client.is_connected:
                logging.error("BLE client reports disconnected while idle/paused; treating connection as dead.")
                _handle_disconnect(None)
                break
        except Exception as exc:
            logging.debug(f"Idle watchdog is_connected check error (falling back to ping): {exc}")

        if _check_staleness_and_disconnect("while idle/paused", threshold=_IDLE_STALE_STATUS_TIMEOUT_SECONDS):
            break

        # Bound acquisition, not just the ping: a stuck belt sequence holding
        # the lock must not prevent this loop from reaching its next
        # iteration, where the staleness check above (which doesn't need the
        # lock) can still run and eventually catch a truly dead link.
        try:
            await _run_locked(lambda: asyncio.wait_for(controller.ask_stats(), timeout=_BLE_READ_TIMEOUT_SECONDS))
        except Exception as exc:
            logging.debug(f"Idle watchdog ping error (will retry next cycle): {exc}")


async def _stats_monitor():
    """Active monitor: explicitly request a status packet every second.

    ask_stats() only sends the request. The reply (if any) arrives via a
    separate BLE notification handled by _handle_status_update, so a falsy/
    empty return here is normal on some devices and is NOT a failure signal.
    Connection health is judged instead by _last_status_update_monotonic,
    which process_status_packet() stamps on every successful update from
    either path (active poll or passive notification).
    """
    global current_session_active_seconds, _last_status_update_monotonic
    logging.info("Stats monitor started")

    _base_seconds = current_session_active_seconds
    _monitor_start = time.monotonic()
    _ticks_since_save = 0
    _last_status_update_monotonic = time.monotonic()  # fresh grace period for this session

    try:
        while belt_running:
            current_session_active_seconds = _base_seconds + int(time.monotonic() - _monitor_start)

            try:
                status = await _run_locked(lambda: asyncio.wait_for(controller.ask_stats(), timeout=_BLE_READ_TIMEOUT_SECONDS))
                if status:
                    dist, steps, speed = _extract_status_fields(status)
                    process_status_packet(dist, steps, speed)
                    logging.debug(f"Poll {status}")
                else:
                    logging.debug("ask_stats returned no reply (expected on this device/library)")
            except asyncio.TimeoutError:
                logging.warning("Status poll timed out (device unresponsive or BLE command lock busy)")
            except Exception as exc:
                logging.warning(f"ask_stats error: {exc}")

            if _check_staleness_and_disconnect("(active or passive)"):
                break

            _ticks_since_save += 1
            if _ticks_since_save >= _SESSION_STATE_SAVE_INTERVAL_SECONDS:
                _ticks_since_save = 0
                _save_session_state()

            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                logging.info("Stats monitor cancelled")
                break
    except Exception as exc:
        logging.error(f"Stats monitor error: {exc}")
    finally:
        logging.info("Stats monitor stopped")


async def _cancel_task(task: asyncio.Task | None) -> None:
    """Cancel a task if it's still running and wait for it to fully unwind.

    Shared by every module-level task this app tracks (stats monitor, idle
    watchdog, in-flight belt sequence) -- callers pass their own task
    variable; this never reassigns it, matching the previous per-task
    helpers' behavior (the global gets overwritten next time a new task is
    created, same as before).
    """
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _full_wake_sequence():
    """The always-safe STANDBY→MANUAL→start_belt() toggle (commit 83724a7):
    a plain start_belt() alone silently no-ops (no exception) if the device
    has gone to sleep. Caller must already hold _ble_command_lock.
    """
    await asyncio.wait_for(controller.switch_mode(WalkingPad.MODE_STANDBY), timeout=_BLE_WRITE_TIMEOUT_SECONDS)
    await asyncio.sleep(0.5)
    await asyncio.wait_for(controller.switch_mode(WalkingPad.MODE_MANUAL), timeout=_BLE_WRITE_TIMEOUT_SECONDS)
    await asyncio.sleep(0.5)
    await asyncio.wait_for(controller.start_belt(), timeout=_BLE_WRITE_TIMEOUT_SECONDS)
    await asyncio.sleep(0.5)


async def _try_light_wake() -> bool:
    """Bare start_belt() -- no mode toggle -- then poll ask_stats() looking
    for a status update confirming nonzero speed. Returns True if confirmed
    within the probe window, False otherwise. Caller must already hold
    _ble_command_lock.

    switch_mode(STANDBY) is what resets the WalkingPad's own onboard
    display/session counters; this lets Resume skip that toggle whenever
    the device is still awake, which is the common case shortly after our
    own stop_belt() (Pause).

    Checks freshness via _last_status_update_monotonic against a baseline
    taken before start_belt() is even sent, not just current_speed_kmh > 0
    alone: without that, a stale nonzero current_speed_kmh left over from
    before Pause's stop_belt() reply landed could false-positive the probe.
    """
    baseline = time.monotonic()
    try:
        await asyncio.wait_for(controller.start_belt(), timeout=_LIGHT_WAKE_PROBE_TIMEOUT_SECONDS)
    except Exception as exc:
        # Deliberately not fatal, matching each probe attempt below: even if
        # this write itself failed/timed out, the polling loop is still the
        # correct way to find out whether the belt is moving -- if it never
        # confirms, the caller falls back to _full_wake_sequence() same as
        # any other unconfirmed probe. Letting this raise uncaught would
        # abort the whole resume sequence and skip that fallback entirely.
        logging.debug(f"Light wake start_belt() error (still probing): {exc}")
    await asyncio.sleep(0.5)

    for attempt in range(1, _LIGHT_WAKE_PROBE_ATTEMPTS + 1):
        try:
            await asyncio.wait_for(controller.ask_stats(), timeout=_LIGHT_WAKE_PROBE_TIMEOUT_SECONDS)
        except Exception as exc:
            logging.debug(f"Light wake probe {attempt}/{_LIGHT_WAKE_PROBE_ATTEMPTS} ask_stats error: {exc}")

        await asyncio.sleep(_LIGHT_WAKE_PROBE_INTERVAL_SECONDS)

        if _last_status_update_monotonic > baseline and current_speed_kmh > 0:
            logging.info(
                f"Light wake confirmed belt moving on probe {attempt}/{_LIGHT_WAKE_PROBE_ATTEMPTS} "
                f"({time.monotonic() - baseline:.1f}s) -- skipped STANDBY/MANUAL mode toggle."
            )
            return True

    logging.warning(
        f"Light wake unconfirmed after {_LIGHT_WAKE_PROBE_ATTEMPTS} probes "
        f"({time.monotonic() - baseline:.1f}s); falling back to full STANDBY/MANUAL wake sequence."
    )
    return False


async def _wake_and_start_belt(target_speed_kmh: float | None = None, try_light_wake: bool = False):
    """Get the belt moving, optionally at a specific speed.

    try_light_wake=True (Resume only) first attempts _try_light_wake().
    Falls back to _full_wake_sequence() -- identical to the
    try_light_wake=False path -- if that can't be confirmed, so reliability
    is never worse than before this change, only sometimes gentler on the
    WalkingPad's own display.
    """
    async def _body():
        if not (try_light_wake and await _try_light_wake()):
            await _full_wake_sequence()

        if target_speed_kmh is not None:
            logging.info(f"Setting speed to {target_speed_kmh:.1f} km/h.")
            await asyncio.wait_for(controller.change_speed(int(target_speed_kmh * 10)), timeout=_BLE_WRITE_TIMEOUT_SECONDS)
            await asyncio.sleep(0.5)

    await _run_locked(_body, timeout=_BLE_SEQUENCE_LOCK_TIMEOUT_SECONDS)


async def _locked_change_speed(dev_speed: int):
    """change_speed(), serialized via _ble_command_lock so a manual speed
    adjustment can't interleave mid-write with the active stats monitor's
    concurrent ask_stats() poll on the same ble_loop.

    Fire-and-forget from the caller's side (scheduled via
    run_coroutine_threadsafe() with the returned Future discarded, not
    tracked in any task variable) -- so unlike the belt sequences, nothing
    else will ever observe or retry a failure here. Handle it the same way
    _start_belt_sequence()/_resume_belt_sequence() treat any failed BLE
    write: log it and declare the connection dead rather than leaving it
    silently swallowed.
    """
    try:
        await _run_locked(
            lambda: asyncio.wait_for(controller.change_speed(dev_speed), timeout=_BLE_WRITE_TIMEOUT_SECONDS),
            timeout=_BLE_SEQUENCE_LOCK_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logging.error(f"change_speed failed: {exc}")
        _handle_disconnect(None)


def _ble_thread():
    global connected, connecting, connection_failed, ble_loop, _ble_command_lock

    # Create new event loop for BLE thread (works on Linux, MacOS, and Windows)
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    except RuntimeError as e:
        logging.error(f"Failed to create event loop: {e}")
        connecting = False
        connection_failed = True
        return

    ble_loop = loop
    # Fresh lock per connection attempt -- see the module-level comment on
    # _ble_command_lock for why this can't just be created once at import time.
    _ble_command_lock = asyncio.Lock()

    try:
        try:
            connected_result = loop.run_until_complete(_connect_to_pad())
        except Exception as exc:
            # Any failure during connect must still fall through to the
            # connection_failed path below -- a BLE transport error, our own
            # _BLE_WRITE_TIMEOUT_SECONDS timeout on a stuck GATT call, or the
            # loop being stopped out from under us by a shutdown mid-scan
            # (RuntimeError) all need the same handling. Without this,
            # `connecting` never resets and _start_ble_thread()'s guard
            # permanently blocks every future reconnect attempt.
            logging.warning(f"BLE connection attempt failed: {exc}")
            connected_result = False

        if not connected_result:
            connecting = False
            connection_failed = True
            return

        connected = True
        connecting = False
        logging.info("BLE connection established, starting event loop")

        try:
            loop.run_forever()
        except KeyboardInterrupt:
            logging.info("BLE thread interrupted")
        except Exception as e:
            logging.error(f"Event loop error: {e}")
    finally:
        # Only clear the global if this thread's loop is still the current
        # one: if a newer connection attempt has already superseded this one
        # (ble_loop reassigned), this thread's cleanup is for a stale
        # connection and must not clobber the newer one's state. In practice
        # this thread's stop-to-cleanup is near-instant (milliseconds) versus
        # a new connection's multi-second BLE scan, so the ordering this
        # guards against is unlikely, but cheap enough to close outright.
        if ble_loop is loop:
            connected = False
        logging.info("Closing BLE event loop")
        try:
            # Cancel all remaining tasks
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception as e:
            logging.debug(f"Error canceling tasks: {e}")
        finally:
            loop.close()


def _start_ble_thread():
    global connecting, connection_failed
    with _start_ble_thread_lock:
        if connected or connecting:
            return
        connecting = True
        connection_failed = False
    # Stop the previous connection's event loop, if any, before starting a
    # new one. Nothing else ever does this on a plain disconnect (only a full
    # app shutdown stops a loop) -- without it, every reconnect leaves the
    # prior _ble_thread() idling in run_forever() forever, since it has
    # nothing left to do but nothing ever tells it to stop: one leaked daemon
    # thread per reconnect for the life of the process.
    if ble_loop and not ble_loop.is_closed() and ble_loop.is_running():
        ble_loop.call_soon_threadsafe(ble_loop.stop)
    threading.Thread(target=_ble_thread, daemon=True).start()


def _handle_disconnect(client):
    """Callback function to handle unexpected disconnections. Safe to call
    from any thread -- both the stats monitor and the idle watchdog call this
    on themselves when they detect a dead link (same-thread, on ble_loop),
    and bleak's own disconnect callback may call it from a different thread
    depending on platform/backend.
    """
    global connected, belt_running, connecting, connection_failed
    global _stats_monitor_task, _idle_watchdog_task
    if connected:
        logging.warning("Device has disconnected unexpectedly.")
    connected = False
    belt_running = False
    connecting = False
    connection_failed = True

    try:
        current = asyncio.current_task()
    except RuntimeError:
        current = None  # no running event loop in this thread

    # Cancel any running watchdog tasks, skipping self-cancellation: cancelling
    # a task from inside its own currently-running step still marks it
    # cancelled() even after it exits cleanly via `break`, which is misleading
    # -- that task is already unwinding on its own, no cancellation needed.
    #
    # task.cancel() itself is only safe to call from the thread running the
    # task's own event loop -- routed through call_soon_threadsafe() so this
    # is correct regardless of which thread actually called _handle_disconnect
    # (per the docstring above, that's not guaranteed to be ble_loop's own
    # thread). Falls back to a direct call only if ble_loop is already gone,
    # in which case there's no loop left to schedule onto anyway.
    for name, task in (("stats monitor", _stats_monitor_task), ("idle watchdog", _idle_watchdog_task)):
        if task and not task.done() and task is not current:
            logging.info(f"Cancelling {name} due to disconnect")
            if ble_loop and not ble_loop.is_closed():
                ble_loop.call_soon_threadsafe(task.cancel)
            else:
                task.cancel()


def _handle_signal_shutdown(signum, frame):
    """Handle SIGTERM/SIGINT by triggering graceful shutdown of the device."""
    global _shutting_down, _server_stopping
    with _shutting_down_lock:
        if _shutting_down:
            logging.info("Signal received but shutdown already in progress")
            return
        _shutting_down = True

    # Set UI-visible flag early so the next /stats poll can inform the browser
    _server_stopping = True
    logging.info(f"Received signal {signum}, initiating graceful shutdown...")

    if ble_loop and not ble_loop.is_closed():
        try:
            asyncio.run_coroutine_threadsafe(_graceful_shutdown(), ble_loop).result(timeout=10)
        except Exception as exc:
            logging.error(f"Graceful shutdown from signal failed: {exc}")
    else:
        logging.info("No BLE loop active during signal shutdown")

    # Stop the event loop so the BLE thread can exit cleanly
    if ble_loop and ble_loop.is_running():
        try:
            ble_loop.call_soon_threadsafe(ble_loop.stop)
        except Exception as exc:
            logging.debug(f"Error stopping BLE loop from signal: {exc}")

    # Give the browser ~2 s to receive the last /stats response (stopping: true),
    # render the shutdown message, and then force-kill the process.
    time.sleep(2)
    os._exit(0)


# ── Flask routes ────────────────────────────────────────────────────────
@app.route("/")
def root():
    if not connected:
        return render_template("connecting.html")

    time_active_display = "0:00:00"  # Default for start/paused if not running
    if session_active:  # Only calculate if a session is or was active
        time_active_display = format_seconds_to_hms(current_session_active_seconds)

    if not session_active:
        # For start_session, show history (last N sessions)
        history = _load_session_history(limit=HISTORY_DISPLAY_LIMIT)
        pending_health_export = bool(history) and not history[0].get("health_logged", False)

        pending_restore = None
        if _pending_restore:
            pending_restore = {
                "distance_mi": round(_pending_restore.get("current_distance_km", 0.0) * KM_TO_MI, 2),
                "steps": _pending_restore.get("current_steps", 0),
                "calories": round(_pending_restore.get("current_calories", 0.0)),
                "time_active": format_seconds_to_hms(_pending_restore.get("current_session_active_seconds", 0)),
            }

        return render_template(
            "start_session.html", time_active="0:00:00", history=history, pending_restore=pending_restore,
            pending_health_export=pending_health_export,
        )

    template = "active_session.html" if belt_running else "paused_session.html"

    return render_template(
        template,
        speed=current_speed_kmh * KM_TO_MI,
        distance=current_distance_km * KM_TO_MI,
        steps=current_steps,
        calories=current_calories,
        time_active=time_active_display,
    )


# ── End Session ────────────────────────────────────────────────────────
@app.route("/end_session", methods=["POST"])
def end_session():
    """End the current session: save to history, reset counters, return to start."""
    global session_active, belt_running, current_distance_km, current_steps, current_speed_kmh
    global current_calories, current_session_active_seconds, _session_start_time

    with _session_state_lock:
        if not session_active:
            return redirect(url_for("root"))

        was_running = belt_running
        belt_running = False

        # Cancel any in-flight belt sequence and monitor, then stop the belt.
        # Without cancelling, an in-flight resume sequence could keep running
        # after end_session returns, recreating a monitor for a dead session.
        async def _end_belt_sequence():
            global _belt_sequence_task, _belt_transitioning
            # Registers itself as _belt_sequence_task and sets
            # _belt_transitioning like its three siblings (start/pause/resume)
            # -- previously it did neither, the one belt sequence that didn't
            # follow the pattern, so nothing else could observe or cancel an
            # in-flight End Session the way it can for the others.
            await _cancel_task(_belt_sequence_task)
            _belt_sequence_task = asyncio.current_task()
            _belt_transitioning = True
            try:
                await _cancel_task(_stats_monitor_task)
                if was_running and controller:
                    try:
                        await _run_locked(
                            lambda: asyncio.wait_for(controller.stop_belt(), timeout=_BLE_WRITE_TIMEOUT_SECONDS),
                            timeout=_BLE_SEQUENCE_LOCK_TIMEOUT_SECONDS,
                        )
                    except Exception as exc:
                        # The belt may still be physically moving even though the
                        # session was already ended in the UI -- treat this the
                        # same as any other failed BLE write and surface it as a
                        # dead connection instead of swallowing it silently.
                        logging.error(f"Error stopping belt on end_session: {exc}")
                        _handle_disconnect(None)
            finally:
                _belt_sequence_task = None
                _belt_transitioning = False

        asyncio.run_coroutine_threadsafe(_end_belt_sequence(), ble_loop)

        # Save session to history
        _save_session()
        _clear_session_state()
        logging.info("Session ended by user, saved to history")

        # Reset all counters
        current_distance_km = current_calories = 0.0
        current_speed_kmh = 0.0
        current_steps = 0
        current_session_active_seconds = 0
        speed_history.clear()
        session_active = False
        _session_start_time = None

    return redirect(url_for("root"))


# ── Export CSV ──────────────────────────────────────────────────────────
@app.route("/export_csv")
def export_csv():
    """Export full session history as a CSV download."""
    history = _load_session_history()
    si = io.StringIO()
    writer = csv.writer(si)
    writer.writerow(["date", "start_time", "end_time", "duration_seconds",
                      "distance_km", "distance_mi", "steps", "calories",
                      "avg_speed_kmh", "avg_speed_mph"])
    for row in history:
        writer.writerow([row.get("date"), row.get("start_time"), row.get("end_time"),
                         row.get("duration_seconds"), row.get("distance_km"),
                         row.get("distance_mi"), row.get("steps"), row.get("calories"),
                         row.get("avg_speed_kmh"), row.get("avg_speed_mph")])

    resp = make_response(si.getvalue())
    resp.headers["Content-Disposition"] = "attachment; filename=walkingdad_history.csv"
    resp.headers["Content-Type"] = "text/csv"
    return resp


# ── Clear History ──────────────────────────────────────────────────────
@app.route("/clear_history", methods=["POST"])
def clear_history():
    """Clear all session history."""
    _clear_session_history()
    return jsonify({"status": "cleared"})


# ── Dismiss Apple Health Export Prompt ───────────────────────────────────
@app.route("/dismiss_health_export", methods=["POST"])
def dismiss_health_export():
    """Mark the most recent session as dismissed from the Apple Health export prompt."""
    _dismiss_health_export()
    return jsonify({"status": "dismissed"})


# Waitress won't necessarily start (or will be unusably starved) with too
# few threads -- unlike the other numeric settings, a bad value here doesn't
# just misbehave one subsystem, it can take down the whole app on next
# restart. Clamped rather than rejected outright since there's no user-facing
# form-validation/error-message path for this settings page.
_MIN_WAITRESS_THREADS = 4  # Waitress's own long-standing default
_MAX_WAITRESS_THREADS = 128  # Generous ceiling; guards against a typo starving the OS thread pool


def _clamp_waitress_threads(v, cur):
    n = int(v)
    if n < _MIN_WAITRESS_THREADS:
        logging.warning(f"waitress_threads={n} is below the safe minimum; clamping to {_MIN_WAITRESS_THREADS}")
        return _MIN_WAITRESS_THREADS
    if n > _MAX_WAITRESS_THREADS:
        logging.warning(f"waitress_threads={n} exceeds the safe maximum; clamping to {_MAX_WAITRESS_THREADS}")
        return _MAX_WAITRESS_THREADS
    return n


# RESUME_GRACE_PERIOD_SECONDS gets re-stamped after every Start/Resume
# sequence completes (see _start_belt_sequence()/_resume_belt_sequence()'s
# finally blocks) specifically to give the belt a real window to physically
# reach speed before process_status_packet()'s auto-pause logic starts
# judging speed=0 readings as an unexpected stop. Set too low, that window
# collapses to nothing and a normal post-start/resume ramp-up can trigger a
# false auto-pause -- clamped rather than rejected outright, same convention
# as waitress_threads above.
_MIN_RESUME_GRACE_PERIOD_SECONDS = 3


def _clamp_resume_grace_period(v, cur):
    n = int(v)
    if n < _MIN_RESUME_GRACE_PERIOD_SECONDS:
        logging.warning(
            f"resume_grace_period_seconds={n} is below the safe minimum; "
            f"clamping to {_MIN_RESUME_GRACE_PERIOD_SECONDS}"
        )
        return _MIN_RESUME_GRACE_PERIOD_SECONDS
    return n


# (form field name, config.py attr name, cast(raw_str, current_value) -> value,
#  also update the live app.py module global immediately vs. only take effect
#  on next restart)
_SETTINGS_SCHEMA = [
    ("ble_device_name", "BLE_DEVICE_NAME", lambda v, cur: v.strip(), True),
    ("max_speed_kmh", "MAX_SPEED_KMH", lambda v, cur: float(v), True),
    ("min_speed_kmh", "MIN_SPEED_KMH", lambda v, cur: float(v), True),
    ("speed_step", "SPEED_STEP", lambda v, cur: float(v), True),
    ("slow_walk_speed_kmh", "SLOW_WALK_SPEED_KMH", lambda v, cur: float(v), True),
    ("kcal_per_mile", "KCAL_PER_MILE", lambda v, cur: int(v), True),
    ("resume_grace_period_seconds", "RESUME_GRACE_PERIOD_SECONDS", _clamp_resume_grace_period, True),
    ("history_display_limit", "HISTORY_DISPLAY_LIMIT", lambda v, cur: int(v), True),
    ("host", "HOST", lambda v, cur: v.strip() or cur, False),
    ("port", "PORT", lambda v, cur: int(v), False),
    ("waitress_threads", "WAITRESS_THREADS", _clamp_waitress_threads, False),
    ("apple_health_shortcut_name", "APPLE_HEALTH_SHORTCUT_NAME", lambda v, cur: v.strip() or cur, True),
]


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        updates = {}
        for form_key, const_name, cast, _live in _SETTINGS_SCHEMA:
            current = getattr(config, const_name)
            raw = request.form.get(form_key, str(current))
            try:
                updates[form_key] = cast(raw, current)
            except (ValueError, TypeError):
                logging.warning(f"Invalid value for {form_key} ({raw!r}); keeping current value.")
                updates[form_key] = current

        _write_config(updates)
        for form_key, const_name, _cast, live in _SETTINGS_SCHEMA:
            setattr(config, const_name, updates[form_key])
            if live:
                globals()[const_name] = updates[form_key]
        return redirect(url_for("root", saved=1))

    return render_template(
        "settings.html",
        **{form_key: getattr(config, const_name) for form_key, const_name, _, _ in _SETTINGS_SCHEMA},
    )


@app.route("/reconnect")
def reconnect():
    if not connected and not connecting:
        _start_ble_thread()
    return redirect(url_for("root"))


# ── Restore / Discard interrupted session ───────────────────────────────
@app.route("/restore_session", methods=["POST"])
def restore_session():
    """Restore a session that was interrupted by a crash/restart, in paused state."""
    global session_active, belt_running, current_distance_km, current_steps, current_calories
    global current_session_active_seconds, resume_speed_kmh, _session_start_time
    global _last_dev_dist, _last_dev_steps, _pending_restore

    if not connected:
        return redirect(url_for("root"))

    with _session_state_lock:
        if session_active or not _pending_restore:
            return redirect(url_for("root"))

        state = _pending_restore
        _pending_restore = None

        current_distance_km = state.get("current_distance_km", 0.0)
        current_steps = state.get("current_steps", 0)
        current_calories = state.get("current_calories", 0.0)
        current_session_active_seconds = state.get("current_session_active_seconds", 0)
        resume_speed_kmh = state.get("resume_speed_kmh", 2.0)
        _last_dev_dist = state.get("last_dev_dist", 0)
        _last_dev_steps = state.get("last_dev_steps", 0)
        start_str = state.get("session_start_time")
        _session_start_time = datetime.fromisoformat(start_str) if start_str else datetime.now()
        speed_history.clear()

        # Restored in paused state. The belt isn't actually running; user hits
        # Resume to reconnect the belt sequence and stats monitor.
        session_active = True
        belt_running = False
        _save_session_state()
        logging.info("Restored interrupted session from session_state.json")

    return redirect(url_for("root"))


@app.route("/discard_session", methods=["POST"])
def discard_session():
    """Discard a pending crash-recovery session prompt."""
    global _pending_restore
    with _session_state_lock:
        if session_active:
            # A restore (or a fresh start) already claimed this session; don't
            # delete its live state file out from under it.
            return redirect(url_for("root"))
        _pending_restore = None
        _clear_session_state()
    return redirect(url_for("root"))


@app.route("/start", methods=["POST"])
def start_session():
    """Begin a new session: reset counters, start belt, launch stats monitor."""
    global session_active, belt_running, current_distance_km, current_steps, current_calories, resume_speed_kmh
    global current_session_active_seconds, _stats_monitor_task, _session_start_time, current_speed_kmh
    global _resume_grace_deadline, _pending_restore

    if not connected:
        return redirect(url_for("root"))

    with _session_state_lock:
        if session_active:
            return redirect(url_for("root"))

        current_distance_km = current_calories = 0.0
        current_steps = 0
        current_speed_kmh = 0.0
        current_session_active_seconds = 0
        resume_speed_kmh = 2.0
        speed_history.clear()
        _session_start_time = datetime.now()
        _resume_grace_deadline = time.time() + RESUME_GRACE_PERIOD_SECONDS

        session_active = True
        belt_running = True
        _pending_restore = None  # A fresh session supersedes any unresolved restore prompt
        _save_session_state()

        async def _start_belt_sequence():
            global belt_running, _stats_monitor_task, _belt_sequence_task, _belt_transitioning
            global _resume_grace_deadline
            await _cancel_task(_belt_sequence_task)
            _belt_sequence_task = asyncio.current_task()
            _belt_transitioning = True
            try:
                logging.info("Starting belt...")
                await _cancel_task(_stats_monitor_task)
                # Always the full STANDBY/MANUAL toggle here, never light-wake:
                # a fresh Start has no recent-activity context to justify
                # skipping it (the device could have been idle for hours), and
                # resetting the WalkingPad's own display/counters is expected
                # for a brand-new session anyway -- our own counters were just
                # reset above too.
                await _wake_and_start_belt()
                logging.info("Starting stats monitor...")
                _stats_monitor_task = asyncio.create_task(_stats_monitor())
                logging.info("Session started successfully")
            except asyncio.CancelledError:
                logging.info("Start sequence cancelled")
            except Exception as exc:
                logging.error(f"Start sequence error: {exc}")
                belt_running = False
                _handle_disconnect(None)
            finally:
                _belt_sequence_task = None
                _belt_transitioning = False
                # Re-stamped here, not just once when the button was clicked:
                # _wake_and_start_belt() can legitimately run long on a slow
                # connection, which would otherwise eat into (or exhaust) the
                # grace window this is meant to give the belt to physically
                # reach speed *after* the sequence completes -- see
                # process_status_packet()'s AUTO-PAUSE LOGIC comment.
                _resume_grace_deadline = time.time() + RESUME_GRACE_PERIOD_SECONDS

        try:
            asyncio.run_coroutine_threadsafe(_start_belt_sequence(), ble_loop)
        except Exception as exc:
            logging.error(f"Failed to queue start sequence: {exc}")
            belt_running = False
            return redirect(url_for("root"))

    return redirect(url_for("root"))


# ── Pause / Resume ───────────────────────────────────────────────────────
@app.route("/pause", methods=["POST"], endpoint="pause")
@app.route("/pause_session", methods=["POST"], endpoint="pause_session")
def pause_session():
    global belt_running, resume_speed_kmh
    with _session_state_lock:
        if not belt_running:
            return redirect(url_for("root"))

        # Use the most recent speed from our history for manual pause
        if speed_history:
            resume_speed_kmh = speed_history[-1]

        belt_running = False
        _save_session_state()

        # Single ordered sequence on ble_loop: cancel monitor before stop_belt()
        # so an immediate resume can't interleave with the monitor's in-flight polls.
        async def _pause_belt_sequence():
            global _belt_sequence_task, _belt_transitioning
            # Cancel any prior belt sequence before self-registering.
            await _cancel_task(_belt_sequence_task)
            _belt_sequence_task = asyncio.current_task()
            _belt_transitioning = True
            try:
                await _cancel_task(_stats_monitor_task)
                await _run_locked(
                    lambda: asyncio.wait_for(controller.stop_belt(), timeout=_BLE_WRITE_TIMEOUT_SECONDS),
                    timeout=_BLE_SEQUENCE_LOCK_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                logging.info("Pause sequence cancelled")
            except Exception as exc:
                # belt_running was already set False in the UI the moment Pause
                # was clicked -- if the belt didn't actually confirm stopping,
                # that's a real safety gap (it may still be moving), so treat
                # this the same as any other failed BLE write: surface it as a
                # dead connection instead of leaving a silent success.
                logging.error(f"Error stopping belt on pause: {exc}")
                _handle_disconnect(None)
            finally:
                _belt_sequence_task = None
                _belt_transitioning = False

        asyncio.run_coroutine_threadsafe(_pause_belt_sequence(), ble_loop)

    return redirect(url_for("root"))


@app.route("/resume", methods=["POST"], endpoint="resume")
@app.route("/resume_session", methods=["POST"], endpoint="resume_session")
def resume_session():
    global belt_running, _resume_grace_deadline, session_active, _stats_monitor_task

    with _session_state_lock:
        if not session_active:
            logging.warning("Resume called but no active session.")
            return redirect(url_for("root"))

        if belt_running:
            logging.info("Resume called but belt is already running.")
            return redirect(url_for("root"))

        logging.info("Resume button clicked. Setting app state to active.")
        belt_running = True
        _resume_grace_deadline = time.time() + RESUME_GRACE_PERIOD_SECONDS
        _save_session_state()

        async def _resume_belt_sequence():
            global belt_running, _stats_monitor_task, _belt_sequence_task, _belt_transitioning
            global _resume_grace_deadline
            # Cancel any prior in-flight sequence (e.g. pause) before sending
            # commands; concurrent coroutines on ble_loop interleave BLE writes.
            await _cancel_task(_belt_sequence_task)
            _belt_sequence_task = asyncio.current_task()
            _belt_transitioning = True
            try:
                logging.info("Attempting resume: Sending wake-up and start sequence to device...")
                await _cancel_task(_stats_monitor_task)
                # try_light_wake=True: Resume typically follows our own recent
                # stop_belt() (Pause) by seconds, not long enough for the device
                # to have gone back to sleep on its own -- see _try_light_wake()'s
                # docstring. Falls back automatically to the same guaranteed-safe
                # toggle Start always uses if that assumption doesn't hold.
                await _wake_and_start_belt(resume_speed_kmh, try_light_wake=True)
                logging.info("Starting stats monitor...")
                _stats_monitor_task = asyncio.create_task(_stats_monitor())
                logging.info("Resume sequence commands sent, monitor ensured.")
            except asyncio.CancelledError:
                logging.info("Resume sequence cancelled")
            except Exception as exc:
                logging.error(f"Error during resume sequence: {exc}")
                belt_running = False
                _handle_disconnect(None)
            finally:
                _belt_sequence_task = None
                _belt_transitioning = False
                # See _start_belt_sequence()'s matching comment: re-stamped here
                # so a slow wake-up sequence can't eat into the post-completion
                # ramp-up protection this deadline is meant to provide.
                _resume_grace_deadline = time.time() + RESUME_GRACE_PERIOD_SECONDS

        try:
            asyncio.run_coroutine_threadsafe(_resume_belt_sequence(), ble_loop)
        except Exception as exc:
            logging.error(f"Failed to queue resume sequence: {exc}")
            belt_running = False
            return redirect(url_for("root"))

    return redirect(url_for("root"))


# ── Speed Controls ───────────────────────────────────────────────────────
@app.route("/decrease_speed", methods=["POST"])
def decrease_speed():
    """Decrease the belt speed by one step."""
    if not belt_running:
        return redirect(url_for("root"))

    new_speed_kmh = max(MIN_SPEED_KMH, current_speed_kmh - SPEED_STEP)
    dev_speed = int(new_speed_kmh * 10)
    asyncio.run_coroutine_threadsafe(_locked_change_speed(dev_speed), ble_loop)
    return redirect(url_for("root"))


def _set_preset_speed(speed_kmh: float):
    """Shared body for the fixed-speed presets (min/slow/max) below."""
    if not belt_running:
        return redirect(url_for("root"))

    dev_speed = int(speed_kmh * 10)
    asyncio.run_coroutine_threadsafe(_locked_change_speed(dev_speed), ble_loop)
    return redirect(url_for("root"))


@app.route("/min_speed", methods=["POST"])
def min_speed():
    """Set the belt speed to the configured floor (a gentle warm-up pace)."""
    return _set_preset_speed(MIN_SPEED_KMH)


@app.route("/slow_speed", methods=["POST"])
def slow_speed():
    """Set the belt speed to a predefined slow walk speed."""
    return _set_preset_speed(SLOW_WALK_SPEED_KMH)


@app.route("/increase_speed", methods=["POST"])
def increase_speed():
    """Increase the belt speed by one step."""
    if not belt_running:
        return redirect(url_for("root"))

    new_speed_kmh = min(MAX_SPEED_KMH, current_speed_kmh + SPEED_STEP)
    dev_speed = int(new_speed_kmh * 10)
    asyncio.run_coroutine_threadsafe(_locked_change_speed(dev_speed), ble_loop)
    return redirect(url_for("root"))


@app.route("/max_speed", methods=["POST"])
def max_speed():
    """Set the belt speed to maximum."""
    return _set_preset_speed(MAX_SPEED_KMH)


# ── Live JSON endpoint ───────────────────────────────────────────────────
def _build_stats_payload() -> dict:
    """Build the stats snapshot dict from current global state.

    Single source of truth for the wire payload shape, shared by the
    polling /stats endpoint and the SSE broadcaster.
    """
    return dict(
        is_connected=connected,
        connection_failed=connection_failed,
        is_running=belt_running,
        belt_transitioning=_belt_transitioning,
        speed=round(current_speed_kmh * KM_TO_MI, 1),
        distance=round(current_distance_km * KM_TO_MI, 2),
        steps=current_steps,
        calories=round(current_calories),
        time_active=format_seconds_to_hms(current_session_active_seconds),
        stopping=_server_stopping,
    )


@app.route("/stats", endpoint="get_stats")
def stats_json():
    """One-shot JSON snapshot. No template or app.py code polls this anymore
    -- every screen uses /stats_stream (SSE) instead -- kept only as a
    stable single-request endpoint for external scripting/tooling against
    a running instance.
    """
    resp = make_response(jsonify(_build_stats_payload()))
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── SSE broadcaster ──────────────────────────────────────────────────────
def _sse_frame(payload: dict) -> str:
    """Format a stats payload as one SSE 'data:' frame."""
    return f"data: {json.dumps(payload)}\n\n"


def _sse_broadcast_loop():
    """Daemon thread: push a stats snapshot to all subscribers every second.

    Runs unconditionally (unlike _stats_monitor, which only runs while
    belt_running) so idle/paused screens stay live too. This is the only
    broadcaster thread in the process. An unhandled exception here would
    silently and permanently stop all SSE delivery, so every tick runs
    under a broad except that logs and keeps the loop alive.
    """
    while True:
        time.sleep(_SSE_BROADCAST_INTERVAL_SECONDS)
        try:
            # Serialize once here rather than in each subscriber's own generator,
            # since every subscriber gets the identical frame this tick.
            frame = _sse_frame(_build_stats_payload())
            with _sse_subscribers_lock:
                subscribers = list(_sse_subscribers)
            for q in subscribers:
                # This thread is the sole writer to each per-subscriber queue,
                # so only the latest snapshot matters: drop any stale pending
                # item before pushing, unconditionally.
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                q.put_nowait(frame)
        except Exception as exc:
            logging.error(f"SSE broadcast tick failed (continuing): {exc}")


def _start_sse_broadcaster():
    threading.Thread(target=_sse_broadcast_loop, daemon=True).start()


@app.route("/stats_stream")
def stats_stream():
    """SSE endpoint: pushes a stats snapshot roughly once a second."""
    q: queue.Queue = queue.Queue(maxsize=1)
    with _sse_subscribers_lock:
        _sse_subscribers.append(q)

    def _generate():
        try:
            # Immediate snapshot so first paint doesn't wait up to 1s for the next tick.
            yield _sse_frame(_build_stats_payload())
            while True:
                try:
                    frame = q.get(timeout=_SSE_KEEPALIVE_TIMEOUT_SECONDS)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield frame
        finally:
            with _sse_subscribers_lock:
                if q in _sse_subscribers:
                    _sse_subscribers.remove(q)

    resp = Response(_generate(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# ── Shutdown endpoint ──────────────────────────────────────────────────
@app.route("/shutdown", methods=['POST'])
def shutdown():
    """Gracefully shut down: stop belt, disconnect BLE, then exit."""
    global _shutting_down, _server_stopping

    with _shutting_down_lock:
        if _shutting_down:
            logging.info("Shutdown already in progress, ignoring duplicate request")
            return jsonify({"status": "shutting_down"})
        _shutting_down = True

    # Set UI-visible flag immediately so the next /stats poll informs the browser
    _server_stopping = True
    logging.info("Graceful shutdown initiated via HTTP...")

    if ble_loop and not ble_loop.is_closed():
        try:
            fut = asyncio.run_coroutine_threadsafe(_graceful_shutdown(), ble_loop)
            # Wait for the coroutine to actually complete (with a timeout as safety net)
            fut.result(timeout=10)
        except Exception as exc:
            logging.error(f"Graceful shutdown error: {exc}")

        # Stop the BLE event loop so the thread can exit cleanly
        if ble_loop.is_running():
            try:
                ble_loop.call_soon_threadsafe(ble_loop.stop)
            except Exception as exc:
                logging.debug(f"Error stopping BLE loop: {exc}")

    # Return the HTTP response so the client knows shutdown was accepted
    resp = jsonify({"status": "shutting_down"})

    # Use os._exit(0) here because Waitress catches SystemExit from sys.exit(0)
    # and continues running, which would prevent the server from actually stopping.
    def _deferred_exit():
        time.sleep(5)  # Give browser time to receive /stats with stopping:true
        logging.info("Exiting process after graceful shutdown...")
        os._exit(0)

    threading.Thread(target=_deferred_exit, daemon=True).start()
    return resp


# ── Signal handlers for graceful shutdown on Ctrl+C / SIGTERM ──────────
signal.signal(signal.SIGTERM, _handle_signal_shutdown)
signal.signal(signal.SIGINT, _handle_signal_shutdown)

# ── Atexit handler as safety net ────────────────────────────────────────
def _atexit_cleanup():
    """Safety net: attempt to stop the belt and disconnect BLE on process exit."""
    global _shutting_down, ble_loop
    if _shutting_down:
        return  # Already handled gracefully
    logging.info("atexit: performing emergency cleanup...")
    if ble_loop and not ble_loop.is_closed():
        try:
            asyncio.run_coroutine_threadsafe(_graceful_shutdown(), ble_loop).result(timeout=5)
        except Exception as exc:
            logging.error(f"atexit cleanup error: {exc}")

atexit.register(_atexit_cleanup)

# ── Kick off BLE thread ──────────────────────────────────────────────────
# The server is no longer started here. This just pre-starts the BLE thread.
_start_ble_thread()
_start_sse_broadcaster()
