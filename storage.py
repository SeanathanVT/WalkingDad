"""SQLite session storage. All SQL lives in this module; app.py must not issue SQL directly."""
import json
import logging
import os
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta

_write_lock = threading.Lock()
_db_path = None

SCHEMA_VERSION = 3
UNASSIGNED = "default"

_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id                 TEXT PRIMARY KEY,
  schema_version     INTEGER NOT NULL DEFAULT 2,
  status             TEXT NOT NULL,
  profile            TEXT NOT NULL DEFAULT 'default',
  sport              TEXT NOT NULL DEFAULT 'walking',
  sub_sport          TEXT NOT NULL DEFAULT 'treadmill',
  start_time         TEXT NOT NULL,
  end_time           TEXT,
  tz_assumed         INTEGER NOT NULL DEFAULT 0,
  elapsed_s          REAL,
  moving_s           REAL,
  distance_m         REAL,
  steps              INTEGER,
  calories_kcal      REAL,
  calories_estimated INTEGER NOT NULL DEFAULT 1,
  avg_speed_mps      REAL,
  max_speed_mps      REAL,
  device_model       TEXT,
  app_version        TEXT,
  source             TEXT NOT NULL DEFAULT 'walkingdad',
  has_samples        INTEGER NOT NULL DEFAULT 0,
  health_logged      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pauses (
  session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  start_time  TEXT NOT NULL,
  end_time    TEXT,
  reason      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS samples (
  session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  t_ms        INTEGER NOT NULL,
  speed_mps   REAL,
  distance_m  REAL,
  steps       INTEGER,
  belt_running INTEGER NOT NULL,
  hr_bpm      INTEGER,
  PRIMARY KEY (session_id, t_ms)
);

CREATE TABLE IF NOT EXISTS profiles (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL COLLATE NOCASE UNIQUE,
  height_m    REAL,
  weight_kg   REAL,
  created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start_time);
CREATE INDEX IF NOT EXISTS idx_sessions_profile_start ON sessions(profile, start_time);
"""


def _connect(path):
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


_MIGRATIONS = {
    3: """
CREATE TABLE IF NOT EXISTS profiles (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL COLLATE NOCASE UNIQUE,
  height_m    REAL,
  weight_kg   REAL,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_profile_start ON sessions(profile, start_time);
""",
}


class DataVersionError(Exception):
    """A stored file is newer than this code understands, or upgrading it failed."""


def _stored_version(conn, path):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone():
        return None
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        return None
    try:
        found = int(row[0])
    except ValueError:
        found = None
    # Below the oldest version any release wrote, so no migration path exists from it.
    if found is None or found < min(_MIGRATIONS) - 1:
        raise DataVersionError(f"Can't start: {path} has an unreadable schema version {row[0]!r}.")
    return found


def init_db(path, backup_dir=None):
    """Create the DB, or upgrade an older one in place (after a backup into backup_dir,
    default: alongside). Raises DataVersionError if it is newer than SCHEMA_VERSION."""
    global _db_path
    _db_path = path
    with _write_lock:
        conn = _connect(path)
        try:
            found = _stored_version(conn, path)
            if found is not None and found > SCHEMA_VERSION:
                raise DataVersionError(
                    f"Can't start: {path} was written by schema v{found}; this version of WalkingDad understands "
                    f"up to v{SCHEMA_VERSION}. Run a newer WalkingDad, or restore the pre-upgrade copy from data/backups/."
                )
            conn.execute("PRAGMA journal_mode = WAL")
            if found is None:
                conn.executescript(_SCHEMA_SQL)
                conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
                conn.commit()
            elif found < SCHEMA_VERSION:
                _upgrade(conn, path, found, backup_dir or os.path.dirname(path))
        finally:
            conn.close()


def _upgrade(conn, path, found, backup_dir):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(backup_dir, f"{os.path.basename(path)}.v{found}-{stamp}")
    try:
        os.makedirs(backup_dir, exist_ok=True)
        dest = sqlite3.connect(backup)
        try:
            conn.backup(dest)
            dest.execute("PRAGMA journal_mode = DELETE")  # So opening the copy doesn't create -wal/-shm beside it.
        finally:
            dest.close()
    except (OSError, sqlite3.Error) as exc:
        if os.path.isfile(backup):
            os.remove(backup)
        raise DataVersionError(f"Can't start: backing up {path} failed ({exc}), so it was not upgraded.") from exc
    for version in range(found + 1, SCHEMA_VERSION + 1):
        try:
            # executescript commits first, so the step carries its own transaction.
            conn.executescript(
                f"BEGIN; {_MIGRATIONS[version]}; "
                f"UPDATE meta SET value = '{version}' WHERE key = 'schema_version'; COMMIT;"
            )
        except sqlite3.Error as exc:
            conn.rollback()
            raise DataVersionError(
                f"Can't start: upgrading {path} to schema v{version} failed ({exc}). "
                f"It is unchanged at v{version - 1}; a copy from before the upgrade is at {backup}."
            ) from exc
        logging.info(f"Upgraded {path} to schema v{version} (backup: {backup})")


def create_session(start_time, device_model, app_version, profile=UNASSIGNED):
    session_id = str(uuid.uuid4())
    with _write_lock:
        conn = _connect(_db_path)
        try:
            with conn:
                conn.execute(
                    """INSERT INTO sessions
                       (id, schema_version, status, profile, start_time, device_model, app_version)
                       VALUES (?, ?, 'active', ?, ?, ?, ?)""",
                    (session_id, SCHEMA_VERSION, profile, start_time, device_model, app_version),
                )
        finally:
            conn.close()
    return session_id


def add_pause(session_id, start_time, reason):
    """Open a pause; no-op if one is already open, so every pause path can call it blindly."""
    with _write_lock:
        conn = _connect(_db_path)
        try:
            with conn:
                conn.execute(
                    """INSERT INTO pauses(session_id, start_time, reason)
                       SELECT ?, ?, ?
                       WHERE NOT EXISTS (
                         SELECT 1 FROM pauses WHERE session_id = ? AND end_time IS NULL
                       )""",
                    (session_id, start_time, reason, session_id),
                )
        finally:
            conn.close()


def end_pause(session_id, end_time):
    with _write_lock:
        conn = _connect(_db_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE pauses SET end_time = ? WHERE session_id = ? AND end_time IS NULL",
                    (end_time, session_id),
                )
        finally:
            conn.close()


def append_samples(session_id, rows):
    if not rows:
        return
    with _write_lock:
        conn = _connect(_db_path)
        try:
            with conn:
                conn.executemany(
                    """INSERT INTO samples
                       (session_id, t_ms, speed_mps, distance_m, steps, belt_running, hr_bpm)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    [(session_id, *row) for row in rows],
                )
        finally:
            conn.close()


def complete_session(session_id, summary):
    """Mark completed with the given totals. has_samples, and max_speed_mps unless given,
    come from the samples table so they stay right across a crash-restore. A pause still
    open (ended while paused) is closed at the session's end_time."""
    fields = (
        "end_time", "elapsed_s", "moving_s", "distance_m", "steps",
        "calories_kcal", "calories_estimated", "avg_speed_mps",
    )
    values = [summary.get(f) for f in fields]
    values[fields.index("calories_estimated")] = summary.get("calories_estimated", 1)
    with _write_lock:
        conn = _connect(_db_path)
        try:
            with conn:
                conn.execute(
                    f"""UPDATE sessions SET
                        status = 'completed',
                        {", ".join(f"{f} = ?" for f in fields)},
                        max_speed_mps = COALESCE(?, (SELECT MAX(speed_mps) FROM samples WHERE session_id = ?)),
                        has_samples = EXISTS (SELECT 1 FROM samples WHERE session_id = ?)
                        WHERE id = ?""",
                    (*values, summary.get("max_speed_mps"), session_id, session_id, session_id),
                )
                conn.execute(
                    "UPDATE pauses SET end_time = ? WHERE session_id = ? AND end_time IS NULL",
                    (summary.get("end_time"), session_id),
                )
        finally:
            conn.close()


def delete_session(session_id):
    with _write_lock:
        conn = _connect(_db_path)
        try:
            with conn:
                conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        finally:
            conn.close()


def list_sessions(limit=None, profile=None):
    query = "SELECT * FROM sessions WHERE status = 'completed'"
    params = []
    if profile is not None:
        query += " AND profile = ?"
        params.append(profile)
    query += " ORDER BY start_time DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    conn = _connect(_db_path)
    try:
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def list_active_sessions():
    conn = _connect(_db_path)
    try:
        rows = conn.execute("SELECT * FROM sessions WHERE status = 'active'").fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def get_session(session_id):
    conn = _connect(_db_path)
    try:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def get_samples(session_id):
    conn = _connect(_db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM samples WHERE session_id = ? ORDER BY t_ms", (session_id,)
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def list_profiles():
    conn = _connect(_db_path)
    try:
        rows = conn.execute("SELECT * FROM profiles ORDER BY name").fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def get_profile(profile_id):
    conn = _connect(_db_path)
    try:
        row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def create_profile(name, height_m=None, weight_kg=None):
    """Raises sqlite3.IntegrityError if the name is taken (case-insensitive)."""
    profile_id = str(uuid.uuid4())
    with _write_lock:
        conn = _connect(_db_path)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO profiles(id, name, height_m, weight_kg, created_at) VALUES (?, ?, ?, ?, ?)",
                    (profile_id, name, height_m, weight_kg, datetime.now().astimezone().isoformat(timespec="seconds")),
                )
        finally:
            conn.close()
    return profile_id


def update_profile(profile_id, name, height_m, weight_kg):
    """False if there's no such profile. Raises sqlite3.IntegrityError if the name is taken."""
    with _write_lock:
        conn = _connect(_db_path)
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE profiles SET name = ?, height_m = ?, weight_kg = ? WHERE id = ?",
                    (name, height_m, weight_kg, profile_id),
                )
        finally:
            conn.close()
    return cur.rowcount == 1


