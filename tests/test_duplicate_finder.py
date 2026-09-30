"""duplicate-finder: identical content only, the right copy stays, nothing leaves without your yes."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from argus.config import load_config
from argus.context import Argus
from argus.worker import Worker
from test_worker import Server, client, wait_for

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = "duplicate-finder"


def setup(tmp_path: Path, monkeypatch, live: bool) -> tuple[Argus, Path]:
    home = tmp_path / "home"
    for d in ("Downloads/old", "Pictures/2026"):
        (home / d).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    big = os.urandom(200_000)
    (home / "Pictures/2026/photo.jpg").write_bytes(big)            # the one to keep
    (home / "Downloads/photo (1).jpg").write_bytes(big)            # extra
    (home / "Downloads/old/IMG_0001.jpg").write_bytes(big)         # extra
    (home / "Downloads/same-start.jpg").write_bytes(big[:-10] + b"0123456789")  # same size and head: not a copy
    (home / "Downloads/tiny.txt").write_text("x")                  # too small to look at
    (home / "Downloads/tiny2.txt").write_text("x")
    (home / "Downloads/.hidden.jpg").write_bytes(big)               # hidden: left alone
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.1\n"
        f"plugins:\n  dirs: ['{(ROOT / 'plugins').as_posix()}']\n  live: {[PLUGIN] if live else []}\n",
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return Argus(load_config(tmp_path / "argus.yaml")), home


def scan(cl, w) -> dict:
    job = cl.post(f"/plugins/{PLUGIN}/run", {})
    assert w.run_once(wait=2)
    return wait_for(lambda: (j := cl.get(f"/jobs/{job['id']}"))["state"] in ("succeeded", "dead", "waiting") and j)


def test_dry_run_only_reports(tmp_path, monkeypatch):
    argus, home = setup(tmp_path, monkeypatch, live=False)
    with Server(argus.open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], watch_folders=False)
        w.register()
        assert PLUGIN in w.plugins, w.plugin_errors
        job = scan(cl, w)
    assert job["state"] == "succeeded", job["error"]
    r = job["result"]
    assert r["dry_run"] and r["sets"] == 1 and r["extras"] == 2
    assert sorted(r["would_remove"]) == ["~/Downloads/old/IMG_0001.jpg", "~/Downloads/photo (1).jpg"]
    assert (home / "Downloads/photo (1).jpg").exists()


def test_live_asks_then_recycles_the_extras(tmp_path, monkeypatch):
    sys.modules.pop("send2trash", None)
    monkeypatch.setitem(sys.modules, "send2trash", None)  # use the ~/.argus-trash fallback (no system bin in tests)
    argus, home = setup(tmp_path, monkeypatch, live=True)
    with Server(argus.open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], watch_folders=False)
        w.register()
        job = scan(cl, w)
        assert job["state"] == "waiting", job
        a = cl.get("/approvals?state=pending")[0]
        assert a["type"] == "batch" and a["payload"]["count"] == 2 and "total" not in a["payload"]
        assert {i["keeps"] for i in a["payload"]["items"]} == {"~/Pictures/2026/photo.jpg"}
        (home / "Downloads/old/IMG_0001.jpg").write_bytes(b"changed while you decided" * 9000)
        cl.post(f"/approvals/{a['id']}/decide", {"answer": "approve"})
        wait_for(lambda: cl.get(f"/jobs/{job['id']}")["state"] == "queued")
        assert w.run_once(wait=2)
        done = wait_for(lambda: (j := cl.get(f"/jobs/{job['id']}"))["state"] in ("succeeded", "dead") and j)
    assert done["state"] == "succeeded", done["error"]
    assert done["result"]["removed"] == 1 and done["result"]["skipped"] == ["~/Downloads/old/IMG_0001.jpg"]
    assert not (home / "Downloads/photo (1).jpg").exists() and (home / "Pictures/2026/photo.jpg").exists()
    assert (home / "Downloads/old/IMG_0001.jpg").exists() and (home / "Downloads/same-start.jpg").exists()
    assert list((home / ".argus-trash").rglob("photo (1).jpg"))  # restorable


def test_reject_removes_nothing(tmp_path, monkeypatch):
    argus, home = setup(tmp_path, monkeypatch, live=True)
    with Server(argus.open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], watch_folders=False)
        w.register()
        job = scan(cl, w)
        a = cl.get("/approvals?state=pending")[0]
        cl.post(f"/approvals/{a['id']}/decide", {"answer": "reject"})
        wait_for(lambda: cl.get(f"/jobs/{job['id']}")["state"] == "queued")
        assert w.run_once(wait=2)
        done = wait_for(lambda: (j := cl.get(f"/jobs/{job['id']}"))["state"] in ("succeeded", "dead") and j)
    assert done["result"]["rejected"] is True and (home / "Downloads/photo (1).jpg").exists()
