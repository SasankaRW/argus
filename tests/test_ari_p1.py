"""Ari P1: late answers in order, one voice, livelier talk, Ollama kept up, a full off / on."""

from __future__ import annotations

import json
import threading

from argus import ari_listen
from argus import voice_live as vl
from argus.supervisor import OllamaKeeper
from argus.worker import Worker
from fakes import FakeOllama
from test_ari_think import make, settle
from test_voice_live import DT, Clock, F, wake_rest
from test_worker import Server, client, wait_for


class Player:
    def __init__(self):
        self.said: list[str] = []
        self.busy, self.text = False, ""

    def say(self, parts):
        self.said.append(" ".join(parts))

    def add(self, parts):
        self.said.append(" ".join(parts))

    def pause(self):
        pass

    def resume(self):
        pass

    def stop(self):
        pass


def talk_with(answers: dict):
    """A Talk that asks in the background; each question's answer is released by the test (gates)."""
    gates = {q: threading.Event() for q in answers}
    heard: list[str] = []
    p = Player()
    jobs: list[threading.Thread] = []

    def ask(q):
        gates[q].wait(5)
        return {"reply": answers[q]}

    def run(f):
        t = threading.Thread(target=f, daemon=True)
        jobs.append(t)
        t.start()

    talk = vl.Talk(turns=vl.Turns(), transcribe=lambda a: heard.pop(0), wake_rest=wake_rest, ask=ask, player=p,
                   clock=Clock(), ask_async=True, run=run)
    return talk, p, heard, gates, jobs


def speak(talk):
    for _ in range(round(1.0 / DT)):
        talk.frame(F, 0.95)
    for _ in range(round(1.0 / DT)):
        talk.frame(F, 0.05)


def test_a_late_answer_comes_after_the_newer_one_and_says_what_it_was_about():
    talk, p, heard, gates, jobs = talk_with({"what's the weather tomorrow": "Sunny, 31 degrees.",
                                             "open spotify": "Spotify's up."})
    heard += ["Hey Ari, what's the weather tomorrow", "open spotify"]
    speak(talk)  # asked; its answer is slow
    speak(talk)  # you carry on while Ari works: no waiting for the first
    gates["what's the weather tomorrow"].set()  # the older answer arrives first: it waits
    wait_for(lambda: talk._held)
    assert p.said == []
    gates["open spotify"].set()
    for j in jobs:
        j.join(5)
    assert p.said[0] == "Spotify's up."  # the newest question first
    assert p.said[1].startswith("Oh, and about") and "weather tomorrow" in p.said[1] and "Sunny" in p.said[1]


def test_a_late_answer_the_newer_question_replaced_is_dropped():
    talk, p, heard, gates, jobs = talk_with({"what's the weather tomorrow": "Sunny.",
                                             "what's the weather on sunday": "Rain on Sunday."})
    heard += ["Hey Ari, what's the weather tomorrow", "what's the weather on sunday"]
    speak(talk)
    speak(talk)
    gates["what's the weather on sunday"].set()
    gates["what's the weather tomorrow"].set()
    for j in jobs:
        j.join(5)
    assert p.said == ["Rain on Sunday."]


def test_stop_drops_answers_on_their_way_and_a_slow_one_says_still_on_it():
    talk, p, heard, gates, jobs = talk_with({"what's running": "Two jobs."})
    heard += ["Hey Ari, what's running"]
    speak(talk)
    talk.clock.t += 20
    talk.tick()
    assert p.said == ["Still on it."]
    talk.drop_pending()  # the island's Stop / Ari off
    gates["what's running"].set()
    for j in jobs:
        j.join(5)
    assert p.said == ["Still on it."]


def test_about_and_replaced():
    assert vl.about("Hey Ari, can you tell me the weather for tomorrow in Colombo please") == \
        "the weather for tomorrow in Colombo"
    assert vl.replaced("weather tomorrow", ["and the weather on sunday?"])
    assert not vl.replaced("open spotify", ["what's the weather"])


