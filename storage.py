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

CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start_time);
CREATE INDEX IF NOT EXISTS idx_sessions_profile_start ON sessions(profile, start_time);
"""


def _connect(path):
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path):
    global _db_path
    _db_path = path
    with _write_lock:
        conn = _connect(path)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA_SQL)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '2')"
            )
            conn.commit()
        finally:
            conn.close()


def create_session(start_time, device_model, app_version, profile="default"):
    session_id = str(uuid.uuid4())
    with _write_lock:
        conn = _connect(_db_path)
        try:
            with conn:
                conn.execute(
                    """INSERT INTO sessions
                       (id, status, profile, start_time, device_model, app_version)
                       VALUES (?, 'active', ?, ?, ?, ?)""",
                    (session_id, profile, start_time, device_model, app_version),
                )
        finally:
            conn.close()
    return session_id


def add_pause(session_id, start_time, reason):
    with _write_lock:
        conn = _connect(_db_path)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO pauses(session_id, start_time, reason) VALUES (?, ?, ?)",
                    (session_id, start_time, reason),
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
    fields = (
        "end_time", "elapsed_s", "moving_s", "distance_m", "steps",
        "calories_kcal", "calories_estimated", "avg_speed_mps", "max_speed_mps",
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
                        {", ".join(f"{f} = ?" for f in fields)}
                        WHERE id = ?""",
                    (*values, session_id),
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


def mark_health_logged(session_id):
    with _write_lock:
        conn = _connect(_db_path)
        try:
            with conn:
                conn.execute("UPDATE sessions SET health_logged = 1 WHERE id = ?", (session_id,))
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


def migrate_json(json_path):
    """One-time import of legacy session_history.json. No-op once meta.migrated_from_json is set."""
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

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(json_path, f"{json_path}.bak-{stamp}")

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
                    """INSERT INTO sessions
                       (id, start_time, end_time, elapsed_s, moving_s, distance_m, steps,
                        calories_kcal, avg_speed_mps, health_logged,
                        status, source, tz_assumed)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', 'migrated_json', 1)""",
                    rows,
                )
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('migrated_from_json', ?)",
                    (datetime.now().astimezone().isoformat(timespec="seconds"),),
                )
        finally:
            conn.close()

    try:
        os.replace(json_path, f"{json_path}.migrated")
    except OSError as exc:
        logging.warning(f"Migrated {json_path} but could not rename it: {exc}")
    logging.info(f"Migrated {len(rows)} sessions from {json_path}, skipped {len(records) - len(rows)}")
