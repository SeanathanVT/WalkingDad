# Changelog

All notable changes to WalkingDad will be documented in this file.

## [Unreleased]

### Added

- **Keyboard shortcuts** (ROADMAP 3.3). `Space` pauses or resumes; `↑`/`W` and `↓`/`S` change speed; `L`, `K`, and `M` pick the Slow, Moderate, and Max presets. Press `?` for the list, or hover a button to see its key. Start and End Session have no shortcut. Turn them off with the **Enable keyboard shortcuts** switch on the Settings page (`keyboard_shortcuts_enabled`).
- **The browser tab shows your speed and distance while walking** (ROADMAP 3.13), e.g. `3.2 mph · 1.40 mi - WalkingDad`.

### Changed

- **Dependency versions are bounded** (ROADMAP 2.6). `requirements.txt` now sets a tested minimum and blocks the next major version of each package, so a fresh install can't pull in a breaking release.

### Fixed

- **A quick second press can no longer undo Start, Pause, Resume, or End.** The next screen used to load with its buttons enabled for about a second, so a double-tap on Pause could resume the belt while it was still stopping. Buttons now stay disabled until the belt finishes, and a page brought back with the browser's Back or Forward button reloads instead of showing stale, enabled buttons.
- **End Session right after Pause always stops the belt.** Ending a session (e.g. from a second device) while a Pause was still reaching the treadmill could cancel the Pause before it stopped the belt, leaving it running.
- **Speed changes no longer land on top of a Start, Resume, Pause, or End.** A speed button pressed while the belt was still starting or resuming (e.g. from a second device) could take effect right after it, from a near-zero reading; it's now ignored until the belt is moving. A speed change still on its way when you pause or end could reach the treadmill after it stopped; Pause and End now cancel it first.
- **Speed buttons and Resume no longer land 0.1 km/h short.** A rounding error made some steps fall short, e.g. 3.8 + 0.6 km/h set 4.3 instead of 4.4.

### Internal

- **`AGENTS.md` asks agents to follow common UI conventions and WCAG 2.2 AA** wherever practical, and to call out deliberate deviations in their designs.
- **`AGENTS.md` asks agents to run new or changed tests on every Python version CI uses**, after a test passed locally on 3.12 but failed in CI on 3.10.

---

## [1.9.0] (2026-10-08)

### Added

- **Add to Home Screen opens WalkingDad full-screen** (ROADMAP 3.19), like an app, without the browser's address and tool bars.
- **Log to Apple Health from the iPhone or iPad running WalkingDad.** A phone can't scan its own screen, so on iPhone/iPad the per-session QR code is now a **Log to Apple Health** button, and the setup QR on the Settings page is an **Install Shortcut** button. Computers still show the QR codes, since a Mac can't write to Apple Health.
- **The Log to Apple Health prompt clears itself once the workout is logged**, on every open WalkingDad page. After the Shortcut finishes, the phone opens WalkingDad in Safari, which marks that session as logged. Dismissing the prompt is now recorded separately from logging. The installed Shortcut doesn't need to change.
- **See which sessions were logged to Apple Health, and log missed ones later.** With Apple Health export on, each session in Recent Sessions shows a filled heart once logged and an outline heart if not. **Edit** adds a **Log** button to each unlogged session.
- **Apple Health export logs steps.** WalkingDad now sends the session's step count to the Shortcut, which logs it as a Steps sample alongside the workout. Reinstall the Shortcut from Settings to pick this up; the old one keeps working without steps.
- **Delete individual sessions.** **Edit** also adds a delete button to each session in Recent Sessions, handy for removing test walks without clearing everything.
- **The screen stays on during an active session** (ROADMAP 3.12), and returns to normal on Pause or End. Browsers only allow this on `localhost` or HTTPS, so it works on the computer running WalkingDad but not on a phone connecting over your network.

### Changed

