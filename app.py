import asyncio
import atexit
import contextlib
import csv
import io
import json
import logging
import os
import queue
import signal
import socket
import threading
import time
import urllib.parse
from collections import deque
from datetime import datetime, timedelta

from bleak import BleakScanner
from flask import (
    Flask,
    Response,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from markupsafe import Markup
from ph4_walkingpad.pad import Controller, WalkingPad

import config
import storage
from config import (
    APPLE_HEALTH_EXPORT_ENABLED,
    APPLE_HEALTH_SHORTCUT_NAME,
    BLE_DEVICE_NAME,
    HISTORY_DISPLAY_LIMIT,
    KCAL_PER_MILE,
    MAX_SPEED_KMH,
    MIN_SPEED_KMH,
    RESUME_GRACE_PERIOD_SECONDS,
    SLOW_WALK_SPEED_KMH,
    SPEED_STEP,
    STALE_PAUSE_TIMEOUT_MINUTES,
)
from samples import SampleBuffer, summary_from_samples
from units import KM_TO_MI, legacy_record

# ── Logging Setup ────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
)


def kcal_estimate(miles: float) -> float:
    return KCAL_PER_MILE * miles


def format_seconds_to_hms(total_seconds: int) -> str:
    """Converts total seconds to H:MM:SS string format."""
    total_seconds = int(total_seconds)
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
# Monotonic time of the last status packet that showed the belt moving during a
# walking segment; active time accrues from it. None = no open moving interval.
_last_moving_packet_monotonic: float | None = None
# A longer gap between packets is counted only up to this, so a stall or a
# report that arrives late after the belt stopped can't add much phantom time.
_MAX_MOVING_GAP_SECONDS = 5
_stats_monitor_task: asyncio.Task | None = None  # Track the stats monitor task
_idle_watchdog_task: asyncio.Task | None = None  # Track the paused/idle connection watchdog
_belt_sequence_task: asyncio.Task | None = None  # Track an in-flight start/resume/pause belt sequence
_belt_transitioning = False  # True while a belt sequence is in flight; exposed in /stats for UI
# Bumped per queued sequence; only the newest may clear _belt_transitioning, so a
# superseded or cancelled one can't re-enable the UI under a newer one.
_belt_transition_gen = 0
_belt_transition_lock = threading.Lock()
_auto_reconnect_task: asyncio.Task | None = None  # Track an in-flight auto-reconnect retry loop
_speed_change_task: asyncio.Task | None = None  # Track an in-flight _locked_change_speed() call
HISTORY_FILE = os.path.join(config.DATA_DIR, "session_history.json")  # legacy; migrated into the DB at startup
_CONFIG_FILE = config.CONFIG_FILE

_session_state_file_lock = threading.Lock()  # Protect session_state.json reads/writes
SESSION_STATE_FILE = os.path.join(config.DATA_DIR, "session_state.json")
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
# Auto-reconnect after an unexpected disconnect (see _handle_disconnect()/
# _auto_reconnect()): capped exponential backoff between attempts, bounded
# so a genuinely powered-off pad still reaches the manual "Try Again"
# screen instead of an endless spinner with no escape. Each attempt is
# itself a full _connect_to_pad() call with its own internal 3-scan retry
# (up to ~30s), so worst-case time to give up is the sum of both layers --
# roughly 7-8 minutes, not just the delays below on their own.
_MAX_RECONNECT_ATTEMPTS = 8
_RECONNECT_BASE_DELAY_SECONDS = 5
_RECONNECT_MAX_DELAY_SECONDS = 30
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
_session_id: str | None = None  # DB row for the in-progress session
_session_start_monotonic = 0.0  # monotonic clock at _session_start_time; sample t_ms origin
_samples = SampleBuffer()
_session_state_lock = threading.Lock()  # Protect session_active/belt_running check-then-set transitions
_shutting_down = False
_server_stopping = False  # Flag for UI to detect Ctrl+C / signal shutdown
_shutting_down_lock = threading.Lock()  # Protect shutdown state mutations


def _is_shutting_down() -> bool:
    with _shutting_down_lock:
        return _shutting_down


resume_speed_kmh = 2.0  # default if none yet

current_speed_kmh = current_distance_km = 0.0
current_steps = 0
current_calories = 0.0
current_session_active_seconds = 0

_last_dev_dist = _last_dev_steps = 0

# ── SSE broadcaster state ────────────────────────────────────────────────
_sse_subscribers: list[queue.Queue] = []
_sse_subscribers_lock = threading.Lock()  # Protect _sse_subscribers list mutations
_SSE_BROADCAST_INTERVAL_SECONDS = 1
# Bounds how long a subscriber's generator can block on q.get() with nothing
# to send. Well above the normal ~1s broadcast cadence, so this only ever
# fires if a tick is genuinely missed -- at which point sending a comment
# line forces a real write attempt on the socket, surfacing a dead/blackholed
# connection sooner than blocking indefinitely would.
_SSE_KEEPALIVE_TIMEOUT_SECONDS = 20


# ── Session History Persistence ────────────────────────────────────────

# Every storage call below is wrapped: a database failure must never stop
# the belt, block BLE handling, or break End Session / shutdown.

def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _begin_db_session(existing_id: str | None = None):
    """Attach the in-progress session to a DB row: reuse existing_id if it still
    exists (crash restore), else create one. Measures the sample clock from
    _session_start_time, so restored sessions keep counting from the real start."""
    global _session_id, _session_start_monotonic
    # Aware arithmetic: a naive difference is off by an hour if a DST change falls between a crash and its restore.
    elapsed_s = (datetime.now().astimezone() - _session_start_time.astimezone()).total_seconds()
    _session_start_monotonic = time.monotonic() - elapsed_s
    _samples.reset()
    try:
        if existing_id and storage.get_session(existing_id):
            _session_id = existing_id
        else:
            _session_id = storage.create_session(
                _session_start_time.astimezone().isoformat(timespec="seconds"), BLE_DEVICE_NAME, None,
            )
    except Exception:
        _session_id = None
        logging.exception("Failed to create session row")


def _record_sample(force: bool = False):
    if not session_active or _session_id is None:
        return
    t_ms = int((time.monotonic() - _session_start_monotonic) * 1000)
    speed_mps = current_speed_kmh / 3.6 if belt_running else 0.0
    _samples.add(t_ms, speed_mps, current_distance_km * 1000, current_steps, belt_running, force)


