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
| **Configurable Device Name** | `BLE_DEVICE_NAME` constant at top of `app.py` — users change device name without touching scanner logic |
| **Bleak API Version Compatibility** | Supports both `set_disconn_callback()` (newer) and `set_disconnected_callback()` (older) — works across Bleak versions |
| **Stats Monitor Lifecycle Fix** | Global `_stats_monitor_task` tracks active monitor; old tasks cancelled before new ones on resume; cleaned up on disconnect. Fixes metrics-not-updating-after-pause/resume bug |

**Compatibility:** macOS 12+, Windows, Linux · Bleak 0.19+ · Python 3.8+

---

### 1.2 Graceful Shutdown
- **Status:** Planned
- **Problem:** The `/shutdown` route uses `os._exit(0)`, which is a hard process kill. This leaves the treadmill in an undefined state (belt may still be running, BLE connection not cleanly closed).
- **Solution:** Replace `os._exit(0)` with a proper shutdown sequence:
    1. Stop the belt if a session is active (`controller.stop_belt()`)
    2. Cancel the stats monitor task
    3. Switch device to standby mode
    4. Stop the BLE event loop gracefully
    5. Exit the Flask/Waitress server
- **Implementation:** Refactor `shutdown()` route to queue a cleanup coroutine on `ble_loop`, then signal the Waitress server to stop.

---

### 1.3 Session State Persistence
- **Status:** Planned
- **Problem:** All cumulative stats (time, distance, steps, calories) are lost if the server restarts or crashes mid-session.
- **Solution:** Persist session state to a local JSON file (`session_state.json`) after each stat update, and restore on startup if a session was active.
- **Implementation:**
    - Add `session_state.json` in the project directory
    - Write state periodically (e.g., every 5 seconds) or on state changes
    - On app start, check for existing state file and offer to restore the previous session
- **File:** `session_state.json` (auto-generated)

---

### 1.4 Automatic Reconnect on Disconnection
- **Status:** Planned
- **Problem:** When the BLE connection drops unexpectedly, the user must manually click "Try Again" to reconnect.
- **Solution:** Implement automatic reconnection with configurable retry interval (e.g., attempt every 5 seconds for up to 60 seconds), while still showing a "Disconnected" state in the UI.
- **Implementation:** Add a background reconnect task that monitors `connected` state and triggers `_start_ble_thread()` after a delay when disconnection is detected.

---

## Phase 2: User Experience (Medium Priority)

### ✅ 2.1 Dark Mode
**Status:** ✅ Complete
**Files Modified:** `templates/base.html`

Three-state theme toggle (Light → Dark → System) with `localStorage` persistence and automatic OS preference following. Cycles: Light (sun) → Dark (moon) → System (display icon). Auto-follows OS theme changes in system mode.

---

### 2.2 Server-Sent Events for Real-Time Updates
- **Status:** Planned
- **Problem:** The client polls `/stats` every 1.5 seconds, which adds unnecessary HTTP overhead and introduces latency between stat updates on the server and display in the browser.
- **Solution:** Replace polling with Server-Sent Events (SSE) for pushing stat updates from server to client in real time.
- **Implementation:**
    - Add `/stats_stream` endpoint that returns `text/event-stream`
    - Use a thread-safe queue to push stat snapshots from the BLE thread to the SSE endpoint
    - Update frontend JavaScript to consume the SSE stream instead of `setInterval(fetch(...))`

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

### 2.5 Session History Log
- **Status:** Planned
- **Problem:** There is no record of past workouts. Users cannot track progress over time.
- **Solution:** Store completed sessions in a local JSON file (`session_history.json`) and display a summary table on the start screen.
- **Implementation:**
    - On session end (shutdown, or explicit "End Session" button), write session data to `session_history.json`
    - Display last N sessions (date, time, distance, steps, calories) as a table on the start screen
    - Add optional "Export as CSV" functionality

---

## Phase 3: Code Quality (Medium Priority)

### 3.1 External Configuration File
- **Status:** Planned
- **Problem:** All settings (`BLE_DEVICE_NAME`, speed constants, `KCAL_PER_MILE`) are hardcoded in `app.py`. Users must edit source code to customize behavior.
- **Solution:** Move all configurable settings to a `config.json` file (with `config.py` as a loader that provides defaults).
- **Implementation:**
    - Create `config.json.example` with all tunable parameters
    - On startup, load `config.json` if it exists, otherwise use defaults from `config.py`
    - Document all config options in README.md

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

## Notes

- Items within each phase can be implemented in any order unless dependencies exist.
- New features or bug fixes discovered during development may be added to this roadmap.
- For questions or feature requests, open an issue on the project repository.
