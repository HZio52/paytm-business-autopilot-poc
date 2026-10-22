"""SQLite storage for the Paytm Business Autopilot demo backend.

Uses only the Python standard library. Every request opens its own short-lived
connection; writes are serialised with a lock so SQLite never reports
"database is locked" during the demo.
"""

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("AUTOPILOT_DB_PATH", BACKEND_DIR / "data" / "autopilot.db"))

_write_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile (
    id   INTEGER PRIMARY KEY CHECK (id = 1),
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    phone             TEXT NOT NULL UNIQUE,
    consent           TEXT NOT NULL CHECK (consent IN ('opted_in', 'pending', 'none')),
    source            TEXT NOT NULL CHECK (source IN ('paytm', 'manual', 'seed')),
    created_at        TEXT NOT NULL,
    last_contacted_at TEXT,
    -- Simulation-only fields. They drive synthetic future payments during
    -- fast-forward and are never sent to the frontend. Segments are always
    -- computed from the payments table, not from these.
    sim_gap_days      INTEGER NOT NULL DEFAULT 7,
    sim_basket        INTEGER NOT NULL DEFAULT 500,
    sim_active        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS payments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    amount      INTEGER NOT NULL CHECK (amount > 0),
    paid_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_customer ON payments (customer_id, paid_at);
CREATE INDEX IF NOT EXISTS idx_payments_time ON payments (paid_at);

CREATE TABLE IF NOT EXISTS experiments (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at         TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('running', 'completed', 'cancelled')),
    offer_amount       INTEGER NOT NULL,
    basket             INTEGER NOT NULL,
    expected_rate      REAL NOT NULL,
    natural_rate       REAL NOT NULL,
    predicted_net      INTEGER NOT NULL,
    measure_after_days INTEGER NOT NULL DEFAULT 7,
    approved_via       TEXT NOT NULL DEFAULT 'tap',
    completed_at       TEXT,
    result             TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER REFERENCES experiments(id) ON DELETE CASCADE,
    customer_id   INTEGER REFERENCES customers(id) ON DELETE SET NULL,
    customer_name TEXT NOT NULL,
    phone_masked  TEXT NOT NULL,
    grp           TEXT NOT NULL CHECK (grp IN ('offer', 'control', 'direct')),
    language      TEXT NOT NULL DEFAULT 'en',
    text          TEXT,
    status        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    returned_at   TEXT,
    return_amount INTEGER
);
CREATE INDEX IF NOT EXISTS idx_messages_experiment ON messages (experiment_id);

CREATE TABLE IF NOT EXISTS tickets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('open', 'drafted', 'resolved')),
    customer_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    who      TEXT NOT NULL CHECK (who IN ('Agent', 'Merchant', 'System')),
    event    TEXT NOT NULL,
    decision TEXT NOT NULL DEFAULT '',
    outcome  TEXT NOT NULL DEFAULT ''
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def reading():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def writing():
    with _write_lock:
        conn = connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def get_profile(conn) -> dict:
    row = conn.execute("SELECT data FROM profile WHERE id = 1").fetchone()
    return json.loads(row["data"]) if row else {}


def save_profile(conn, data: dict) -> None:
    conn.execute(
        "INSERT INTO profile (id, data) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
        (json.dumps(data, ensure_ascii=False),),
    )


def add_log(conn, at: str, who: str, event: str, decision: str = "", outcome: str = "") -> None:
    conn.execute(
        "INSERT INTO activity_log (at, who, event, decision, outcome) VALUES (?, ?, ?, ?, ?)",
        (at, who, event, decision, outcome),
    )


def clear_all(conn) -> None:
    for table in ("messages", "experiments", "payments", "tickets", "activity_log", "customers", "profile", "settings"):
        conn.execute(f"DELETE FROM {table}")
    try:
        conn.execute("DELETE FROM sqlite_sequence")
    except sqlite3.OperationalError:
        pass
