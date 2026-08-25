# Roadmap

## Phase 1: Stability & Safety (High Priority)

### ✅ 1.1 macOS / Cross-Platform BLE Reliability Fixes
**Status:** ✅ Complete
**Files Modified:** `app.py`

A comprehensive set of reliability improvements for Bluetooth Low Energy communication across all platforms (macOS, Windows, Linux). Implemented before the ROADMAP existed.

| Fix | Description |
|---|---|
| **Context Manager Scanning** | Replaced unreliable `BleakScanner.find_device_*()` static methods with `async with BleakScanner()` context manager — more stable on macOS CoreBluetooth and consistent across all platforms |
| **Retry with Exponential Backoff** | 3-retry mechanism (up to 10s wait) handles transient Bluetooth connectivity issues; searches by cached MAC address and device name |
| **Event Loop Cleanup** | Proper task cancellation before loop close prevents resource leaks and crashes on all platforms |
| **Stats Monitor Robustness** | `asyncio.wait_for()` with 2s timeout, proper cancellation handling, no event-loop crashes on errors |
| **Async Sequence Error Handling** | `start_belt()`, `resume_session()` now have try-catch; failures update app state and trigger disconnect handling |
| **Thread-Safe Coroutine Execution** | Error handling on `run_coroutine_threadsafe()` calls; proper state management if queueing fails |
| **Configurable Device Name** | Device name configurable without touching scanner logic — initially a constant in `app.py`, now via `config.json` / Settings page (3.1) |
| **Bleak API Version Compatibility** | Supports both `set_disconn_callback()` (newer) and `set_disconnected_callback()` (older) — works across Bleak versions |
| **Stats Monitor Lifecycle Fix** | Global `_stats_monitor_task` tracks active monitor; old tasks cancelled before new ones on resume; cleaned up on disconnect. Fixes metrics-not-updating-after-pause/resume bug |

**Compatibility:** macOS 12+, Windows, Linux · Bleak 0.19+ · Python 3.8+

---

### ✅ 1.2 Graceful Shutdown
**Status:** ✅ Complete
**Files Modified:** `app.py`, `run.py`, `templates/*.html`

Multi-layer graceful shutdown ensuring the treadmill belt always stops and BLE disconnects cleanly, regardless of how the process exits. Includes a web UI notification so the user knows shutdown is in progress.

| Layer | Trigger | Description |
|---|---|---|
| **HTTP `/shutdown` route** | User clicks "Close" in the UI, or `run.py` calls it on Ctrl+C | Sets `_server_stopping = True`, awaits `_graceful_shutdown()` via `fut.result(timeout=10)`, stops the BLE event loop, returns HTTP response, then a background thread sleeps 5s and exits via `os._exit(0)` (Waitress suppresses `sys.exit()`) |
| **Signal handlers (`SIGTERM`, `SIGINT`)** | Direct Ctrl+C on Waitress process (rare) | Same cleanup: sets `_server_stopping = True`, stops belt, standby mode, cancels monitor, disconnects BLE, stops event loop, then 2s delay + `os._exit(0)` |
| **`atexit` safety net** | Any process exit not caught by the above layers | Last-resort cleanup that calls `_graceful_shutdown()` with a 5-second timeout to ensure the belt is stopped even on unexpected exits |

Shutdown coroutine (`_graceful_shutdown()`) steps:
1. **Stop Belt** — If `belt_running`, call `controller.stop_belt()` and wait 0.5s
2. **Cancel Monitor** — Cancel `_stats_monitor_task` and await its `CancelledError`
3. **Standby Mode** — Switch device to `WalkingPad.MODE_STANDBY`
4. **BLE Disconnect** — Call `controller.client.disconnect()` to close the connection cleanly

Additional fixes:
- Duplicate shutdown requests are ignored via thread-safe `_shutting_down` flag protected by `threading.Lock()`
- Uses `os._exit(0)` (not `sys.exit(0)`) for reliable Waitress termination — Waitress catches and suppresses `SystemExit` from `sys.exit()`
- **UI shutdown notification** — all session templates check `/stats` for `stopping: true` and display "Server is shutting down" message
- **Process-isolated subprocess** — `run.py` launches Waitress via `os.setsid()` so Ctrl+C only hits the wrapper, not Waitress directly; gives the HTTP shutdown path time to complete
- Edge case handling when `ble_loop` or `controller` is None (skip BLE cleanup, exit directly)

