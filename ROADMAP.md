# Roadmap

Organized by theme (Area), not build order. Work has never moved through these top to bottom, so grouping by "priority phase" stopped meaning anything. Each item carries its own **Priority** (High/Medium/Low) independent of which Area it's filed under.

## Reliability & Safety

### ✅ 1.1 macOS / Cross-Platform BLE Reliability Fixes
**Status:** ✅ Complete
**Priority:** High
**Files Modified:** `app.py`

A comprehensive set of reliability improvements for Bluetooth Low Energy communication across all platforms (macOS, Windows, Linux). Implemented before the ROADMAP existed.

| Fix | Description |
|---|---|
| **Context Manager Scanning** | Replaced unreliable `BleakScanner.find_device_*()` static methods with `async with BleakScanner()` context manager, more stable on macOS CoreBluetooth and consistent across all platforms |
| **Retry with Exponential Backoff** | 3-retry mechanism (up to 10s wait) handles transient Bluetooth connectivity issues; searches by cached MAC address and device name |
| **Event Loop Cleanup** | Proper task cancellation before loop close prevents resource leaks and crashes on all platforms |
| **Stats Monitor Robustness** | `asyncio.wait_for()` with 2s timeout, proper cancellation handling, no event-loop crashes on errors |
| **Async Sequence Error Handling** | `start_belt()`, `resume_session()` now have try-catch; failures update app state and trigger disconnect handling |
| **Thread-Safe Coroutine Execution** | Error handling on `run_coroutine_threadsafe()` calls; proper state management if queueing fails |
| **Configurable Device Name** | Device name configurable without touching scanner logic; initially a constant in `app.py`, now via `config.json` / Settings page (2.1) |
| **Bleak API Version Compatibility** | Supports both `set_disconn_callback()` (newer) and `set_disconnected_callback()` (older), working across Bleak versions |
| **Stats Monitor Lifecycle Fix** | Global `_stats_monitor_task` tracks active monitor; old tasks cancelled before new ones on resume; cleaned up on disconnect. Fixes metrics-not-updating-after-pause/resume bug |

**Compatibility:** macOS 12+, Windows, Linux · Bleak 0.19+ · Python 3.10+

---

### ✅ 1.2 Graceful Shutdown
**Status:** ✅ Complete
**Priority:** High
**Files Modified:** `app.py`, `run.py`, `templates/*.html`

Multi-layer graceful shutdown ensuring the treadmill belt always stops and BLE disconnects cleanly, regardless of how the process exits. Includes a web UI notification so the user knows shutdown is in progress.

| Layer | Trigger | Description |
|---|---|---|
| **HTTP `/shutdown` route** | User clicks "Close" in the UI, or `run.py` calls it on Ctrl+C | Sets `_server_stopping = True`, awaits `_graceful_shutdown()` via `fut.result(timeout=10)`, stops the BLE event loop, returns HTTP response, then a background thread sleeps 5s and exits via `os._exit(0)` (Waitress suppresses `sys.exit()`) |
| **Signal handlers (`SIGTERM`, `SIGINT`)** | Direct Ctrl+C on Waitress process (rare) | Same cleanup: sets `_server_stopping = True`, stops belt, standby mode, cancels monitor, disconnects BLE, stops event loop, then 2s delay + `os._exit(0)` |
| **`atexit` safety net** | Any process exit not caught by the above layers | Last-resort cleanup that calls `_graceful_shutdown()` with a 5-second timeout to ensure the belt is stopped even on unexpected exits |

Shutdown coroutine (`_graceful_shutdown()`) steps:
1. **Stop Belt**: If `belt_running`, call `controller.stop_belt()` and wait 0.5s
2. **Cancel Monitors**: Cancel `_stats_monitor_task` and `_idle_watchdog_task`, awaiting each `CancelledError`
3. **Standby Mode**: Switch device to `WalkingPad.MODE_STANDBY`
4. **BLE Disconnect**: Call `controller.client.disconnect()` to close the connection cleanly

Additional fixes:
- Duplicate shutdown requests are ignored via thread-safe `_shutting_down` flag protected by `threading.Lock()`
- Uses `os._exit(0)` (not `sys.exit(0)`) for reliable Waitress termination; Waitress catches and suppresses `SystemExit` from `sys.exit()`
- **UI shutdown notification**: all session templates watch the `/stats_stream` SSE connection for `stopping: true` and display "Server is shutting down" message
- **Process-isolated subprocess**: `run.py` launches Waitress via `os.setsid()` so Ctrl+C only hits the wrapper, not Waitress directly; gives the HTTP shutdown path time to complete
- Edge case handling when `ble_loop` or `controller` is None (skip BLE cleanup, exit directly)

---

### ✅ 1.3 Session State Persistence
**Status:** ✅ Complete
**Priority:** High
**Files Modified:** `app.py`, `templates/start_session.html`, `.gitignore`

Cumulative session stats now survive a server crash or restart. In-progress session state is written to `session_state.json` and offered back to the user on next launch instead of being silently lost.

