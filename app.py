import asyncio
import logging
import os
import threading
import webbrowser
from threading import Timer
import time
from collections import deque

from bleak import BleakScanner
from flask import Flask, render_template, redirect, url_for, jsonify, make_response, request
from ph4_walkingpad.pad import Controller, WalkingPad

# ── Logging Setup ────────────────────────────────────────────────────────
# All print() statements will be replaced with this logging configuration.
# It provides timed, leveled output. Set level=logging.DEBUG to see verbose messages.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
)

# ── Device constants ────────────────────────────────────────────────────
BLE_DEVICE_NAME = "WalkingPad"  # Change this to match your device's Bluetooth name

# ── Conversion constants ─────────────────────────────────────────────────
KM_TO_MI = 0.621371
KMH_TO_MPH = 0.621371
KCAL_PER_MILE = 95  # rough kcal per mile

# Speed control constants
MAX_SPEED_KMH = 6.0  # Approx 3.7 mph, a common max for these pads
MIN_SPEED_KMH = 1.0
SPEED_STEP = 0.6  # Speed change per button press in km/h
SLOW_WALK_SPEED_KMH = 4.5 # Approx 2.8 MPH



def kcal_estimate(miles: float) -> float:
    return KCAL_PER_MILE * miles

# In app.py
def format_seconds_to_hms(total_seconds):
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
_pad_address: str | None = None
_auto_pause_grace_until = 0
speed_history = deque(maxlen=15)
_stats_monitor_task: asyncio.Task | None = None  # Track the stats monitor task

session_active = belt_running = False
resume_speed_kmh = 2.0  # default if none yet

current_speed_kmh = current_distance_km = 0.0
current_steps = 0
current_calories = 0.0
current_session_active_seconds = 0 

_last_dev_dist = _last_dev_steps = 0


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
            if _pad_address:
                for dev in devices:
                    if dev.address == _pad_address:
                        logging.info(f"Found device by known address: {_pad_address}")
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
    global controller, _pad_address
    dev = None
    max_retries = 3
    retry_count = 0

    while retry_count < max_retries and not dev:
        if retry_count > 0:
            wait_time = min(2 ** retry_count, 10)
            logging.info(f"Retry {retry_count}/{max_retries} in {wait_time}s...")
            await asyncio.sleep(wait_time)

        if _pad_address:
            logging.info(f"Scanning for known device: {_pad_address}")
        else:
            logging.info(f"Scanning for device by name '{BLE_DEVICE_NAME}'...")

        dev = await _scan_for_device(timeout=10)

        if not dev:
            retry_count += 1
            if retry_count < max_retries:
                logging.warning(f"Device not found, retrying... ({retry_count}/{max_retries})")

    if not dev:
        logging.error(f"Could not find {BLE_DEVICE_NAME} after retries. Ensure it is on and in range.")
        _pad_address = None
        return False

    _pad_address = dev.address
    logging.info(f"Device found! Address: {_pad_address}")

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

    def _status_cb(_sender, st):
        try:
            if isinstance(st, dict):
                dist = st.get("dist", 0)
                steps = st.get("steps", 0)
                speed = st.get("speed", 0)
            else:
                dist = getattr(st, "dist", 0)
                steps = getattr(st, "steps", 0)
                speed = getattr(st, "speed", 0)
            process_status_packet(dist, steps, speed)
            logging.debug(f"Push d={dist} s={steps} v={speed}")
        except Exception as exc:
            logging.warning(f"status_cb error: {exc}")

    controller.on_cur_status_received = _status_cb

    if hasattr(controller, "enable_notifications"):
        try:
            await controller.enable_notifications()
        except Exception as exc:
            logging.warning(f"enable_notifications failed: {exc}")
    return True


def process_status_packet(dev_dist, dev_steps, dev_speed):
    """Update cumulative stats from raw values AND handle auto-pause."""
    global belt_running, resume_speed_kmh, _auto_pause_grace_until
    global current_speed_kmh, current_distance_km, current_steps, current_calories
    global _last_dev_dist, _last_dev_steps

    new_reported_speed_kmh = dev_speed / 10.0

    # Continuously populate the speed history with stable, non-zero speeds.
    if belt_running and new_reported_speed_kmh > MIN_SPEED_KMH:
        speed_history.append(new_reported_speed_kmh)

    # AUTO-PAUSE LOGIC
    if time.time() > _auto_pause_grace_until:
        if belt_running and new_reported_speed_kmh == 0 and current_speed_kmh > 0:
            logging.info("Belt has stopped unexpectedly. Auto-pausing session.")
            
            # Use the OLDEST speed from history to ignore the deceleration phase.
            if speed_history:
                resume_speed_kmh = speed_history[0] # Use the first (oldest) item
            else:
                # Fallback if pause happens too quickly after starting
                resume_speed_kmh = MIN_SPEED_KMH

            belt_running = False

    # CUMULATIVE STATS LOGIC (is unchanged)
    # ...
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


async def _stats_monitor():
    """Active monitor: explicitly request a status packet every second."""
    global current_session_active_seconds
    logging.info("Stats monitor started")

    try:
        while belt_running:
            if belt_running:  # Double check, as belt_running can change between await calls
                current_session_active_seconds += 1

            try:
                status = await asyncio.wait_for(controller.ask_stats(), timeout=2.0)
                if status:
                    if isinstance(status, dict):
                        dist = status.get("dist", 0)
                        steps = status.get("steps", 0)
                        speed = status.get("speed", 0)
                    else:
                        dist = getattr(status, "dist", 0)
                        steps = getattr(status, "steps", 0)
                        speed = getattr(status, "speed", 0)
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
        if not loop.run_until_complete(_connect_to_pad()):
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

