# Changelog

All notable changes to WalkingDad will be documented in this file.

## [1.5.2] — 2026-07-15

### Fixed

- **Stats could silently freeze mid-session** — If the BLE status-read path (`ask_stats()`) degraded while writes kept working, a session could start normally (belt moving) but speed/distance/steps/calories would stay frozen at zero indefinitely while Time kept ticking, with no error surfaced. `_stats_monitor()` now tracks consecutive `ask_stats()` failures and treats 5 in a row as a dead connection, triggering the same disconnect/reconnect flow ("Connection Failed — Try Again") already used for other BLE disconnects.

---

## [1.5.1] — 2026-07-15

### Added

- **Eleven new roadmap items, including two new phases** — `ROADMAP.md` now tracks: Auto-End Stale Paused Session (1.5), QR Code for LAN Access (2.6), Touch/Swipe Gesture Speed Controls (2.7), Self-Hosted Static Assets (3.4), and Interval/Programmed Speed Sequences (4.4) in existing phases, plus two new phases — **Phase 5: Stats & Motivation** (Personal Records, Daily/Weekly Goals, Trend Summaries, Per-User Profiles) and **Phase 6: Data Export & Integrations** (Apple Health export via Shortcuts, generic GPX/TCX export). All Planned, no code changes yet.

---

## [1.5.0] — 2026-07-15

### Added

- **Session state persistence** — Cumulative session stats (time, distance, steps, calories) now survive a server crash or restart instead of being silently lost. In-progress state is snapshotted to `session_state.json` every 5 seconds, on every start/pause/resume transition, and immediately on auto-pause. On next launch, a leftover state file is offered back as a **Restore Session** / **Discard** prompt on the start screen; restoring lands the session in paused state so a device reconnect is required before belt motion resumes. The file is atomically written (temp file + rename) and automatically cleaned up on any clean exit (`End Session`, Ctrl+C, `/shutdown`), so a normal exit never shows a stale restore prompt.

---

## [1.4.0] — 2026-07-08

### Added

- **External configuration file** — All user-tunable settings are now loaded from `config.json` (copy `config.json.example` to get started). Running without the file uses built-in defaults identical to the previous hardcoded values.
- **Settings page** — A gear icon in the header opens a Settings page where all configurable options — device name, speed limits, calorie constant, server port, and more — can be changed in plain language without editing any files. Changes to most settings take effect immediately; host and port require a restart.

---

## [1.3.0] — 2026-07-08

### Fixed

- **Session start time was always wrong** — `session_history.json` recorded the save time as both `start_time` and `end_time`. Records now capture the real start time from when the session begins.
- **CSV export race condition** — A missing lock on the history read in `/export_csv` meant a concurrent session save could corrupt the data.
- **Session timer ran ~10% fast** — Poll overhead (~100ms per cycle) wasn't accounted for, causing ~6 minutes of drift over a 60-minute session. Timer now tracks wall-clock elapsed time.
- **Pausing then immediately resuming could leave the belt stopped** — Two separate races let the pause and resume sequences execute concurrently, interleaving their BLE commands on the device. Belt sequences now cancel any prior in-flight sequence before executing, guaranteeing serial execution.
- **Ending a session right after Resume could restart the belt** — The in-flight resume sequence had no way to be cancelled, so it could keep sending device commands after the session was already considered over. End session now cancels any in-flight sequence first.
- **Double-tapping Start/Pause/Resume/End could trigger duplicate belt commands** — A missing lock between the state-check and dispatch let two simultaneous taps both pass the guard. The four routes are now serialised with a lock.

### Changed

- **Belt-control routes are now POST-only** — `/start`, `/pause`, `/resume`, `/end_session`, and all speed controls now require POST; templates updated from `<a href>` links to `<form method="post">` buttons. Prevents browser prefetch, back-navigation, or a stray link preview from accidentally sending a belt command.
- **Action buttons disable immediately on tap and while the belt is transitioning** — Buttons on the active and paused screens disable on submit so a double-tap cannot fire a second command before the page reloads. On the paused screen, Resume and End Session also remain disabled while the pause sequence is still in progress, with a "Pausing belt…" indicator.