---

### ✅ 1.3 Session State Persistence
**Status:** ✅ Complete
**Files Modified:** `app.py`, `templates/start_session.html`, `.gitignore`

Cumulative session stats now survive a server crash or restart. In-progress session state is written to `session_state.json` and offered back to the user on next launch instead of being silently lost.

| Feature | Description |
|---|---|
| **Periodic Save** | `_save_session_state()` writes distance/steps/calories/active-time/resume-speed/raw-device-counters to `session_state.json` every 5 seconds while the stats monitor is running |
| **State-Change Saves** | Also saved immediately on `/start`, `/pause`, and `/resume` so a crash right after a transition doesn't lose it |
| **Startup Detection** | On launch, `_load_session_state()` checks for a leftover file from an unclean exit; if found, it's held as a pending restore rather than auto-resumed (no BLE connection to trust yet) |
| **Restore Banner** | Start screen shows a summary (distance, steps, calories, time) of the interrupted session with **Restore Session** / **Discard** actions |
| **Restore Session** | `/restore_session` reinstates the session in **paused** state (belt physically stopped) — user hits Resume to reconnect and continue, matching the existing pause/resume flow |
| **Clean-Exit Cleanup** | `session_state.json` is removed once a session is finalized normally — on `/end_session` (after saving to history) and during graceful shutdown (Ctrl+C / signal / `/shutdown`) — so a clean exit never triggers a restore prompt |
| **Fault Tolerant** | Corrupted or missing `session_state.json` is ignored gracefully, same pattern as `session_history.json` |

---

### 1.4 Automatic Reconnect on Disconnection
- **Status:** Planned
- **Problem:** When the BLE connection drops unexpectedly, the user must manually click "Try Again" to reconnect.
- **Solution:** Implement automatic reconnection with configurable retry interval (e.g., attempt every 5 seconds for up to 60 seconds), while still showing a "Disconnected" state in the UI.
- **Implementation:** Add a background reconnect task that monitors `connected` state and triggers `_start_ble_thread()` after a delay when disconnection is detected.

---

### 1.5 Auto-End Stale Paused Session
- **Status:** Planned
- **Problem:** A paused session (manual or auto-pause) has no timeout — if the user forgets to resume or end it, it sits in `paused_session.html` indefinitely. `session_state.json` is written once at the pause transition and never refreshed again until resume/end, so the file also goes stale the longer it sits.
- **Solution:** If a session stays paused longer than a configurable timeout (e.g. 30 minutes), automatically run the same path as `/end_session` — save to history, clear state, return to the start screen.
- **Implementation:** Track a pause-started timestamp when `belt_running` flips to `False`; check it on a lightweight timer even while the belt sequence is idle; add `stale_pause_timeout_minutes` to `config.json`/Settings.

---

## Phase 2: User Experience (Medium Priority)

### ✅ 2.1 Dark Mode
**Status:** ✅ Complete
**Files Modified:** `templates/base.html`

Three-state theme toggle (Light → Dark → System) with `localStorage` persistence and automatic OS preference following. Cycles: Light (sun) → Dark (moon) → System (display icon). Auto-follows OS theme changes in system mode.

---

### ✅ 2.2 Server-Sent Events for Real-Time Updates
**Status:** ✅ Complete
**Files Modified:** `app.py`, `run.py`, `config.py`, `config.json.example`, `templates/base.html`, `templates/active_session.html`, `templates/paused_session.html`, `templates/start_session.html`, `templates/settings.html`

Replaced the three templates' `setInterval(fetch('/stats'))` polling loops (1.5s / 3s cadences) with a single `/stats_stream` Server-Sent Events endpoint. A daemon thread broadcasts a stats snapshot to all subscribers once a second via thread-safe per-client queues, independent of whether the belt is running — so active, paused, and start screens all get uniform live updates. `/stats` is kept unchanged for compatibility; both routes now share one `_build_stats_payload()` helper.

Shipping this surfaced a real gap it needed to close first: a dead BLE connection while paused or idle went undetected entirely, since nothing was polling for liveness outside an active session. That's now covered by a dedicated idle/paused connection watchdog with its own staleness threshold, serialized against belt commands, the active stats poll, and speed changes via a lock recreated per connection attempt. See `CHANGELOG.md` `[1.6.0]` for details.