- **WalkingDad now has a real phone layout** (ROADMAP 3.19). Phones previously showed a shrunken desktop page. Now the speed reading fills the screen, the preset and Pause/End buttons sit full-width at the bottom within thumb reach, every button is large enough to tap while walking, the header controls fold into one menu button, and Recent Sessions shows as cards instead of a wide table. The desktop layout is unchanged.
- **Buttons show a pressed state when clicked or tapped**, instead of fading to transparent while held.
- **Everything WalkingDad writes now lives in a `data/` folder** instead of next to the code: settings (`data/config.json`), the session database, crash-recovery state, and the old JSON-migration backups (`data/backups/`). Existing files move there automatically the first time you start this version; nothing is overwritten. To edit settings by hand, use `data/config.json`.
- **`/reconnect` is now POST-only**, like the belt-control routes. The Connect and Try Again buttons on the connecting screen look and work the same.

### Fixed

- **Other websites can no longer control the treadmill** (ROADMAP 2.2). A page open in your browser could previously submit Start, speed, Clear History, Bluetooth reconnect, or Shutdown requests to WalkingDad behind your back. Requests that come from another website are now refused. Using WalkingDad from any device on your network works exactly as before.
- **Pause and speed buttons no longer error out when Bluetooth isn't running.** If the Bluetooth connection had already gone away, pressing Pause or a speed button showed a server error page. Pause now still pauses the session, and the speed buttons do nothing.
- **A corrupted crash-recovery file no longer stops the app from starting.** If `session_state.json` contained unreadable (non-UTF-8) bytes, startup crashed. Startup now ignores the file, the same as an unparseable one.
- **Speed buttons no longer flash a "Getting the belt moving…" message** (ROADMAP 3.18). Presets and the +/- buttons briefly showed it and pushed the buttons down, and Start, Pause, Resume and End flashed the wrong message ("Stopping belt…", "Pausing belt…"). The message now appears only while the belt is actually starting, pausing or stopping.
- **A quick double-tap can no longer send a button press twice.** Buttons could re-enable mid-request on a live stats update; they now stay disabled from the moment you press one until the next screen loads.

### Internal

- **Tests now cover nearly all of the Python code** (ROADMAP 2.5): routes, Bluetooth sequences against a fake treadmill, status-packet math, persistence, and `run.py`'s launcher. The suite runs in about a second.
- **Continuous integration** (ROADMAP 2.7): `ruff` lint and the test suite run on pull/merge requests and on pushes to `main`/`development` via GitHub Actions (Python 3.10 and 3.13) and GitLab CI (Python 3.12, with coverage shown in merge requests). `ruff` is pinned in `requirements-dev.txt`, and lint fixes in `app.py` (imports sorted, unused `global` declarations removed) have no behavior change.
- **Importing `app.py` with `WALKINGDAD_NO_STARTUP=1` no longer installs the Ctrl+C/SIGTERM handlers or the `atexit` hook**, so pressing Ctrl+C during a test run interrupts pytest instead of killing the process two seconds later. Running the app normally is unchanged.
- **pytest, ruff, and coverage settings merged into `pyproject.toml`**, replacing `pytest.ini`, `ruff.toml`, and `.coveragerc`. `requirements-dev.txt` adds `coverage[toml]` so coverage reads it on Python 3.10.
- **`run.py`'s launcher logic moved into a `main()` function** so it can be tested. `python run.py` behaves the same.
- **`AGENTS.md` added for AI coding agents** (`CLAUDE.md` is a symlink to it): keep docs in sync with code, the git workflow (PRs target `development`, the maintainer commits), the desktop-first design target, and the supported platforms.

---

## [1.8.0] (2026-10-05)

### Added

