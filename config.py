import json
import os

_DEFAULTS = {
    "ble_device_name": "KS-BLC2",
    "max_speed_kmh": 6.0,
    "min_speed_kmh": 1.0,
    "speed_step": 0.6,
    "slow_walk_speed_kmh": 4.5,
    "kcal_per_mile": 95,
    "resume_grace_period_seconds": 7,
    "history_display_limit": 10,
    "host": "0.0.0.0",
    "port": 5001,
}

_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
_overrides = {}
if os.path.isfile(_path):
    with open(_path) as _f:
        _overrides = json.load(_f)


def _get(key):
    return _overrides.get(key, _DEFAULTS[key])


BLE_DEVICE_NAME: str              = _get("ble_device_name")
MAX_SPEED_KMH: float              = _get("max_speed_kmh")
MIN_SPEED_KMH: float              = _get("min_speed_kmh")
SPEED_STEP: float                 = _get("speed_step")
SLOW_WALK_SPEED_KMH: float        = _get("slow_walk_speed_kmh")
KCAL_PER_MILE: int                = _get("kcal_per_mile")
RESUME_GRACE_PERIOD_SECONDS: int  = _get("resume_grace_period_seconds")
HISTORY_DISPLAY_LIMIT: int        = _get("history_display_limit")
HOST: str                         = _get("host")
PORT: int                         = _get("port")
