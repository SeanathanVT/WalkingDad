# Roadmap

Organized by theme (Area), not build order. Work has never moved through these top to bottom, so grouping by "priority phase" stopped meaning anything. Each item carries its own **Priority** (High/Medium/Low) independent of which Area it's filed under.

## Reliability & Safety

### ✅ 1.1 macOS / Cross-Platform BLE Reliability Fixes
**Status:** ✅ Complete
**Changelog:** `[1.0.0]`
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
**Changelog:** `[1.1.0]`
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
**Changelog:** `[1.5.0]`
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
**Changelog:** `[1.8.0]`
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
**Changelog:** `[1.8.0]`
**Priority:** Medium
**Files Modified:** `app.py`, `config.py`, `config.json.example`, `templates/paused_session.html`, `templates/settings.html`, `README.md`, `tests/test_stale_pause.py`

A session left paused (manual, auto, Bluetooth drop, or restored after a crash) longer than `stale_pause_timeout_minutes` (default 30, `0` disables) is ended exactly as if End Session were pressed: saved to history, state file cleared, back to the start screen.

| Feature | Description |
|---|---|
| **No New Timer** | `_end_session_if_stale_pause()` runs on the existing 1 s SSE broadcaster tick, so it covers every pause path without hooking each one. Timed on the wall clock, since `time.monotonic()` stops during system sleep and a laptop closed overnight is the main case |
| **Shared End Path** | `/end_session` and the auto-end both call `_end_session()`; the auto-end re-checks `belt_running` under the session lock so a Resume racing the timeout wins |
| **UI** | The stats payload now carries `session_active`; the paused screen reloads to the start screen when it flips false |

---

### ✅ 1.6 Port-in-Use Check on Startup
**Status:** ✅ Complete
**Changelog:** `[1.8.0]`
**Priority:** Medium
**Files Modified:** `run.py`, `start_app.bat`, `README.md`, `tests/test_port_check.py`

`run.py` checks `host`/`port` before spawning Waitress (or opening the browser), exiting with the OS error and the fix on failure.

| Feature | Description |
|---|---|
| **Mirrors Waitress** | `_listen_addrs()` resolves `host` the way Waitress does (`getaddrinfo` with `AF_UNSPEC`/`AI_PASSIVE`, `[...]` stripped, `*` as wildcard, zone-index dedupe) and test-binds every result, so `*`, `localhost`, and IPv6 hosts are probed on each address Waitress will bind, not just the first |
| **Bind, Not Connect** | A `bind()` probe rather than `connect_ex()`, so it also catches Windows-reserved ports and listeners on other interfaces. `create_server()` omits `SO_REUSEADDR` on Windows (Waitress sets it, which would let a second WalkingDad bind over a live listener) and sets it on POSIX, so a TIME_WAIT port from the last run counts as free |
| **Specific Messages** | Separate messages for a port in use (likely a second WalkingDad), a port the OS won't allow (below 1024 on Linux without root, or Windows-reserved), a `host` that doesn't resolve, and a `port` outside 1-65535 (checked up front, since `getaddrinfo` silently wraps 70000 to 4464) |
| **Readable on Windows** | `start_app.bat` pauses on a non-zero exit so the message stays open |

---

### 1.7 Bluetooth Disconnect Callback Never Registered
- **Status:** Planned
- **Priority:** High
- **Problem:** `_connect_to_pad()` registers `_handle_disconnect` via `set_disconn_callback()` or `set_disconnected_callback()`, but `BleakClient` in Bleak 3.x has neither; it takes the callback only as a constructor argument, and `ph4-walkingpad`'s `Controller.connect()` builds the client without one. So a drop is only noticed by the staleness watchdogs (~15 s active, ~45 s idle/paused) instead of immediately. The tests' fake controller has `set_disconn_callback`, which hides this.
- **Solution:** Pass `disconnected_callback=_handle_disconnect` when the `BleakClient` is built, by overriding `Controller.connect()`, and drop the dead old-API branches.
- **Implementation:** Its own `bugfix/` branch, with a test against the real `BleakClient` signature rather than the fake.

---

## Security & Code Quality

### ✅ 2.1 External Configuration File
**Status:** ✅ Complete
**Changelog:** `[1.4.0]`
**Priority:** High
**Files Modified:** `config.py`, `config.json.example`, `app.py`, `run.py`, `.gitignore`, `README.md`, `templates/base.html`, `templates/settings.html`

