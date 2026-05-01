# WalkingDad

> Your WalkingPad's original app is trash. This isn't.

A desktop web controller for KingSmith WalkingPad treadmills. Runs locally, connects over Bluetooth, and doesn't ask you to create an account before it lets you walk. Because you shouldn't need a damn account to walk.

## Why

The official WalkingPad experience is a bloated mobile app that wants your email, your personal data, and probably a blood sample. WalkingDad runs on whatever machine has Python and Bluetooth, serves a clean web UI to any browser on your network, and ships with zero accounts, zero telemetry, and zero opinions about how far you should walk.

## Features

- **Web UI** — Control your treadmill from any browser on your local network
- **Real-time stats** — Speed, distance, steps, calories, and active time, updated live
- **Smart pause & resume** — Auto-detects when you step off; remembers your speed; 7-second grace period prevents re-triggering on restart
- **Speed presets** — Max, slow walk, and incremental increase/decrease buttons
- **Dark mode** — Three-state toggle (Light → Dark → System) with localStorage persistence
- **Cross-platform BLE** — Tested on Windows, macOS, and Linux with retry logic and event loop cleanup
- **No account. No cloud. No phone required.**

## Screenshots

**Start**
![Start Session](screenshots/start.png)

**Active**
![Active Session](screenshots/active.png)

**Paused**
![Paused Session](screenshots/paused.png)

## Quick Start

**Requirements:** Python 3.8+, Bluetooth adapter, compatible WalkingPad (confirmed: C2 / `KS-BLC2`).

```bash
# Clone and enter the project
git clone git@github.com:SeanathanVT/WalkingDad.git
cd WalkingDad

# Set up a virtual environment
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Power on your WalkingPad, then run
python run.py
```

The app opens your browser automatically at `http://127.0.0.1:5001`. A console window will stream timestamped logs — keep it open while the app runs.

**Windows shortcut:** Double-click `start_app.bat` instead of running the commands manually.

## Usage

1. Power on your WalkingPad. The app connects automatically on startup (up to 3 retries with exponential backoff).
2. Click **Start** to begin a session. Stats update in real time.
3. Adjust speed with the control buttons, or click **Pause** to stop the belt.
4. If you step off the pad, the app auto-pauses. Click **Resume** to pick back up.
5. Toggle themes with the icon in the header. Close the app with **Close**.

## Configuration

All settings live at the top of `app.py`:

| Setting | Default | Description |
|---|---|---|
| `MAX_SPEED_KMH` | `6.0` | Max speed button (~3.7 mph) |
| `MIN_SPEED_KMH` | `1.0` | Speed floor |
| `SPEED_STEP` | `0.6` | Increment per button press |
| `SLOW_WALK_SPEED_KMH` | `4.5` | Slow Walk preset (~2.8 mph) |
| `BLE_DEVICE_NAME` | `"KS-BLC2"` | Your treadmill's Bluetooth name |
| `KCAL_PER_MILE` | `95` | Calorie estimate constant |

Change the server port by editing `PORT` in `run.py` (default: `5001`).

## Troubleshooting

- **Won't connect:** Make sure your WalkingPad is powered on and not paired to another device (like your phone). Check the console for log details.
- **Icons missing:** Bootstrap Icons load from a CDN — make sure your browser has internet access.
- **Stats stuck after resume:** Rare, but can happen. Restart the app and check the console for `ask_stats error` messages.
- **macOS BLE quirks:** See [ROADMAP.md](ROADMAP.md) Phase 1.1 for the full list of cross-platform reliability fixes.

## Credits

Forked and expanded from the original [walkingpad](https://github.com/CodeJawn/walkingpad) app by **[CodeJawn](https://github.com/CodeJawn)** — solid foundation, good on you, dude. Built on the excellent [`ph4-walkingpad`](https://pypi.org/project/ph4-walkingpad/) library by ph4x, which handles all Bluetooth protocol communication with the treadmill. None of this would work without that reverse engineering effort.

## Roadmap

See [ROADMAP.md](ROADMAP.md) for completed features and planned improvements across four phases.

## License

See [LICENSE](LICENSE).