def delete_profile(profile_id):
    """Its sessions become Unassigned rather than being deleted."""
    with _write_lock:
        conn = _connect(_db_path)
        try:
            with conn:
                conn.execute("UPDATE sessions SET profile = ? WHERE profile = ?", (UNASSIGNED, profile_id))
                conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        finally:
            conn.close()


def count_unassigned():
    conn = _connect(_db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM sessions WHERE profile = ? AND status = 'completed'", (UNASSIGNED,)).fetchone()[0]
    finally:
        conn.close()


def assign_unassigned(profile_id):
    with _write_lock:
        conn = _connect(_db_path)
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE sessions SET profile = ? WHERE profile = ? AND status = 'completed'",
                    (profile_id, UNASSIGNED),
                )
        finally:
            conn.close()
    return cur.rowcount


def set_session_profile(session_id, profile):
    """False unless session_id is a completed session."""
    with _write_lock:
        conn = _connect(_db_path)
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE sessions SET profile = ? WHERE id = ? AND status = 'completed'", (profile, session_id)
                )
        finally:
            conn.close()
    return cur.rowcount == 1


def clear_history(profile=None):
    query = "DELETE FROM sessions WHERE status = 'completed'"
    params = []
    if profile is not None:
        query += " AND profile = ?"
        params.append(profile)
    with _write_lock:
        conn = _connect(_db_path)
        try:
            with conn:
                conn.execute(query, params)
        finally:
            conn.close()


