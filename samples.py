import threading

WALKING_INTERVAL_MS = 1000
PAUSED_INTERVAL_MS = 5000


class SampleBuffer:
    """Throttled in-memory buffer of session samples, filled from the BLE thread and drained on flush.
    ponytail: keeps the first reading per interval, not the latest; same result at ~1 packet/s."""

    def __init__(self):
        self._lock = threading.Lock()
        self._rows = []
        self._last_t_ms = None

    def reset(self):
        with self._lock:
            self._rows = []
            self._last_t_ms = None

    def add(self, t_ms, speed_mps, distance_m, steps, belt_running, force=False):
        interval = WALKING_INTERVAL_MS if belt_running else PAUSED_INTERVAL_MS
        with self._lock:
            last = self._last_t_ms
            if last is not None and (t_ms <= last or (not force and t_ms - last < interval)):
                return
            self._rows.append((t_ms, speed_mps, distance_m, steps, int(belt_running), None))
            self._last_t_ms = t_ms

    def drain(self):
        with self._lock:
            rows, self._rows = self._rows, []
        return rows
