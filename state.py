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
        conn.execute("""\n            CREATE TABLE IF NOT EXISTS routing_log (\n                id INTEGER PRIMARY KEY AUTOINCREMENT,\n                ts REAL NOT NULL,\n                subtask_id TEXT,\n                role TEXT NOT NULL,\n                model TEXT NOT NULL,\n                backend TEXT NOT NULL,\n                tier TEXT NOT NULL,\n                status INTEGER NOT NULL,\n                complex INTEGER DEFAULT 0,\n                failures INTEGER DEFAULT 0,\n                role_source TEXT DEFAULT '',\n                fallback_reason TEXT DEFAULT '',\n                requested_model TEXT DEFAULT '',\n                resolved_role TEXT DEFAULT ''\n            )\n        """)
        conn.commit()
        conn.close()
    _migrate_db()


def _migrate_db():
    """Add missing columns on startup — safe for existing DBs."""
    conn = _connect()
    cursor = conn.execute("PRAGMA table_info(routing_log)")
    columns = {row[1] for row in cursor.fetchall()}
    if "role_source" not in columns:
        conn.execute("ALTER TABLE routing_log ADD COLUMN role_source TEXT DEFAULT ''")
    if "fallback_reason" not in columns:
        conn.execute("ALTER TABLE routing_log ADD COLUMN fallback_reason TEXT DEFAULT ''")
    if "requested_model" not in columns:
        conn.execute("ALTER TABLE routing_log ADD COLUMN requested_model TEXT DEFAULT ''")
    if "resolved_role" not in columns:
        conn.execute("ALTER TABLE routing_log ADD COLUMN resolved_role TEXT DEFAULT ''")
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
    role_source: str = "",
    fallback_reason: str = "",
    requested_model: str = "",
    resolved_role: str = "",
):
    """Persist one routing record."""
    conn = _connect()
    conn.execute(
        """INSERT INTO routing_log (ts, subtask_id, role, model, backend, tier, status, complex, failures, role_source, fallback_reason, requested_model, resolved_role)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (time.time(), subtask_id, role, model, backend, str(tier), status,
         1 if complex_flag else 0, failure_count, role_source, fallback_reason, requested_model, resolved_role),
    )
    conn.commit()
    conn.close()


def get_routing_log(subtask_id: str | None = None, limit: int = 100) -> list[dict]:
    """Query routing log. If subtask_id is provided, filter by it."""
    conn = _connect()
    cols = "id, ts, subtask_id, role, model, backend, tier, status, complex, failures, role_source, fallback_reason, requested_model, resolved_role"
    if subtask_id:
        rows = conn.execute(
            f"""SELECT {cols}
               FROM routing_log WHERE subtask_id = ?
               ORDER BY ts DESC LIMIT ?""",
            (subtask_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""SELECT {cols}
               FROM routing_log ORDER BY ts DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    conn.close()
    return [
        {
            "id": r[0], "ts": r[1], "subtask_id": r[2], "role": r[3],
            "model": r[4], "backend": r[5], "tier": r[6], "status": r[7],
            "complex": bool(r[8]), "failures": r[9],
            "role_source": r[10] if len(r) > 10 else "",
            "fallback_reason": r[11] if len(r) > 11 else "",
            "requested_model": r[12] if len(r) > 12 else "",
            "resolved_role": r[13] if len(r) > 13 else "",
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