# sessions.health_logged values. Rows set to 1 before "dismissed" existed may have been dismissed.
HEALTH_PENDING, HEALTH_LOGGED, HEALTH_DISMISSED = 0, 1, 2


def set_health_status(session_id, status):
    with _write_lock:
        conn = _connect(_db_path)
        try:
            with conn:
                conn.execute("UPDATE sessions SET health_logged = ? WHERE id = ?", (status, session_id))
        finally:
            conn.close()


def _legacy_json_row(record, tz):
    def parse(time_str):
        return datetime.strptime(f"{record['date']} {time_str}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)

    start = parse(record["start_time"])
    moving_s = float(record["duration_seconds"])
    try:
        end = parse(record["end_time"])
        if end < start:
            end += timedelta(days=1)
        elapsed_s = (end - start).total_seconds()
        end_iso = end.isoformat(timespec="seconds")
    except (KeyError, TypeError, ValueError):
        elapsed_s, end_iso = moving_s, None
    return (
        str(uuid.uuid4()),
        start.isoformat(timespec="seconds"),
        end_iso,
        elapsed_s,
        moving_s,
        float(record["distance_km"]) * 1000,
        int(record["steps"]),
        float(record["calories"]),
        float(record["avg_speed_kmh"]) / 3.6,
        int(bool(record.get("health_logged", False))),
    )


def migrate_json(json_path, backup_dir=None):
    """One-time import of legacy session_history.json. No-op once meta.migrated_from_json is set.
    The .bak-<stamp> copy and the renamed .migrated original go in backup_dir (default: alongside)."""
    if not os.path.exists(json_path):
        return
    conn = _connect(_db_path)
    try:
        done = conn.execute("SELECT 1 FROM meta WHERE key = 'migrated_from_json'").fetchone()
    finally:
        conn.close()
    if done:
        return

    try:
        with open(json_path) as f:
            text = f.read()
        records = json.loads(text) if text.strip() else []
        if not isinstance(records, list):
            raise ValueError("top level is not a list")
    except (OSError, ValueError) as exc:
        logging.error(f"Cannot migrate {json_path}, leaving it untouched: {exc}")
        return

    backup_base = os.path.join(backup_dir or os.path.dirname(json_path), os.path.basename(json_path))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(json_path, f"{backup_base}.bak-{stamp}")

    # Legacy records have no offset; assume the machine's current one (tz_assumed=1).
    tz = datetime.now().astimezone().tzinfo
    rows = []
    for record in records:
        try:
            rows.append(_legacy_json_row(record, tz))
        except (KeyError, TypeError, ValueError) as exc:
            logging.warning(f"Skipping unparseable history record {record!r}: {exc}")

    with _write_lock:
        conn = _connect(_db_path)
        try:
            with conn:
                conn.executemany(
                    f"""INSERT INTO sessions
                       (id, start_time, end_time, elapsed_s, moving_s, distance_m, steps,
                        calories_kcal, avg_speed_mps, health_logged,
                        status, source, tz_assumed, schema_version)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', 'migrated_json', 1, {SCHEMA_VERSION})""",
                    rows,
                )
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('migrated_from_json', ?)",
                    (datetime.now().astimezone().isoformat(timespec="seconds"),),
                )
        finally:
            conn.close()

    try:
        os.replace(json_path, f"{backup_base}.migrated")
    except OSError as exc:
        logging.warning(f"Migrated {json_path} but could not rename it: {exc}")
    logging.info(f"Migrated {len(rows)} sessions from {json_path}, skipped {len(records) - len(rows)}")
