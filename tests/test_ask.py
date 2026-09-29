"""Ask Argus: common asks answered by rules at once; the rest by a model job; actions only on a tap."""

from __future__ import annotations

from pathlib import Path

import pytest

from argus import ask
from argus.config import load_config
from argus.context import Argus
from argus.worker import Worker
from argus.worker.client import ApiError
from fakes import FakeOllama
from test_worker import Server, client, wait_for

ROOT = Path(__file__).resolve().parents[1]
ACTIONS = [{"id": "run:downloads-organizer:sort", "label": "Sort Downloads now", "about": "Downloads organizer"},
           {"id": "run:downloads-organizer:tidy", "label": "Tidy folders", "about": "Downloads organizer"},
           {"id": "run:screenshot-renamer:sweep", "label": "Name screenshots now", "about": "Screenshot renamer"},
           {"id": "power:shutdown", "label": "Shut the PC down", "about": "PC power"}]
SNAP = {"queue": {"running": ["demo.sleep (nap-0)"], "queued": ["demo.echo"], "waiting_for_you": []},
        "approvals": ["CEB bill"], "recent": [{"job": "demo.fail", "state": "dead", "when": "2 min ago",
                                                "error": "boom"}]}


@pytest.mark.parametrize("text,action", [
    ("sort downloads", "run:downloads-organizer:sort"),
    ("Sort my downloads please", "run:downloads-organizer:sort"),
    ("tidy folders", "run:downloads-organizer:tidy"),
    ("name screenshots", "run:screenshot-renamer:sweep"),
    ("shut down the pc", "power:shutdown"),
    ("what's running?", "show:queue"),
    ("anything waiting for me", "show:queue"),
    ("open logs", "show:logs"),
])
def test_rules_catch_the_common_asks(text, action):
    assert ask.rules(text, ACTIONS, SNAP)["action"] == action


def test_rules_leave_the_rest_to_the_model():
    for text in ("why did the bill job fail yesterday", "downloads organizer", "organizer settings?", "hello"):
        assert ask.rules(text, ACTIONS, SNAP) is None, text
    assert "demo.fail" in ask.rules("failures?", ACTIONS, SNAP)["reply"]


def make(tmp_path: Path, monkeypatch) -> Argus:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "Downloads").mkdir(parents=True)
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.1\n"
        "models:\n  tiers:\n    T1: {provider: ollama, model: 'qwen2.5-coder:7b'}\n  chain: [T1]\n"
        f"plugins:\n  dirs: ['{(ROOT / 'plugins').as_posix()}']\n", encoding="utf-8")
    return Argus(load_config(tmp_path / "argus.yaml"))


def test_ask_end_to_end(tmp_path, monkeypatch):
    replies = {"qwen2.5-coder:7b": [{"reply": "I'll do it", "action": "run:made-up:x"},  # not in the list: rejected
                                    {"reply": "That sorts your Downloads.", "action": "run:downloads-organizer:sort"}]}
    with FakeOllama(replies) as ol, Server(make(tmp_path, monkeypatch).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = cl.post("/ask", {"text": "sort downloads"})
        assert r == {"via": "rules", "reply": "Sort Downloads now?", "action": "run:downloads-organizer:sort",
                     "label": "Sort Downloads now"}
        m = cl.post("/ask", {"text": "clean up the files I downloaded"})
        assert m["via"] == "model"
        assert w.run_once(wait=2)
        job = wait_for(lambda: (j := cl.get(f"/jobs/{m['job_id']}"))["state"] in ("succeeded", "dead") and j)
        res = job["result"]
        assert res["action"] == "run:downloads-organizer:sort" and res["label"] == "Sort Downloads now"
        sent = ol.requests[0]["messages"][1]["content"]
        assert "clean up the files" in sent and "Sort Downloads now" in sent
        # nothing ran until the tap
        assert [j["workflow"] for j in cl.get("/jobs?plugin=downloads-organizer")] == []
        done = cl.post("/ask/do", {"action": "run:downloads-organizer:sort"})
        assert cl.get(f"/jobs/{done['job_id']}")["workflow"] == "sort"
        assert cl.post("/ask/do", {"action": "show:queue"}) == {"view": "queue"}
        with pytest.raises(ApiError) as e:
            cl.post("/ask/do", {"action": "run:downloads-organizer:undo"})  # not a button
        assert e.value.status == 404
