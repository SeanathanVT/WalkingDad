import asyncio
import atexit
import csv
import io
import json
import logging
import os
import signal
import sys
import threading
import time
from collections import deque
from datetime import datetime

from bleak import BleakScanner
from flask import Flask, render_template, redirect, url_for, jsonify, make_response
from ph4_walkingpad.pad import Controller, WalkingPad

# ── Logging Setup ────────────────────────────────────────────────────────
# All print() statements will be replaced with this logging configuration.
# It provides timed, leveled output. Set level=logging.DEBUG to see verbose messages.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
)

# ── Device constants ────────────────────────────────────────────────────
BLE_DEVICE_NAME = "KS-BLC2"  # Change this to match your device's Bluetooth name

# ── Conversion constants ─────────────────────────────────────────────────
KM_TO_MI = 0.621371
KMH_TO_MPH = 0.621371
KCAL_PER_MILE = 95  # rough kcal per mile

# ── Speed control constants ──────────────────────────────────────────────
MAX_SPEED_KMH = 6.0  # Approx 3.7 mph, a common max for these pads
MIN_SPEED_KMH = 1.0
SPEED_STEP = 0.6  # Speed change per button press in km/h
SLOW_WALK_SPEED_KMH = 4.5  # Approx 2.8 MPH


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
_history_lock = threading.Lock()  # Protect session_history.json reads/writes
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session_history.json")
HISTORY_DISPLAY_LIMIT = 10  # Max sessions shown on start screen

session_active = belt_running = False
_shutting_down = False
_server_stopping = False  # Flag for UI to detect Ctrl+C / signal shutdown
_shutting_down_lock = threading.Lock()  # Protect shutdown state mutations
resume_speed_kmh = 2.0  # default if none yet

current_speed_kmh = current_distance_km = 0.0
current_steps = 0
current_calories = 0.0
current_session_active_seconds = 0

_last_dev_dist = _last_dev_steps = 0


# ── Session History Persistence ────────────────────────────────────────