- **Apple Health export**. Off by default (Settings page toggle). Once enabled, a "Log to Apple Health" prompt appears on the start screen after a session ends; scanning the QR it shows runs a Shortcut on your iPhone that logs the session as a Workout via `Log Workout`, no manual re-entry. A separate one-time setup QR (Settings page) installs the Shortcut itself via an iCloud share link. No Apple Developer account, no network fetch back to WalkingDad's server for either QR. See the README's Apple Health Export section.
- **Automatic reconnect**. When the Bluetooth connection drops unexpectedly, the app now retries on its own (up to 8 attempts over about 7-8 minutes) instead of going straight to **Connection Failed, Try Again**. Walking briefly out of range or a transient glitch no longer needs a click. A session that was walking is paused by the drop; press Resume once reconnected. **Try Again** still appears if every attempt fails, for example when the pad is switched off.
- **Per-second session samples and pause log**. Every session now records speed, distance, and steps about once per second while walking (sparser while paused), plus each pause with its reason: manual, auto (stepped off or Bluetooth dropped), or shutdown (crash downtime). Nothing displays these yet; they're the data future TCX/FIT export, charts, and personal records will read.
- **Auto-end for forgotten paused sessions**. A session left paused longer than `stale_pause_timeout_minutes` (default 30, set on the Settings page; `0` turns it off) is ended and saved to history automatically, as if you had pressed End Session.
- **`database_path` setting**. Where the new database lives (default `walkingdad.db` in the app directory). Requires a restart.

### Changed

- **Session history moved from `session_history.json` to SQLite (`walkingdad.db`)**. On first launch after updating, your existing history is imported automatically: the original file is first copied to `session_history.json.bak-<timestamp>`, then renamed to `session_history.json.migrated` once the import succeeds. A history file that can't be read is left untouched and not marked as imported, so fixing it and restarting retries the import. The start-screen table, Apple Health prompt, and Clear History behave as before. CSV export keeps its existing columns in the same order and adds two at the end: `id` and `has_samples`.
- **Crash recovery keeps sessions whole**. Restoring an interrupted session continues the same database record instead of starting over, and Discard removes it completely. A session interrupted by a crash and never restored is no longer lost: on the next launch it's saved from its last recorded sample, or dropped if it crashed before recording any.

### Fixed

- **Active walking time now comes from the treadmill's own speed reports**, counting only stretches where the belt was actually moving, instead of a stopwatch running whenever the app thought you were walking. This affects the history duration, CSV export, Apple Health export, and average speed. The startup countdown no longer counts, and time is no longer lost rounding at each pause.
- **A belt that stops (or never starts) right after Start or Resume now auto-pauses.** Before, stepping off within the first few seconds left the session showing as walking indefinitely, with time still accumulating.
- **Clear error when the port is taken.** If port 5001 (or your configured `port`) is already in use, often by a second WalkingDad, startup now stops with a message naming the port and how to fix it, instead of a Waitress traceback after the browser opens. A `port` outside 1-65535, a `host` that doesn't resolve, and ports the OS won't allow (below 1024 on Linux without root, or reserved by Windows) get their own messages. The Windows launcher window stays open so you can read it.

### Internal

- **First automated tests**. `pytest` suite under `tests/` covering storage, the JSON migration, unit conversions, sample capture, and in `app.py` active-time accrual, auto-pause detection, and the stale-pause check, plus `run.py`'s port-in-use check. Importing `app.py` with `WALKINGDAD_NO_STARTUP=1` (set by `tests/conftest.py`) skips its startup, so tests never touch the real database or treadmill. Install with `requirements-dev.txt`; see the README.

---

## [1.7.0] (2026-08-26)

### Added

