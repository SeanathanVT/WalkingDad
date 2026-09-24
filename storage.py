"""SQLite session storage. All SQL lives in this module; app.py must not issue SQL directly."""
import sqlite3
import threading
import uuid

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
  has_samples        INTEGER NOT NULL DEFAULT 0
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
