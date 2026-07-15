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
    HISTORY_DISPLAY_LIMIT,
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
# ask_stats() only sends a request — the ph4_walkingpad library never returns the reply
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
    }


def _save_session():
    """Append the current session to session_history.json (thread-safe)."""
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

        try:
            with open(HISTORY_FILE, "w") as f:
                json.dump(history, f, indent=2)
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
    """Delete or truncate the session history file (thread-safe)."""
    with _history_lock:
        try:
            with open(HISTORY_FILE, "w") as f:
                json.dump([], f)
            logging.info("Session history cleared")
        except IOError as exc:
            logging.error(f"Failed to clear session history: {exc}")


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
    return dict(connected=connected, connecting=connecting, connection_failed=connection_failed)


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
    await controller.run(dev.address)

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

    await controller.switch_mode(WalkingPad.MODE_MANUAL)

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
            await controller.enable_notifications()
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
    passive BLE notification callback (_handle_status_update) — either path
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
    if time.time() > _resume_grace_deadline:
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
            await controller.stop_belt()
            belt_running = False
            await asyncio.sleep(0.5)

        # Step 2: Cancel stats monitor and idle watchdog tasks
        logging.info("Cancelling stats monitor for shutdown")
        await _cancel_stats_monitor()
        await _cancel_idle_watchdog()

        # Step 3: Switch device to standby mode
        if controller:
            logging.info("Switching device to standby mode...")
            await controller.switch_mode(WalkingPad.MODE_STANDBY)
            await asyncio.sleep(0.5)

        # Step 4: Disconnect BLE client gracefully
        if controller and hasattr(controller, 'client') and controller.client:
            logging.info("Disconnecting BLE client...")
            try:
                await controller.client.disconnect()
            except Exception as exc:
                logging.warning(f"BLE disconnect error (non-fatal): {exc}")
    except Exception as exc:
        logging.error(f"Graceful shutdown error (continuing exit): {exc}")
    finally:
        connected = False
        session_active = False
        belt_running = False
        logging.info("Device cleanup complete")


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

        if time.monotonic() - _last_status_update_monotonic > _STALE_STATUS_TIMEOUT_SECONDS:
            logging.error(
                f"No status update in over {_STALE_STATUS_TIMEOUT_SECONDS}s while idle/paused; "
                "treating connection as dead."
            )
            _handle_disconnect(None)
            break

        try:
            await asyncio.wait_for(controller.ask_stats(), timeout=2.0)
        except Exception as exc:
            logging.debug(f"Idle watchdog ping error (will retry next cycle): {exc}")


