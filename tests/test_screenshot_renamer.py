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


def screen(text: str) -> dict:
    return {"text": text, "focus": [text]} if text else {}


def run_sweep(argus, replies, screens, monkeypatch):
    with FakeOllama(replies) as ol, Server(argus.open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        assert ssr.PLUGIN in w.plugins, w.plugin_errors
        it = iter(screens)
        monkeypatch.setattr(sys.modules["argus_plugin_screenshot_renamer"], "read_screen", lambda data: next(it))
        job = cl.post(f"/plugins/{ssr.PLUGIN}/run", {})
        assert w.run_once(wait=2)
        done = wait_for(lambda: (j := cl.get(f"/jobs/{job['id']}"))["state"] in ("succeeded", "dead", "retry") and j)
        assert done["state"] == "succeeded", done["error"]
        return done, ol, cl.get(f"/jobs/{job['id']}/changes")


def test_vision_first_and_names_are_checked(tmp_path, monkeypatch):
    argus, shots = setup(tmp_path, monkeypatch)
    replies = {"qwen2.5vl:7b": [{"words": "Screenshot"},  # generic: rejected, tried again with the reason
                                {"main_window": "Cashly", "words": "cashly login bug"},
                                {"main_window": "Helios", "words": "argus live map view"}]}
    done, ol, changes = run_sweep(argus, replies, [screen("Cashly sign in failed: invalid token. Try again"),
                                                   screen("")], monkeypatch)
    vision = [r for r in ol.requests if r["model"] == "qwen2.5vl:7b"]
    assert len(vision) == 3 and all(r["messages"][1]["images"] for r in vision[:1])
    assert "text_near_the_middle" in vision[0]["messages"][1]["content"]
    assert len(changes) == 2 and all(c["can_undo"] for c in changes)
    assert sorted(p.name for p in shots.iterdir()) == ["2026-09-28 cashly login bug.png",
                                                       "2026-09-29 argus live map view.png", "notes.png"]


def test_made_up_or_background_words_are_rejected(tmp_path, monkeypatch):
    """The real case: a File Explorer screenshot named after a sidebar of another app behind it."""
    argus, shots = setup(tmp_path, monkeypatch)
    text = "Downloads Search Downloads Web Archives Installers Misc Last week Earlier this month"
    replies = {"qwen2.5vl:7b": [{"words": "argus integration desktop"}],  # twice: not in the window in front
               "qwen2.5-coder:7b": [{"words": "file explorer downloads folder"}]}
    done, ol, _ = run_sweep(argus, replies, [{"text": text + " with Argus integration", "focus": [text]},
                                             screen("")], monkeypatch)
    first = done["result"]["results"][0]
    assert first["renamed"] == "2026-09-28 file explorer downloads folder.png" and first["how"] == "text (T1)"
    rejected = [r for r in ol.requests if r["model"] == "qwen2.5vl:7b"][1]["messages"][-1]["content"]
    assert "background" in rejected and "integration" in rejected
    assert ssr.ungrounded("file explorer downloads folder", text) == []
    assert ssr.ungrounded("argus project notes", text) == ["argus", "project"]


def test_ranking_prefers_the_window_in_front():
    # a big title in the middle, a sidebar line at the far left, joined by Tesseract into one line
    d = {"text": ["Downloads", "Argus", "integration", "Documents"], "conf": [95, 90, 90, 90],
         "block_num": [1, 2, 2, 3], "par_num": [1, 1, 1, 1], "line_num": [1, 1, 1, 1],
         "left": [800, 5, 60, 900], "top": [500, 300, 300, 600], "width": [200, 50, 60, 120],
         "height": [40, 12, 12, 14]}
    r = ssr.rank_lines(d, 1920, 1080)
    assert r["focus"][0] == "Downloads" and r["focus"][-1] == "Argus integration"


def test_no_vision_model_keeps_the_name(tmp_path, monkeypatch):
    argus, shots = setup(tmp_path, monkeypatch)
    with FakeOllama({"qwen2.5-coder:7b": [{"words": "x"}]}) as ol, Server(argus.open()) as srv:  # V1 not pulled
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        monkeypatch.setattr(sys.modules["argus_plugin_screenshot_renamer"], "read_screen", lambda data: {})
        job = cl.post("/jobs", {"plugin": ssr.PLUGIN, "workflow": "name", "needs": ["desktop"],
                                "input": {"path": str(shots / "Screenshot 2026-09-28 214501.png")}})
        assert w.run_once(wait=2)
        done = wait_for(lambda: (j := cl.get(f"/jobs/{job['id']}"))["state"] in ("succeeded", "dead") and j)
    assert done["state"] == "succeeded" and "qwen2.5vl" in done["result"]["skipped"]
    assert (shots / "Screenshot 2026-09-28 214501.png").exists()
