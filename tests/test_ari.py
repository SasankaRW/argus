"""Ari: talk to Argus. Times become schedules (after your yes), actions wait for your yes, the rest goes to a model
with the conversation so far."""

from __future__ import annotations

from datetime import datetime

import pytest

from argus.ari import parse_when
from argus.worker import Worker
from conftest import run
from fakes import FakeOllama
from test_ask import make
from test_worker import Server, client, wait_for

NOW = datetime(2026, 9, 30, 14, 0).timestamp()  # a Wednesday, 2 pm


@pytest.mark.parametrize("text,cron,once,rest", [
    ("every morning at 7 sort downloads", "0 7 * * *", False, "sort downloads"),
    ("sort downloads every day at 7:30pm", "30 19 * * *", False, "sort downloads"),
    ("remind me to call mum tomorrow at 5 pm", "0 17 1 10 *", True, "remind me to call mum"),
    ("shut down the pc at 11pm", "0 23 30 9 *", True, "shut down the pc"),
    ("in 20 minutes remind me to check the oven", "20 14 30 9 *", True, "remind me to check the oven"),
    ("every weekday at 9 name screenshots", "0 9 * * 1-5", False, "name screenshots"),
    ("every monday and friday at 8am sort downloads", "0 8 * * 1,5", False, "sort downloads"),
    ("every 2 hours sort downloads", "0 */2 * * *", False, "sort downloads"),
    ("at 5 remind me to leave", "0 17 30 9 *", True, "remind me to leave"),  # 5 said at 2 pm: 5 pm
    ("tonight at 10 shut down the pc", "0 22 30 9 *", True, "shut down the pc"),
    ("every night sort downloads", "0 22 * * *", False, "sort downloads"),
])
def test_times_in_plain_words(text, cron, once, rest):
    w = parse_when(text, NOW)
    assert w is not None and (w.cron, w.once, w.rest) == (cron, once, rest), w


@pytest.mark.parametrize("text", ["sort downloads", "what's running", "sort 3 files", "hello ari"])
def test_no_time_no_schedule(text):
    assert parse_when(text, NOW) is None


def test_schedule_by_talking_needs_your_yes(tmp_path, monkeypatch):
    a = make(tmp_path, monkeypatch)
    with Server(a.open()) as srv:
        cl = client(srv.url)
        r = cl.post("/ari", {"text": "Hey Ari, sort my downloads every morning at 7"})
        conv = r["conv"]
        assert "Every day at 7 am, I'll sort Downloads." in r["reply"] and r["pending"]["kind"] == "schedule"
        assert [s for s in cl.get("/schedules") if s["owner"] == "you"] == []  # nothing yet
        done = cl.post("/ari", {"text": "yes please", "conv": conv})
        assert done["reply"].startswith("Done.") and done["schedule"]["cron"] == "0 7 * * *"
        mine = [s for s in cl.get("/schedules") if s["owner"] == "you"]
        assert len(mine) == 1 and mine[0]["plugin"] == "downloads-organizer" and mine[0]["workflow"] == "sort"
        assert mine[0]["label"] == "every day at 7 am: sort Downloads"
        # pause, then delete it
        cl.call("PATCH", f"/schedules/{mine[0]['id']}", {"enabled": False})
        assert not [s for s in cl.get("/schedules") if s["owner"] == "you"][0]["enabled"]
        cl.call("DELETE", f"/schedules/{mine[0]['id']}")
        assert [s for s in cl.get("/schedules") if s["owner"] == "you"] == []
        # no: nothing is made
        r = cl.post("/ari", {"text": "remind me to stretch in 30 minutes", "conv": conv})
        assert 'remind you: "stretch"' in r["reply"]
        assert cl.post("/ari", {"text": "no", "conv": conv})["reply"] == "Okay, I won't."
        turns = cl.get(f"/ari/{conv}")["turns"]
        assert [t["role"] for t in turns] == ["you", "ari", "you", "ari", "you", "ari", "you", "ari"]


def test_a_reminder_goes_to_the_phone_at_its_time(tmp_path, monkeypatch):
    a = make(tmp_path, monkeypatch).open()
    from argus import ari

    w = ari.parse_when("remind me to drink water in 5 minutes", NOW)
    run(a.store.write(lambda c: ari.add_schedule(c, NOW, w, "remind:drink water", plugin="argus",
                                                 workflow="remind", input={"text": "drink water"}, needs=[],
                                                 priority=50, label="x")))
    a.scheduler.clock = lambda: NOW + 301
    assert run(a.scheduler.tick()) == 1
    msgs = run(a.store.read(lambda c: [r[0] for r in c.execute("SELECT payload FROM outbox")]))
    assert any("drink water" in m for m in msgs)
    s = [x for x in run(a.scheduler.list()) if x["owner"] == "you"][0]
    assert s["enabled"] is False  # once: done
    a.scheduler.clock = lambda: NOW + 9999
    assert run(a.scheduler.tick()) == 0
    a.store.close()


def test_actions_wait_for_yes_and_the_model_gets_the_conversation(tmp_path, monkeypatch):
    replies = {"qwen2.5-coder:7b": [{"reply": "Hi! I'm Ari. Want me to sort your downloads?",
                                     "action": "run:downloads-organizer:sort"}]}
    with FakeOllama(replies) as ol, Server(make(tmp_path, monkeypatch).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = cl.post("/ari", {"text": "sort downloads"})
        conv = r["conv"]
        assert r["pending"] == {"kind": "action", "action": "run:downloads-organizer:sort"}
        assert cl.get("/jobs?plugin=downloads-organizer") == []
        d = cl.post(f"/ari/{conv}/answer", {"yes": True})
        assert d["reply"] == "Done." and cl.get(f"/jobs/{d['job_id']}")["workflow"] == "sort"
        m = cl.post("/ari", {"text": "hello there, who are you?", "conv": conv})
        assert m["reply"] is None and m["job_id"]
        cl.post(f"/jobs/{d['job_id']}/cancel")  # (the sort job would run first)
        assert w.run_once(wait=2)
        t = wait_for(lambda: (x := cl.get(f"/ari/{conv}")["turns"][-1])["text"] and x)
        assert t["text"].startswith("Hi! I'm Ari") and t["pending"]["action"] == "run:downloads-organizer:sort"
        sent = ol.requests[0]["messages"]
        assert "You are Ari" in sent[0]["content"] and "conversation_so_far" in sent[1]["content"]
        assert "sort downloads" in sent[1]["content"]  # the earlier turns