- **Selectable color themes**: A new palette icon in the header lets you pick a color theme (Slate is the default; the rest run Red, Amber, Lime, Forest, Teal, Cyan, Blue, Violet, and Pink in spectral order) independently of the Light/Dark/System toggle, so any combination (e.g. Teal + Dark) is available. The 9 hued options are evenly spaced around the color wheel so each reads as clearly distinct in the picker. Each theme tints the whole surface, not just buttons: backgrounds, cards, and the browser's own chrome (Safari's tab bar color, Android's address bar, etc.) all follow along now.
- **"Special" theme group**: Virginia Tech (brand colors and typography verified against VT's own site and guidelines, including Chicago Maroon, Impact Orange, Rubik/Crimson Text as their documented substitutes for the licensed Acherus Grotesque/Gineso, tightened heading tracking, and a square-dot/rule accent drawn from VT's own design-elements guide), Bloom, Tide, Harvest, and Frost (in calendar order), each with a two-tone swatch and a heading font fetched only if selected. Bloom (spring, green/pink, Quicksand headings) adds drifting cherry blossom petals; Tide (summer, turquoise/sand, Pacifico headings) adds crabs scuttling along the bottom of the screen. Harvest is mode-aware: cozy autumn in Light mode (Rye headings, falling leaves, orange/gold/brown) and spooky Halloween in Dark mode (Creepster headings, glowing eyes in the dark, orange/purple). Frost adds a falling-snow effect. All ambient effects are non-interactive and respect reduced-motion settings.
- **Console-style interface**: Active, Paused, and Start now read like the WalkingPad's own onboard display instead of a generic dashboard, with one large live reading in a tabular-digit instrument face (Speed while walking, elapsed time while paused, a Start control when idle) and Time/Distance/Steps/Calories in a compact readout strip below. Values tick over with a brief mechanical animation when they change, and connection status is now a small colored status light next to the logo. A new Slow speed preset (your configured speed floor) joins the renamed Moderate and existing Max.
- **Favicon and home-screen icon**: The browser tab and "Add to Home Screen" icon now use the app's own logo mark instead of the browser default.

### Fixed

- Navigating between pages could briefly show the default appearance before your saved Light/Dark/System and color-theme selections applied. Now applied before first paint.
- The "Connecting…" screen reloaded the whole page on a fixed 3-second timer regardless of whether anything had actually changed, causing a jarring flash/reset every cycle. It now only reloads once the connection attempt actually succeeds or gives up.

### Internal

- Extracted shared theme/font/ambient-effect rendering, plus the console screens' stat-tick and button-disable behavior, into common functions so every screen and the color-theme picker stay in sync without duplicating logic.

---

## [1.6.0] (2026-08-25)

### Added

- **Real-time stats via Server-Sent Events**. Replaced the polling loops on the active, paused, and start screens with a single streaming connection, cutting update latency and HTTP overhead.
- **Connection watchdog while paused or idle**. The app now detects a lost Bluetooth connection even when a session is paused or no session is running, not just while actively walking. A dropped connection now always shows **Connection Failed, Try Again** instead of a stats display that's quietly stopped updating.
- **Configurable server thread count**. The `waitress_threads` setting (Settings page or `config.json`) controls how many concurrent connections the server can handle; previously hardcoded.

### Fixed