---

### 2.3 Keyboard Shortcuts
- **Status:** Planned
- **Problem:** Adjusting speed or pausing requires using a mouse/touch, which is inconvenient while walking.
- **Solution:** Add keyboard shortcuts for core actions:
    - `Arrow Up` / `W`: Increase speed
    - `Arrow Down` / `S`: Decrease speed
    - `Space`: Pause / Resume
    - `M`: Max speed
    - `L`: Slow walk
- **Implementation:** Add a keyboard event listener in the active session template that sends `fetch()` requests to the corresponding routes.

---

### 2.4 Dual-Unit Display (Imperial / Metric Toggle)
- **Status:** Planned
- **Problem:** The app always displays imperial units (mph, miles). Users who prefer metric must mentally convert or edit code constants.
- **Solution:** Add a unit toggle (Imperial ↔ Metric) in the header that switches between mph/miles and km/h/km in real time. Store preference in `localStorage`.
- **Implementation:**
    - Return both imperial and metric values from `/stats` JSON endpoint
    - Frontend toggles display based on user preference
    - Add toggle button next to the theme toggle in the header

---

### ✅ 2.5 Session History Log
**Status:** ✅ Complete
**Files Modified:** `app.py`, `templates/start_session.html`, `templates/active_session.html`, `templates/paused_session.html`, `templates/base.html`, `.gitignore`

Stores completed sessions in a local JSON file (`session_history.json`) and displays a summary table on the start screen. Includes CSV export and history clearing.

| Feature | Description |
|---|---|
| **Session Persistence** | On session end (explicit "End Session" button or graceful shutdown), all session stats are written to `session_history.json` as a JSON array of record objects |
| **Thread-Safe I/O** | All file reads/writes protected by `threading.Lock()` (`_history_lock`) to prevent corruption across Flask request threads and the BLE thread |
| **Session Record** | Each record stores: date, start_time, end_time, duration_seconds, distance_km, distance_mi, steps, calories, avg_speed_kmh, avg_speed_mph |
| **Recent Sessions Table** | Start screen displays last 10 sessions (configurable via `HISTORY_DISPLAY_LIMIT`) in a responsive table with date, time, duration, distance, steps, calories, and avg speed |
| **End Session Button** | Red "End Session" button on Active and Paused screens — stops belt, cancels monitor, saves session to history, resets all counters, returns to start screen |
| **CSV Export** | `/export_csv` route generates a downloadable CSV with all historical sessions (full history, no limit) |
| **Clear History** | "Clear" button on start screen with confirmation dialog, calls `/clear_history` POST endpoint to truncate the history file |
| **Graceful Shutdown Hook** | `_save_session()` called at the start of `_graceful_shutdown()` so in-progress sessions are captured even on Ctrl+C or server Close |
| **Fault Tolerant** | Corrupted or missing `session_history.json` is handled gracefully — starts fresh with empty array and logs warning |

---

### 2.6 QR Code for LAN Access
- **Status:** Planned
- **Problem:** The app's pitch is controlling the treadmill "from any browser on your network," but there's no way to get from the desktop console to a phone except typing the LAN IP by hand.
- **Solution:** Render a QR code pointing at `http://<lan-ip>:<port>` so a phone can scan-and-go instead.
- **Implementation:** Resolve the LAN IP at startup (`socket`), generate a QR code (small dependency, or an inline SVG generator to avoid one), show it in `run.py`'s console output and/or a corner of `connecting.html`.

---

### 2.7 Touch / Swipe Gesture Speed Controls
- **Status:** Planned
- **Problem:** The primary interaction is a desktop/laptop browser at the desk the WalkingPad sits under, which 2.3 (Keyboard Shortcuts) already serves well. On the occasions the control page is pulled up on a phone or tablet as a secondary device, though, every speed change is still a full button tap.
- **Solution:** Add swipe-up/swipe-down gestures over the stat cards on `active_session.html` as a touch-friendly alternative to button taps whenever a touchscreen is in use.
- **Implementation:** `touchstart`/`touchend` delta listeners on the stats grid, calling the same `/increase_speed` and `/decrease_speed` routes as the buttons.