| Feature | Description |
|---|---|
| **Periodic Save** | `_save_session_state()` writes distance/steps/calories/active-time/resume-speed/raw-device-counters to `session_state.json` every 5 seconds while the stats monitor is running |
| **State-Change Saves** | Also saved immediately on `/start`, `/pause`, and `/resume` so a crash right after a transition doesn't lose it |
| **Startup Detection** | On launch, `_load_session_state()` checks for a leftover file from an unclean exit; if found, it's held as a pending restore rather than auto-resumed (no BLE connection to trust yet) |
| **Restore Banner** | Start screen shows a summary (distance, steps, calories, time) of the interrupted session with **Restore Session** / **Discard** actions |
| **Restore Session** | `/restore_session` reinstates the session in **paused** state (belt physically stopped); user hits Resume to reconnect and continue, matching the existing pause/resume flow |
| **Clean-Exit Cleanup** | `session_state.json` is removed once a session is finalized normally: on `/end_session` (after saving to history) and during graceful shutdown (Ctrl+C / signal / `/shutdown`). A clean exit never triggers a restore prompt |
| **Fault Tolerant** | Corrupted or missing `session_state.json` is ignored gracefully |
| **Database Link** | The state file carries the session's database `session_id`, so Restore continues the same record and Discard deletes it; see 2.9 |

---

### ✅ 1.4 Automatic Reconnect on Disconnection
**Status:** ✅ Complete
**Priority:** High
**Files Modified:** `app.py`

An unexpected Bluetooth drop no longer goes straight to the manual "Try Again" screen. The app retries on its own, so walking briefly out of range or a transient glitch heals itself.

| Feature | Description |
|---|---|
| **Bounded Retry** | `_auto_reconnect()` retries `_connect_to_pad()` up to 8 times (`_MAX_RECONNECT_ATTEMPTS`): first attempt immediately, then waits of 5, 10, 20, then 30 s (capped). With each attempt's own scan, about 7-8 minutes in total before falling back to **Connection Failed / Try Again**. Bounded because a powered-off pad and one temporarily out of range look identical |
| **Same Event Loop** | Runs on the existing `ble_loop`, which `_ble_thread()` leaves idling after a disconnect; no new thread, loop, or lock |
| **UI State** | Reuses the existing `connecting` flag, so the console shows the connecting state during retries; no new template state |
| **Session Survives** | The drop pauses a walking session (recorded as an `auto` pause, see 2.9); after reconnecting, Resume continues it |
| **Race Safety** | In-flight belt sequences, speed changes, and reconnect attempts are cancelled before shutdown or before a new connection replaces `controller`, so a stale coroutine can't send commands to the wrong client |

Not configurable: retry counts and delays are module constants in `app.py`.

---

### ✅ 1.5 Auto-End Stale Paused Session
**Status:** ✅ Complete
**Priority:** Medium
**Files Modified:** `app.py`, `config.py`, `config.json.example`, `templates/paused_session.html`, `templates/settings.html`, `README.md`, `tests/test_stale_pause.py`

A session left paused (manual, auto, Bluetooth drop, or restored after a crash) longer than `stale_pause_timeout_minutes` (default 30, `0` disables) is ended exactly as if End Session were pressed: saved to history, state file cleared, back to the start screen.

| Feature | Description |
|---|---|
| **No New Timer** | `_end_session_if_stale_pause()` runs on the existing 1 s SSE broadcaster tick, so it covers every pause path without hooking each one. Timed on the wall clock, since `time.monotonic()` stops during system sleep and a laptop closed overnight is the main case |
| **Shared End Path** | `/end_session` and the auto-end both call `_end_session()`; the auto-end re-checks `belt_running` under the session lock so a Resume racing the timeout wins |
| **UI** | The stats payload now carries `session_active`; the paused screen reloads to the start screen when it flips false |

---

### 1.6 Port-in-Use Check on Startup
- **Status:** Planned
- **Priority:** Medium
- **Problem:** If port 5001 (or a configured alternate) is already taken, Waitress fails with an opaque bind error in the console instead of a clear explanation.
- **Solution:** Check the configured port is free before launching Waitress; if not, fail fast with a clear message naming the port and suggesting a fix (change `port` in Settings/`config.json`).
- **Implementation:** A quick `socket.bind()`/`connect_ex()` probe in `run.py` before spawning the Waitress subprocess.

---

## Security & Code Quality

### ✅ 2.1 External Configuration File
**Status:** ✅ Complete
**Priority:** High
**Files Modified:** `config.py`, `config.json.example`, `app.py`, `run.py`, `.gitignore`, `README.md`, `templates/base.html`, `templates/settings.html`

All user-tunable settings are now loaded from an optional `config.json` file, with `config.py` providing defaults. A Settings page (gear icon in the header) allows changing any setting from the browser without editing files. Most settings take effect immediately; host, port, and waitress_threads require a restart.

---

### 2.2 Route Security
- **Status:** Planned
- **Priority:** High
- **Problem:** Routes like `/start`, `/pause`, `/increase_speed` have no authentication or CSRF protection. Since the server binds to `0.0.0.0`, any device on the local network could control the treadmill.
- **Solution:** Add a simple secret token mechanism:
    - Generate a random token on startup (stored in config)
    - Require `?token=...` query parameter or `X-Auth-Token` header on all action routes
    - Display the token in the UI and use it automatically for frontend requests