def test_small_talk_is_not_said_the_same_way_every_time(tmp_path):
    with FakeOllama({"qwen2.5-coder:7b": [{"reply": "Doing great, thanks!", "mood": "cheerful"}]}) as ol, \
            Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop", "session"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = cl.post("/ari", {"text": "how are you doing today"})
        settle(cl, w, r["conv"])
        temps = [q.get("options", {}).get("temperature") for q in ol.requests]
        assert temps and temps[0] > 0.5  # talk: varied; picking tools stays at 0


def test_ari_off_drops_pending_answers_and_turn_off_by_voice(tmp_path):
    with Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        r = cl.post("/ari", {"text": "write me a poem about rain"})
        out = cl.post("/ari/power", {"on": False})
        assert out["listening"] is False and out["dropped"] == 1
        assert cl.get(f"/jobs/{r['job_id']}")["state"] == "cancelled"
        evs = cl.get("/events?kinds=ari.stop,ari.listening,ari.state&limit=10")["events"]
        assert {e["kind"] for e in evs} >= {"ari.stop", "ari.listening"}
        assert cl.post("/ari/power", {"on": True})["listening"] is True
        said = cl.post("/ari", {"text": "Hey Ari, turn off"})
        assert "I'm off" in said["reply"] and cl.get("/ari/listening")["listening"] is False


def test_the_listener_waits_for_its_own_answer_not_the_last_one(tmp_path, monkeypatch):
    c = ari_listen.AriClient("http://x", None, tmp_path / "conv")
    calls = {"n": 0}

    def req(method, path, body=None, timeout=30):
        if method == "POST" and path == "/ari":
            return json.dumps({"conv": "c1", "turn": 7, "job_id": "J7", "reply": None}).encode()
        calls["n"] += 1
        turns = [{"id": 5, "role": "ari", "text": "The OLD answer.", "job_id": "J5"},
                 {"id": 6, "role": "you", "text": "new question", "job_id": None},
                 {"id": 7, "role": "ari", "text": "The new answer." if calls["n"] > 2 else None, "job_id": "J7"}]
        if calls["n"] == 2:  # a moment where an older answer is the last one with text
            turns = turns[:1]
        return json.dumps({"turns": turns}).encode()

    monkeypatch.setattr(c, "_req", req)
    monkeypatch.setattr(ari_listen.time, "sleep", lambda s: None)
    assert c.say("new question")["reply"] == "The new answer."


def test_the_pc_keeps_ollama_up():
    state = {"up": False, "started": 0, "t": 0.0}
    k = OllamaKeeper("http://127.0.0.1:11434", start=lambda: state.update(started=state["started"] + 1) or True,
                     up=lambda: state["up"], clock=lambda: state["t"])
    k.check()
    assert state["started"] == 0  # one miss: maybe just busy
    k.check()
    assert state["started"] == 1
    state["t"] = 60
    k.check()
    k.check()
    assert state["started"] == 1  # not again within 5 minutes
    state["up"] = True
    k.check()
    assert k.misses == 0
    assert not OllamaKeeper("http://pc.tail.ts.net:11434").local()  # not ours to start


def test_fillers_are_made_again_when_the_voice_changes():
    made: list[str] = []
    v = [0]
    f = vl.Fillers(lambda t: made.append(t) or ([0.0], 24000), texts=["Hmm."], version=lambda: v[0])
    wait_for(lambda: f.ready)
    v[0] = 1
    assert f.pick() is None  # the old voice's fillers are gone; new ones are being made
    wait_for(lambda: len(made) == 2 and f.ready)


def test_the_island_shows_ari_off():
    from argus.ari_popup import AriState

    a = AriState(phase="speaking", text="hi")
    assert a.apply([{"kind": "ari.listening", "data": {"listening": False}}], 1.0)
    assert a.off and a.phase == "idle"
    assert a.apply([{"kind": "ari.listening", "data": {"listening": True}}], 2.0) and not a.off