---

## Phase 3: Code Quality (Medium Priority)

### ✅ 3.1 External Configuration File
**Status:** ✅ Complete
**Files Modified:** `config.py`, `config.json.example`, `app.py`, `run.py`, `.gitignore`, `README.md`, `templates/base.html`, `templates/settings.html`

All user-tunable settings are now loaded from an optional `config.json` file, with `config.py` providing defaults. A Settings page (gear icon in the header) allows changing any setting from the browser without editing files. Most settings take effect immediately; host and port require a restart.

---

### 3.2 Route Security
- **Status:** Planned
- **Problem:** Routes like `/start`, `/pause`, `/increase_speed` have no authentication or CSRF protection. Since the server binds to `0.0.0.0`, any device on the local network could control the treadmill.
- **Solution:** Add a simple secret token mechanism:
    - Generate a random token on startup (stored in config)
    - Require `?token=...` query parameter or `X-Auth-Token` header on all action routes
    - Display the token in the UI and use it automatically for frontend requests
- **Alternative:** Restrict server to `127.0.0.1` only (breaks network access but is simpler).

---

### 3.3 Configurable Logging Level
- **Status:** Planned
- **Problem:** `logging.basicConfig(level=logging.INFO)` is hardcoded. Users cannot enable verbose DEBUG output without editing code.
- **Solution:** Allow setting log level via environment variable (`LOG_LEVEL=DEBUG`) or command-line argument.
- **Implementation:** Read `LOG_LEVEL` from `os.environ` with a default of `INFO`, pass to `logging.basicConfig(level=...)`.

---

