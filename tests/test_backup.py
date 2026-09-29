"""Nightly backups: made while running, checked by restoring, pruned, and copied to the PC by its worker."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from argus.backup import Backups
from argus.config import load_config
from argus.context import Argus
from argus.worker import Worker
from conftest import run
from test_worker import Server, client, wait_for


def make(tmp_path: Path, extra: str = "") -> Argus:
    (tmp_path / "argus.yaml").write_text("logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.1\n" + extra,
                                         encoding="utf-8")
    return Argus(load_config(tmp_path / "argus.yaml"))


def test_make_verify_prune(tmp_path):
    a = make(tmp_path).open()
    t = [datetime(2026, 9, 30, 2, 30).timestamp()]
    b = Backups(a.cfg.db_path, keep=2, clock=lambda: t[0])
    run(a.jobs.enqueue("demo", "echo"))
    for _ in range(3):
        t[0] += 86400
        r = b.make()
        assert r["ok"] and r["jobs"] == 1 and r["schema"] >= 7
    assert [f["file"] for f in b.list()] == ["argus-20261003-023000.db", "argus-20261002-023000.db"]
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database at all" * 100)
    assert Backups.verify(broken)["ok"] is False
    a.store.close()


def test_due_once_per_night(tmp_path):
    a = make(tmp_path, "backup:\n  at: '02:30'\n").open()
    night = datetime(2026, 9, 30, 2, 40).timestamp()
    assert a._backup_due(night)  # nothing yet
    a.backups.clock = lambda: night
    a.backups.make()
    assert not a._backup_due(night + 3600)
    assert not a._backup_due(datetime(2026, 9, 30, 23, 0).timestamp())
    assert a._backup_due(datetime(2026, 10, 1, 2, 31).timestamp())
    a.store.close()


def test_backup_api_and_the_pc_copy(tmp_path):
    dest = tmp_path / "pc-backups"
    a = make(tmp_path, f"backup:\n  copy_to: '{dest.as_posix()}'\n  keep: 3\n")
    with Server(a.open()) as srv:
        cl = client(srv.url)
        pc = Worker(cl, "pc", capabilities=["desktop"], watch_folders=False)
        pc.register()
        res = cl.post("/backups")
        assert res["ok"] and cl.get("/backups")["files"][0]["file"] == res["file"]
        assert pc.run_once(wait=2)
        job = wait_for(lambda: (j := cl.get("/jobs?plugin=backup")[0])["state"] in ("succeeded", "dead") and j)
        assert job["state"] == "succeeded", job["error"]
        with __import__("pytest").raises(Exception):
            cl.get("/backups/files/..%2Fargus.db")
    copied = dest / res["file"]
    assert copied.exists() and sqlite3.connect(copied).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
