import threading
from datetime import datetime, timedelta

from units import KM_TO_MI

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


def summary_from_samples(start_time: str, rows: list[dict], kcal_per_mile: float) -> dict:
    """complete_session() summary for a session that never ended cleanly, rebuilt from its samples.
    ponytail: moving time = count of walking samples (they're ~1 s apart); exact would need
    the live counter, which died with the process."""
    last = rows[-1]
    end = datetime.fromisoformat(start_time) + timedelta(milliseconds=last["t_ms"])
    moving_s = sum(r["belt_running"] for r in rows)
    distance_m = last["distance_m"] or 0.0
    return {
        "end_time": end.isoformat(timespec="seconds"),
        "elapsed_s": last["t_ms"] / 1000,
        "moving_s": moving_s,
        "distance_m": distance_m,
        "steps": last["steps"],
        "calories_kcal": kcal_per_mile * distance_m / 1000 * KM_TO_MI,
        "avg_speed_mps": distance_m / max(moving_s, 1),
    }
