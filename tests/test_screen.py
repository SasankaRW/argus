"""Screen and clipboard: Ari looks at the screen (V1, or the text on it) and sums up what you copied, locally."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from PIL import Image

from argus.config import load_config
from argus.context import Argus
from argus.worker import Worker
from fakes import FakeOllama, fake_claude
from test_worker import Server, client, wait_for

ROOT = Path(__file__).resolve().parents[1]
MOD = "argus_plugin_screen"


def setup(tmp_path: Path, monkeypatch, vision: bool = True, claude: bool = False) -> Argus:
    tiers = "    T1: {provider: ollama, model: 'qwen2.5-coder:7b'}\n"
    if vision:
        tiers += "    V1: {provider: ollama, model: 'qwen2.5vl:7b'}\n"
    extra = ""
    if claude:
        tiers += "    T3: {provider: claude}\n"
        cmd = fake_claude(tmp_path, result=json.dumps({"answer": "x"}))
        extra = f"claude:\n  command: {json.dumps(cmd)}\n"
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.1\n" + extra +
        f"models:\n  tiers:\n{tiers}  chain: [T1{', T3' if claude else ''}]\n"
        f"plugins:\n  dirs: ['{(ROOT / 'plugins').as_posix()}']\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return Argus(load_config(tmp_path / "argus.yaml"))


def run(argus, replies, monkeypatch, workflow, patch, **inp):
    with FakeOllama(replies) as ol, Server(argus.open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop", "session"], ollama_url=ol.url, watch_folders=False)
        w.register()
        assert "screen" in w.plugins, w.plugin_errors
        for name, fn in patch.items():
            monkeypatch.setattr(sys.modules[MOD], name, fn)
        job = cl.post("/jobs", {"plugin": "screen", "workflow": workflow, "needs": ["session"], "input": inp})
        w.run_once(wait=2)
        done = wait_for(lambda: (j := cl.get(f"/jobs/{job['id']}"))["state"] in ("succeeded", "dead") and j)
        return done, ol


def shot(w=2560, h=1440):
    return Image.new("RGB", (w, h), (30, 30, 30))


def test_it_looks_with_the_vision_model_and_saves_nothing(tmp_path, monkeypatch):
    replies = {"qwen2.5vl:7b": [{"answer": "- a list"}, {"answer": "VS Code is open on think.py, with no errors."}]}
    done, ol = run(setup(tmp_path, monkeypatch), replies, monkeypatch, "look", {"grab": shot},
                   question="any errors?")
    assert done["state"] == "succeeded", done["error"]
    assert done["result"] == {"answer": "VS Code is open on think.py, with no errors.", "how": "vision (V1)"}
    first = ol.requests[0]["messages"][1]
    assert first["images"] and "any errors?" in first["content"]
    assert "no lists" in ol.requests[1]["messages"][-1]["content"]
    assert not list(tmp_path.rglob("*.png"))


def test_no_vision_model_reads_the_text_on_screen(tmp_path, monkeypatch):
    replies = {"qwen2.5-coder:7b": [{"answer": "From the text on it: a build failed with 3 errors."}]}
    done, ol = run(setup(tmp_path, monkeypatch, vision=False), replies, monkeypatch, "look",
                   {"grab": shot, "screen_text": lambda img: "Build failed 3 errors src/app.ts"})
    assert done["state"] == "succeeded", done["error"]
    assert done["result"]["how"] == "the text on it (T1)"
    assert "Build failed" in ol.requests[0]["messages"][1]["content"]


def test_nothing_on_the_screen_or_clipboard_goes_to_claude(tmp_path, monkeypatch):
    replies = {"qwen2.5-coder:7b": [{"answer": ""}] * 6}
    done, _ = run(setup(tmp_path, monkeypatch, vision=False, claude=True), replies, monkeypatch, "clipboard",
                  {"read_clipboard": lambda: {"text": "quarterly numbers " * 40}})
    assert done["state"] == "dead" and "couldn't sum it up" in done["error"]
    argv = tmp_path / "claude_argv.json"  # only the login check ran ("auth status"), never a question
    assert not argv.exists() or json.loads(argv.read_text()) == ["auth", "status"]


@pytest.mark.parametrize("clip, want", [
    ({}, "The clipboard is empty."),
    ({"text": "  call Nimal at 5  "}, "You copied: call Nimal at 5"),
])
def test_short_or_empty_clipboard_needs_no_model(tmp_path, monkeypatch, clip, want):
    done, ol = run(setup(tmp_path, monkeypatch), {}, monkeypatch, "clipboard", {"read_clipboard": lambda: clip})
    assert done["result"]["answer"] == want and ol.requests == []


def test_long_text_is_summed_up_and_a_copied_picture_is_looked_at(tmp_path, monkeypatch):
    text = "Meeting notes. " + "We agreed to ship the island on Friday and Kamal writes the tests. " * 30
    replies = {"qwen2.5-coder:7b": [{"answer": "You agreed to ship the island on Friday; Kamal writes the tests."}],
               "qwen2.5vl:7b": [{"answer": "It's a bar chart of monthly sales."}]}
    argus = setup(tmp_path, monkeypatch)
    done, ol = run(argus, replies, monkeypatch, "clipboard", {"read_clipboard": lambda: {"text": text}},
                   how="action items")
    assert done["result"]["answer"].startswith("You agreed") and done["result"]["copied"].endswith("words")
    assert "action items" in ol.requests[0]["messages"][1]["content"]
    (tmp_path / "b").mkdir()
    done, _ = run(setup(tmp_path / "b", monkeypatch), replies, monkeypatch, "clipboard",
                  {"read_clipboard": lambda: {"image": shot(800, 600)}})
    assert done["result"] == {"answer": "It's a bar chart of monthly sales.", "how": "vision (V1)",
                              "copied": "a picture"}