def _flush_samples():
    # Read once: End can clear _session_id on another thread mid-flush.
    session_id = _session_id
    rows = _samples.drain()
    if not rows or session_id is None:
        return
    try:
        storage.append_samples(session_id, rows)
    except Exception:
        logging.exception(f"Failed to write {len(rows)} samples")


def _record_pause(reason: str, at: str | None = None):
    if _session_id is None:
        return
    try:
        storage.add_pause(_session_id, at or _now_iso(), reason)
    except Exception:
        logging.exception("Failed to record pause")


def _last_sample_time() -> str | None:
    """Wall-clock time of the session's last stored sample: the last moment a
    crashed session is known to have been alive."""
    try:
        rows = storage.get_samples(_session_id) if _session_id else []
    except Exception:
        logging.exception("Failed to read samples")
        return None
    if not rows:
        return None
    at = _session_start_time.astimezone() + timedelta(milliseconds=rows[-1]["t_ms"])
    return at.isoformat(timespec="seconds")


def _sweep_orphaned_sessions(keep_id: str | None):
    """An 'active' row that session_state.json doesn't point at can never be
    restored (crash followed by Start or Discard-less restart). Complete it
    from its samples, or delete it if it has none."""
    try:
        for s in storage.list_active_sessions():
            if s["id"] == keep_id:
                continue
            rows = storage.get_samples(s["id"])
            if rows:
                storage.complete_session(s["id"], summary_from_samples(s["start_time"], rows, KCAL_PER_MILE))
                logging.info(f"Completed orphaned session {s['id']} from its last sample")
            else:
                storage.delete_session(s["id"])
                logging.info(f"Deleted orphaned session {s['id']} (no samples)")
    except Exception:
        logging.exception("Orphaned session sweep failed")


def _record_resume():
    if _session_id is None:
        return
    try:
        storage.end_pause(_session_id, _now_iso())
    except Exception:
        logging.exception("Failed to record resume")


def _save_session():
    """Complete the in-progress session's DB row with its final totals."""
    global _session_id
    if not session_active:
        return

    try:
        if _session_id is None:
            _begin_db_session()  # row creation failed at Start; try once more
        if _session_id is None:
            logging.error("Session not saved: no database row")
            return
        _record_sample(force=True)
        _flush_samples()
        start = _session_start_time.astimezone()
        end = datetime.now().astimezone()
        distance_m = current_distance_km * 1000
        storage.complete_session(_session_id, {
            "end_time": end.isoformat(timespec="seconds"),
            "elapsed_s": (end - start).total_seconds(),
            "moving_s": current_session_active_seconds,
            "distance_m": distance_m,
            "steps": current_steps,
            "calories_kcal": current_calories,
            "avg_speed_mps": distance_m / max(current_session_active_seconds, 1),
        })
        logging.info(f"Session {_session_id} saved to database")
    except Exception:
        logging.exception("Failed to save session to database")
    finally:
        _session_id = None


def _load_session_history(limit: int | None = None) -> list:
    """Completed sessions, most recent first, in the legacy record shape. Pass limit=None for all."""
    try:
        return [legacy_record(row) for row in storage.list_sessions(limit=limit)]
    except Exception:
        logging.exception("Failed to load session history")
        return []


def _clear_session_history():
    try:
        storage.clear_history()
        logging.info("Session history cleared")
    except Exception:
        logging.exception("Failed to clear session history")


# {"id", "status"} of the last Apple Health status change, pushed over SSE so other open
# pages (e.g. the desktop prompt while the phone logs) update without a reload.
_last_health_status_change = None


def _set_health_status(session_id, status) -> bool:
    """Only completed sessions have an Apple Health status; False for anything else or a failed write."""
    global _last_health_status_change
    try:
        row = storage.get_session(session_id)
        if not row or row["status"] != "completed":
            return False
        # A Dismiss from a page that hasn't seen the log yet must not undo it.
        if status == storage.HEALTH_DISMISSED and row["health_logged"] == storage.HEALTH_LOGGED:
            return True
        storage.set_health_status(session_id, status)
    except Exception:
        logging.exception("Failed to update Apple Health status")
        return False
    _last_health_status_change = {"id": session_id, "status": status}
    return True


def _dismiss_health_export(session_id=None):
    """Mark session_id (default: the most recent session) as no longer pending an Apple Health export."""
    if session_id is None:
        try:
            recent = storage.list_sessions(limit=1)
        except Exception:
            logging.exception("Failed to dismiss health export flag")
            return
        if not recent:
            return
        session_id = recent[0]["id"]
    _set_health_status(session_id, storage.HEALTH_DISMISSED)


# ── Session State Persistence (crash/restart recovery) ──────────────────