def _build_session_record() -> dict:
    """Build a session record dict from current global state."""
    now = datetime.now()
    distance_mi = current_distance_km * KM_TO_MI
    duration = max(current_session_active_seconds, 1)  # avoid div-by-zero
    avg_speed_kmh = current_distance_km / (duration / 3600.0)
    avg_speed_mph = avg_speed_kmh * KMH_TO_MPH

    return {
        "date": now.strftime("%Y-%m-%d"),
        "start_time": now.strftime("%H:%M:%S"),
        "end_time": now.strftime("%H:%M:%S"),
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


def _load_session_history(limit: int = HISTORY_DISPLAY_LIMIT) -> list:
    """Load session history from disk, return most recent entries first (thread-safe)."""
    with _history_lock:
        if not os.path.exists(HISTORY_FILE):
            return []
        try:
            with open(HISTORY_FILE, "r") as f:
                history = json.load(f)
            if not isinstance(history, list):
                return []
            # Most recent first, sliced to limit
            return list(reversed(history))[:limit]
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


def _load_full_session_history() -> list:
    """Load all session history (no limit), most recent first."""
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r") as f:
            history = json.load(f)
        if not isinstance(history, list):
            return []
        return list(reversed(history))
    except (json.JSONDecodeError, IOError) as exc:
        logging.warning(f"Failed to read full session history: {exc}")
        return []


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
    global controller, _device_ble_address
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
    return True


def _extract_status_fields(status) -> tuple:
    """Extract (distance, steps, speed) from a status dict or object."""
    if isinstance(status, dict):
        return status.get("dist", 0), status.get("steps", 0), status.get("speed", 0)
    return getattr(status, "dist", 0), getattr(status, "steps", 0), getattr(status, "speed", 0)


def process_status_packet(dev_dist: float, dev_steps: int, dev_speed: float):
    """Update cumulative stats from raw values AND handle auto-pause."""
    global belt_running, resume_speed_kmh, _resume_grace_deadline
    global current_speed_kmh, current_distance_km, current_steps, current_calories
    global _last_dev_dist, _last_dev_steps

    new_reported_speed_kmh = dev_speed / 10.0

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


async def _graceful_shutdown():
    """Safely stop the treadmill, cancel monitors, and disconnect BLE before exit."""
    global connected, belt_running, session_active, _stats_monitor_task
    try:
        # Step 0.5: Save in-progress session to history before cleanup
        if session_active:
            _save_session()

        # Step 1: Stop belt if running
        if belt_running and controller:
            logging.info("Stopping belt for graceful shutdown...")
            await controller.stop_belt()
            belt_running = False
            await asyncio.sleep(0.5)

        # Step 2: Cancel stats monitor task
        if _stats_monitor_task and not _stats_monitor_task.done():
            logging.info("Cancelling stats monitor for shutdown")
            _stats_monitor_task.cancel()
            try:
                await _stats_monitor_task
            except asyncio.CancelledError:
                pass

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


async def _stats_monitor():
    """Active monitor: explicitly request a status packet every second."""
    global current_session_active_seconds
    logging.info("Stats monitor started")

    try:
        while belt_running:
            current_session_active_seconds += 1

            try:
                status = await asyncio.wait_for(controller.ask_stats(), timeout=2.0)
                if status:
                    dist, steps, speed = _extract_status_fields(status)
                    process_status_packet(dist, steps, speed)
                    logging.debug(f"Poll {status}")
            except asyncio.TimeoutError:
                logging.warning("Status poll timeout")
            except Exception as exc:
                logging.warning(f"ask_stats error: {exc}")

            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                logging.info("Stats monitor cancelled")
                break
    except Exception as exc:
        logging.error(f"Stats monitor error: {exc}")
    finally:
        logging.info("Stats monitor stopped")


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
    """Callback function to handle unexpected disconnections."""
    global connected, belt_running, connecting, connection_failed, _stats_monitor_task
    if connected:
        logging.warning("Device has disconnected unexpectedly.")
    connected = False
    belt_running = False
    connecting = False
    connection_failed = True

    # Cancel any running stats monitor task (safe to call from any thread)
    if _stats_monitor_task and not _stats_monitor_task.done():
        logging.info("Cancelling stats monitor due to disconnect")
        _stats_monitor_task.cancel()


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
        history = _load_session_history(HISTORY_DISPLAY_LIMIT)
        return render_template("start_session.html", time_active="0:00:00", history=history)

    template = "active_session.html" if belt_running else "paused_session.html"

    return render_template(
        template,
        speed=current_speed_kmh * KMH_TO_MPH,
        distance=current_distance_km * KM_TO_MI,
        steps=current_steps,
        calories=current_calories,
        time_active=time_active_display,
    )


# ── End Session ────────────────────────────────────────────────────────
@app.route("/end_session")
def end_session():
    """End the current session: save to history, reset counters, return to start."""
    global session_active, belt_running, current_distance_km, current_steps
    global current_calories, current_session_active_seconds, _stats_monitor_task

    if not session_active:
        return redirect(url_for("root"))

    # Stop belt if running
    if belt_running and controller:
        try:
            asyncio.run_coroutine_threadsafe(controller.stop_belt(), ble_loop)
        except Exception as exc:
            logging.error(f"Error stopping belt on end_session: {exc}")
        belt_running = False
        time.sleep(0.5)

    # Cancel stats monitor
    if _stats_monitor_task and not _stats_monitor_task.done():
        _stats_monitor_task.cancel()

    # Save session to history
    _save_session()
    logging.info("Session ended by user, saved to history")

    # Reset all counters
    current_distance_km = current_steps = current_calories = 0.0
    current_session_active_seconds = 0
    speed_history.clear()
    session_active = False

    return redirect(url_for("root"))


# ── Export CSV ──────────────────────────────────────────────────────────
@app.route("/export_csv")
def export_csv():
    """Export full session history as a CSV download."""
    history = _load_full_session_history()
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


@app.route("/reconnect", endpoint="reconnect")
@app.route("/manual_reconnect", endpoint="manual_reconnect")
def reconnect():
    if not connected and not connecting:
        _start_ble_thread()
    return redirect(url_for("root"))


@app.route("/start")
def start_session():
    """Begin a new session: reset counters, start belt, launch stats monitor."""
    global session_active, belt_running, current_distance_km, current_steps, current_calories, resume_speed_kmh
    global current_session_active_seconds, _stats_monitor_task

    if not connected:
        return redirect(url_for("root"))

    current_distance_km = current_steps = current_calories = 0.0
    current_session_active_seconds = 0
    resume_speed_kmh = 2.0
    speed_history.clear()

    session_active = True
    belt_running = True

    async def _start_belt_sequence():
        global belt_running, _stats_monitor_task
        try:
            logging.info("Starting belt...")
            await controller.start_belt()
            await asyncio.sleep(0.5)

            # Cancel any existing stats monitor task
            if _stats_monitor_task and not _stats_monitor_task.done():
                logging.info("Cancelling existing stats monitor task")
                _stats_monitor_task.cancel()
                try:
                    await _stats_monitor_task
                except asyncio.CancelledError:
                    pass

            logging.info("Starting stats monitor...")
            _stats_monitor_task = asyncio.create_task(_stats_monitor())
            logging.info("Session started successfully")
        except Exception as exc:
            logging.error(f"Start sequence error: {exc}")
            belt_running = False
            _handle_disconnect(None)

    try:
        asyncio.run_coroutine_threadsafe(_start_belt_sequence(), ble_loop)
    except Exception as exc:
        logging.error(f"Failed to queue start sequence: {exc}")
        belt_running = False
        return redirect(url_for("root"))

    return redirect(url_for("root"))


# ── Pause / Resume ───────────────────────────────────────────────────────

@app.route("/pause", endpoint="pause")
@app.route("/pause_session", endpoint="pause_session")
def pause_session():
    global belt_running, resume_speed_kmh, _stats_monitor_task
    if not belt_running:
        return redirect(url_for("root"))

    # Use the most recent speed from our history for manual pause
    if speed_history:
        resume_speed_kmh = speed_history[-1]

    belt_running = False

    # Close the stats monitor by setting belt_running to False
    # (it will exit its loop naturally)
    logging.info("Pausing session - stats monitor will exit on next cycle")

    asyncio.run_coroutine_threadsafe(controller.stop_belt(), ble_loop)
    return redirect(url_for("root"))


@app.route("/resume", endpoint="resume")
@app.route("/resume_session", endpoint="resume_session")
def resume_session():
    global belt_running, _resume_grace_deadline, session_active, _stats_monitor_task

    if not session_active:
        logging.warning("Resume called but no active session.")
        return redirect(url_for("root"))

    if belt_running:
        logging.info("Resume called but belt is already running.")
        return redirect(url_for("root"))

    logging.info("Resume button clicked. Setting app state to active.")
    belt_running = True
    _resume_grace_deadline = time.time() + 7

    async def _resume_belt_sequence():
        global belt_running, _stats_monitor_task
        try:
            logging.info("Attempting resume: Sending wake-up and start sequence to device...")

            # Standard wake-up and start sequence
            await controller.switch_mode(WalkingPad.MODE_STANDBY)
            await asyncio.sleep(0.5)
            await controller.switch_mode(WalkingPad.MODE_MANUAL)
            await asyncio.sleep(0.5)

            await controller.start_belt()
            await asyncio.sleep(0.5)

            logging.info(f"Setting speed to {resume_speed_kmh:.1f} km/h.")
            await controller.change_speed(int(resume_speed_kmh * 10))
            await asyncio.sleep(0.5)
            
            # Cancel any existing stats monitor task and create a fresh one
            if _stats_monitor_task and not _stats_monitor_task.done():
                logging.info("Cancelling existing stats monitor task")
                _stats_monitor_task.cancel()
                try:
                    await _stats_monitor_task
                except asyncio.CancelledError:
                    pass

            logging.info("Starting stats monitor...")
            _stats_monitor_task = asyncio.create_task(_stats_monitor())
            logging.info("Resume sequence commands sent, monitor ensured.")

        except Exception as exc:
            logging.error(f"Error during resume sequence: {exc}")
            belt_running = False
            _handle_disconnect(None)

    try:
        asyncio.run_coroutine_threadsafe(_resume_belt_sequence(), ble_loop)
    except Exception as exc:
        logging.error(f"Failed to queue resume sequence: {exc}")
        belt_running = False
        return redirect(url_for("root"))

    return redirect(url_for("root"))


# ── Speed Controls ───────────────────────────────────────────────────────
@app.route("/decrease_speed")
def decrease_speed():
    """Decrease the belt speed by one step."""
    if not belt_running:
        return redirect(url_for("root"))

    new_speed_kmh = max(MIN_SPEED_KMH, current_speed_kmh - SPEED_STEP)
    dev_speed = int(new_speed_kmh * 10)
    asyncio.run_coroutine_threadsafe(controller.change_speed(dev_speed), ble_loop)
    return redirect(url_for("root"))

@app.route("/slow_speed")
def slow_speed():
    """Set the belt speed to a predefined slow walk speed."""
    if not belt_running:
        return redirect(url_for("root"))
    
    dev_speed = int(SLOW_WALK_SPEED_KMH * 10)
    asyncio.run_coroutine_threadsafe(controller.change_speed(dev_speed), ble_loop)
    return redirect(url_for("root"))

@app.route("/increase_speed")
def increase_speed():
    """Increase the belt speed by one step."""
    if not belt_running:
        return redirect(url_for("root"))

    new_speed_kmh = min(MAX_SPEED_KMH, current_speed_kmh + SPEED_STEP)
    dev_speed = int(new_speed_kmh * 10)
    asyncio.run_coroutine_threadsafe(controller.change_speed(dev_speed), ble_loop)
    return redirect(url_for("root"))


@app.route("/max_speed")
def max_speed():
    """Set the belt speed to maximum."""
    if not belt_running:
        return redirect(url_for("root"))

    dev_speed = int(MAX_SPEED_KMH * 10)
    asyncio.run_coroutine_threadsafe(controller.change_speed(dev_speed), ble_loop)
    return redirect(url_for("root"))


# ── Live JSON endpoint ───────────────────────────────────────────────────
@app.route("/stats", endpoint="get_stats")
def stats_json():
    formatted_time_active = format_seconds_to_hms(current_session_active_seconds)

    data = dict(
        is_connected=connected,
        is_running=belt_running,
        speed=round(current_speed_kmh * KMH_TO_MPH, 1),
        distance=round(current_distance_km * KM_TO_MI, 2),
        steps=current_steps,
        calories=round(current_calories),
        time_active=formatted_time_active,
        stopping=_server_stopping,
    )

    resp = make_response(jsonify(data))
    resp.headers["Cache-Control"] = "no-store"
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
