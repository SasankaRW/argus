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
    # enabled: false: the nightly one (after 02:30) must not race the button in this test
    a = make(tmp_path, f"backup:\n  enabled: false\n  copy_to: '{dest.as_posix()}'\n  keep: 3\n")
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


# ---------------------------------------------------------------------- restore


def _backup_of(tmp_path: Path, jobs: int) -> tuple[Argus, Path]:
    a = make(tmp_path).open()
    for _ in range(jobs):
        run(a.jobs.enqueue("demo", "echo"))
    made = a.backups.make()
    assert made["ok"]
    return a, a.backups.dir / made["file"]


def _jobs(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    finally:
        conn.close()


def test_restore_puts_the_backup_back_and_keeps_the_old_database(tmp_path):
    from argus.backup import restore
    from argus.db.store import available_migrations

    a, backup = _backup_of(tmp_path, jobs=2)
    run(a.jobs.enqueue("demo", "echo"))  # a third job after the backup
    a.store.close()
    db = a.cfg.db_path
    assert _jobs(db) == 3
    r = restore(backup, db, available_migrations()[-1][0], clock=lambda: datetime(2026, 11, 13, 9, 0).timestamp())
    assert r["ok"] and r["jobs"] == 2 and _jobs(db) == 2
    aside = Path(r["replaced"])
    assert aside == db.parent / "replaced" / "argus-20261113-090000"
    assert _jobs(aside / db.name) == 3  # nothing deleted: the replaced database still has all three
    assert sorted(p.name for p in db.parent.glob("argus.db*")) == ["argus.db"]  # no scraps beside it
    b = Argus(a.cfg).open()  # argusd opens the restored database
    assert b.store.schema_version == available_migrations()[-1][0]
    b.store.close()


def test_restore_refuses_bad_and_newer_backups(tmp_path):
    import pytest

    from argus.backup import RestoreError, restore

    a, backup = _backup_of(tmp_path, jobs=1)
    a.store.close()
    db = a.cfg.db_path
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database at all" * 100)
    with pytest.raises(RestoreError, match="failed its check"):
        restore(broken, db, 999)
    with pytest.raises(RestoreError, match="no backup"):
        restore(tmp_path / "missing.db", db, 999)
    with pytest.raises(RestoreError, match="Upgrade Argus first"):
        restore(backup, db, 1)  # this code "knows" only schema 1
    assert _jobs(db) == 1 and not (db.parent / "replaced").exists()  # nothing touched


def test_restore_into_an_empty_data_folder(tmp_path):
    from argus.backup import restore

    a, backup = _backup_of(tmp_path, jobs=1)
    a.store.close()
    fresh = tmp_path / "fresh" / "argus.db"
    r = restore(backup, fresh, 999)
    assert r["replaced"] is None and _jobs(fresh) == 1


def test_argusd_restore_shows_the_plan_then_restores(tmp_path, capsys):
    from argus.daemon import main

    a, backup = _backup_of(tmp_path, jobs=2)
    run(a.jobs.enqueue("demo", "echo"))
    port = a.cfg.server.port
    a.store.close()
    cfg = str(tmp_path / "argus.yaml")
    from argus import backup as backup_mod

    real = backup_mod.argusd_listening
    backup_mod.argusd_listening = lambda host, p: p == port  # "argusd is running"
    try:
        assert main(["--config", cfg, "--restore", "latest", "--yes"]) == 4
        assert "Stop it first" in capsys.readouterr().err
    finally:
        backup_mod.argusd_listening = real
    backup_mod.argusd_listening = lambda host, p: False
    try:
        assert main(["--config", cfg, "--restore", "latest"]) == 0
        assert "Nothing changed" in capsys.readouterr().out
        assert _jobs(a.cfg.db_path) == 3
        assert main(["--config", cfg, "--restore", backup.name, "--yes"]) == 0
        assert "Restored" in capsys.readouterr().out
        assert _jobs(a.cfg.db_path) == 2
        assert main(["--config", cfg, "--restore", "nope.db", "--yes"]) == 4
    finally:
        backup_mod.argusd_listening = real