def _build_session_state_snapshot() -> dict:
    """Build a dict of the in-progress session's live state for crash recovery."""
    return {
        "session_active": session_active,
        "session_id": _session_id,
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
    """Write the current in-progress session to session_state.json (thread-safe, atomic).
    Also flushes buffered samples: this already runs every 5 s while walking and on
    every pause/resume, which is exactly the sample flush cadence we want."""
    if not session_active:
        return
    _flush_samples()
    snapshot = _build_session_state_snapshot()
    with _session_state_file_lock:
        tmp_path = f"{SESSION_STATE_FILE}.tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(snapshot, f, indent=2)
            os.replace(tmp_path, SESSION_STATE_FILE)
        except OSError as exc:
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
        except (OSError, ValueError) as exc:
            logging.warning(f"Failed to read session state, ignoring: {exc}")
            return None


def _clear_session_state():
    """Remove session_state.json once a session ends cleanly or is resolved (thread-safe)."""
    with _session_state_file_lock:
        try:
            if os.path.exists(SESSION_STATE_FILE):
                os.remove(SESSION_STATE_FILE)
        except OSError as exc:
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


# Tests set this so importing app can't touch the real database or Bluetooth pad.
_STARTUP_ENABLED = os.environ.get("WALKINGDAD_NO_STARTUP") != "1"

if _STARTUP_ENABLED:
    config.relocate_legacy_files()
    storage.init_db(os.path.join(config.DATA_DIR, config.DATABASE_PATH))
    storage.migrate_json(HISTORY_FILE, config.BACKUP_DIR)

    _pending_restore = _load_session_state()  # Check for an interrupted session at startup
    _sweep_orphaned_sessions((_pending_restore or {}).get("session_id"))


# ── Apple Health export (Shortcuts QR codes) ─────────────────────────────
# The setup QR points at an iCloud share link, not a repo-hosted file.
# Confirmed by hand (repeated Safari address-bar testing, every url=/name=
# encoding and ordering tried): shortcuts://import-shortcut?url=<raw GitHub
# file> reliably fails with "shortcut URL provided was invalid", a known,
# documented unreliability of GitHub-hosted .shortcut files with this scheme,
# not an encoding bug on WalkingDad's end. An iCloud share link (Share ->
# Copy iCloud Link in the Shortcuts app) is Apple's actual supported
# distribution path; every shortcut-sharing community, including RoutineHub,
# ultimately hands off to one of these under the hood. It's also just a
# plain https:// link, not the shortcuts:// scheme -- Apple's own "Get
# Shortcut" page at the other end handles the import itself.
_APPLE_HEALTH_SHORTCUT_ICLOUD_LINK = "https://www.icloud.com/shortcuts/fb023c69aa204562afff9587b0b6441a"


def _build_setup_shortcut_url() -> str:
    """URL for the one-time setup QR that installs the Shortcut. See the
    module-level comment above for why this is an iCloud link rather than a
    shortcuts://import-shortcut URL pointed at the repo's .shortcut file."""
    return _APPLE_HEALTH_SHORTCUT_ICLOUD_LINK


def _phone_reachable_base_url() -> str:
    """request.host_url, but with a loopback host (the desktop's usual
    http://localhost:<port>) swapped for this machine's LAN IP, since a phone
    opens this URL. Falls back to request.host_url if no route exists."""
    parts = urllib.parse.urlsplit(request.host_url)
    if parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        return request.host_url
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.254.254.254", 1))  # UDP connect only picks a route; nothing is sent.
            ip = s.getsockname()[0]
    except OSError:
        return request.host_url
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{ip}{port}/"


def _build_log_shortcut_url(session_record: dict, success_url: str) -> str:
    """shortcuts://x-callback-url/run-shortcut runs the already-installed
    Shortcut with that session's data embedded directly in the URL, then opens
    success_url once it finishes, which marks the session logged. Percent-encodes
    name= and the JSON text= payload (which has its own
    unsafe characters -- spaces, quotes, braces), but leaves ":" and "/" alone:
    a raw, un-percent-encoded space isn't valid inside a URI at all (unlike :
    and /, which are allowed unencoded in a query component per RFC 3986), and
    a literal space broke this scheme's name= handling during testing."""
    payload = json.dumps({
        "date": session_record["date"],
        "start_time": session_record["start_time"],
        "duration_seconds": session_record["duration_seconds"],
        "distance_km": session_record["distance_km"],
        "distance_mi": session_record["distance_mi"],
        "calories": session_record["calories"],
        "steps": session_record["steps"],
    })
    name = urllib.parse.quote(APPLE_HEALTH_SHORTCUT_NAME, safe=":/")
    text = urllib.parse.quote(payload, safe=":/")
    success = urllib.parse.quote(success_url, safe="")
    return f"shortcuts://x-callback-url/run-shortcut?name={name}&input=text&text={text}&x-success={success}"


def _health_log_url(session_record: dict, base_url: str) -> str:
    success_url = base_url.rstrip("/") + url_for("health_logged", session_id=session_record["id"])
    return _build_log_shortcut_url(session_record, success_url)


def hotkey(keys: str) -> Markup:
    """The aria-keyshortcuts attribute binding `keys`, or nothing when shortcuts
    are off, so screen readers aren't told about dead keys (WCAG 2.1.4 requires
    single-character shortcuts be switchable off). base.html derives the
    tooltip and the `?` list from it."""
    if not config.KEYBOARD_SHORTCUTS_ENABLED:
        return Markup("")
    return Markup(' aria-keyshortcuts="{}"').format(keys)


# ── Context processor so templates always know flags ────────────────────
@app.context_processor
def inject_flags():
    return {
        "connected": connected, "connecting": connecting, "connection_failed": connection_failed,
        "apple_health_shortcut_name": APPLE_HEALTH_SHORTCUT_NAME,
        "setup_shortcut_url": _build_setup_shortcut_url(),
        "keyboard_shortcuts_enabled": config.KEYBOARD_SHORTCUTS_ENABLED,
        "hotkey": hotkey,
        "belt_transitioning": _belt_transitioning,
    }


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


async def _safe_disconnect_client(client, context: str) -> None:
    """Best-effort BleakClient disconnect, shared by _connect_to_pad() (a
    stale client left over from a previous failed attempt) and
    _graceful_shutdown() (the live client on exit) -- same shape, same
    failure handling, just a different caller. Never raises: a disconnect
    failing here is logged, not fatal, since the caller is either about to
    replace the client anyway or the process is exiting regardless.
    """
    if not client:
        return
    try:
        if client.is_connected:
            await asyncio.wait_for(client.disconnect(), timeout=_BLE_WRITE_TIMEOUT_SECONDS)
    except Exception as exc:
        logging.warning(f"BLE disconnect error ({context}, non-fatal): {exc}")


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

    # Disconnect any live client left over from a previous attempt in this
    # same retry loop (e.g. controller.run() succeeded but a later step
    # below raised) before discarding the reference below -- otherwise a
    # retry loop can leak one open BLE connection per failed attempt, which
    # can make the pad refuse new connections.
    await _safe_disconnect_client(getattr(controller, "client", None), "stale controller before reconnect")

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
    global belt_running, resume_speed_kmh
    global current_speed_kmh, current_distance_km, current_steps, current_calories
    global _last_dev_dist, _last_dev_steps, _last_status_update_monotonic
    global current_session_active_seconds, _last_moving_packet_monotonic

    new_reported_speed_kmh = dev_speed / 10.0
    just_auto_paused = False

    # Active time is measured from the device's own reports, not from how long
    # the app believes the belt is running: an interval counts only if the
    # packet that opened it showed the belt moving. Evaluated before auto-pause
    # below so the interval ending in the stop is still counted.
    now = time.monotonic()
    walking = session_active and belt_running
    if walking and _last_moving_packet_monotonic is not None:
        current_session_active_seconds += min(now - _last_moving_packet_monotonic, _MAX_MOVING_GAP_SECONDS)

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
    # Fires on any zero reading once the grace window has passed, not only on a
    # moving-to-zero transition: a belt that stopped (or never started) inside
    # the window would otherwise leave the session "walking" indefinitely.
    if (time.time() > _resume_grace_deadline and not _belt_transitioning
            and belt_running and new_reported_speed_kmh == 0):
        logging.info("Belt has stopped unexpectedly. Auto-pausing session.")

        # Use the OLDEST speed from history to ignore the deceleration phase.
        if speed_history:
            resume_speed_kmh = speed_history[0] # Use the first (oldest) item
        elif current_speed_kmh > 0:
            # Walked only at or below MIN_SPEED_KMH, so nothing was recorded in history
            resume_speed_kmh = MIN_SPEED_KMH
        # else the belt never moved this segment: keep the speed it was asked for

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
    _last_moving_packet_monotonic = now if walking and new_reported_speed_kmh > 0 else None
    # Forced on auto-pause: the paused-rate throttle would otherwise drop the stop moment.
    _record_sample(force=just_auto_paused)

    # Stamped last, after all accumulation above has succeeded, so a
    # mid-function exception (e.g. malformed packet data) can't mark the
    # connection falsely "alive" and mask a real failure from the staleness
    # watchdog in _stats_monitor().
    _last_status_update_monotonic = time.monotonic()

    # Persist immediately on auto-pause, same guarantee as the manual /pause route.
    if just_auto_paused:
        _record_pause("auto")
        _save_session_state()


async def _graceful_shutdown():
    """Safely stop the treadmill, cancel monitors, and disconnect BLE before exit."""
    global connected, belt_running, session_active
    try:
        # Step 0: Cancel any in-flight auto-reconnect / belt sequence / speed
        # change first, before anything below touches `controller` -- all
        # three can reassign or act on it concurrently on this same loop
        # (auto-reconnect via _connect_to_pad()'s `controller = Controller()`),
        # and would otherwise race every step that follows.
        await _cancel_task(_auto_reconnect_task)
        await _cancel_task(_belt_sequence_task)
        await _cancel_task(_speed_change_task)

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
        if controller and getattr(controller, "client", None):
            logging.info("Disconnecting BLE client...")
            await _safe_disconnect_client(controller.client, "shutdown")
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
    global _last_status_update_monotonic
    logging.info("Stats monitor started")

    _ticks_since_save = 0
    _last_status_update_monotonic = time.monotonic()  # fresh grace period for this session

    try:
        while belt_running:
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
    """Cancel a task if it's still running and wait for it to finish.

    Shared by every module-level task this app tracks (stats monitor, idle
    watchdog, in-flight belt sequence) -- callers pass their own task
    variable; this never reassigns it (the global gets overwritten next
    time a new task is created). Waits for `task` only, not for a task that
    `task` was itself cancelling; _ble_command_lock and the belt-transition
    generation keep any such stragglers' side effects in order.
    """
    if task and not task.done():
        if task.get_loop() is not asyncio.get_running_loop():
            # Left over from a previous connection's loop, which is tearing it
            # down; cancel it there and don't wait across threads.
            with contextlib.suppress(RuntimeError):  # that loop already closed
                task.get_loop().call_soon_threadsafe(task.cancel)
            return
        task.cancel()
        # wait(), not `await task`: cancelling the caller mid-await would be
        # forwarded to `task`, which (like every belt sequence) may swallow it,
        # letting the caller run on as if never cancelled.
        await asyncio.wait({task})


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
    try_light_wake=False path -- if that can't be confirmed, so it's never
    less reliable than the full sequence, only sometimes gentler on the
    WalkingPad's own display.
    """
    async def _body():
        if not (try_light_wake and await _try_light_wake()):
            await _full_wake_sequence()

        if target_speed_kmh is not None:
            logging.info(f"Setting speed to {target_speed_kmh:.1f} km/h.")
            await asyncio.wait_for(controller.change_speed(round(target_speed_kmh * 10)), timeout=_BLE_WRITE_TIMEOUT_SECONDS)
            await asyncio.sleep(0.5)

    await _run_locked(_body, timeout=_BLE_SEQUENCE_LOCK_TIMEOUT_SECONDS)


async def _locked_change_speed(dev_speed: int):
    """change_speed(), serialized via _ble_command_lock so a manual speed
    adjustment can't interleave mid-write with the active stats monitor's
    concurrent ask_stats() poll on the same ble_loop.

    Fire-and-forget from the caller's side (scheduled via
    run_coroutine_threadsafe() with the returned Future discarded) -- so
    unlike the belt sequences, nothing else will ever observe or retry a
    failure here. Self-registers into _speed_change_task purely so
    _handle_disconnect() can cancel it if it's still waiting on the lock
    when a disconnect happens; otherwise, once auto-reconnect reassigns
    `controller` to a new client, this could wake up and send change_speed()
    to the wrong (newly-reconnected) device instead of failing safely as
    the paragraph above assumes.
    """
    global _speed_change_task
    # Registered before waiting on the predecessor, so a disconnect meanwhile
    # cancels this one too (the belt sequences do the same).
    prev, _speed_change_task = _speed_change_task, asyncio.current_task()
    try:
        await _cancel_task(prev)
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

    # Reset before publishing the loop, so a route that begins a transition
    # on the new loop can't have its flag cleared under its running sequence.
    _end_belt_transition()  # anything queued on the previous loop died with it
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


async def _auto_reconnect():
    """Retries _connect_to_pad() with capped exponential backoff after an
    unexpected disconnect. Runs on the same (still-alive) ble_loop rather
    than spawning a new thread/loop: _ble_thread() leaves that loop idling
    in run_forever() after a disconnect specifically so this has somewhere
    to run without a fresh event loop or _ble_command_lock. Gives up after
    _MAX_RECONNECT_ATTEMPTS and falls back to the existing manual "Try
    Again" screen -- there's no way to tell a powered-off pad apart from
    one temporarily out of BLE range, so a bounded retry is the one
    behavior that's reasonable for both.
    """
    global connected, connecting, connection_failed, _auto_reconnect_task
    _auto_reconnect_task = asyncio.current_task()

    for attempt in range(_MAX_RECONNECT_ATTEMPTS):
        if _is_shutting_down():
            return
        if attempt > 0:
            delay = min(_RECONNECT_BASE_DELAY_SECONDS * 2 ** (attempt - 1), _RECONNECT_MAX_DELAY_SECONDS)
            await asyncio.sleep(delay)
            if _is_shutting_down():
                return
        logging.info(f"Auto-reconnect attempt {attempt + 1}/{_MAX_RECONNECT_ATTEMPTS}")
        try:
            ok = await _connect_to_pad()
        except Exception as exc:
            logging.warning(f"Auto-reconnect attempt failed: {exc}")
            ok = False
        if ok:
            connected = True
            connecting = False
            logging.info("Auto-reconnect succeeded")
            return

    logging.error(f"Auto-reconnect gave up after {_MAX_RECONNECT_ATTEMPTS} attempts")
    connecting = False
    connection_failed = True


def _handle_disconnect(client):
    """Callback function to handle unexpected disconnections. Safe to call
    from any thread -- both the stats monitor and the idle watchdog call this
    on themselves when they detect a dead link (same-thread, on ble_loop),
    and bleak's own disconnect callback may call it from a different thread
    depending on platform/backend.
    """
    global connected, belt_running, connecting, connection_failed

    with _start_ble_thread_lock:
        # Idempotent: this can legitimately fire 2-3 times for the same
        # physical drop (bleak's own callback, a belt-sequence exception
        # handler, and a watchdog can all detect it independently). Without
        # this guard, each call after the first would re-schedule its own
        # _auto_reconnect() below, stacking up concurrent reconnect attempts
        # that race each other over controller/_device_ble_address.
        if not connected:
            return
        # Reject a stale callback from a superseded client -- bleak's own
        # callback can fire late for a client a fresh reconnect attempt has
        # already replaced; internal callers always pass client=None.
        if client is not None and client is not getattr(controller, "client", None):
            return
        logging.warning("Device has disconnected unexpectedly.")
        connected = False
        belt_running = False
        # Only promise the "CONNECTING" spinner if _auto_reconnect() below is
        # actually going to be scheduled -- otherwise (shutting down, or the
        # BLE loop is already gone/not running) there is nothing left to
        # flip connecting back to False, and the UI would be stuck on the
        # spinner forever with no route to the manual "Try Again" screen.
        will_auto_reconnect = (
            not _is_shutting_down()
            and ble_loop is not None
            and ble_loop.is_running()  # implies not closed
        )
        connecting = will_auto_reconnect
        connection_failed = not will_auto_reconnect

    # A drop stops the belt, so a walking session is now paused. No-op if a
    # pause is already open (e.g. the drop happened while paused).
    if session_active:
        _record_pause("auto")

    try:
        current = asyncio.current_task()
    except RuntimeError:
        current = None  # no running event loop in this thread

    # Cancel any running watchdog/sequence tasks, skipping self-cancellation:
    # cancelling a task from inside its own currently-running step still
    # marks it cancelled() even after it exits cleanly via `break`, which is
    # misleading -- that task is already unwinding on its own, no
    # cancellation needed.
    #
    # The in-flight belt sequence (if any) must be cancelled here too, not
    # just the two watchdogs: _auto_reconnect() below can reconnect and
    # reassign the global `controller` to a brand-new client while a stale
    # sequence is still suspended mid-await on the OLD controller (e.g.
    # between the sleep()s in a wake sequence) -- left alone, it would
    # resume by issuing belt commands against whichever controller happens
    # to be current by the time it wakes up, not the one it started with.
    #
    # task.cancel() itself is only safe to call from the thread running the
    # task's own event loop -- routed through call_soon_threadsafe() so this
    # is correct regardless of which thread actually called _handle_disconnect
    # (per the docstring above, that's not guaranteed to be ble_loop's own
    # thread), and onto the task's own loop, which after a manual reconnect
    # may be the previous connection's. Falls back to a direct call only if
    # that loop is already closed, leaving nothing to schedule onto anyway.
    for name, task in (
        ("stats monitor", _stats_monitor_task),
        ("idle watchdog", _idle_watchdog_task),
        ("belt sequence", _belt_sequence_task),
        ("speed change", _speed_change_task),
    ):
        if task and not task.done() and task is not current:
            logging.info(f"Cancelling {name} due to disconnect")
            try:
                task.get_loop().call_soon_threadsafe(task.cancel)
            except RuntimeError:  # loop closed
                task.cancel()

    if will_auto_reconnect:
        try:
            asyncio.run_coroutine_threadsafe(_auto_reconnect(), ble_loop)
        except Exception as exc:
            # Scheduling itself failed (e.g. the loop closed between the
            # check above and here) -- fall back to the manual screen rather
            # than leaving `connecting` stuck True with nothing left to
            # ever flip it back.
            logging.error(f"Failed to schedule auto-reconnect: {exc}")
            with _start_ble_thread_lock:
                connecting = False
                connection_failed = True


def _handle_signal_shutdown(signum, frame):
    """Handle SIGTERM/SIGINT by triggering graceful shutdown of the device."""
    global _shutting_down, _server_stopping
    with _shutting_down_lock:
        if _shutting_down:
            logging.info("Signal received but shutdown already in progress")
            return
        _shutting_down = True

    # Set UI-visible flag early so the next /stats_stream tick can inform the browser
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

    # Give the browser ~2 s to receive the last /stats_stream frame (stopping: true),
    # render the shutdown message, and then force-kill the process.
    time.sleep(2)
    os._exit(0)


# ── CSRF guard (ROADMAP 2.2) ────────────────────────────────────────────
@app.before_request
def _block_cross_site_posts():
    """LAN devices are trusted by design (the app is LAN-only); this only stops
    another website open in a browser from driving the treadmill. Browsers
    send Origin on every cross-origin POST, including plain form submits;
    Sec-Fetch-Site still catches it when an extension or proxy strips Origin.
    "same-site" is rejected too: another port on the same host counts as same-site."""
    if request.method in ("GET", "HEAD"):
        return None
    origin = request.headers.get("Origin")
    if (request.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site")
            or (origin and origin != request.host_url.rstrip("/"))):
        return "Cross-site request blocked.", 403
    return None


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
        # Deliberately a separate query rather than reusing history[0]: with
        # history_display_limit set to 0, history is [] and would silently
        # (and wrongly) disable the Apple Health prompt too, an unrelated
        # display-count setting reaching into a feature it has nothing to do
        # with.
        most_recent_session = _load_session_history(limit=1)
        pending_health_export = (
            APPLE_HEALTH_EXPORT_ENABLED
            and bool(most_recent_session)
            and most_recent_session[0]["health_status"] == storage.HEALTH_PENDING
        )
        if APPLE_HEALTH_EXPORT_ENABLED:
            base_url = _phone_reachable_base_url()
            for record in history:
                if record["health_status"] != storage.HEALTH_LOGGED:
                    record["log_shortcut_url"] = _health_log_url(record, base_url)
        log_shortcut_url = _health_log_url(most_recent_session[0], base_url) if pending_health_export else None

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
            pending_health_export=pending_health_export, log_shortcut_url=log_shortcut_url,
            health_session_id=most_recent_session[0]["id"] if pending_health_export else None,
            health_export_enabled=APPLE_HEALTH_EXPORT_ENABLED,
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
def _begin_belt_transition() -> int:
    """Mark the belt transitioning from the route, not when the sequence first runs
    on ble_loop, so the page the route redirects to already renders its buttons
    disabled. Returns the generation the sequence passes to _end_belt_transition()."""
    global _belt_transitioning, _belt_transition_gen
    with _belt_transition_lock:
        _belt_transition_gen += 1
        _belt_transitioning = True
        return _belt_transition_gen


def _end_belt_transition(gen: int | None = None):
    """Clear the flag if `gen` is still the newest sequence; None clears it
    unconditionally (a new BLE loop: nothing from the old one is in flight)."""
    global _belt_transitioning, _belt_transition_gen
    with _belt_transition_lock:
        if gen is None:
            _belt_transition_gen += 1
        elif gen != _belt_transition_gen:
            return
        _belt_transitioning = False


def _queue_belt_sequence(coro, gen: int):
    """Schedule a belt sequence; on failure clear its transition and re-raise."""
    try:
        asyncio.run_coroutine_threadsafe(coro, ble_loop)
    except Exception:
        _end_belt_transition(gen)
        raise


def _end_session(stale: bool = False):
    """Save the session to history, reset counters. stale=True (auto-end of a long
    pause) re-checks under the lock so a Resume racing the timer wins."""
    global session_active, belt_running, current_distance_km, current_steps, current_speed_kmh
    global current_calories, current_session_active_seconds, _session_start_time

    with _session_state_lock:
        if not session_active or (stale and belt_running):
            return

        was_running = belt_running
        belt_running = False

        # Cancel any in-flight belt sequence and monitor, then stop the belt.
        # Without cancelling, an in-flight resume sequence could keep running
        # after end_session returns, recreating a monitor for a dead session.
        async def _end_belt_sequence():
            global _belt_sequence_task
            # Registers itself as _belt_sequence_task like its three siblings
            # (start/pause/resume), so an in-flight End Session can be observed
            # and cancelled the same way as the others. Registering before
            # awaiting the predecessor's cancellation means a sequence queued
            # meanwhile cancels this one instead of running alongside it.
            prev, _belt_sequence_task = _belt_sequence_task, asyncio.current_task()
            # A Pause cancelled here may not have sent its stop_belt yet (and a
            # Start/Resume may have started the belt), so stop it ourselves.
            interrupted = prev is not None and not prev.done()
            try:
                await _cancel_task(prev)
                await _cancel_task(_stats_monitor_task)
                if (was_running or interrupted) and controller:
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
                if _belt_sequence_task is asyncio.current_task():
                    _belt_sequence_task = None
                _end_belt_transition(gen)

        # Guarded so a dead loop can't skip the history save below.
        if ble_loop and not ble_loop.is_closed():
            gen = _begin_belt_transition()
            try:
                _queue_belt_sequence(_end_belt_sequence(), gen)
            except Exception as exc:  # the loop closed after the check above
                logging.error(f"Failed to queue end sequence: {exc}")

        # Save session to history
        _save_session()
        _clear_session_state()
        logging.info(f"Session ended {'after stale pause' if stale else 'by user'}, saved to history")

        # Reset all counters
        current_distance_km = current_calories = 0.0
        current_speed_kmh = 0.0
        current_steps = 0
        current_session_active_seconds = 0
        speed_history.clear()
        session_active = False
        _session_start_time = None


@app.route("/end_session", methods=["POST"])
def end_session():
    _end_session()
    return redirect(url_for("root"))


_paused_since: float | None = None


def _end_session_if_stale_pause():
    """Called every broadcaster tick. Covers every way a session ends up paused
    (manual, auto, disconnect, restore) without hooking each one."""
    global _paused_since
    if not session_active or belt_running:
        _paused_since = None
        return
    # Wall clock, not monotonic: monotonic stops during system sleep, and a laptop
    # closed overnight with a paused session is the main case this exists for.
    now = time.time()
    if _paused_since is None:
        _paused_since = now
    elif STALE_PAUSE_TIMEOUT_MINUTES > 0 and now - _paused_since >= STALE_PAUSE_TIMEOUT_MINUTES * 60:
        _paused_since = None
        _end_session(stale=True)


# ── Export CSV ──────────────────────────────────────────────────────────
@app.route("/export_csv")
def export_csv():
    """Export full session history as a CSV download."""
    history = _load_session_history()
    si = io.StringIO()
    writer = csv.writer(si)
    # Legacy columns first, in their original order, so existing spreadsheets keep working.
    columns = ["date", "start_time", "end_time", "duration_seconds",
               "distance_km", "distance_mi", "steps", "calories",
               "avg_speed_kmh", "avg_speed_mph", "id", "has_samples"]
    writer.writerow(columns)
    for row in history:
        writer.writerow([row[c] for c in columns])

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
    """Dismiss the prompt's session (sent by the banner), so a session that ended
    meanwhile on another device isn't dismissed in its place."""
    _dismiss_health_export(request.form.get("session_id"))
    return jsonify({"status": "dismissed"})