All user-tunable settings are now loaded from an optional `config.json` file, with `config.py` providing defaults. A Settings page (gear icon in the header) allows changing any setting from the browser without editing files. Most settings take effect immediately; host, port, and waitress_threads require a restart.

---

### ✅ 2.2 Route Security
**Status:** ✅ Complete
**Changelog:** `[1.9.0]`
**Priority:** High
**Files Modified:** `app.py`, `templates/connecting.html`, `tests/test_csrf.py`, `tests/test_routes.py`

Routes like `/start`, `/pause`, `/increase_speed` had no CSRF protection, so any website open in a browser on the network could control the treadmill. A `before_request` guard (`_block_cross_site_posts()`) now rejects any non-GET request whose `Origin` isn't the app's own origin (scheme, host, and port), or whose `Sec-Fetch-Site` is `cross-site` or `same-site` (catches it when an extension or proxy strips `Origin`). Requests without either header (curl, `run.py`'s `/shutdown`) pass. `/reconnect` became POST-only (the Connect / Try Again links on the connecting screen are now form buttons), so every state-changing route goes through the guard.

- **Not done, deliberately:** a per-device token. The app is LAN-only and devices on the LAN are trusted by design; the treadmill is only reachable over Bluetooth from home, and anyone at home can press its buttons anyway. A token would add a sign-in step for every phone, break scripts, and add a credential to manage, guarding against a threat that doesn't apply. Revisit only if the app is ever exposed beyond the LAN (tunnel, Tailscale).
- **Known limit:** a deliberate DNS-rebinding attack makes `Origin` match the app's origin and gets through; that takes an attack targeted at this app specifically, so it's accepted. The fix would be a `Host` allowlist (IP literals, `localhost`, the machine's own name), at the cost of breaking access through custom DNS names (`.lan`, Tailscale).

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

### ✅ 2.5 Unit Tests
**Status:** ✅ Complete
**Changelog:** `[1.8.0]`, `[1.9.0]`
**Priority:** Medium
**Files Modified:** `app.py`, `run.py`, `requirements-dev.txt`, `pytest.ini`, `ruff.toml`, `.coveragerc`, `.gitignore`, `README.md`, `tests/`

A `pytest` suite covering every module without a treadmill. How to run it: the README's **Running tests** line.

| Area | Coverage |
|---|---|
| **Pure helpers** | Time formatting, calorie estimate, status-field extraction, settings casts and clamps, Apple Health shortcut URLs, stats payload and SSE framing |
| **Status packets** | Distance/step accumulation across device counter resets, calories, speed history filtering and cap, resume speed chosen on auto-pause, liveness stamping (plus the existing active-time and auto-pause tests) |
| **Routes** | Every Flask route through the test client: start/pause/resume/end, speed controls and clamping, restore/discard, CSV export, settings save, `/stats`, `/stats_stream`, `/shutdown` |
| **BLE and async** | A fake controller drives the wake/start/stop sequences, light wake, lock timeouts, the stats monitor, the idle watchdog, disconnect handling, auto-reconnect backoff, scanning, connecting, the BLE thread lifecycle, and signal/HTTP shutdown |
| **Persistence** | Session-state file save/load/clear (including corrupt files), `config.json` writes, and every database helper against a temp SQLite DB, including "log and continue" behavior when storage fails |
| **`run.py`** | Port check, browser launch, the HTTP shutdown call, and `main()`'s Waitress subprocess and Ctrl+C handling |

**Implementation:** `tests/conftest.py` sets `WALKINGDAD_NO_STARTUP=1` before importing `app.py`, so the import skips opening `walkingdad.db`, the JSON migration, the orphan sweep, the BLE thread, the SSE broadcaster, and installing the signal/`atexit` handlers. Its `app_state` fixture resets every module global, points the state file, `config.json`, and the database at `tmp_path`, and records BLE coroutines instead of scheduling them, so tests can run a captured sequence against a fake controller.

**Not covered:** the inline template JavaScript (left to 2.12's browser smoke test); code that only runs at import (`app.py`'s startup block, `config.py`'s no-`config.json` path); the background-thread entry points (SSE broadcaster start, `/shutdown`'s delayed exit); `_ble_thread`'s handlers for errors escaping asyncio's own `run_forever()` and task cleanup; and `run.py`'s Windows branch and `__main__` guard.

---

### ✅ 2.6 Pin Dependency Versions
**Status:** ✅ Complete
**Priority:** Medium
**Files Modified:** `requirements.txt`

Each direct dependency in `requirements.txt` (including `markupsafe`, imported by `app.py`) has a floor at its tested version and a cap below the next major (e.g. `bleak>=3.0.2,<4`), so patch releases still arrive but a breaking major can't. `requirements.txt` is the one record of the tested versions.

---

### ✅ 2.7 Continuous Integration
**Status:** ✅ Complete
**Changelog:** `[1.9.0]`
**Priority:** Medium
**Files Modified:** `.github/workflows/ci.yml`, `.gitlab-ci.yml`, `requirements-dev.txt`, `ruff.toml`

Pull/merge requests and pushes to `main`/`development` run the lint and test suite on both GitHub Actions and GitLab CI.

| Feature | Description |
|---|---|
| **GitHub Actions** | `.github/workflows/ci.yml` on `ubuntu-latest`, Python 3.10 (the supported floor) and 3.13. Pushes run only on `main`/`development`, so a PR branch isn't tested twice |
| **GitLab CI** | `.gitlab-ci.yml` on the `python:3.12` image. Runs a merge-request pipeline when an MR is open, otherwise a branch pipeline, and shows the coverage percentage and a Cobertura report in the MR |
| **Steps** | `pip install -r requirements.txt -r requirements-dev.txt`, `ruff check .`, `python -m pytest --cov=.`. CI reports coverage but never fails on it |
| **Lint** | `ruff` pinned in `requirements-dev.txt`, since its default rule set changes between releases. `pyproject.toml` ignores only rules that flag deliberate project style (root logger, catch-all excepts around BLE I/O, naive local datetimes, a `ValueError` raised to share an `except` clause) |
| **Linux only** | BLE is fully mocked, so no Bluetooth hardware or OS-specific stack is needed. `tests/test_run.py` assumes POSIX (macOS/Linux) |

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
**Changelog:** `[1.8.0]`
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
| **JSON Migration** | One-time, automatic, single transaction: backup to `session_history.json.bak-<timestamp>`, then rename to `.migrated` (both in `data/backups/`). Unparseable records are logged and skipped; an unreadable file is left untouched for a later retry |
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
- **Implementation:** A `.github/dependabot.yml` for version-update PRs plus security alerts; a `pip-audit` step added to both CI configs from 2.7.

---

### 2.12 Integration Smoke Test
- **Status:** Planned
- **Priority:** Medium
- **Problem:** 2.5's tests drive each route through Flask's test client, but nothing loads the rendered pages in a real browser and clicks through Start → Pause → Resume → End, so a template JavaScript or front-end wiring regression could still slip through.
- **Solution:** One browser-driven smoke test that boots the app against a mocked BLE device/controller and clicks through the full session lifecycle, asserting each screen renders and each transition lands where expected.
- **Implementation:** Playwright (or Selenium) driving a test instance of the Flask app with `controller`/`BleakScanner` mocked out; run as part of the CI configs from 2.7.

---

### 2.13 CONTRIBUTING.md
- **Status:** Planned
- **Priority:** Low
- **Problem:** There's no documented contribution process or versioning policy, which matters more now that 2.7 (CI) and 2.5 (Unit Tests) make outside PRs realistically reviewable.
- **Solution:** A `CONTRIBUTING.md` covering local setup, how to run tests/lint, commit/PR expectations, and the project's versioning policy (semver against `CHANGELOG.md`).
- **Implementation:** Plain markdown doc, no tooling.

---

## User Experience

### ✅ 3.1 Dark Mode
**Status:** ✅ Complete
**Changelog:** `[1.0.0]`
**Priority:** Medium
**Files Modified:** `templates/base.html`

Three-state theme toggle (Light → Dark → System) with `localStorage` persistence and automatic OS preference following. Cycles: Light (sun) → Dark (moon) → System (display icon). Auto-follows OS theme changes in system mode.

---

### ✅ 3.2 Server-Sent Events for Real-Time Updates
**Status:** ✅ Complete
**Changelog:** `[1.6.0]`
**Priority:** Medium
**Files Modified:** `app.py`, `run.py`, `config.py`, `config.json.example`, `templates/base.html`, `templates/active_session.html`, `templates/paused_session.html`, `templates/start_session.html`, `templates/settings.html`

Replaced the three templates' `setInterval(fetch('/stats'))` polling loops (1.5s / 3s cadences) with a single `/stats_stream` Server-Sent Events endpoint. A daemon thread broadcasts a stats snapshot to all subscribers once a second via thread-safe per-client queues, independent of whether the belt is running, so active, paused, and start screens all get uniform live updates. `/stats` is kept unchanged for compatibility; both routes now share one `_build_stats_payload()` helper.

Shipping this surfaced a real gap it needed to close first: a dead BLE connection while paused or idle went undetected entirely, since nothing was polling for liveness outside an active session. That's now covered by a dedicated idle/paused connection watchdog with its own staleness threshold, serialized against belt commands, the active stats poll, and speed changes via a lock recreated per connection attempt.

---

### ✅ 3.3 Keyboard Shortcuts
**Status:** ✅ Complete
**Priority:** Medium
**Files Modified:** `app.py`, `config.py`, `config.json.example`, `templates/base.html`, `templates/active_session.html`, `templates/paused_session.html`, `templates/settings.html`, `requirements.txt`, `README.md`, `tests/`

`Space` pauses or resumes; `↑`/`W` and `↓`/`S` step the speed; `L`, `K`, and `M` pick the Slow, Moderate, and Max presets; `?` lists them all. Start and End Session have no key, so a stray press can't start the belt with nobody on it or end a walk.

| Feature | Description |
|---|---|
| **Clicks the Button** | `hotkey()` in `app.py` tags each button with the native `aria-keyshortcuts` attribute (also read by screen readers) plus a `title` tooltip; one listener in `base.html` clicks the matching enabled button, so a shortcut takes the same form-submit path, double-tap guard, and cross-site check as a click |
| **Stays Out of the Way** | Ignored while typing in a field, with Ctrl/Alt/Cmd held, while a dialog, the color-theme popover, or the phone menu is open, and for `Space` on a button or link focused from the keyboard (Tab), not by a mouse click. A key bound to a disabled button (belt transitioning) is still swallowed, so `Space` doesn't scroll the page. Held keys don't repeat, deliberately unlike e.g. YouTube's volume keys: each press is a locked Bluetooth command |
| **Help Fits the Screen** | `?` lists only the shortcuts bound on the current screen |
| **Can Be Turned Off** | `keyboard_shortcuts_enabled` (Settings page, default on), as WCAG 2.1.4 requires for single-character shortcuts. Off removes the attributes and the listener entirely |

---

### 3.4 Dual-Unit Display (Imperial / Metric Toggle)
- **Status:** Planned
- **Priority:** Medium
- **Problem:** The app always displays imperial units (mph, miles). Users who prefer metric must mentally convert or edit code constants.
- **Solution:** Add a unit toggle (Imperial ↔ Metric) in the header that switches between mph/miles and km/h/km in real time. Store preference in `localStorage`.
- **Implementation:**
    - Return both imperial and metric values from `_build_stats_payload()` (shared by `/stats_stream` and `/stats`)
    - Frontend toggles display based on user preference
    - Add toggle button next to the theme toggle in the header
    - Cover the live tab title (3.13) too, which shows mph like the page

---

### ✅ 3.5 Session History Log
**Status:** ✅ Complete
**Changelog:** `[1.2.0]`
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
**Changelog:** `[1.7.0]`
**Priority:** Medium
**Files Modified:** `templates/base.html`

10 standard color themes (Slate default, plus 9 hues run in even 40° spectral steps: Red, Amber, Lime, Forest, Teal, Cyan, Blue, Violet, Pink) and 5 "special" themes (Virginia Tech, Bloom, Tide, Harvest, Frost), selectable independently of the Light/Dark/System toggle via a palette icon in the header. Each theme tints the whole surface (backgrounds, cards, browser chrome) through CSS custom properties, not just accent buttons. Special themes add a two-tone swatch, a heading font (fetched only if selected), and, except Virginia Tech, a non-interactive ambient effect (falling petals/snow/leaves, scuttling crabs, glowing eyes) that respects `prefers-reduced-motion`.

---

### ✅ 3.9 Console-Style Interface Redesign
**Status:** ✅ Complete
**Changelog:** `[1.7.0]`
**Priority:** Medium
**Files Modified:** `templates/base.html`, `templates/active_session.html`, `templates/paused_session.html`, `templates/start_session.html`, `templates/connecting.html`, `static/favicon.ico`, `static/apple-touch-icon.png`

Active, Paused, and Start screens redesigned to read like the WalkingPad's own onboard display: one large tabular-digit instrument-face reading (Speed while walking, elapsed time while paused, a Start control when idle) plus a compact Time/Distance/Steps/Calories readout strip below, with a brief mechanical tick animation on value changes and a colored connection status LED next to the logo. Also added a favicon and "Add to Home Screen" icon using the app's own logo mark instead of the browser default.

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

### ✅ 3.12 Screen Wake Lock During Active Session
**Status:** ✅ Complete
**Changelog:** `[1.9.0]`
**Priority:** Medium
**Files Modified:** `templates/active_session.html`

The Active screen requests a native screen wake lock (`navigator.wakeLock`) and re-requests it when the tab becomes visible again; leaving the page (Pause/End) releases it. The API only exists in a secure context, so it works on `localhost` and HTTPS but not on a phone loading the app over LAN HTTP.

---

### ✅ 3.13 Live Stats in Browser Tab Title
**Status:** ✅ Complete
**Priority:** Low
**Files Modified:** `templates/base.html`, `templates/active_session.html`

While walking, the tab reads `3.2 mph · 1.40 mi - WalkingDad`: live content first, app name last, the convention most sites follow (tabs truncate from the right). Rendered server-side on load, then updated from the existing `/stats_stream` handler. Pause and End load a new page, which restores the plain title.

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
- **Problem:** `base.html` has exactly one `@media (max-width: 480px)` rule, tuned for phone-width screens. Desktop is the primary use case, but the app's own pitch covers "any browser on your network," and 3.7 (Touch/Swipe Gesture Controls) already anticipates a phone/tablet as a secondary device, so there's nothing tuned for the tablet width range in between.
- **Solution:** Add an intermediate breakpoint (e.g. `max-width: 900px`) tuned for tablet-class screens (iPad-size), rather than jumping straight from desktop layout to the 480px phone rules.
- **Implementation:** Audit `console-hero`/`console-strip`/history table layout at common tablet widths (768-1024px) and add a second `@media` block alongside the existing 480px one.

---

### ✅ 3.18 Transition Hint Shows on Every Button Press
**Status:** ✅ Complete
**Changelog:** `[1.9.0]`
**Priority:** Medium
**Files Modified:** `templates/base.html`, `templates/active_session.html`, `templates/paused_session.html`, `templates/start_session.html`

Submitting a form only disables and dims the buttons now; each page's `#transitioning-hint` follows the SSE `belt_transitioning` flag alone, so it shows only during a real start/pause/resume/end belt sequence, with matching text. A `submitting` flag in `base.html` also keeps the buttons disabled through SSE ticks until the page navigates away (cleared on a back/forward-cache restore), closing a double-tap window. The hint still toggles `display`, so the legitimate post-Start/Resume hint shifts the layout briefly; reserving its space was skipped to avoid a permanent gap on the phone layout.

---

### ✅ 3.19 Phone Layout
**Status:** ✅ Complete
**Changelog:** `[1.9.0]`
**Priority:** Medium
**Files Modified:** `templates/base.html`, `templates/active_session.html`, `templates/paused_session.html`, `templates/start_session.html`, `static/manifest.json`

Added the missing viewport meta, so phones get a real phone layout instead of a scaled-down desktop page. Under 480px: the reading fills the screen with preset and Pause/End rows docked at the bottom in thumb reach, 44px+ tap targets, a single menu button for the header controls, and session history as stacked cards. A web app manifest lets Add to Home Screen launch full-screen. Desktop layout is unchanged.

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
**Changelog:** `[1.8.0]`, `[1.9.0]`
**Priority:** Low
**Files Modified:** `app.py`, `config.py`, `config.json.example`, `templates/base.html`, `templates/start_session.html`, `templates/settings.html`, `README.md`

Verified against current Apple documentation before building (the original plan below, kept for history, assumed a network-fetch design that turned out to need correcting): no Apple Developer Program membership is needed (a personal Shortcut is never distributed), and critically, the Health app does not exist on macOS as of Tahoe 26. This can only run on the iPhone, not the Mac WalkingDad itself runs on.

Two QR codes, both self-contained (no fetch back to WalkingDad's server): a **setup QR** (a plain iCloud share link, `Share → Copy iCloud Link` in the Shortcuts app) installs the Shortcut once per phone, and a **per-session QR** (`shortcuts://run-shortcut?name=...&input=text&text=<session JSON>`) runs it with that session's data embedded directly in the URL. Hosting the `.shortcut` file on GitHub and using `shortcuts://import-shortcut?url=...` was tried first and abandoned. It reliably failed with "shortcut URL provided was invalid" across every URL encoding/ordering tried by hand, a known, documented unreliability of GitHub-hosted `.shortcut` files with that scheme, not an encoding bug on WalkingDad's end. An iCloud link is Apple's actual supported distribution path. The Shortcut itself (`Get Dictionary from Input` → `Log Workout`, Type Walking, Date/Duration/Calories/Distance bound via Magic Variable, then `Log Health Sample` for Steps) was hand-built in the Shortcuts app and shared from there, since there's no official API to generate a `.shortcut` file programmatically. When rebuilding it, `Log Health Sample`'s Value row for Steps only appears after granting Shortcuts write access to Steps.

**Off by default.** An `apple_health_export_enabled` setting (Settings page, styled as a pill/slide toggle) gates the whole feature; when off, neither QR nor the start-screen prompt appears. Turning it on doesn't retroactively surface an old, unrelated session that happened to be most recent before the feature existed: the currently-most-recent session is pre-marked as handled on that specific off→on transition, unless it's from today, in which case it's left showing (plausibly the reason someone would enable the feature mid-session in the first place).

The start screen shows a persistent "Log to Apple Health" banner after a session ends, driven by a `health_logged` flag stored on the session record itself (not a one-shot flash). It survives navigation and reloads, and clears once the Shortcut reports success (an `x-success` callback to `/health_logged/<id>`), on an explicit Dismiss (stored as dismissed, not logged), or once a newer session supersedes it. This check is independent of the unrelated `history_display_limit` setting (querying the single most recent session directly rather than reusing the display-limited history list), so setting that to `0` doesn't silently disable Apple Health export too. Tapping the banner opens an in-page modal (no navigation) with the per-session QR, with a link inside to swap to the setup QR for anyone who hasn't installed the Shortcut yet. The Settings page also keeps a permanent copy of the setup QR (inside the same collapsing toggle section) for reinstalling later. QR rendering is client-side (`qrcodejs` via CDN, matching how `base.html` already pulls Bootstrap/Icons/Fonts), no new Python dependency.

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

### 5.4 Native HealthKit Companion App
- **Status:** Planned
- **Priority:** Low
- **Problem:** Shortcuts can't log what KS Fit (the WalkingPad's own app) does. As of iOS 27, `Log Workout` takes only type, date, duration, calories and distance: "Walking" has no indoor variant and there's no metadata field, so every session shows as an outdoor walk. `Log Health Sample` has no End Date, so steps are stamped at the session start, and no Shortcuts action (built-in, or third-party such as Actions or Toolbox Pro) can attach samples to a workout.
- **Solution:** A small iOS app whose only job is one Shortcuts action, "Log Treadmill Walk", that takes the same JSON WalkingDad already sends. The installed Shortcut swaps `Log Workout` + `Log Health Sample` for that one action; WalkingDad and the `x-success` callback don't change.
- **Implementation:** An App Intent using `HKWorkoutBuilder`: `.walking` with `HKMetadataKeyIndoorWorkout = true`, energy and distance samples, and a step-count sample spanning start to end, added to the builder so they're attached to the workout. HealthKit is available to a free Apple ID (Personal Team in Xcode; Apple's "Supported capabilities" table lists it for free accounts), so no $99 membership is needed. The catch is that free provisioning expires every 7 days and has to be reinstalled from Xcode on a Mac, and it can't be distributed to anyone else. Prototype first: KS Fit's own workouts don't attach steps either, and Fitness may not show workout steps even when attached, so check that the Indoor Walk label and steps actually appear before committing to the 7-day reinstalls.

---

## Notes

- Items are grouped by theme (Area), not by execution order; each item's own **Priority** is what signals urgency, not its Area or position in the list.
- New features or bug fixes discovered during development may be added to this roadmap.
- **Google Fit / Health Connect native sync was considered and declined**: the only free path requires a native companion app using the platform SDK (Health Connect has no web/API route in), which is out of scope while WalkingDad stays a pure web app. Revisit only if that constraint changes; 5.2 (GPX/TCX export) is the current cross-platform fallback.
- For questions or feature requests, open an issue on the project repository.