- Stats could silently freeze mid-session. Speed/distance/steps/calories could stay stuck at zero with no error shown if the connection stopped delivering real updates.
- Rare cases where reconnecting to the treadmill, or a stuck Bluetooth command while connecting, starting, or resuming, could leave the app unresponsive with no way to recover except restarting it.
- Changing speed during an active session could occasionally interfere with the live stats connection.
- Entering an invalid value on the Settings page could show an error instead of being handled gracefully.
- Resuming a paused session reset the treadmill's own onboard display/counters in the common case. Resume now tries a lighter wake-up first and only falls back to the display-resetting sequence if the belt genuinely needs it (e.g. after a long pause).
- The active-session screen's controls were clickable for a moment right after Start or Resume, before the belt had actually finished responding. They're now disabled (matching the paused screen's existing behavior) until the belt command in flight completes.
- A resumed session's grace period (which avoids mistaking a normal restart for an unexpected stop) could be cut short by a slow reconnection, or set low enough in Settings to defeat it entirely.
- Session history could be left corrupted if the app crashed while saving it.
- Reconnecting to the treadmill left the previous connection's background thread running indefinitely instead of closing it, harmless but wasteful over a long-running instance with several reconnects.
- The documented minimum Python version (3.8+) was wrong. The app actually requires 3.10+ and would fail to start on older versions. Corrected in the README and ROADMAP.

### Internal

- Consolidated repeated settings-page and connection-monitoring code into shared helpers.
- Extracted shared code for building the stats payload and wiring up the client-side live connection.
- Hardened Bluetooth disconnect handling for correctness on platforms where the disconnect notification can arrive on a different thread than expected.

---

## [1.5.1] (2026-07-15)

### Added

- **Eleven new roadmap items, including two new phases**. `ROADMAP.md` now tracks: Auto-End Stale Paused Session (1.5), QR Code for LAN Access (2.6), Touch/Swipe Gesture Speed Controls (2.7), Self-Hosted Static Assets (3.4), and Interval/Programmed Speed Sequences (4.4) in existing phases, plus two new phases, **Phase 5: Stats & Motivation** (Personal Records, Daily/Weekly Goals, Trend Summaries, Per-User Profiles) and **Phase 6: Data Export & Integrations** (Apple Health export via Shortcuts, generic GPX/TCX export). All Planned, no code changes yet.

---

## [1.5.0] (2026-07-15)

### Added

- **Session state persistence**. Cumulative session stats (time, distance, steps, calories) now survive a server crash or restart instead of being silently lost. In-progress state is snapshotted to `session_state.json` every 5 seconds, on every start/pause/resume transition, and immediately on auto-pause. On next launch, a leftover state file is offered back as a **Restore Session** / **Discard** prompt on the start screen; restoring lands the session in paused state so a device reconnect is required before belt motion resumes. The file is atomically written (temp file + rename) and automatically cleaned up on any clean exit (`End Session`, Ctrl+C, `/shutdown`), so a normal exit never shows a stale restore prompt.

---

## [1.4.0] (2026-07-08)

### Added

- **External configuration file**. All user-tunable settings are now loaded from `config.json` (copy `config.json.example` to get started). Running without the file uses built-in defaults identical to the previous hardcoded values.
- **Settings page**. A gear icon in the header opens a Settings page where all configurable options (device name, speed limits, calorie constant, server port, and more) can be changed in plain language without editing any files. Changes to most settings take effect immediately; host and port require a restart.

---

## [1.3.0] (2026-07-08)

### Fixed

- **Session start time was always wrong**. `session_history.json` recorded the save time as both `start_time` and `end_time`. Records now capture the real start time from when the session begins.
- **CSV export race condition**. A missing lock on the history read in `/export_csv` meant a concurrent session save could corrupt the data.
- **Session timer ran ~10% fast**. Poll overhead (~100ms per cycle) wasn't accounted for, causing ~6 minutes of drift over a 60-minute session. Timer now tracks wall-clock elapsed time.
- **Pausing then immediately resuming could leave the belt stopped**. Two separate races let the pause and resume sequences execute concurrently, interleaving their BLE commands on the device. Belt sequences now cancel any prior in-flight sequence before executing, guaranteeing serial execution.
- **Ending a session right after Resume could restart the belt**. The in-flight resume sequence had no way to be cancelled, so it could keep sending device commands after the session was already considered over. End session now cancels any in-flight sequence first.
- **Double-tapping Start/Pause/Resume/End could trigger duplicate belt commands**. A missing lock between the state-check and dispatch let two simultaneous taps both pass the guard. The four routes are now serialised with a lock.

### Changed

- **Belt-control routes are now POST-only**. `/start`, `/pause`, `/resume`, `/end_session`, and all speed controls now require POST; templates updated from `<a href>` links to `<form method="post">` buttons. Prevents browser prefetch, back-navigation, or a stray link preview from accidentally sending a belt command.
- **Action buttons disable immediately on tap and while the belt is transitioning**. Buttons on the active and paused screens disable on submit so a double-tap cannot fire a second command before the page reloads. On the paused screen, Resume and End Session also remain disabled while the pause sequence is still in progress, with a "Pausing belt…" indicator.

### Internal

- Merged `_load_full_session_history()` into `_load_session_history(limit=None)`. The two functions were identical except for a limit slice and a missing lock on the full variant.
- Extracted `showShutdownOverlay()` into `base.html`. The shutdown DOM block was copy-pasted verbatim across three templates.
- Extracted `_cancel_stats_monitor()`, `_cancel_belt_sequence()`, and `_wake_and_start_belt()` helpers. Deduplicated repeated monitor-cancel and device wake-up sequences shared by start, pause, and resume.
- Named `RESUME_GRACE_PERIOD_SECONDS` constant; removed dead `_session_start_time or datetime.now()` fallback in `_build_session_record()`.
- Removed duplicate `KMH_TO_MPH` constant (identical to `KM_TO_MI`); removed unused `sys` import; removed stale logging comment.
- Removed `/manual_reconnect` route alias; `run.py` kwargs dict renamed from `startupinfo` to `popen_kwargs`.

---

## [1.2.1] (2026-05-05)

### Changed

- **Updated screenshots**
- **Added logo**

---

## [1.2.0] (2026-05-02)

### Added

- **Session History Log**. Completed sessions are saved to `session_history.json` with date, time, duration, distance (km/mi), steps, calories, and average speed (km/h and mph). Start screen displays the last 10 sessions in a responsive table. Red "End Session" button on Active and Paused screens explicitly ends a session and saves it. CSV export (`/export_csv`) and Clear History functionality included. Thread-safe file I/O via `threading.Lock()`. In-progress sessions are captured on graceful shutdown (Ctrl+C, Close). Corrupted history files are handled gracefully.

### Fixed

- **Session history table dark mode**. Table background was not following the Light / Dark theme toggle, causing white-on-white (invisible) text in dark mode. Removed Bootstrap `.table` class dependency and rewrote all table styling from scratch using CSS custom properties so every color (background, borders, hover, text) properly switches with the theme toggle.

---

## [1.1.0] (2026-05-01)

### Fixed

- **Graceful shutdown overhaul**. Replaced hardcoded delays with proper coroutine synchronization (`fut.result(timeout=10)`), `os._exit(0)` for reliable Waitress termination (Waitress suppresses `SystemExit` from `sys.exit()`), and `atexit` safety net for unexpected exits
- **Ctrl+C no longer leaves belt running**. Added `SIGTERM`/`SIGINT` signal handlers that trigger device cleanup (stop belt, standby mode, BLE disconnect)
- **Process-isolated Waitress subprocess**. `run.py` launches Waitress via `os.setsid()` so Ctrl+C only hits the wrapper process, not the server directly; gives `/shutdown` HTTP endpoint time to complete cleanly
- **Web UI shutdown notification**. When you press Ctrl+C or click Close, all session pages show "Server is shutting down. You may close this window." instead of silently going dead
- **Thread-safe shutdown flag**. `_shutting_down` protected by `threading.Lock()` to prevent duplicate/racy shutdown attempts across Flask, signal, and background threads
- **Shutdown during BLE scanning**. Clicking Close while the app is still scanning for the device no longer crashes with `RuntimeError: Event loop stopped before Future completed`; connection attempt now gracefully exits when loop is stopped

---

## [1.0.0] (2026-05-01)

### Added

- **Dark mode**. Three-state toggle (Light / Dark / System) with localStorage persistence and automatic OS preference following
- **Cross-platform BLE reliability**. Context manager scanning, exponential backoff retry, event loop cleanup, Bleak API version fallbacks, and stats monitor lifecycle fixes across macOS, Windows, and Linux

### Changed

- **Rebranded from "WalkingPad Web Controller" to "WalkingDad"** across all templates, scripts, and docs
- **README rewrite**. Condensed from 229 to 96 lines; new tone, structure, and quick-start flow
- **Screenshots**. Updated all three (start, active, paused) with dark mode visuals
- **requirements.txt**. Sorted alphabetically for consistency
- **.gitignore**. Added `.DS_Store` exclusion

### Fixed

- macOS CoreBluetooth connection reliability

---

## [0.x] (Pre-release History)

Aggregated from the original walkingpad app pre-fork commits:

- First commit, basic BLE connectivity and belt control
- Speed controls with adjustable step increments
- UI redesign with stat cards and session screens
- Connection status indicator (Bootstrap Icons)
- Auto-browser launch on startup
- Session pause/resume with outside-pause detection (remote button)
- Sleep state bugfixes
- Slow speed preset button
- Cumulative timer across pauses
- Production-ready logging, debug removal
- Windows batch launcher script

---

*Format inspired by [Keep a Changelog](https://keepachangelog.com/).*