@app.route("/delete_session/<session_id>", methods=["POST"])
def delete_session(session_id):
    """Delete one completed session (and its pauses/samples). Never an
    in-progress or awaiting-restore one, same rule as Clear History."""
    try:
        row = storage.get_session(session_id)
        if not row or row["status"] != "completed":
            return jsonify({"status": "not_found"}), 404
        storage.delete_session(session_id)
    except Exception:
        logging.exception("Failed to delete session")
        return jsonify({"status": "error"}), 500
    return jsonify({"status": "deleted"})


@app.route("/health_logged/<session_id>")
def health_logged(session_id):
    """x-success target of the Log to Apple Health link. A GET because Shortcuts
    opens it as a Safari navigation; the CSRF guard skips GETs, so another
    website could at most mark one session as logged."""
    if not _set_health_status(session_id, storage.HEALTH_LOGGED):
        return "Session not found, or it couldn't be marked as logged.", 404
    return redirect(url_for("root"))


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
    ("stale_pause_timeout_minutes", "STALE_PAUSE_TIMEOUT_MINUTES", lambda v, cur: max(int(v), 0), True),
    ("host", "HOST", lambda v, cur: v.strip() or cur, False),
    ("port", "PORT", lambda v, cur: int(v), False),
    ("waitress_threads", "WAITRESS_THREADS", _clamp_waitress_threads, False),
    ("apple_health_shortcut_name", "APPLE_HEALTH_SHORTCUT_NAME", lambda v, cur: v.strip() or cur, True),
]


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    global APPLE_HEALTH_EXPORT_ENABLED
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

        # Unlike every field above, a checkbox is absent from form data
        # entirely when unchecked rather than submitting a falsy value, so
        # the generic default-to-current-value loop above can't handle it --
        # the two boolean settings are handled as one-offs rather than
        # complicating _SETTINGS_SCHEMA's cast-function contract for them.
        was_enabled = APPLE_HEALTH_EXPORT_ENABLED
        updates["apple_health_export_enabled"] = "apple_health_export_enabled" in request.form
        updates["keyboard_shortcuts_enabled"] = "keyboard_shortcuts_enabled" in request.form

        _write_config(updates)
        for form_key, const_name, _cast, live in _SETTINGS_SCHEMA:
            setattr(config, const_name, updates[form_key])
            if live:
                globals()[const_name] = updates[form_key]
        config.APPLE_HEALTH_EXPORT_ENABLED = updates["apple_health_export_enabled"]
        APPLE_HEALTH_EXPORT_ENABLED = updates["apple_health_export_enabled"]
        config.KEYBOARD_SHORTCUTS_ENABLED = updates["keyboard_shortcuts_enabled"]

        # Turning the feature on shouldn't retroactively surface a session
        # that predates it being enabled (pending_health_export only checks
        # whether the most recent session has ever been logged/dismissed,
        # with no notion of "before vs. after enabling"). But don't
        # blanket-suppress on every enable either -- someone finishing a walk
        # and enabling the feature specifically to log THAT walk shouldn't
        # have it silently marked as already handled before they ever see
        # the banner. Split the difference: only pre-dismiss if the most
        # recent session isn't from today, since "today's most recent
        # session" is the one case where enabling right after a walk is a
        # plausible, common reason to be here at all.
        if updates["apple_health_export_enabled"] and not was_enabled:
            recent = _load_session_history(limit=1)
            if recent and recent[0].get("date") != datetime.now().strftime("%Y-%m-%d"):
                _dismiss_health_export()

        return redirect(url_for("root", saved=1))

    return render_template(
        "settings.html",
        apple_health_export_enabled=APPLE_HEALTH_EXPORT_ENABLED,
        **{form_key: getattr(config, const_name) for form_key, const_name, _, _ in _SETTINGS_SCHEMA},
    )