### 3.4 Self-Hosted Static Assets
- **Status:** Planned
- **Problem:** Bootstrap, Bootstrap Icons, and Google Fonts all load from CDNs in `base.html`, so the UI visibly breaks without internet access (already called out in the README's "Icons missing" troubleshooting entry) — at odds with the "runs locally, no cloud" pitch.
- **Solution:** Vendor Bootstrap CSS/JS, Bootstrap Icons, and the two Google Fonts (Noto Sans and Space Grotesk, all weights currently loaded) into a local `static/` directory and reference them relatively instead of via CDN.
- **Implementation:** Download and pin the exact versions currently used, serve via Flask's default `static` route, update `base.html`'s `<link>`/`<script>` tags. Pure dependency removal, no functional change.

---

## Phase 4: Nice-to-Have (Low Priority)

### 4.1 Heart Rate Display
- **Status:** Planned
- **Problem:** Some WalkingPad models have hand-held heart rate sensors, but the data is not exposed in the UI.
- **Solution:** If `ph4-walkingpad` provides heart rate data in status packets, display it as an additional stat card during active sessions.
- **Dependency:** Verify heart rate data availability via `ph4-walkingpad` library and device firmware support.

---

### 4.2 Estimated Time to Distance Goal
- **Status:** Planned
- **Problem:** Users cannot set a target distance and see how long it will take to reach it.
- **Solution:** Add an optional "Target Distance" input on the start screen, then display estimated time remaining during active sessions based on current speed.
- **Implementation:** Simple calculation: `remaining_distance / current_speed`, displayed as MM:SS.

---

### 4.3 Unit Tests
- **Status:** Planned
- **Problem:** No automated tests exist, making refactoring risky.
- **Solution:** Write unit tests for pure functions and stateless logic:
    - `format_seconds_to_hms()` - time formatting edge cases
    - `kcal_estimate()` - calorie calculation
    - `process_status_packet()` - stat accumulation, auto-pause detection, speed history management (mock BLE data)
- **Implementation:** Use `pytest` with fixtures for mock status packets. Place tests in a new `tests/` directory.

---

### 4.4 Interval / Programmed Speed Sequences
- **Status:** Planned
- **Problem:** Speed changes are entirely manual (buttons/presets); there's no way to set up a warm-up → intervals → cool-down pattern and have the belt drive itself.
- **Solution:** Let the user define a sequence of (speed, duration) steps before starting; the app advances through the sequence automatically, calling `controller.change_speed()` at each transition.
- **Implementation:** A small program list (stored client-side or as a new config block); a `_program_task` coroutine running alongside `_stats_monitor` that fires speed changes on schedule; UI to build/save/select a program on the start screen.

---

## Phase 5: Stats & Motivation (Medium Priority)

### 5.1 Personal Records
- **Status:** Planned
- **Problem:** `session_history.json` already has everything needed to highlight records, but nothing surfaces them — every session looks the same as the last.
- **Solution:** Compute and display "bests" on the start screen: longest session, farthest distance, most steps in a day, fastest avg speed.
- **Implementation:** A `_compute_records()` helper over `_load_session_history(limit=None)`, rendered as a small stat row near the history table.

---

### 5.2 Daily / Weekly Goals with Progress Bar
- **Status:** Planned
- **Problem:** There's no way to set a target and see progress toward it beyond a single session — distinct from 4.2's live per-session ETA, this is about tracking progress across multiple sessions over time.
- **Solution:** Let the user set a daily or weekly step/distance goal in Settings; show a progress bar on the start screen summing today's/this week's sessions from history.
- **Implementation:** New config keys (`goal_type`, `goal_target`, `goal_period`); an aggregation function filtering `session_history.json` by date range; progress bar on `start_session.html`.

---

### 5.3 Weekly / Monthly Trend Summary
- **Status:** Planned
- **Problem:** History is a flat list of individual sessions — there's no sense of trend (more or less active than last week/month).
- **Solution:** Add a compact summary comparing this week's/month's totals (distance, steps, sessions) against the previous period.
- **Implementation:** Aggregate `session_history.json` by ISO week/month; render as text deltas or a minimal inline sparkline — no new charting dependency needed for a first pass.

---

### 5.4 Per-User Profiles
- **Status:** Planned
- **Problem:** WalkingDad is built for a household to share, but `session_history.json` mixes everyone's sessions together — history, records (5.1), and goals (5.2) can't be attributed to a person.
- **Solution:** A lightweight profile selector (name only, no accounts/auth) that tags each session with a `profile` field; history, CSV export, personal records (5.1), and goals (5.2) all filter by the active profile.
- **Implementation:** Add `profile` to the session record schema (`_build_session_record()`); update 5.1's `_compute_records()` and 5.2's date-range aggregation to filter by the active profile; a profile switcher in the header or start screen; default to a single "default" profile so existing history isn't invalidated.

---

## Phase 6: Data Export & Integrations (Low Priority)

### 6.1 Apple Health Export via Shortcuts
- **Status:** Planned
- **Problem:** Completed sessions have nowhere to go besides `session_history.json`/CSV. A native HealthKit integration would require an Apple Developer Program membership and an actual iOS app — out of scope.
- **Solution:** Expose a small JSON export endpoint for a session's stats, and document an Apple Shortcuts recipe (`Get Contents of URL` → `Log Workout`) that reads it and logs the session to Apple Health. No app, no developer account — the Shortcut runs entirely on the user's own device.
- **Implementation:** A route returning duration/distance/steps/calories for the most recently completed session (no id needed — reads the last entry in `session_history.json`); a documented Shortcuts recipe in the README. Exporting an arbitrary past session by id is a follow-on, gated on the identifier prerequisite noted in 6.2.

---

### 6.2 Generic GPX/TCX Export
- **Status:** Planned
- **Problem:** CSV covers spreadsheets, but most fitness platforms and importers (Strava, RunGap, Health Connect-integrated apps) expect a GPX or TCX file.
- **Solution:** Add a per-session GPX/TCX export alongside the existing CSV export, so users on any platform can hand the file to whatever importer they already use — no direct API integration on WalkingDad's side.
- **Implementation:** Requires first adding a stable identifier to the session record schema — `_build_session_record()`/`session_history.json` have none today (an index or ISO timestamp key would work). Then an `/export_gpx/<session_id>`-style route generating a minimal GPX/TCX document (timestamp, distance, duration; no GPS track since the treadmill doesn't produce one).

---

## Notes

- Items within each phase can be implemented in any order unless dependencies exist.
- New features or bug fixes discovered during development may be added to this roadmap.
- **Google Fit / Health Connect native sync was considered and declined** — the only free path requires a native companion app using the platform SDK (Health Connect has no web/API route in), which is out of scope while WalkingDad stays a pure web app. Revisit only if that constraint changes; 6.2 (GPX/TCX export) is the current cross-platform fallback.
- For questions or feature requests, open an issue on the project repository.
