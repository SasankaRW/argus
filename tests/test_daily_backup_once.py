"""A failed backup shows once in the brief: as "Backup failed", not again as a job that failed overnight."""

from __future__ import annotations

import sqlite3
import time

from argus.daily import brief_parts


def db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE jobs (id TEXT, plugin TEXT, workflow TEXT, state TEXT, finished_at REAL, result TEXT,"
              " input TEXT, created_at REAL)")
    c.execute("CREATE TABLE approvals (title TEXT, state TEXT, created_at REAL)")
    return c


def test_failed_backup_is_not_counted_twice():
    c, now = db(), time.time()
    c.execute("INSERT INTO jobs VALUES ('1', 'backup', 'copy', 'dead', ?, NULL, NULL, ?)", (now - 3600, now - 3700))
    c.execute("INSERT INTO jobs VALUES ('2', 'vault', 'tidy', 'dead', ?, NULL, NULL, ?)", (now - 3600, now - 3700))
    p = brief_parts(c, now, last_backup={"ok": False, "at": now - 3600, "problem": "drive full"}, weather={})
    assert p["backup"]["problem"] == "drive full" and p["dead"] == ["vault.tidy"] and p["dead_n"] == 1
    p = brief_parts(c, now, last_backup={"ok": True, "at": now - 3600}, weather={})
    assert p["backup"] is None and p["dead_n"] == 2
