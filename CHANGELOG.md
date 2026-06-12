# Changelog

All notable changes to WalkingDad will be documented in this file.

## [1.3.0] — 2026-06-12

### Fixed

- **Session start time was always wrong** — `session_history.json` recorded the save time as both `start_time` and `end_time`. Added `_session_start_time` tracked from the moment the session begins; records now show the correct start and end times independently.
- **CSV export race condition** — `_load_full_session_history()` (called by `/export_csv`) read `session_history.json` without holding `_history_lock`, meaning a concurrent session save could produce a corrupt read. Now consistently lock-protected like every other history I/O call.
- **Session timer ran ~10% fast** — The stats monitor incremented the timer by one tick then slept for one second, but the `ask_stats()` poll itself takes ~100ms, causing ~6 minutes of drift over a 60-minute session. Timer now uses `time.monotonic()` so elapsed time reflects the wall clock, not loop iterations.

### Changed

- **Belt-control routes are now POST-only** — `/start`, `/pause`, `/resume`, `/end_session`, and all speed controls previously accepted GET requests, meaning browser prefetch, back-button navigation, or a stray link preview could accidentally send a belt command. All action routes now require POST; templates updated from `<a href>` links to `<form method="post">` buttons.

### Internal

- Merged `_load_full_session_history()` into `_load_session_history(limit=None)` — the two functions were identical except for a limit slice and the missing lock on the full variant.
- Extracted `showShutdownOverlay()` into `base.html` — the shutdown DOM block was copy-pasted verbatim across three templates.
- Removed duplicate `KMH_TO_MPH` constant (identical to `KM_TO_MI`); removed unused `sys` import; removed stale logging comment.
- Removed `/manual_reconnect` route alias; `run.py` kwargs dict renamed from `startupinfo` to `popen_kwargs`; dropped a `time.sleep(0.5)` in `end_session` that followed a fire-and-forget coroutine dispatch.

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