async def _stats_monitor():
    """Active monitor: explicitly request a status packet every second.

    ask_stats() only sends the request — the reply (if any) arrives via a
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
                status = await asyncio.wait_for(controller.ask_stats(), timeout=2.0)
                if status:
                    dist, steps, speed = _extract_status_fields(status)
                    process_status_packet(dist, steps, speed)
                    logging.debug(f"Poll {status}")
                else:
                    logging.debug("ask_stats returned no reply (expected on this device/library)")
            except asyncio.TimeoutError:
                logging.warning("Status poll timeout")
            except Exception as exc:
                logging.warning(f"ask_stats error: {exc}")

            if time.monotonic() - _last_status_update_monotonic > _STALE_STATUS_TIMEOUT_SECONDS:
                logging.error(
                    f"No status update (active or passive) in over {_STALE_STATUS_TIMEOUT_SECONDS}s; "
                    "treating connection as dead."
                )
                _handle_disconnect(None)
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


async def _cancel_stats_monitor():
    """Cancel the running stats monitor and wait for it to fully unwind."""
    if _stats_monitor_task and not _stats_monitor_task.done():
        _stats_monitor_task.cancel()
        try:
            await _stats_monitor_task
        except asyncio.CancelledError:
            pass


async def _cancel_idle_watchdog():
    """Cancel the running idle connection watchdog and wait for it to unwind."""
    if _idle_watchdog_task and not _idle_watchdog_task.done():
        _idle_watchdog_task.cancel()
        try:
            await _idle_watchdog_task
        except asyncio.CancelledError:
            pass


async def _cancel_belt_sequence():
    """Cancel any in-flight belt sequence; call before self-registering to prevent concurrent BLE commands."""
    global _belt_sequence_task
    if _belt_sequence_task and not _belt_sequence_task.done():
        _belt_sequence_task.cancel()
        try:
            await _belt_sequence_task
        except asyncio.CancelledError:
            pass


async def _wake_and_start_belt(target_speed_kmh: float | None = None):
    """STANDBY→MANUAL toggle required: stop_belt() leaves device in STANDBY (commit 5cbb8ba)."""
    await controller.switch_mode(WalkingPad.MODE_STANDBY)
    await asyncio.sleep(0.5)
    await controller.switch_mode(WalkingPad.MODE_MANUAL)
    await asyncio.sleep(0.5)
    await controller.start_belt()
    await asyncio.sleep(0.5)

    if target_speed_kmh is not None:
        logging.info(f"Setting speed to {target_speed_kmh:.1f} km/h.")
        await controller.change_speed(int(target_speed_kmh * 10))
        await asyncio.sleep(0.5)


def _ble_thread():
    global connected, connecting, connection_failed, ble_loop

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

    try:
        try:
            connected_result = loop.run_until_complete(_connect_to_pad())
        except RuntimeError:
            # Event loop stopped during connection attempt (e.g., graceful shutdown
            # while scanning is still in progress). This is expected and harmless.
            logging.info("BLE connection interrupted by shutdown")
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
    if connected or connecting:
        return
    connecting = True
    connection_failed = False
    threading.Thread(target=_ble_thread, daemon=True).start()


def _handle_disconnect(client):
    """Callback function to handle unexpected disconnections. Safe to call
    from any thread -- both the stats monitor and the idle watchdog can call
    this on themselves when they detect a dead link.
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
    for name, task in (("stats monitor", _stats_monitor_task), ("idle watchdog", _idle_watchdog_task)):
        if task and not task.done() and task is not current:
            logging.info(f"Cancelling {name} due to disconnect")
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
            await _cancel_belt_sequence()
            await _cancel_stats_monitor()
            if was_running and controller:
                try:
                    await controller.stop_belt()
                except Exception as exc:
                    logging.error(f"Error stopping belt on end_session: {exc}")

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


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    global BLE_DEVICE_NAME, MAX_SPEED_KMH, MIN_SPEED_KMH, SPEED_STEP
    global SLOW_WALK_SPEED_KMH, KCAL_PER_MILE, RESUME_GRACE_PERIOD_SECONDS
    global HISTORY_DISPLAY_LIMIT
    if request.method == "POST":
        updates = {
            "ble_device_name":             request.form.get("ble_device_name", BLE_DEVICE_NAME).strip(),
            "max_speed_kmh":               float(request.form.get("max_speed_kmh", MAX_SPEED_KMH)),
            "min_speed_kmh":               float(request.form.get("min_speed_kmh", MIN_SPEED_KMH)),
            "speed_step":                  float(request.form.get("speed_step", SPEED_STEP)),
            "slow_walk_speed_kmh":         float(request.form.get("slow_walk_speed_kmh", SLOW_WALK_SPEED_KMH)),
            "kcal_per_mile":               int(request.form.get("kcal_per_mile", KCAL_PER_MILE)),
            "resume_grace_period_seconds": int(request.form.get("resume_grace_period_seconds", RESUME_GRACE_PERIOD_SECONDS)),
            "history_display_limit":       int(request.form.get("history_display_limit", HISTORY_DISPLAY_LIMIT)),
            "host":                        request.form.get("host", config.HOST).strip() or config.HOST,
            "port":                        int(request.form.get("port", config.PORT)),
            "waitress_threads":            int(request.form.get("waitress_threads", config.WAITRESS_THREADS)),
        }
        _write_config(updates)
        BLE_DEVICE_NAME             = config.BLE_DEVICE_NAME             = updates["ble_device_name"]
        MAX_SPEED_KMH               = config.MAX_SPEED_KMH               = updates["max_speed_kmh"]
        MIN_SPEED_KMH               = config.MIN_SPEED_KMH               = updates["min_speed_kmh"]
        SPEED_STEP                  = config.SPEED_STEP                  = updates["speed_step"]
        SLOW_WALK_SPEED_KMH         = config.SLOW_WALK_SPEED_KMH         = updates["slow_walk_speed_kmh"]
        KCAL_PER_MILE               = config.KCAL_PER_MILE               = updates["kcal_per_mile"]
        RESUME_GRACE_PERIOD_SECONDS = config.RESUME_GRACE_PERIOD_SECONDS = updates["resume_grace_period_seconds"]
        HISTORY_DISPLAY_LIMIT       = config.HISTORY_DISPLAY_LIMIT       = updates["history_display_limit"]
        return redirect(url_for("root", saved=1))
    return render_template(
        "settings.html",
        ble_device_name=BLE_DEVICE_NAME,
        max_speed_kmh=MAX_SPEED_KMH,
        min_speed_kmh=MIN_SPEED_KMH,
        speed_step=SPEED_STEP,
        slow_walk_speed_kmh=SLOW_WALK_SPEED_KMH,
        kcal_per_mile=KCAL_PER_MILE,
        resume_grace_period_seconds=RESUME_GRACE_PERIOD_SECONDS,
        history_display_limit=HISTORY_DISPLAY_LIMIT,
        host=config.HOST,
        port=config.PORT,
        waitress_threads=config.WAITRESS_THREADS,
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

        # Restored in paused state — the belt isn't actually running; user hits
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
            await _cancel_belt_sequence()
            _belt_sequence_task = asyncio.current_task()
            _belt_transitioning = True
            try:
                logging.info("Starting belt...")
                await _cancel_stats_monitor()
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
            await _cancel_belt_sequence()
            _belt_sequence_task = asyncio.current_task()
            _belt_transitioning = True
            try:
                await _cancel_stats_monitor()
                await controller.stop_belt()
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
            # Cancel any prior in-flight sequence (e.g. pause) before sending
            # commands; concurrent coroutines on ble_loop interleave BLE writes.
            await _cancel_belt_sequence()
            _belt_sequence_task = asyncio.current_task()
            _belt_transitioning = True
            try:
                logging.info("Attempting resume: Sending wake-up and start sequence to device...")
                await _cancel_stats_monitor()
                await _wake_and_start_belt(resume_speed_kmh)
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
    asyncio.run_coroutine_threadsafe(controller.change_speed(dev_speed), ble_loop)
    return redirect(url_for("root"))


@app.route("/slow_speed", methods=["POST"])
def slow_speed():
    """Set the belt speed to a predefined slow walk speed."""
    if not belt_running:
        return redirect(url_for("root"))

    dev_speed = int(SLOW_WALK_SPEED_KMH * 10)
    asyncio.run_coroutine_threadsafe(controller.change_speed(dev_speed), ble_loop)
    return redirect(url_for("root"))


@app.route("/increase_speed", methods=["POST"])
def increase_speed():
    """Increase the belt speed by one step."""
    if not belt_running:
        return redirect(url_for("root"))

    new_speed_kmh = min(MAX_SPEED_KMH, current_speed_kmh + SPEED_STEP)
    dev_speed = int(new_speed_kmh * 10)
    asyncio.run_coroutine_threadsafe(controller.change_speed(dev_speed), ble_loop)
    return redirect(url_for("root"))


@app.route("/max_speed", methods=["POST"])
def max_speed():
    """Set the belt speed to maximum."""
    if not belt_running:
        return redirect(url_for("root"))

    dev_speed = int(MAX_SPEED_KMH * 10)
    asyncio.run_coroutine_threadsafe(controller.change_speed(dev_speed), ble_loop)
    return redirect(url_for("root"))


# ── Live JSON endpoint ───────────────────────────────────────────────────
def _build_stats_payload() -> dict:
    """Build the stats snapshot dict from current global state.

    Single source of truth for the wire payload shape — shared by the
    polling /stats endpoint and the SSE broadcaster.
    """
    return dict(
        is_connected=connected,
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
    resp = make_response(jsonify(_build_stats_payload()))
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── SSE broadcaster ──────────────────────────────────────────────────────
def _sse_broadcast_loop():
    """Daemon thread: push a stats snapshot to all subscribers every second.

    Runs unconditionally (unlike _stats_monitor, which only runs while
    belt_running) so idle/paused screens stay live too. This is the only
    broadcaster thread in the process — an unhandled exception here would
    silently and permanently stop all SSE delivery, so every tick runs
    under a broad except that logs and keeps the loop alive.
    """
    while True:
        time.sleep(_SSE_BROADCAST_INTERVAL_SECONDS)
        try:
            payload = _build_stats_payload()
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
                q.put_nowait(payload)
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
            yield f"data: {json.dumps(_build_stats_payload())}\n\n"
            while True:
                try:
                    payload = q.get(timeout=_SSE_KEEPALIVE_TIMEOUT_SECONDS)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(payload)}\n\n"
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
