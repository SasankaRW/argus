"""The timeline: requests to Ari as stories, one request's trace, what isn't working, and the machines' lanes."""

from __future__ import annotations

import json
import time
from pathlib import Path

from argus import timeline
from argus.config import load_config
from argus.context import Argus
from argus.events import insert_event
from conftest import run
from test_worker import Server, client


def make(tmp_path: Path) -> Argus:
    (tmp_path / "argus.yaml").write_text("logging:\n  file: null\n", encoding="utf-8")
    return Argus(load_config(tmp_path / "argus.yaml"))


def job(c, jid, state, result=None, error=None, at=None, plugin="ari", workflow="think"):
    now = at or time.time()
    c.execute("INSERT INTO jobs (id, plugin, workflow, state, run_after, input, result, error, created_at, updated_at,"
              " finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              (jid, plugin, workflow, state, now, "{}", json.dumps(result) if result else None, error, now - 3, now,
               now))


def seed(c):
    now = time.time()
    # a request that failed in a tool, with how it was heard
    c.execute("INSERT INTO ari_turns (conv, role, text, created_at, meta) VALUES ('c1','you','play famous',?,?)",
              (now - 60, json.dumps({"source": "voice", "heard": {"accepted": 0.4, "top": "si", "top_p": 0.45}})))
    job(c, "j1", "succeeded", {"reply": "Couldn't.", "used": [{"tool": "play_on_spotify",
                                                               "result": {"done": False,
                                                                          "problem": "search box not found"}}]},
        at=now - 52)
    c.execute("INSERT INTO ari_turns (conv, role, text, job_id, created_at, meta) VALUES ('c1','ari','Couldn''t.',"
              "'j1',?,?)", (now - 59, json.dumps({"answer_ms": 8100, "spoken": True})))
    c.execute("INSERT INTO model_calls (created_at, job_id, plugin, workflow, step, tier, model, verdict, reason,"
              " latency_ms, bytes) VALUES (?,?,?,?,?,?,?,?,?,?,0)",
              (now - 58, "j1", "ari", "think", "decide 1", "T1", "qwen3:8b", "rejected", "not one of the tools", 900))
    # one answered at once (the rules) and one that died
    c.execute("INSERT INTO ari_turns (conv, role, text, created_at) VALUES ('c2','you','where is my phone',?)",
              (now - 30,))
    c.execute("INSERT INTO ari_turns (conv, role, text, created_at) VALUES ('c2','ari','At home.',?)", (now - 30,))
    c.execute("INSERT INTO ari_turns (conv, role, text, created_at) VALUES ('c3','you','check my mails',?)",
              (now - 20,))
    job(c, "j2", "dead", error="Traceback (most recent call last):\n  File x\nPermanentError: Gmail isn't allowed yet",
        at=now - 15)
    c.execute("INSERT INTO ari_turns (conv, role, text, job_id, created_at) VALUES ('c3','ari',NULL,'j2',?)",
              (now - 19,))
    insert_event(c, now - 40, "ari.ignored", data={"why": "another language (si 80%, English 10%)",
                                                   "text": "kohomada"})
    insert_event(c, now - 45, "job.leased", job_id="j1", src="desktop-pc", dst="ari")
    insert_event(c, now - 40, "job.succeeded", job_id="j1", src="desktop-pc", dst="ari")


def test_requests_read_as_stories(tmp_path):
    a = make(tmp_path)
    a.open()
    run(a.store.write(seed))
    rows = run(a.store.read(lambda c: timeline.requests(c, limit=10)))
    by = {r["text"]: r for r in rows}
    assert [r["result"] for r in rows] == ["failed", "ok", "ignored", "failed"]  # newest first
    play = by["play famous"]
    assert play["why"] == "search box not found" and play["tools"][0]["tool"] == "play_on_spotify"
    assert play["heard"]["top"] == "si"
    assert play["answer_ms"] == 8100 and play["models"][0]["verdict"] == "rejected" and play["source"] == "voice"
    assert by["where is my phone"]["via"] == "rules" and by["check my mails"]["why"].startswith("PermanentError")
    assert "kohomada" in [r["text"] for r in rows] and by["kohomada"]["why"].startswith("another language")
    assert timeline.line(play).endswith("s") and "✗" in timeline.line(play)


def test_a_trace_and_what_isnt_working(tmp_path):
    a = make(tmp_path)
    a.open()
    run(a.store.write(seed))
    t = run(a.store.read(lambda c: timeline.trace(c, "j1")))
    assert t["text"] == "play famous" and [s["kind"] for s in t["steps"]] == ["model"]
    assert run(a.store.read(lambda c: timeline.trace(c, "last")))["text"] == "check my mails"
    p = run(a.store.read(lambda c: timeline.problems(c, time.time(), 24)))
    areas = {g["area"]: g for g in p["problems"]}
    assert {"tool play_on_spotify", "ari.think", "model qwen3:8b", "hearing"} <= set(areas)
    assert areas["model qwen3:8b"]["hint"] and "problem" in timeline.headline(p)
    lanes = run(a.store.read(lambda c: timeline.lanes(c, time.time(), 60)))
    assert lanes["workers"][0]["worker"] == "desktop-pc" and lanes["workers"][0]["jobs"][0]["workflow"] == "think"


def test_the_endpoints_and_the_listener_notes(tmp_path):
    a = make(tmp_path)
    with Server(a.open()) as srv:
        cl = client(srv.url)
        r = cl.post("/ari", {"text": "where's my phone", "source": "voice", "heard": {"accepted": 0.9, "top": "en"}})
        cl.post("/ari/ignored", {"why": "no wake phrase", "text": "Hey, I did it."})
        cl.post("/ari/spoken", {"conv": r["conv"], "answer_ms": 700, "spoken": True})
        rows = cl.get("/ari/timeline")["requests"]
        mine = next(x for x in rows if x.get("conv") == r["conv"])
        assert mine["source"] == "voice" and mine["heard"]["accepted"] == 0.9 and mine["answer_ms"] == 700
        assert any(x["result"] == "ignored" and x["text"] == "Hey, I did it." for x in rows)
        assert cl.get("/ari/trace/last")["text"] == "where's my phone"
        assert "headline" in cl.get("/problems") and "workers" in cl.get("/timeline/lanes")
        assert len(cl.get("/timeline/activity?hours=6")["ari"]) == 6


def test_activity_hour_by_hour(tmp_path):
    a = make(tmp_path)
    a.open()
    run(a.store.write(seed))
    act = run(a.store.read(lambda c: timeline.activity(c, time.time(), 24)))
    assert len(act["jobs"]) == len(act["ari"]) == 24
    t = act["total"]
    assert (t["ok"], t["failed"], t["ignored"]) == (1, 2, 1)
    assert t["jobs_ok"] == 1 and t["jobs_failed"] == 1 and t["answer_ms"] is not None
    assert sum(b["failed"] for b in act["ari"][-2:]) == 2  # in the last hour or two (seeded a minute ago)
