"""The screenshot-renamer plugin: which names it touches, the name check, and both routes (text -> T1,
picture -> V1)."""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

from argus.config import load_config
from argus.context import Argus
from argus.worker import Worker
from fakes import FakeOllama
from test_worker import Server, client, wait_for

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ssr_under_test", ROOT / "plugins" / "screenshot-renamer" / "plugin.py")
ssr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ssr)

PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                    "0000000d4944415478da63f8ffff3f0005fe02fea7d6a4c50000000049454e44ae426082")


def test_only_default_names_are_touched():
    for n in ("Screenshot 2026-09-28 214501.png", "Screenshot (12).png", "image.png", "Capture.JPG",
              "Screenshot_20260928_101500.png", "screenshot.png"):
        assert ssr.is_default_name(n), n
    for n in ("2026-09-28 cashly login bug.png", "notes.png", "Screenshot tips.pdf", "logo final.png"):
        assert not ssr.is_default_name(n), n


def test_names_are_checked_in_code():
    assert ssr.problem("cashly login bug") is None
    assert ssr.problem("Cashly: Login bug!") is None and ssr.clean("Cashly: Login bug!") == "cashly login bug"
    assert "3 to 6" in ssr.problem("bug")
    assert "generic" in ssr.problem("screenshot of screen")
    assert "dates" in ssr.problem("meeting notes 2026-09-28")
    assert ssr.date_for("Screenshot 2026-09-28 214501.png", 0) == "2026-09-28"
    assert ssr.date_for("Capture.png", time.mktime((2026, 1, 2, 12, 0, 0, 0, 0, -1))) == "2026-01-02"


def setup(tmp_path: Path, monkeypatch) -> tuple[Argus, Path]:
    home = tmp_path / "home"
    shots = home / "Pictures" / "Screenshots"
    shots.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    old = time.time() - 600
    for n in ("Screenshot 2026-09-28 214501.png", "Screenshot 2026-09-29 080000.png", "notes.png"):
        (shots / n).write_bytes(PNG)
        os.utime(shots / n, (old, old))
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.1\n"
        "models:\n  tiers:\n    T1: {provider: ollama, model: 'qwen2.5-coder:7b'}\n"
        "    V1: {provider: ollama, model: 'qwen2.5vl:7b'}\n  chain: [T1]\n"
        f"plugins:\n  dirs: ['{(ROOT / 'plugins').as_posix()}']\n  live: [{ssr.PLUGIN}]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return Argus(load_config(tmp_path / "argus.yaml")), shots


def test_sweep_uses_text_when_there_is_text_and_the_picture_when_not(tmp_path, monkeypatch):
    argus, shots = setup(tmp_path, monkeypatch)
    replies = {"qwen2.5-coder:7b": [{"words": "Screenshot"}, {"words": "cashly login bug"}],  # 1st fails the check
               "qwen2.5vl:7b": [{"words": "argus live map view"}]}
    with FakeOllama(replies) as ol, Server(argus.open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        assert ssr.PLUGIN in w.plugins, w.plugin_errors
        mod = sys.modules["argus_plugin_screenshot_renamer"]
        texts = iter(["Cashly sign in failed: invalid token. Try again", ""])  # 1st has text, 2nd none
        monkeypatch.setattr(mod, "ocr", lambda data: next(texts))
        job = cl.post(f"/plugins/{ssr.PLUGIN}/run", {})
        assert w.run_once(wait=2)
        done = wait_for(lambda: (j := cl.get(f"/jobs/{job['id']}"))["state"] in ("succeeded", "dead", "retry") and j)
        assert done["state"] == "succeeded", done["error"]
        vision = [r for r in ol.requests if r["model"] == "qwen2.5vl:7b"]
        assert len(vision) == 1 and vision[0]["messages"][1]["images"]  # the picture went to V1 only
        assert all("images" not in m for r in ol.requests if r["model"] != "qwen2.5vl:7b" for m in r["messages"])
        changes = cl.get(f"/jobs/{job['id']}/changes")
        assert len(changes) == 2 and all(c["can_undo"] for c in changes)
    assert sorted(p.name for p in shots.iterdir()) == ["2026-09-28 cashly login bug.png",
                                                       "2026-09-29 argus live map view.png", "notes.png"]


def test_no_vision_model_keeps_the_name(tmp_path, monkeypatch):
    argus, shots = setup(tmp_path, monkeypatch)
    with FakeOllama({"qwen2.5-coder:7b": [{"words": "x"}]}) as ol, Server(argus.open()) as srv:  # V1 not pulled
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        monkeypatch.setattr(sys.modules["argus_plugin_screenshot_renamer"], "ocr", lambda data: "")
        job = cl.post("/jobs", {"plugin": ssr.PLUGIN, "workflow": "name", "needs": ["desktop"],
                                "input": {"path": str(shots / "Screenshot 2026-09-28 214501.png")}})
        assert w.run_once(wait=2)
        done = wait_for(lambda: (j := cl.get(f"/jobs/{job['id']}"))["state"] in ("succeeded", "dead") and j)
    assert done["state"] == "succeeded" and "qwen2.5vl" in done["result"]["skipped"]
    assert (shots / "Screenshot 2026-09-28 214501.png").exists()