- **Alternative:** Restrict server to `127.0.0.1` only (breaks network access but is simpler).

---

### 2.3 Configurable Logging Level
- **Status:** Planned
- **Priority:** Low
- **Problem:** `logging.basicConfig(level=logging.INFO)` is hardcoded. Users cannot enable verbose DEBUG output without editing code.
- **Solution:** Allow setting log level via environment variable (`LOG_LEVEL=DEBUG`) or command-line argument.
- **Implementation:** Read `LOG_LEVEL` from `os.environ` with a default of `INFO`, pass to `logging.basicConfig(level=...)`.

---

### 2.4 Self-Hosted Static Assets
- **Status:** Planned
- **Priority:** Medium
- **Problem:** Bootstrap, Bootstrap Icons, and Google Fonts all load from CDNs in `base.html`, so the UI visibly breaks without internet access (already called out in the README's "Icons missing" troubleshooting entry), at odds with the "runs locally, no cloud" pitch.
- **Solution:** Vendor Bootstrap CSS/JS, Bootstrap Icons, and the always-loaded Google Fonts (Noto Sans, IBM Plex Sans Condensed, JetBrains Mono) into a local `static/` directory and reference them relatively instead of via CDN. The per-special-theme fonts (Rubik/Crimson Text, Quicksand, Pacifico, Rye/Creepster, Mountains of Christmas), fetched only when that theme is selected, can stay CDN-loaded since they're already conditional, or get vendored too as a follow-on.
- **Implementation:** Download and pin the exact versions currently used, serve via Flask's default `static` route, update `base.html`'s `<link>`/`<script>` tags. Pure dependency removal, no functional change.

---

### 2.5 Unit Tests
- **Status:** In Progress
- **Priority:** Medium
- **Problem:** Most of `app.py` has no automated tests, making refactoring risky. Covered so far: `storage.py`, the JSON migration, `units.py`, `samples.py` (2.9), and in `app.py` the stale-pause check (1.5) plus `process_status_packet()`'s active-time accrual and auto-pause detection.
- **Solution:** Extend the suite to `app.py`'s logic:
    - `format_seconds_to_hms()` - time formatting edge cases
    - `kcal_estimate()` - calorie calculation
    - `process_status_packet()` - distance/steps accumulation and device counter resets, speed history management (mock BLE data)
- **Implementation:** Tests import `app.py` directly; `tests/conftest.py` sets `WALKINGDAD_NO_STARTUP=1` so the import skips opening `walkingdad.db`, the JSON migration, the orphan sweep, the BLE thread, and the SSE broadcaster.

---

### 2.6 Pin Dependency Versions
- **Status:** Planned
- **Priority:** Medium
- **Problem:** `requirements.txt` lists `bleak`, `flask`, `ph4-walkingpad`, and `waitress` with no version constraints. A fresh `pip install` can silently pull a breaking major version with no warning.
- **Solution:** Pin each dependency to a known-working version (exact `==` or a floor `>=` plus a documented upper bound), tested against the versions currently in use.
- **Implementation:** Capture current working versions from an active `venv` (`pip freeze`), add them to `requirements.txt`, note the tested versions in the README.

---

### 2.7 Continuous Integration
- **Status:** Planned
- **Priority:** Medium
- **Problem:** There's no GitHub Actions workflow; nothing runs automatically on push/PR, even though a `pytest` suite now exists (2.5).
- **Solution:** A GitHub Actions workflow that installs dependencies and runs the test suite plus a linter, on every push and PR.
- **Implementation:** `.github/workflows/ci.yml` running on `ubuntu-latest`, `pip install -r requirements.txt -r requirements-dev.txt`, then `pytest` and a linter (e.g. `ruff`).

---

### 2.8 Packaged Executable
- **Status:** Planned
- **Priority:** Low
- **Problem:** Running WalkingDad requires cloning the repo, creating a venv, and using a terminal, a barrier for a non-technical household member who just wants to double-click something.
- **Solution:** A packaged, double-clickable build (PyInstaller for Windows/Linux, py2app for macOS) that bundles the Python runtime and dependencies.
- **Implementation:** A build script producing a platform-specific bundle from `run.py`, published as a release asset. `start_app.bat` stays as the source-checkout path for anyone who prefers it.

---

### ✅ 2.9 SQLite Session Storage
**Status:** ✅ Complete
**Priority:** Medium
**Files Modified:** `app.py`, `storage.py`, `units.py`, `samples.py`, `config.py`, `config.json.example`, `.gitignore`, `tests/`

Session data lives in a local SQLite database (`walkingdad.db`, configurable via `database_path`) instead of a JSON file that was fully rewritten on every session end. The schema stores SI units and timezone-aware timestamps and maps onto FIT/TCX concepts, so it doubles as the groundwork for 3.10, 4.2-4.5, 5.2, and 5.3.

| Feature | Description |
|---|---|
| **Storage Module** | `storage.py` (stdlib `sqlite3`, no ORM) is the only place SQL lives. Short-lived connection per call, WAL mode, a module lock for writes |
| **Schema** | `sessions` (UUID `id`, `status` active/completed, `profile`, SI totals, moving vs. elapsed time, `health_logged`, `has_samples`), `pauses` (start/end, reason `manual`/`auto`/`shutdown`), `samples` (per-second `t_ms`, speed, cumulative distance/steps, belt state, reserved `hr_bpm`), `meta` (`schema_version`, migration marker) |
| **Live Lifecycle** | Row created at Start; pauses recorded at manual pause, step-off auto-pause, and Bluetooth drop; completed at End, stale-pause auto-end (1.5), or graceful shutdown |
| **Samples** | ~1/s while walking, at most 1 per 5 s while paused (in practice every idle-watchdog ping, ~10 s), plus one at the moment of each manual or auto pause, buffered in memory and flushed on the existing 5 s `_save_session_state()` cadence, so a crash loses at most ~5 s. Storage failures are logged, never allowed to stop the belt or block BLE handling |
| **Crash Recovery Link** | `session_state.json` carries the `session_id`: Restore continues the same row (downtime recorded as a `shutdown` pause), Discard deletes it. Unreferenced `active` rows are swept at startup: completed from their last sample, or deleted if they have none |
| **JSON Migration** | One-time, automatic, single transaction: backup to `session_history.json.bak-<timestamp>`, then rename to `.migrated`. Unparseable records are logged and skipped; an unreadable file is left untouched for a later retry |
| **Unchanged Surface** | `units.legacy_record()` rebuilds the pre-SQLite record shape, so the history table, Apple Health export, and CSV columns are identical (CSV gains trailing `id`, `has_samples`) |

---

### 2.10 Config / History Schema Versioning
- **Status:** Planned
- **Priority:** Medium
- **Problem:** `config.json` has no format version. Every past field addition has been handled ad hoc via `.get(key, default)`, which works but leaves no clean way to detect "this file predates feature X" versus "this field is just legitimately absent." (Session history already has one: 2.9's `meta.schema_version`.)
- **Solution:** Add a `schema_version` field to `config.json`, bumped whenever its shape changes, checked on load so a future migration has a clear branch point instead of guessing from field presence.
- **Implementation:** A `_CONFIG_SCHEMA_VERSION` constant written on save; on load, a missing/older version is treated as version 1 (today's implicit shape) and upgraded in place if a migration exists. The database side needs a migration runner only once `schema_version` 3 exists.

---

### 2.11 Dependency Vulnerability Scanning
- **Status:** Planned
- **Priority:** Medium
- **Problem:** Pinning versions (2.6) fixes what's installed but doesn't catch a known CVE in whatever gets pinned, or a new one disclosed later against an already-pinned version.
- **Solution:** Enable Dependabot security alerts on the repo, and/or run `pip-audit` against `requirements.txt` in CI (2.7).
- **Implementation:** A `.github/dependabot.yml` for version-update PRs plus security alerts; a `pip-audit` step added to the CI workflow from 2.7.

---

### 2.12 Integration Smoke Test
- **Status:** Planned
- **Priority:** Medium
- **Problem:** 2.5's unit tests cover pure functions, but nothing exercises the actual Start → Pause → Resume → End route flow end to end, so a template or routing regression could still slip through.
- **Solution:** One browser-driven smoke test that boots the app against a mocked BLE device/controller and clicks through the full session lifecycle, asserting each screen renders and each transition lands where expected.
- **Implementation:** Playwright (or Selenium) driving a test instance of the Flask app with `controller`/`BleakScanner` mocked out; run as part of the CI workflow from 2.7.

---

### 2.13 CONTRIBUTING.md
- **Status:** Planned
- **Priority:** Low
- **Problem:** There's no documented contribution process or versioning policy, which matters more once 2.7 (CI) and 2.5 (Unit Tests) make outside PRs realistically reviewable.
- **Solution:** A `CONTRIBUTING.md` covering local setup, how to run tests/lint, commit/PR expectations, and the project's versioning policy (semver against `CHANGELOG.md`).
- **Implementation:** Plain markdown doc, no tooling.

---

## User Experience

### ✅ 3.1 Dark Mode
**Status:** ✅ Complete
**Priority:** Medium
**Files Modified:** `templates/base.html`

Three-state theme toggle (Light → Dark → System) with `localStorage` persistence and automatic OS preference following. Cycles: Light (sun) → Dark (moon) → System (display icon). Auto-follows OS theme changes in system mode.

---

### ✅ 3.2 Server-Sent Events for Real-Time Updates
**Status:** ✅ Complete
**Priority:** Medium
**Files Modified:** `app.py`, `run.py`, `config.py`, `config.json.example`, `templates/base.html`, `templates/active_session.html`, `templates/paused_session.html`, `templates/start_session.html`, `templates/settings.html`

Replaced the three templates' `setInterval(fetch('/stats'))` polling loops (1.5s / 3s cadences) with a single `/stats_stream` Server-Sent Events endpoint. A daemon thread broadcasts a stats snapshot to all subscribers once a second via thread-safe per-client queues, independent of whether the belt is running, so active, paused, and start screens all get uniform live updates. `/stats` is kept unchanged for compatibility; both routes now share one `_build_stats_payload()` helper.

Shipping this surfaced a real gap it needed to close first: a dead BLE connection while paused or idle went undetected entirely, since nothing was polling for liveness outside an active session. That's now covered by a dedicated idle/paused connection watchdog with its own staleness threshold, serialized against belt commands, the active stats poll, and speed changes via a lock recreated per connection attempt. See `CHANGELOG.md` `[1.6.0]` for details.

---

### 3.3 Keyboard Shortcuts
- **Status:** Planned
- **Priority:** Medium
- **Problem:** Adjusting speed or pausing requires using a mouse/touch, which is inconvenient while walking.
- **Solution:** Add keyboard shortcuts for core actions:
    - `Arrow Up` / `W`: Increase speed
    - `Arrow Down` / `S`: Decrease speed
    - `Space`: Pause / Resume
    - `M`: Max speed
    - `L`: Slow preset
    - `K`: Moderate preset
- **Implementation:** Add a keyboard event listener in the active session template that sends `fetch()` requests to the corresponding routes.

---

### 3.4 Dual-Unit Display (Imperial / Metric Toggle)
- **Status:** Planned
- **Priority:** Medium
- **Problem:** The app always displays imperial units (mph, miles). Users who prefer metric must mentally convert or edit code constants.
- **Solution:** Add a unit toggle (Imperial ↔ Metric) in the header that switches between mph/miles and km/h/km in real time. Store preference in `localStorage`.
- **Implementation:**
    - Return both imperial and metric values from `/stats` JSON endpoint
    - Frontend toggles display based on user preference
    - Add toggle button next to the theme toggle in the header

---

### ✅ 3.5 Session History Log
**Status:** ✅ Complete
**Priority:** Medium
**Files Modified:** `app.py`, `templates/start_session.html`, `templates/active_session.html`, `templates/paused_session.html`, `templates/base.html`, `.gitignore`

Stores completed sessions and displays a summary table on the start screen. Includes CSV export and history clearing. Storage is SQLite (see 2.9); it began as a JSON file, `session_history.json`, which is imported automatically.

| Feature | Description |
|---|---|
| **Session Persistence** | On session end (explicit "End Session" button or graceful shutdown), the session's database record is completed with its final stats |
| **Session Record** | The UI and CSV see: date, start_time, end_time, duration_seconds, distance_km, distance_mi, steps, calories, avg_speed_kmh, avg_speed_mph, rebuilt from SI columns by `units.legacy_record()` |
| **Recent Sessions Table** | Start screen displays last 10 sessions (configurable via `HISTORY_DISPLAY_LIMIT`) in a responsive table with date, time, duration, distance, steps, calories, and avg speed |
| **End Session Button** | Red "End Session" button on Active and Paused screens: stops belt, cancels monitor, saves session to history, resets all counters, returns to start screen |
| **CSV Export** | `/export_csv` route generates a downloadable CSV with all historical sessions (full history, no limit) |
| **Clear History** | "Clear" button on start screen with confirmation dialog, calls `/clear_history` POST endpoint to delete completed sessions (never an in-progress one) |
| **Graceful Shutdown Hook** | `_save_session()` called at the start of `_graceful_shutdown()` so in-progress sessions are captured even on Ctrl+C or server Close |
| **Fault Tolerant** | A storage error is logged and shows an empty history rather than breaking the start screen |

---

### 3.6 QR Code for LAN Access
- **Status:** Planned
- **Priority:** Medium
- **Problem:** The app's pitch is controlling the treadmill "from any browser on your network," but there's no way to get from the desktop console to a phone except typing the LAN IP by hand.
- **Solution:** Render a QR code pointing at `http://<lan-ip>:<port>` so a phone can scan-and-go instead.
- **Implementation:** Resolve the LAN IP at startup (`socket`), generate a QR code, show it in `run.py`'s console output and/or a corner of `connecting.html`. 5.1's Apple Health export already added client-side QR rendering (`qrcodejs` via CDN) that this can reuse directly instead of picking a library from scratch.

---

### 3.7 Touch / Swipe Gesture Speed Controls
- **Status:** Planned
- **Priority:** Low
- **Problem:** The primary interaction is a desktop/laptop browser at the desk the WalkingPad sits under, which 3.3 (Keyboard Shortcuts) is meant to serve. On the occasions the control page is pulled up on a phone or tablet as a secondary device, though, every speed change is still a full button tap.
- **Solution:** Add swipe-up/swipe-down gestures over the speed readout on `active_session.html` as a touch-friendly alternative to button taps whenever a touchscreen is in use.
- **Implementation:** `touchstart`/`touchend` delta listeners on the console hero, calling the same `/increase_speed` and `/decrease_speed` routes as the buttons.

---

### ✅ 3.8 Selectable Color Themes
**Status:** ✅ Complete
**Priority:** Medium
**Files Modified:** `templates/base.html`

10 standard color themes (Slate default, plus 9 hues run in even 40° spectral steps: Red, Amber, Lime, Forest, Teal, Cyan, Blue, Violet, Pink) and 5 "special" themes (Virginia Tech, Bloom, Tide, Harvest, Frost), selectable independently of the Light/Dark/System toggle via a palette icon in the header. Each theme tints the whole surface (backgrounds, cards, browser chrome) through CSS custom properties, not just accent buttons. Special themes add a two-tone swatch, a heading font (fetched only if selected), and, except Virginia Tech, a non-interactive ambient effect (falling petals/snow/leaves, scuttling crabs, glowing eyes) that respects `prefers-reduced-motion`. See `CHANGELOG.md` `[1.7.0]` for details.

---

### ✅ 3.9 Console-Style Interface Redesign
**Status:** ✅ Complete
**Priority:** Medium
**Files Modified:** `templates/base.html`, `templates/active_session.html`, `templates/paused_session.html`, `templates/start_session.html`, `templates/connecting.html`, `static/favicon.ico`, `static/apple-touch-icon.png`

Active, Paused, and Start screens redesigned to read like the WalkingPad's own onboard display: one large tabular-digit instrument-face reading (Speed while walking, elapsed time while paused, a Start control when idle) plus a compact Time/Distance/Steps/Calories readout strip below, with a brief mechanical tick animation on value changes and a colored connection status LED next to the logo. Also added a favicon and "Add to Home Screen" icon using the app's own logo mark instead of the browser default. See `CHANGELOG.md` `[1.7.0]` for details.

---

### 3.10 Heart Rate Display
- **Status:** Planned
- **Priority:** Low
- **Problem:** Some WalkingPad models have hand-held heart rate sensors, but the data is not exposed in the UI.
- **Solution:** If `ph4-walkingpad` provides heart rate data in status packets, display it as an additional item in the instrument strip during active sessions.
- **Dependency:** Verify heart rate data availability via `ph4-walkingpad` library and device firmware support. If the pad doesn't expose it, a watch or chest strap via the standard BLE Heart Rate Service (0x180D / 0x2A37) is the alternative source.
- **Storage:** `samples.hr_bpm` already exists (2.9, always NULL today), so recording heart rate needs no schema change.

---

### 3.11 Interval / Programmed Speed Sequences
- **Status:** Planned
- **Priority:** Low
- **Problem:** Speed changes are entirely manual (buttons/presets); there's no way to set up a warm-up → intervals → cool-down pattern and have the belt drive itself.
- **Solution:** Let the user define a sequence of (speed, duration) steps before starting; the app advances through the sequence automatically, calling `controller.change_speed()` at each transition.
- **Implementation:** A small program list (stored client-side or as a new config block); a `_program_task` coroutine running alongside `_stats_monitor` that fires speed changes on schedule; UI to build/save/select a program on the start screen.

---

### 3.12 Screen Wake Lock During Active Session
- **Status:** Planned
- **Priority:** Medium
- **Problem:** Nothing actively touches the page while walking, so the browser can dim or lock the screen mid-session, right when a quick glance at speed/distance is most likely.
- **Solution:** Request a screen wake lock while `belt_running` is true, and release it on pause/end.
- **Implementation:** The native [Wake Lock API](https://developer.mozilla.org/en-US/docs/Web/API/Screen_Wake_Lock_API) (`navigator.wakeLock.request('screen')`), no dependency needed. Re-request on `visibilitychange` since the browser releases the lock automatically when the tab is hidden.

---

### 3.13 Live Stats in Browser Tab Title
- **Status:** Planned
- **Priority:** Low
- **Problem:** If the WalkingDad tab isn't focused during a session, checking progress means switching back to it.
- **Solution:** Update `document.title` with a compact live readout (e.g. "3.2 mph · 1.4 mi") while a session is active, reverting to the normal title on pause/end.
- **Implementation:** Update `document.title` from the existing `/stats_stream` SSE handler already driving the on-page numbers; no new endpoint needed.

---

### 3.14 Update Check
- **Status:** Planned
- **Priority:** Low
- **Problem:** There's no way to know a newer version of WalkingDad exists short of manually checking the repository.
- **Solution:** A manual "Check for updates" button (Settings page) comparing the running version against the latest GitHub release tag. Read-only and opt-in, consistent with the project's zero-telemetry pitch: nothing is sent, one GitHub API request is made only when the button is clicked.
- **Implementation:** A version constant in `app.py`, a route that fetches the latest release tag from the GitHub API on demand, a Settings-page comparison and a link to the release if newer.

---

### 3.15 Replace Native `confirm()` Dialogs with Themed Modals
- **Status:** Planned
- **Priority:** Medium
- **Problem:** `Close` (`templates/base.html`) and `Clear History` (`templates/start_session.html`) both use the browser's native `confirm()` popup, unlike every other confirmable action in the app (Restore/Discard, Apple Health setup), which uses a styled Bootstrap modal matching dark mode and the active color theme. The native dialog can't be themed and is inconsistent for two fairly consequential actions.
- **Solution:** Replace both `confirm()` calls with a shared themed confirmation modal (title, message, Confirm/Cancel), matching the existing modal pattern already used elsewhere.
- **Implementation:** One reusable modal in `base.html` (title/body/confirm-callback populated per use), wired into the `shutdown-button` and `clear-history-btn` click handlers in place of `confirm()`.

---

### 3.16 Settings Validation Feedback
- **Status:** Planned
- **Priority:** Medium
- **Problem:** `settings_page()` (`app.py`) silently discards an invalid field value (e.g. typing "abc" into Max Speed), logs a server-side warning, and keeps the old value, but the redirect always shows the same "Settings saved" toast regardless. A user gets a false-positive success message with no indication that specific value didn't take.
- **Solution:** Track which fields (if any) failed validation during a POST and surface that in the response, distinct from the generic success toast.
- **Implementation:** `settings_page()` collects the list of rejected `form_key`s during the existing cast/except loop and passes it through the redirect (e.g. a query param or flashed message); `base.html`'s toast logic shows a warning variant naming the rejected field(s) instead of (or alongside) "Settings saved".

---

### 3.17 Tablet-Width Responsive Breakpoint
- **Status:** Planned
- **Priority:** Low
- **Problem:** `base.html` has exactly one `@media (max-width: 480px)` rule, tuned for phone-width screens. Desktop is the primary use case, but the app's own pitch covers "any browser on your network," and 2.7/3.7 (Touch/Swipe Gesture Controls) already anticipate a phone/tablet as a secondary device, so there's nothing tuned for the tablet width range in between.
- **Solution:** Add an intermediate breakpoint (e.g. `max-width: 900px`) tuned for tablet-class screens (iPad-size), rather than jumping straight from desktop layout to the 480px phone rules.
- **Implementation:** Audit `console-hero`/`console-strip`/history table layout at common tablet widths (768-1024px) and add a second `@media` block alongside the existing 480px one.

---

## Stats & Motivation

### 4.1 Estimated Time to Distance Goal
- **Status:** Planned
- **Priority:** Medium
- **Problem:** Users cannot set a target distance and see how long it will take to reach it.
- **Solution:** Add an optional "Target Distance" input on the start screen, then display estimated time remaining during active sessions based on current speed.
- **Implementation:** Simple calculation: `remaining_distance / current_speed`, displayed as MM:SS.

---

### 4.2 Personal Records
- **Status:** Planned
- **Priority:** Medium
- **Problem:** Session history already has everything needed to highlight records, but nothing surfaces them. Every session looks the same as the last.
- **Solution:** Compute and display "bests" on the start screen: longest session, farthest distance, most steps in a day, fastest avg speed (or `max_speed_mps` for top speed, recorded since 2.9).
- **Implementation:** SQL aggregates over completed `sessions` rows in `storage.py` (`MAX(distance_m)`, per-day `SUM(steps)` grouped on the date part of `start_time`), rendered as a small stat row near the history table.

---

### 4.3 Daily / Weekly Goals with Progress Bar
- **Status:** Planned
- **Priority:** Medium
- **Problem:** There's no way to set a target and see progress toward it beyond a single session. Unlike 4.1's live per-session ETA, this tracks progress across multiple sessions over time.
- **Solution:** Let the user set a daily or weekly step/distance goal in Settings; show a progress bar on the start screen summing today's/this week's sessions from history.
- **Implementation:** New config keys (`goal_type`, `goal_target`, `goal_period`); a date-range `SUM` over completed `sessions` rows in `storage.py` (`idx_sessions_start` already indexes `start_time`); progress bar on `start_session.html`.

---

### 4.4 Weekly / Monthly Trend Summary
- **Status:** Planned
- **Priority:** Low
- **Problem:** History is a flat list of individual sessions. There's no sense of trend (more or less active than last week/month).
- **Solution:** Add a compact summary comparing this week's/month's totals (distance, steps, sessions) against the previous period.
- **Implementation:** Aggregate completed `sessions` rows by ISO week/month in `storage.py`; render as text deltas or a minimal inline sparkline. No new charting dependency needed for a first pass.

---

### 4.5 Per-User Profiles
- **Status:** Planned
- **Priority:** Low
- **Problem:** WalkingDad is built for a household to share, but history mixes everyone's sessions together. History, records (4.2), and goals (4.3) can't be attributed to a person.
- **Solution:** A lightweight profile selector (name only, no accounts/auth) that tags each session with a profile; history, CSV export, personal records (4.2), and goals (4.3) all filter by the active profile.
- **Implementation:** The storage side exists since 2.9: `sessions.profile` (every existing row is `'default'`), an `(profile, start_time)` index, and `profile=` filters on `storage.create_session()`, `list_sessions()`, and `clear_history()`. Remaining: a profile switcher in the header or start screen, passing the active profile through those calls, and filtering 4.2/4.3's aggregates the same way.

---

## Data Export & Integrations

### ✅ 5.1 Apple Health Export via Shortcuts
**Status:** ✅ Complete
**Priority:** Low
**Files Modified:** `app.py`, `config.py`, `config.json.example`, `templates/base.html`, `templates/start_session.html`, `templates/settings.html`, `README.md`

Verified against current Apple documentation before building (the original plan below, kept for history, assumed a network-fetch design that turned out to need correcting): no Apple Developer Program membership is needed (a personal Shortcut is never distributed), and critically, the Health app does not exist on macOS as of Tahoe 26. This can only run on the iPhone, not the Mac WalkingDad itself runs on.

Two QR codes, both self-contained (no fetch back to WalkingDad's server): a **setup QR** (a plain iCloud share link, `Share → Copy iCloud Link` in the Shortcuts app) installs the Shortcut once per phone, and a **per-session QR** (`shortcuts://run-shortcut?name=...&input=text&text=<session JSON>`) runs it with that session's data embedded directly in the URL. Hosting the `.shortcut` file on GitHub and using `shortcuts://import-shortcut?url=...` was tried first and abandoned. It reliably failed with "shortcut URL provided was invalid" across every URL encoding/ordering tried by hand, a known, documented unreliability of GitHub-hosted `.shortcut` files with that scheme, not an encoding bug on WalkingDad's end. An iCloud link is Apple's actual supported distribution path. The Shortcut itself (`Get Dictionary from Input` → `Log Workout`, Type Walking, Date/Duration/Calories/Distance bound via Magic Variable) was hand-built in the Shortcuts app and shared from there, since there's no official API to generate a `.shortcut` file programmatically.

**Off by default.** An `apple_health_export_enabled` setting (Settings page, styled as a pill/slide toggle) gates the whole feature; when off, neither QR nor the start-screen prompt appears. Turning it on doesn't retroactively surface an old, unrelated session that happened to be most recent before the feature existed: the currently-most-recent session is pre-marked as handled on that specific off→on transition, unless it's from today, in which case it's left showing (plausibly the reason someone would enable the feature mid-session in the first place).

The start screen shows a persistent "Log to Apple Health" banner after a session ends, driven by a `health_logged` flag stored on the session record itself (not a one-shot flash). It survives navigation and reloads, and only clears on an explicit Dismiss or once a newer session supersedes it. This check is independent of the unrelated `history_display_limit` setting (querying the single most recent session directly rather than reusing the display-limited history list), so setting that to `0` doesn't silently disable Apple Health export too. Tapping the banner opens an in-page modal (no navigation) with the per-session QR, with a link inside to swap to the setup QR for anyone who hasn't installed the Shortcut yet. The Settings page also keeps a permanent copy of the setup QR (inside the same collapsing toggle section) for reinstalling later. QR rendering is client-side (`qrcodejs` via CDN, matching how `base.html` already pulls Bootstrap/Icons/Fonts), no new Python dependency.

This also gave 3.6 (QR Code for LAN Access) a proven client-side QR-rendering approach to reuse rather than starting from scratch.

<details>
<summary>Original plan (superseded by the design above)</summary>

- **Problem:** Completed sessions have nowhere to go besides `session_history.json`/CSV. A native HealthKit integration would require an Apple Developer Program membership and an actual iOS app, which is out of scope.
- **Solution:** Expose a small JSON export endpoint for a session's stats, and document an Apple Shortcuts recipe (`Get Contents of URL` → `Log Workout`) that reads it and logs the session to Apple Health. No app, no developer account: the Shortcut runs entirely on the user's own device.
- **Implementation:** A route returning duration/distance/steps/calories for the most recently completed session (no id needed; reads the last entry in `session_history.json`); a documented Shortcuts recipe in the README. Exporting an arbitrary past session by id is a follow-on, gated on the identifier prerequisite noted in 5.2.

</details>

---

### 5.2 Generic GPX/TCX Export
- **Status:** Planned
- **Priority:** Low
- **Problem:** CSV covers spreadsheets, but most fitness platforms and importers (Strava, RunGap, Health Connect-integrated apps) expect a GPX or TCX file.
- **Solution:** Add a per-session GPX/TCX export alongside the existing CSV export, so users on any platform can hand the file to whatever importer they already use. No direct API integration on WalkingDad's side.
- **Implementation:** The prerequisites exist since 2.9: a stable UUID `sessions.id` (already a CSV column) and per-second `samples`. An `/export/<session_id>.tcx`-style route built from `storage.get_session()` + `get_samples()`: sport Walking (treadmill), distance-only trackpoints from the samples (no GPS since the treadmill doesn't produce one), total time from `elapsed_s` and timer time from `moving_s`. Sessions imported from the old JSON have no samples (`has_samples = 0`), so they'd export as a single summary lap.

---

### 5.3 Session History Import
- **Status:** Planned
- **Priority:** Medium
- **Problem:** `/export_csv` gets history out, but there's no way back in. Migrating to a new machine currently means copying `walkingdad.db` by hand; restoring from a backup CSV isn't possible at all.
- **Solution:** An import action on the Settings or start-screen "Recent Sessions" area that accepts a previously-exported CSV and merges it into the database.
- **Implementation:** An `/import_history` route parsing the uploaded CSV (mirroring `export_csv()`'s column order), validating required fields, and inserting through `storage.py`. The row-to-SI mapping is the same one `storage.migrate_json()` already does for legacy records, so reuse it. Exported CSVs carry each session's `id`, which makes duplicate detection exact; fall back to date/start_time matching for CSVs exported before 2.9.

---

## Notes

- Items are grouped by theme (Area), not by execution order; each item's own **Priority** is what signals urgency, not its Area or position in the list.
- New features or bug fixes discovered during development may be added to this roadmap.
- **Google Fit / Health Connect native sync was considered and declined**: the only free path requires a native companion app using the platform SDK (Health Connect has no web/API route in), which is out of scope while WalkingDad stays a pure web app. Revisit only if that constraint changes; 5.2 (GPX/TCX export) is the current cross-platform fallback.
- For questions or feature requests, open an issue on the project repository.