@app.route("/reconnect", methods=["POST"])
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
        # Reuses the crashed session's row; state files from before session_id existed get a new one.
        _begin_db_session(state.get("session_id"))
        _record_pause("shutdown", at=_last_sample_time())

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
        discarded_id = (_pending_restore or {}).get("session_id")
        _pending_restore = None
        _clear_session_state()
    if discarded_id:
        try:
            storage.delete_session(discarded_id)
        except Exception:
            logging.exception("Failed to delete discarded session row")
    return redirect(url_for("root"))


@app.route("/start", methods=["POST"])
def start_session():
    """Begin a new session: reset counters, start belt, launch stats monitor."""
    global session_active, belt_running, current_distance_km, current_steps, current_calories, resume_speed_kmh
    global current_session_active_seconds, _session_start_time, current_speed_kmh
    global _resume_grace_deadline, _pending_restore, _last_moving_packet_monotonic

    if not connected:
        return redirect(url_for("root"))

    with _session_state_lock:
        if session_active:
            return redirect(url_for("root"))

        current_distance_km = current_calories = 0.0
        current_steps = 0
        current_speed_kmh = 0.0
        current_session_active_seconds = 0
        _last_moving_packet_monotonic = None
        resume_speed_kmh = 2.0
        speed_history.clear()
        _session_start_time = datetime.now()
        _resume_grace_deadline = time.time() + RESUME_GRACE_PERIOD_SECONDS
        _begin_db_session()

        session_active = True
        belt_running = True
        _pending_restore = None  # A fresh session supersedes any unresolved restore prompt
        _save_session_state()

        gen = _begin_belt_transition()

        async def _start_belt_sequence():
            global belt_running, _stats_monitor_task, _belt_sequence_task
            global _resume_grace_deadline
            prev, _belt_sequence_task = _belt_sequence_task, asyncio.current_task()
            try:
                await _cancel_task(prev)
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
                if _belt_sequence_task is asyncio.current_task():
                    _belt_sequence_task = None
                _end_belt_transition(gen)
                # Re-stamped here, not just once when the button was clicked:
                # _wake_and_start_belt() can legitimately run long on a slow
                # connection, which would otherwise eat into (or exhaust) the
                # grace window this is meant to give the belt to physically
                # reach speed *after* the sequence completes -- see
                # process_status_packet()'s AUTO-PAUSE LOGIC comment.
                _resume_grace_deadline = time.time() + RESUME_GRACE_PERIOD_SECONDS

        try:
            _queue_belt_sequence(_start_belt_sequence(), gen)
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
        _record_pause("manual")
        _record_sample(force=True)  # mark the stop moment before the paused-rate throttle applies
        _save_session_state()

        # Single ordered sequence on ble_loop: cancel monitor before stop_belt()
        # so an immediate resume can't interleave with the monitor's in-flight polls.
        gen = _begin_belt_transition()

        async def _pause_belt_sequence():
            global _belt_sequence_task
            # Cancel any prior belt sequence before self-registering.
            prev, _belt_sequence_task = _belt_sequence_task, asyncio.current_task()
            try:
                await _cancel_task(prev)
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
                if _belt_sequence_task is asyncio.current_task():
                    _belt_sequence_task = None
                _end_belt_transition(gen)

        try:
            _queue_belt_sequence(_pause_belt_sequence(), gen)
        except Exception as exc:
            logging.error(f"Failed to queue pause sequence: {exc}")

    return redirect(url_for("root"))