def _ensure_connection(timeout=3.0) -> bool:
    """Ensure a BLE connection is established before sending commands."""
    if connected:
        return True

    if not connecting:
        _start_ble_thread()

    end_time = time.time() + timeout
    while time.time() < end_time:
        if connected:
            return True
        time.sleep(0.1)
    return False

def _handle_disconnect(client):
    """Callback function to handle unexpected disconnections."""
    global connected, belt_running, connecting, connection_failed, _stats_monitor_task
    if connected:
        logging.warning("Device has disconnected unexpectedly.")
    connected = False
    belt_running = False
    connecting = False
    connection_failed = True

    # Cancel any running stats monitor task
    if _stats_monitor_task and not _stats_monitor_task.done():
        logging.info("Cancelling stats monitor due to disconnect")
        _stats_monitor_task.cancel()

# ── Flask routes ────────────────────────────────────────────────────────
@app.route("/")
def root():
    if not connected:
        return render_template("connecting.html") #

    time_active_display = "0:00:00" # Default for start/paused if not running
    if session_active: # Only calculate if a session is or was active
        time_active_display = format_seconds_to_hms(current_session_active_seconds)

    if not session_active:
        # For start_session, always show 0 time initially
        return render_template("start_session.html", time_active="0:00:00")

    template = "active_session.html" if belt_running else "paused_session.html"

    return render_template(
        template,
        speed=current_speed_kmh * KMH_TO_MPH,
        distance=current_distance_km * KM_TO_MI,
        steps=current_steps,
        calories=current_calories,
        time_active=time_active_display 
    )


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

    if not _ensure_connection():
        return redirect(url_for("root"))

    current_distance_km = current_steps = current_calories = 0.0
    current_session_active_seconds = 0
    resume_speed_kmh = 2.0
    speed_history.clear() 

    session_active = True
    belt_running = True

    async def seq():
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
        asyncio.run_coroutine_threadsafe(seq(), ble_loop)
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
    global belt_running, _auto_pause_grace_until, session_active, _stats_monitor_task

    if not session_active:
        logging.warning("Resume called but no active session.")
        return redirect(url_for("root"))

    if belt_running:
        logging.info("Resume called but belt is already running.")
        return redirect(url_for("root"))

    logging.info("Resume button clicked. Setting app state to active.")
    belt_running = True
    _auto_pause_grace_until = time.time() + 7

    async def seq():
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
        asyncio.run_coroutine_threadsafe(seq(), ble_loop)
    except Exception as exc:
        logging.error(f"Failed to queue resume sequence: {exc}")
        belt_running = False
        return redirect(url_for("root"))

    return redirect(url_for("root"))


# ── Speed Controls ───────────────────────────────────────────────────────
@app.route("/decrease_speed")
def decrease_speed():
    """Decrease the belt speed by one step."""
    if not belt_running or not _ensure_connection():
        return redirect(url_for("root"))

    new_speed_kmh = max(MIN_SPEED_KMH, current_speed_kmh - SPEED_STEP)
    dev_speed = int(new_speed_kmh * 10)
    asyncio.run_coroutine_threadsafe(controller.change_speed(dev_speed), ble_loop)
    return redirect(url_for("root"))

@app.route("/slow_speed")
def slow_speed():
    """Set the belt speed to a predefined slow walk speed."""
    if not belt_running or not _ensure_connection():
        return redirect(url_for("root"))
    
    dev_speed = int(SLOW_WALK_SPEED_KMH * 10)
    asyncio.run_coroutine_threadsafe(controller.change_speed(dev_speed), ble_loop)
    return redirect(url_for("root"))

@app.route("/increase_speed")
def increase_speed():
    """Increase the belt speed by one step."""
    if not belt_running or not _ensure_connection():
        return redirect(url_for("root"))

    new_speed_kmh = min(MAX_SPEED_KMH, current_speed_kmh + SPEED_STEP)
    dev_speed = int(new_speed_kmh * 10)
    asyncio.run_coroutine_threadsafe(controller.change_speed(dev_speed), ble_loop)
    return redirect(url_for("root"))


@app.route("/max_speed")
def max_speed():
    """Set the belt speed to maximum."""
    if not belt_running or not _ensure_connection():
        return redirect(url_for("root"))

    dev_speed = int(MAX_SPEED_KMH * 10)
    asyncio.run_coroutine_threadsafe(controller.change_speed(dev_speed), ble_loop)
    return redirect(url_for("root"))


# ── Live JSON endpoint ───────────────────────────────────────────────────
@app.route("/stats", endpoint="get_stats")
def stats_json():
    # Calculate formatted_time_active within the function scope
    formatted_time_active = format_seconds_to_hms(current_session_active_seconds)

    data = dict(
        is_connected=connected,      
        is_running=belt_running,     
        speed=round(current_speed_kmh * KMH_TO_MPH, 1),
        distance=round(current_distance_km * KM_TO_MI, 2),
        steps=current_steps,
        calories=round(current_calories),
        time_active=formatted_time_active 
    )

    resp = make_response(jsonify(data))
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── Shutdown endpoint ──────────────────────────────────────────────────
@app.route("/shutdown", methods=['POST'])
def shutdown():
    """Forcefully shut down the Flask application process."""
    logging.info("Server shutting down via forceful exit...")
    os._exit(0)


# ── Kick off BLE thread ──────────────────────────────────────────────────
# The server is no longer started here. This just pre-starts the BLE thread.
_start_ble_thread()
