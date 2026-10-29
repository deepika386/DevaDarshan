"""SQLite storage (the synopsis names MySQL; the schema is plain SQL and ports directly)."""
import sqlite3
import threading
import config

LOCK = threading.RLock()
_conn = None
_path = None
_demo = False      # True while the separate demo/simulation database is open

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, phone TEXT NOT NULL, email TEXT,
  visit_date TEXT NOT NULL, slot_start TEXT NOT NULL,
  party_size INTEGER NOT NULL, assist INTEGER DEFAULT 0,
  queue_no INTEGER NOT NULL, token TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'booked',        -- booked | inside | exited | no_show | cancelled
  amount INTEGER DEFAULT 0, payment_ref TEXT,
  source TEXT DEFAULT 'app',                    -- app | sim
  reminded INTEGER DEFAULT 0,
  arrived_at TEXT,                              -- first time the QR was shown at the gate
  created_at TEXT, entry_at TEXT, exit_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_b_date ON bookings(visit_date, slot_start, queue_no);
CREATE INDEX IF NOT EXISTS ix_b_token ON bookings(token);
CREATE TABLE IF NOT EXISTS crowd_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, visit_date TEXT,
  inside INTEGER, capacity INTEGER, pct REAL,
  c_current REAL, c_booked REAL, c_predicted REAL,
  score REAL, level TEXT, action TEXT, extra_min INTEGER
);
CREATE TABLE IF NOT EXISTS control_state(
  visit_date TEXT PRIMARY KEY, extra_min INTEGER DEFAULT 0, paused INTEGER DEFAULT 0,
  level TEXT DEFAULT 'Low', score REAL DEFAULT 0, action TEXT DEFAULT 'Open normally',
  updated_at TEXT, tokens REAL, tokens_ts TEXT, notified_level TEXT DEFAULT 'Low'
);
CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY AUTOINCREMENT, booking_id INTEGER, ts TEXT, channel TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS gate_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, booking_id INTEGER, kind TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS footfall_history(
  visit_date TEXT, hour INTEGER, arrivals INTEGER, source TEXT,
  PRIMARY KEY(visit_date, hour, source)
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def use(path=None, demo=False):
    """(Re)open the database at `path` (default: config.DB_PATH). demo=True marks the simulation database."""
    global _conn, _path, _demo
    with LOCK:
        if _conn is not None:
            _conn.close()
        _path = path or config.DB_PATH
        _demo = demo
        if _path != ":memory:":
            import os
            os.makedirs(os.path.dirname(_path), exist_ok=True)
        _conn = sqlite3.connect(_path, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=OFF")
        _conn.executescript(SCHEMA)
    return _conn


def is_demo():
    return _demo


def current_path():
    return _path


def conn():
    if _conn is None:
        use()
    return _conn


# Flask serves requests on several threads but they all share ONE sqlite connection. Every statement
# therefore runs under the same re-entrant lock the engine uses for multi-step operations; without it a
# simulation step and a page refresh could interleave and corrupt each other's cursor.
def q(sql, args=()):
    with LOCK:
        return conn().execute(sql, args).fetchall()


def q1(sql, args=()):
    with LOCK:
        return conn().execute(sql, args).fetchone()


def x(sql, args=()):
    with LOCK:
        return conn().execute(sql, args)


def meta_get(key, default=None):
    r = q1("SELECT value FROM meta WHERE key=?", (key,))
    return r["value"] if r else default


def meta_set(key, value):
    if value is None:
        x("DELETE FROM meta WHERE key=?", (key,))
    else:
        x("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
          (key, str(value)))


def wipe_operational():
    """Clear live data (bookings, logs). SAFETY: only allowed on the demo database (or a throw-away
    in-memory one), so real bookings can never be wiped by a simulation action."""
    if not (_demo or _path == ":memory:"):
        raise PermissionError("Refusing to wipe the real database. Enter demo mode first.")
    with LOCK:
        for t in ("bookings", "crowd_log", "control_state", "notifications", "gate_events"):
            x(f"DELETE FROM {t}")
        x("DELETE FROM sqlite_sequence WHERE name IN ('bookings','crowd_log','notifications','gate_events')")
        x("DELETE FROM footfall_history WHERE source='actual'")
