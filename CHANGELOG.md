# Changelog

All notable changes to WalkingDad will be documented in this file.

## [1.0.0] — 2026-05-01

### Added

- **Dark mode** — Three-state toggle (Light → Dark → System) with localStorage persistence and automatic OS preference following
- **Cross-platform BLE reliability** — Context manager scanning, exponential backoff retry, event loop cleanup, Bleak API version fallbacks, and stats monitor lifecycle fixes across macOS, Windows, and Linux
- **Smart auto-pause & resume** — Detects when you step off the pad or stop via remote; remembers your speed with a 7-second grace period to prevent re-triggering on restart
- **Speed presets** — Max speed, slow walk, and incremental increase/decrease buttons (all configurable in `app.py`)
- **Cumulative session stats** — Distance, steps, calories, and active time persist across pause/resume cycles
- **Web UI** — Responsive Bootstrap 5.3 interface with real-time stat updates, connection status indicator, and clean shutdown button
- **Windows launch shortcut** — `start_app.bat` for one-click startup
- **Project documentation** — README, ROADMAP, LICENSE

### Changed

- **Rebranded from "WalkingPad Web Controller" to "WalkingDad"** across all templates, scripts, and docs
- **README rewrite** — Condensed from 229 to 96 lines; new tone, structure, and quick-start flow
- **Screenshots** — Updated all three (start, active, paused) with dark mode visuals
- **requirements.txt** — Sorted alphabetically for consistency
- **.gitignore** — Added `.DS_Store` exclusion

### Fixed

- Resume speed bug after stepping off the pad (uses oldest speed from 15-sample buffer to skip deceleration noise)
- Stats monitor not updating after pause/resume cycle (global task lifecycle fix)
- Event loop resource leaks on shutdown
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