"""Fixes from the post-0.7 review: power safety, backups, shares, the Files sandbox, the undo log."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

import argus.worker.power as wpower
from argus.config import load_config
from argus.context import Argus
from argus.shares import ShareStore, ShareTooBig
from argus.worker.client import ApiError
from argus.worker.plugins import Files, PermissionDenied
from conftest import run
from test_worker import Server, client


def make(tmp_path: Path, extra: str = "") -> Argus:
    (tmp_path / "argus.yaml").write_text("logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.1\n" + extra,
                                         encoding="utf-8")
    return Argus(load_config(tmp_path / "argus.yaml"))


# ------------------------------------------------------------------ power

QUSER = """ USERNAME              SESSIONNAME        ID  STATE   IDLE TIME  LOGON TIME
>sas                   console             1  Active      {idle}  9/30/2026 8:01 AM
 guest                                     2  Disc        1+02:03  9/29/2026 7:00 PM
"""


@pytest.mark.parametrize("idle,seconds", [("none", 0), (".", 0), ("7", 420), ("1:05", 3900), ("2+01:00", 176400)])
def test_quser_idle_times(idle, seconds):
    assert wpower.parse_quser(QUSER.format(idle=idle)) == seconds


def test_quser_unreadable_or_nobody():
    assert wpower.parse_quser(QUSER.format(idle="??")) is None
    assert wpower.parse_quser(" USERNAME  SESSIONNAME  ID  STATE\n sas  2  Aktiv  5  x\n") is None  # other language
    only_disc = " USERNAME  ID  STATE  IDLE TIME\n sas  2  Disc  5  9/30/2026\n"
    assert wpower.parse_quser(only_disc) == float("inf")


def test_auto_shutdown_skips_when_it_cannot_tell(monkeypatch):
    from argus.worker.workflows import Context

    ran = []
    monkeypatch.setattr(wpower, "_run", lambda cmd: ran.append(cmd) or "")
    monkeypatch.setattr(wpower, "input_idle_seconds", lambda: None)

    class R:
        lease_lost = False

        def step(self, *a, **k):
            pass

    ctx = Context({"id": "j", "input": {"idle_minutes": 20}}, R())
    assert "can't tell" in wpower.auto_shutdown(ctx)["skipped"] and not ran


def test_power_buttons_need_the_pc_online_and_power_jobs_never_wake_it(tmp_path):
    a = make(tmp_path, "power:\n  mode: real\n  pc_mac: 04:7C:16:AB:CD:EF\n  wol_broadcast: 127.0.0.1\n"
                       "  wol_port: 9\n")
    with Server(a.open()) as srv:
        cl = client(srv.url)
        with pytest.raises(ApiError) as e:
            cl.post("/power/shutdown")
        assert e.value.status == 409
        assert cl.post("/power/cancel")["id"] is None  # nothing to cancel on a PC that is off
        # a power job left in the queue does not count as PC work (no wake-on-LAN for it)
        run(a.jobs.enqueue("power", "auto_shutdown", {}, needs=["desktop"], priority=100))
        assert run(a.store.read(lambda c: a.power._pc_work(c))) == (0, 0)


def test_stale_power_jobs_are_dropped_when_the_pc_is_off(tmp_path):
    a = make(tmp_path).open()
    jid, _ = run(a.jobs.enqueue("power", "shutdown", {}, needs=["desktop"], priority=100))
    a.power.clock = lambda: __import__("time").time() + 400
    run(a.power.tick())
    assert run(a.jobs.get(jid)).state.value == "cancelled"
    a.store.close()


# ------------------------------------------------------------------ backups

@pytest.mark.nightly_backup
def test_a_failed_backup_is_not_retried_every_tick(tmp_path, monkeypatch):
    a = make(tmp_path, "backup:\n  at: '00:00'\n").open()
    calls = []

    def bad():
        calls.append(1)
        raise OSError("disk full")

    monkeypatch.setattr(a.backups, "make", bad)
    run(a.backup_tick())
    run(a.backup_tick())
    assert len(calls) == 1 and a.last_backup["ok"] is False
    n = run(a.store.read(lambda c: c.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]))
    assert n == 1
    a.store.close()


def test_backup_is_named_db_only_after_its_check(tmp_path, monkeypatch):
    a = make(tmp_path).open()
    monkeypatch.setattr(a.backups, "verify", lambda p: {"ok": False, "problem": "bad"})
    r = a.backups.make()
    assert not r["ok"] and a.backups.list() == [] and not list(a.backups.dir.glob("*.part"))
    a.store.close()


# ------------------------------------------------------------------ shares

def test_parallel_uploads_keep_every_file(tmp_path):
    st = ShareStore(tmp_path, 10, 1)
    sid = st.create()["id"]
    ts = [threading.Thread(target=st.add_file, args=(sid, "photo.jpg", "image/jpeg", b"x" * 10)) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    meta = st.meta(sid)
    assert len(meta["files"]) == 8 and len({f["name"] for f in meta["files"]}) == 8
    st.add_file(sid, "PHOTO.JPG", "image/jpeg", b"y")
    assert st.meta(sid)["files"][-1]["name"] != "PHOTO.JPG"  # same name ignoring case: renamed
    assert st.add_file(sid, "con.txt", "text/plain", b"z")["files"][-1]["name"] == "_con.txt"
    with pytest.raises(ShareTooBig):
        st.add_file(sid, "big", "x", b"0" * (11 * 1024 * 1024))


def test_shared_html_downloads_instead_of_opening(tmp_path):
    a = make(tmp_path)
    with Server(a.open()) as srv:
        cl = client(srv.url)
        sid = cl.post("/shares", {"title": "t"})["id"]
        import urllib.request

        def put(name, typ, data):
            req = urllib.request.Request(f"{srv.url}/shares/{sid}/files?name={name}&type={typ}", data=data,
                                         method="PUT")
            return json.loads(urllib.request.urlopen(req).read())

        put("x.html", "text/html", b"<script>alert(1)</script>")
        put("p.png", "image/png", b"\x89PNG")
        with urllib.request.urlopen(f"{srv.url}/shares/{sid}/files/x.html") as r:
            assert r.headers["Content-Type"] == "application/octet-stream"
            assert r.headers["Content-Disposition"].startswith("attachment")
            assert r.headers["X-Content-Type-Options"] == "nosniff"
        with urllib.request.urlopen(f"{srv.url}/shares/{sid}/files/p.png") as r:
            assert r.headers["Content-Type"] == "image/png"
        with pytest.raises(ApiError) as e:
            cl.get("/logs/argus?after=-1")
        assert e.value.status == 422


# ------------------------------------------------------------------ Files

def files(root: Path, dry: bool = False, trace=None) -> Files:
    return Files("p", {"read": [str(root)], "write": [str(root)]}, {}, dry,
                 trace or (lambda k, d: None))


@pytest.mark.skipif(os.name == "nt", reason="symlinks need admin on Windows")
def test_free_name_that_is_a_link_out_is_refused(tmp_path):
    root, out = tmp_path / "dl", tmp_path / "outside"
    root.mkdir()
    out.mkdir()
    (root / "a.txt").write_text("a")
    (root / "a (1).txt").symlink_to(out / "pwned.txt")
    with pytest.raises(PermissionDenied):
        files(root).write_text(root / "a.txt", "b")
    assert not (out / "pwned.txt").exists()


def test_names_are_literal_and_writes_never_replace(tmp_path):
    root = tmp_path / "dl"
    root.mkdir()
    f = files(root)
    (root / "price $HOME.pdf").write_text("p")
    (root / "sub").mkdir()
    moved = f.move(root / "price $HOME.pdf", root / "sub")
    assert moved == str((root / "sub" / "price $HOME.pdf").resolve())
    (root / "n.url").write_text("old")
    got = f.write_text(root / "n.url", "a\r\nb\r\n")
    assert Path(got).name == "n (1).url" and Path(got).read_bytes() == b"a\r\nb\r\n"
    assert (root / "n.url").read_text() == "old"


def test_dry_run_remove_dir_counts_planned_moves(tmp_path):
    root = tmp_path / "dl"
    (root / "d").mkdir(parents=True)
    (root / "d" / "x").write_text("x")
    (root / "d" / "y").write_text("y")
    f = files(root, dry=True)
    f.move(root / "d" / "x", root)
    assert f.remove_empty_dir(root / "d") is False  # y would still be there
    f.move(root / "d" / "y", root)
    assert f.remove_empty_dir(root / "d") is True
    assert (root / "d" / "x").exists()  # dry-run: nothing moved


def test_change_is_recorded_after_it_happened(tmp_path):
    root = tmp_path / "dl"
    root.mkdir()
    seen = []
    f = files(root, trace=lambda k, d: seen.append((k, (root / "b").exists())))
    (root / "a").write_text("a")
    with pytest.raises(FileNotFoundError):
        f.move(root / "nope", root / "c")
    assert seen == []  # a move that failed is not in the undo log
    f.move(root / "a", root / "b")
    assert seen == [("file.moved", True)]


def test_a_column_added_late_to_a_migration_is_repaired(tmp_path):
    """A database that applied 0011 before `ari_turns.used` was added to it gets the column on open."""
    import sqlite3

    from argus.db import Store

    s = Store(tmp_path / "a.db")
    s.open()
    s.close()
    c = sqlite3.connect(tmp_path / "a.db")
    c.execute("ALTER TABLE ari_turns DROP COLUMN used")
    c.commit()
    c.close()
    s = Store(tmp_path / "a.db")
    s.open()
    s.close()
    c = sqlite3.connect(tmp_path / "a.db")
    assert "used" in {r[1] for r in c.execute("PRAGMA table_info(ari_turns)")}