@app.route("/resume", methods=["POST"], endpoint="resume")
@app.route("/resume_session", methods=["POST"], endpoint="resume_session")
def resume_session():
    global belt_running, _resume_grace_deadline, _last_moving_packet_monotonic

    with _session_state_lock:
        if not session_active:
            logging.warning("Resume called but no active session.")
            return redirect(url_for("root"))

        if belt_running:
            logging.info("Resume called but belt is already running.")
            return redirect(url_for("root"))

        logging.info("Resume button clicked. Setting app state to active.")
        # Clear any marker left from before the pause, so the paused gap isn't
        # counted when the first post-resume packet arrives.
        _last_moving_packet_monotonic = None
        belt_running = True
        _resume_grace_deadline = time.time() + RESUME_GRACE_PERIOD_SECONDS
        _record_resume()
        _save_session_state()

        gen = _begin_belt_transition()

        async def _resume_belt_sequence():
            global belt_running, _stats_monitor_task, _belt_sequence_task
            global _resume_grace_deadline
            # Cancel any prior in-flight sequence (e.g. pause) before sending
            # commands; concurrent coroutines on ble_loop interleave BLE writes.
            prev, _belt_sequence_task = _belt_sequence_task, asyncio.current_task()
            try:
                await _cancel_task(prev)
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
                if _belt_sequence_task is asyncio.current_task():
                    _belt_sequence_task = None
                _end_belt_transition(gen)
                # See _start_belt_sequence()'s matching comment: re-stamped here
                # so a slow wake-up sequence can't eat into the post-completion
                # ramp-up protection this deadline is meant to provide.
                _resume_grace_deadline = time.time() + RESUME_GRACE_PERIOD_SECONDS

        try:
            _queue_belt_sequence(_resume_belt_sequence(), gen)
        except Exception as exc:
            logging.error(f"Failed to queue resume sequence: {exc}")
            belt_running = False
            return redirect(url_for("root"))

    return redirect(url_for("root"))