### Internal

- Merged `_load_full_session_history()` into `_load_session_history(limit=None)` — the two functions were identical except for a limit slice and a missing lock on the full variant.
- Extracted `showShutdownOverlay()` into `base.html` — the shutdown DOM block was copy-pasted verbatim across three templates.
- Extracted `_cancel_stats_monitor()`, `_cancel_belt_sequence()`, and `_wake_and_start_belt()` helpers — deduplicated repeated monitor-cancel and device wake-up sequences shared by start, pause, and resume.
- Named `RESUME_GRACE_PERIOD_SECONDS` constant; removed dead `_session_start_time or datetime.now()` fallback in `_build_session_record()`.
- Removed duplicate `KMH_TO_MPH` constant (identical to `KM_TO_MI`); removed unused `sys` import; removed stale logging comment.
- Removed `/manual_reconnect` route alias; `run.py` kwargs dict renamed from `startupinfo` to `popen_kwargs`.

---

## [1.2.1] — 2026-05-05

### Changed

- **Updated screenshots**
- **Added logo**

---

## [1.2.0] — 2026-05-02

### Added

- **Session History Log** — Completed sessions are saved to `session_history.json` with date, time, duration, distance (km/mi), steps, calories, and average speed (km/h and mph). Start screen displays the last 10 sessions in a responsive table. Red "End Session" button on Active and Paused screens explicitly ends a session and saves it. CSV export (`/export_csv`) and Clear History functionality included. Thread-safe file I/O via `threading.Lock()`. In-progress sessions are captured on graceful shutdown (Ctrl+C, Close). Corrupted history files are handled gracefully.

### Fixed

- **Session history table dark mode** — Table background was not following the Light / Dark theme toggle, causing white-on-white (invisible) text in dark mode. Removed Bootstrap `.table` class dependency and rewrote all table styling from scratch using CSS custom properties so every color (background, borders, hover, text) properly switches with the theme toggle.

---

## [1.1.0] — 2026-05-01

### Fixed

- **Graceful shutdown overhaul** — Replaced hardcoded delays with proper coroutine synchronization (`fut.result(timeout=10)`), `os._exit(0)` for reliable Waitress termination (Waitress suppresses `SystemExit` from `sys.exit()`), and `atexit` safety net for unexpected exits
- **Ctrl+C no longer leaves belt running** — Added `SIGTERM`/`SIGINT` signal handlers that trigger device cleanup (stop belt, standby mode, BLE disconnect)
- **Process-isolated Waitress subprocess** — `run.py` launches Waitress via `os.setsid()` so Ctrl+C only hits the wrapper process, not the server directly; gives `/shutdown` HTTP endpoint time to complete cleanly
- **Web UI shutdown notification** — When you press Ctrl+C or click Close, all session pages show "Server is shutting down. You may close this window." instead of silently going dead
- **Thread-safe shutdown flag** — `_shutting_down` protected by `threading.Lock()` to prevent duplicate/racy shutdown attempts across Flask, signal, and background threads
- **Shutdown during BLE scanning** — Clicking Close while the app is still scanning for the device no longer crashes with `RuntimeError: Event loop stopped before Future completed`; connection attempt now gracefully exits when loop is stopped

---

## [1.0.0] — 2026-05-01

### Added

- **Dark mode** — Three-state toggle (Light / Dark / System) with localStorage persistence and automatic OS preference following
- **Cross-platform BLE reliability** — Context manager scanning, exponential backoff retry, event loop cleanup, Bleak API version fallbacks, and stats monitor lifecycle fixes across macOS, Windows, and Linux

### Changed

- **Rebranded from "WalkingPad Web Controller" to "WalkingDad"** across all templates, scripts, and docs
- **README rewrite** — Condensed from 229 to 96 lines; new tone, structure, and quick-start flow
- **Screenshots** — Updated all three (start, active, paused) with dark mode visuals
- **requirements.txt** — Sorted alphabetically for consistency
- **.gitignore** — Added `.DS_Store` exclusion

### Fixed

- macOS CoreBluetooth connection reliability

---

## [0.x] — Pre-release History

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
