"""
router_proxy/state.py — Persistent state in SQLite.

- go_healthy: boolean, survives restarts
- Subtask failure counters: keyed by subtask_id
- routing_log: per-request routing records
"""

import sqlite3
import os
import threading
import time
from pathlib import Path

DB_PATH = os.environ.get("PROXY_STATE_DB", str(Path(__file__).parent.parent / "proxy_state.db"))

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    """Create tables if they don't exist."""
    with _lock:
        conn = _connect()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS counters (
                subtask_id TEXT PRIMARY KEY,
                failures INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS routing_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                subtask_id TEXT,
                role TEXT NOT NULL,
                model TEXT NOT NULL,
                backend TEXT NOT NULL,
                tier TEXT NOT NULL,
                status INTEGER NOT NULL,
                complex INTEGER DEFAULT 0,
                failures INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        conn.close()


def _get_state(key: str) -> str | None:
    conn = _connect()
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row[0] if row else None


def _set_state(key: str, value: str):
    conn = _connect()
    conn.execute(
        "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
        (key, value, value),
    )
    conn.commit()
    conn.close()


# ── Go health ──

def get_go_healthy() -> bool:
    val = _get_state("go_healthy")
    if val is None:
        return True
    return val == "true"


def set_go_healthy(healthy: bool):
    _set_state("go_healthy", "true" if healthy else "false")


# ── Subtask failure counters ──

def get_failure_count(subtask_id: str) -> int:
    if not subtask_id:
        return 0
    conn = _connect()
    row = conn.execute(
        "SELECT failures FROM counters WHERE subtask_id = ?", (subtask_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else 0


def increment_failure(subtask_id: str) -> int:
    if not subtask_id:
        return 0
    conn = _connect()
    conn.execute(
        """INSERT INTO counters (subtask_id, failures) VALUES (?, 1)
           ON CONFLICT(subtask_id) DO UPDATE SET failures = failures + 1""",
        (subtask_id,),
    )
    new_count = conn.execute(
        "SELECT failures FROM counters WHERE subtask_id = ?", (subtask_id,)
    ).fetchone()[0]
    conn.commit()
    conn.close()
    return new_count


def set_failure_count(subtask_id: str, count: int):
    if not subtask_id:
        return
    conn = _connect()
    conn.execute(
        """INSERT INTO counters (subtask_id, failures) VALUES (?, ?)
           ON CONFLICT(subtask_id) DO UPDATE SET failures = ?""",
        (subtask_id, count, count),
    )
    conn.commit()
    conn.close()


# ── Routing log ──

def log_routing(
    subtask_id: str | None,
    role: str,
    model: str,
    backend: str,
    tier: float,
    status: int,
    complex_flag: bool = False,
    failure_count: int = 0,
):
    """Persist one routing record."""
    conn = _connect()
    conn.execute(
        """INSERT INTO routing_log (ts, subtask_id, role, model, backend, tier, status, complex, failures)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (time.time(), subtask_id, role, model, backend, str(tier), status,
         1 if complex_flag else 0, failure_count),
    )
    conn.commit()
    conn.close()


def get_routing_log(subtask_id: str | None = None, limit: int = 100) -> list[dict]:
    """Query routing log. If subtask_id is provided, filter by it."""
    conn = _connect()
    if subtask_id:
        rows = conn.execute(
            """SELECT id, ts, subtask_id, role, model, backend, tier, status, complex, failures
               FROM routing_log WHERE subtask_id = ?
               ORDER BY ts DESC LIMIT ?""",
            (subtask_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, ts, subtask_id, role, model, backend, tier, status, complex, failures
               FROM routing_log ORDER BY ts DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    conn.close()
    return [
        {
            "id": r[0], "ts": r[1], "subtask_id": r[2], "role": r[3],
            "model": r[4], "backend": r[5], "tier": r[6], "status": r[7],
            "complex": bool(r[8]), "failures": r[9],
        }
        for r in rows
    ]


# ── Full state dump ──

def get_all_state() -> dict:
    conn = _connect()
    state_rows = conn.execute("SELECT key, value FROM state").fetchall()
    counter_rows = conn.execute("SELECT subtask_id, failures FROM counters").fetchall()
    routing_rows = conn.execute(
        "SELECT id, ts, subtask_id, role, model, backend, tier, status FROM routing_log ORDER BY ts DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return {
        "state": dict(state_rows),
        "counters": dict(counter_rows),
        "recent_routing": [
            {"id": r[0], "ts": r[1], "subtask_id": r[2], "role": r[3],
             "model": r[4], "backend": r[5], "tier": r[6], "status": r[7]}
            for r in routing_rows
        ],
    }


# Initialize on import
init_db()