# ── Speed Controls ───────────────────────────────────────────────────────
@app.route("/decrease_speed", methods=["POST"])
def decrease_speed():
    """Decrease the belt speed by one step."""
    return _set_preset_speed(max(MIN_SPEED_KMH, current_speed_kmh - SPEED_STEP))


def _set_preset_speed(speed_kmh: float):
    """Shared body for the fixed-speed presets (min/slow/max) below."""
    # Mid start/resume the reported speed is ~0 and the sequence sets its own
    # speed; a step queued now would land right after it (the UI's buttons are
    # disabled then too, but not on a second device).
    # Under the lock the belt routes hold, so a Pause/End can't land between
    # the check and the queueing, leaving this step to follow its stop_belt.
    with _session_state_lock:
        if not belt_running or _belt_transitioning:
            return redirect(url_for("root"))

        dev_speed = round(speed_kmh * 10)  # not int(): float sums like (3.8 + 0.6) * 10 land at 43.99...
        try:
            asyncio.run_coroutine_threadsafe(_locked_change_speed(dev_speed), ble_loop)
        except Exception as exc:
            logging.error(f"Failed to queue speed change: {exc}")
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
    return _set_preset_speed(min(MAX_SPEED_KMH, current_speed_kmh + SPEED_STEP))


@app.route("/max_speed", methods=["POST"])
def max_speed():
    """Set the belt speed to maximum."""
    return _set_preset_speed(MAX_SPEED_KMH)


