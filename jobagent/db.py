"""SQLite storage. One small file, no server."""
from __future__ import annotations

import contextlib
import datetime as dt
import sqlite3

from .config import DATA_DIR, ensure_data_dir

DB_PATH = DATA_DIR / "jobagent.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    location TEXT DEFAULT '',
    remote INTEGER DEFAULT 0,
    url TEXT DEFAULT '',
    apply_url TEXT DEFAULT '',
    apply_email TEXT DEFAULT '',
    description TEXT DEFAULT '',
    posted_at TEXT DEFAULT '',
    found_at TEXT NOT NULL,
    score INTEGER DEFAULT 0,
    reasons TEXT DEFAULT '',
    dedupe_key TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe ON jobs (dedupe_key);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL UNIQUE REFERENCES jobs (id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    method TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    applied_at TEXT,
    last_event_at TEXT,
    first_response_at TEXT,
    cover_letter TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    error TEXT DEFAULT '',
    screenshot TEXT DEFAULT '',
    thread_id TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_apps_status ON applications (status);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications (id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    at TEXT NOT NULL,
    summary TEXT DEFAULT '',
    detail TEXT DEFAULT '',
    gmail_id TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_events_app ON events (application_id, at);

CREATE TABLE IF NOT EXISTS interviews (
    id INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications (id) ON DELETE CASCADE,
    scheduled_at TEXT,
    kind TEXT DEFAULT 'Interview',
    link TEXT DEFAULT '',
    interviewer TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    outcome TEXT DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS seen_messages (id TEXT PRIMARY KEY, checked_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
"""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextlib.contextmanager
def conn():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init() -> None:
    ensure_data_dir()
    with conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
    with conn() as c:
        c.executescript(SCHEMA)


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute(sql, params).fetchall()


def one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    with conn() as c:
        return c.execute(sql, params).fetchone()


def execute(sql: str, params: tuple = ()) -> int:
    with conn() as c:
        return c.execute(sql, params).lastrowid


def kv_get(key: str, default: str | None = None) -> str | None:
    row = one("SELECT value FROM kv WHERE key=?", (key,))
    return row["value"] if row else default


def kv_set(key: str, value: str | None) -> None:
    if value is None:
        execute("DELETE FROM kv WHERE key=?", (key,))
    else:
        execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def log(level: str, message: str) -> None:
    execute("INSERT INTO logs (at, level, message) VALUES (?, ?, ?)", (now(), level, message[:500]))
    execute("DELETE FROM logs WHERE id <= (SELECT MAX(id) FROM logs) - 500")


def add_event(app_id: int, kind: str, summary: str = "", detail: str = "",
              gmail_id: str | None = None, at: str | None = None) -> int | None:
    try:
        event_id = execute(
            "INSERT INTO events (application_id, kind, at, summary, detail, gmail_id) VALUES (?,?,?,?,?,?)",
            (app_id, kind, at or now(), summary, detail, gmail_id),
        )
    except sqlite3.IntegrityError:
        return None
    execute("UPDATE applications SET last_event_at=? WHERE id=?", (at or now(), app_id))
    return event_id


def set_status(app_id: int, status: str, **fields) -> None:
    cols = {"status": status, "last_event_at": now(), **fields}
    sets = ", ".join(f"{key}=?" for key in cols)
    execute(f"UPDATE applications SET {sets} WHERE id=?", (*cols.values(), app_id))
