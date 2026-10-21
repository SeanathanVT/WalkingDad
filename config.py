import json
import logging
import os

import storage

CONFIG_SCHEMA_VERSION = 1
SESSION_STATE_SCHEMA_VERSION = 1

_DEFAULTS = {
    "ble_device_name": "KS-BLC2",
    "max_speed_kmh": 6.0,
    "min_speed_kmh": 1.0,
    "speed_step": 0.6,
    "slow_walk_speed_kmh": 4.5,
    "kcal_per_mile": 95,
    "resume_grace_period_seconds": 7,
    "history_display_limit": 10,
    "stale_pause_timeout_minutes": 30,
    "host": "0.0.0.0",
    "port": 5001,
    "waitress_threads": 16,
    "keyboard_shortcuts_enabled": True,
    "apple_health_shortcut_name": "Log WalkingDad Workout",
    "apple_health_export_enabled": False,
    "database_path": "walkingdad.db",
}

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
SESSION_STATE_FILE = os.path.join(DATA_DIR, "session_state.json")
# Where versions before data/ kept it. Read until relocate_legacy_files() moves it at startup.
_LEGACY_CONFIG_FILE = os.path.join(APP_DIR, "config.json")


def _load_overrides(*paths):
    for path in paths:
        if os.path.isfile(path):
            with open(path) as f:
                return json.load(f)
    return {}


def migrate(data, migrations, current):
    """Apply migrations[v] for each version after data's (missing = 1) up to current, in memory."""
    found = data.get("schema_version", 1)
    if type(found) is int and found >= 1:  # Unusable versions are left for prepare_data to refuse.
        for version in range(found + 1, current + 1):
            data = migrations[version](data)
    return data


_CONFIG_MIGRATIONS = {}
_overrides = migrate(_load_overrides(CONFIG_FILE, _LEGACY_CONFIG_FILE), _CONFIG_MIGRATIONS, CONFIG_SCHEMA_VERSION)


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
STALE_PAUSE_TIMEOUT_MINUTES: int  = _get("stale_pause_timeout_minutes")
HOST: str                         = _get("host")
PORT: int                         = _get("port")
WAITRESS_THREADS: int             = _get("waitress_threads")
KEYBOARD_SHORTCUTS_ENABLED: bool  = _get("keyboard_shortcuts_enabled")
APPLE_HEALTH_SHORTCUT_NAME: str   = _get("apple_health_shortcut_name")
APPLE_HEALTH_EXPORT_ENABLED: bool = _get("apple_health_export_enabled")
DATABASE_PATH: str               = _get("database_path")


def relocate_legacy_files():
    """Move runtime files that versions before data/ kept next to the code into
    DATA_DIR (JSON-migration leftovers into BACKUP_DIR). Never overwrites."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    # Each group moves all-or-nothing: a database separated from its -wal loses uncommitted sessions.
    groups = [[(name, DATA_DIR)] for name in ("config.json", "session_state.json", "session_history.json")]
    if not os.path.isabs(DATABASE_PATH):
        groups.append([(DATABASE_PATH + suffix, DATA_DIR) for suffix in ("", "-wal", "-shm")])
    groups += [[(name, BACKUP_DIR)] for name in os.listdir(APP_DIR) if name.startswith("session_history.json.")]
    for group in groups:
        pairs = [(os.path.join(APP_DIR, name), os.path.join(dest_dir, name)) for name, dest_dir in group]
        blocked = [dest for _, dest in pairs if os.path.exists(dest)]
        pairs = [(src, dest) for src, dest in pairs if os.path.isfile(src)]
        if not pairs:
            continue
        if blocked:
            logging.warning(f"Not moving {', '.join(src for src, _ in pairs)}: {', '.join(blocked)} already exists")
            continue
        for src, dest in pairs:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.replace(src, dest)
            logging.info(f"Moved {src} to {dest}")


def _check_version(path, data, current, too_new):
    found = data.get("schema_version", 1)
    if type(found) is not int or found < 1:
        raise storage.DataVersionError(f"Can't start: {path} has an unreadable schema_version {found!r}.")
    if found > current:
        raise storage.DataVersionError(too_new(found))


def prepare_data():
    """Startup data step, run before anything else touches data/: relocate legacy files, refuse
    files newer than this code, then create or upgrade the database. Raises storage.DataVersionError."""
    relocate_legacy_files()
    config_path = CONFIG_FILE
    _check_version(config_path, _overrides, CONFIG_SCHEMA_VERSION, lambda found: (
        f"Can't start: {config_path} was written by settings format v{found}; this version understands "
        f"up to v{CONFIG_SCHEMA_VERSION}. Run a newer WalkingDad, or delete {config_path} to start over "
        "with default settings (your settings will be lost)."))

    state_path = SESSION_STATE_FILE
    try:
        with open(state_path) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = None  # Missing or corrupt: app's restore already ignores it.
    if isinstance(state, dict):
        _check_version(state_path, state, SESSION_STATE_SCHEMA_VERSION, lambda found: (
            f"Can't start: {state_path} (an interrupted walk) was written by format v{found}; this version "
            f"understands up to v{SESSION_STATE_SCHEMA_VERSION}. Run a newer WalkingDad to recover that walk. "
            "Deleting the file discards it; the walk's history row will be closed from its recorded samples "
            "(or removed if none were recorded)."))

    unknown = sorted(set(_overrides) - set(_DEFAULTS) - {"schema_version"})
    if unknown:
        logging.warning(f"Ignoring unknown settings in {config_path}: {', '.join(unknown)}")

    storage.init_db(os.path.join(DATA_DIR, DATABASE_PATH), BACKUP_DIR)