# ── Live JSON endpoint ───────────────────────────────────────────────────
def _build_stats_payload() -> dict:
    """Build the stats snapshot dict from current global state.

    Single source of truth for the wire payload shape, shared by the
    /stats endpoint and the SSE broadcaster.
    """
    return {
        "is_connected": connected,
        "connection_failed": connection_failed,
        "session_active": session_active,
        "is_running": belt_running,
        "belt_transitioning": _belt_transitioning,
        "speed": round(current_speed_kmh * KM_TO_MI, 1),
        "distance": round(current_distance_km * KM_TO_MI, 2),
        "steps": current_steps,
        "calories": round(current_calories),
        "time_active": format_seconds_to_hms(current_session_active_seconds),
        "stopping": _server_stopping,
        "health_status_changed": _last_health_status_change,
    }


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
            _end_session_if_stale_pause()
        except Exception as exc:
            logging.error(f"Stale-pause check failed (continuing): {exc}")
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

    # Set UI-visible flag immediately so the next /stats_stream tick informs the browser
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
        time.sleep(5)  # Give browser time to receive a /stats_stream frame with stopping:true
        logging.info("Exiting process after graceful shutdown...")
        os._exit(0)

    threading.Thread(target=_deferred_exit, daemon=True).start()
    return resp


# ── Atexit handler as safety net ────────────────────────────────────────
def _atexit_cleanup():
    """Safety net: attempt to stop the belt and disconnect BLE on process exit."""
    if _shutting_down:
        return  # Already handled gracefully
    logging.info("atexit: performing emergency cleanup...")
    if ble_loop and not ble_loop.is_closed():
        try:
            asyncio.run_coroutine_threadsafe(_graceful_shutdown(), ble_loop).result(timeout=5)
        except Exception as exc:
            logging.error(f"atexit cleanup error: {exc}")


# ── Startup: exit handlers (Ctrl+C / SIGTERM / atexit), BLE thread, SSE broadcaster ──
# The web server itself is started by run.py. Skipped under WALKINGDAD_NO_STARTUP=1 (tests).
if _STARTUP_ENABLED:
    signal.signal(signal.SIGTERM, _handle_signal_shutdown)
    signal.signal(signal.SIGINT, _handle_signal_shutdown)
    atexit.register(_atexit_cleanup)
    _start_ble_thread()
    _start_sse_broadcaster()
