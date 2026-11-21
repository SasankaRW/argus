"""When you last cleared the problem and dead-job counts (console `c`, Helios, POST /problems/clear).

Clearing deletes nothing: the counts simply start again from that moment (dead jobs that finished before it, and
problems before it, are no longer counted). Kept in the settings table."""

from __future__ import annotations

import json
import sqlite3
import time

KEY = "cleared.problems_at"


def at(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (KEY,)).fetchone()
    try:
        return float(json.loads(row[0])) if row and row[0] else 0.0
    except (ValueError, TypeError):
        return 0.0


def clear(conn: sqlite3.Connection, now: float | None = None) -> float:
    when = time.time() if now is None else now
    conn.execute("INSERT INTO settings (key, value, created_at, updated_at) VALUES (?, ?, ?, ?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                 (KEY, json.dumps(when), when, when))
    return when
