"""Ari P5: it adapts to the situation (small talk, a task, bad news, you in a call or a game), keeps how you want it
to talk after your yes, searches its memory and past chats, and its own tool picking learns like a plugin."""

from __future__ import annotations

import time
from types import SimpleNamespace

from argus import ari as ari_mod
from argus import ari_listen
from argus import busy as busy_mod
from argus.worker import Worker
from argus.worker import think as th
from fakes import FakeOllama
from test_ari_p1 import Player, speak
from test_ari_think import make, settle
from test_voice_live import Clock, wake_rest
from test_worker import Server, client

# ---------------------------------------------------------------- in a call or a game


def test_a_call_or_a_game_is_noticed():
    assert busy_mod.classify(["MSTeams_8wekyb3d8bbwe"], None) == "call"
    assert busy_mod.classify(["C:#Program Files#Zoom#bin#Zoom.exe"], None) == "call"
    assert busy_mod.classify(["C:#Users#sas#argus#.venv#Scripts#python.exe"], None) is None  # Ari's own ears
    # apps that hold the mic all day are not calls (that kept Ari quiet all day)
    for always_on in ("C:#Program Files#Google#Chrome#Application#chrome.exe", "NVIDIA Broadcast", "obs64.exe"):
        assert busy_mod.classify([always_on], None) is None, always_on
    assert busy_mod.classify(["C:#Apps#MyCaller.exe"], None, ["mycaller"]) == "call"  # ari.call_apps
    assert busy_mod.detail(["Discord.exe"], None) == ("call", "discord")
    full = {"fullscreen": True}
    assert busy_mod.classify([], {**full, "exe": r"C:\Program Files\Google\Chrome\Application\chrome.exe"}) is None
    assert busy_mod.classify([], {**full, "exe": r"D:\SteamLibrary\steamapps\common\Hades\Hades.exe"}) == "game"
    assert busy_mod.classify([], {**full, "exe": r"C:\Games\Valorant\VALORANT.exe"}) == "game"
    assert busy_mod.classify([], {"fullscreen": False, "exe": r"C:\Games\x.exe"}) is None


def test_quiet_in_calls_can_be_turned_off(monkeypatch):
    told: list = []
    monkeypatch.setitem(ari_listen.MIC, "busy", "call")
    ari_listen._BUSY_AT[0] = 0.0
    cfg = SimpleNamespace(ari=SimpleNamespace(quiet_in_calls=False, call_apps=[]))
    ari_listen.check_busy(SimpleNamespace(here=lambda: told.append(1)), every=0, look=lambda: "call", cfg=cfg)
    assert ari_listen.MIC["busy"] is None and told == [1]


def test_the_listener_tells_argus_at_once_when_you_join_a_call(monkeypatch):
    told: list[dict] = []

    class Client:
        def here(self):
            told.append(dict(ari_listen.MIC))

    monkeypatch.setitem(ari_listen.MIC, "busy", None)
    ari_listen._BUSY_AT[0] = 0.0
    ari_listen.check_busy(Client(), every=0, look=lambda: "call")
    ari_listen.check_busy(Client(), every=0, look=lambda: "call")  # no change: not told again
    assert [t["busy"] for t in told] == ["call"]
    ari_listen.check_busy(Client(), every=0, look=lambda: None)
    assert [t["busy"] for t in told] == ["call", None]


def test_in_a_call_the_answer_is_shown_not_spoken():
    shown: list[tuple[str, str]] = []
    p = Player()
    heard = ["Hey Ari, what's the time"]
    talk = th_talk(p, heard, shown, quiet=True)
    speak(talk)
    assert p.said == [] and ("done", "It's five.") in shown


def th_talk(p, heard, shown, quiet):
    from argus import voice_live as vl

    return vl.Talk(turns=vl.Turns(), transcribe=lambda a: heard.pop(0), wake_rest=wake_rest,
                   ask=lambda q: {"reply": "It's five."}, player=p, clock=Clock(),
                   show=lambda phase, text: shown.append((phase, text)), quiet=lambda: quiet)


# ---------------------------------------------------------------- how to sound


def test_the_situation_sets_the_style():
    assert th.situation("how are you", None) == "small_talk"
    assert th.situation("open spotify", None) == "task"
    assert th.situation("how are you", "game") == "busy"
    assert th.situation("is the backup ok", None, [{"tool": "backup_status", "result": {"state": "failed"}}]) == \
        "bad_news"
    assert th.situation("open x", None, [{"tool": "open_app", "error": "not found"}]) == "bad_news"
    assert "bad_news" in th.PLAYBOOK and "busy" in th.CHAT


def test_your_preferences_win_over_the_personality():
    who = th.persona({"their_preferences": ["keep it shorter", "don't joke when I'm working"]})
    assert who.index("Ari's personality") < who.index("How they asked you to talk")
    assert "- don't joke when I'm working" in who


def test_how_you_want_ari_to_talk_is_kept_after_your_yes(tmp_path):
    with FakeOllama({"qwen2.5-coder:7b": [{"tool": "", "reply": "Opening it.", "mood": "neutral"}]}) as ol, \
            Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        r = cl.post("/ari", {"text": "Ari, keep it shorter"})
        assert r["pending"] == {"kind": "remember", "fact": "How to talk to them: keep it shorter"}
        assert "keep it shorter" in r["reply"]
        cl.post("/ari", {"text": "yes", "conv": r["conv"]})
        assert any(m["fact"].endswith("keep it shorter") for m in cl.get("/ari-memory"))
        cl.post("/ari/listener", {"busy": "call"})
        w = Worker(cl, "pc", capabilities=["desktop", "session"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r2 = cl.post("/ari", {"text": "find the invoice in my documents folder"})
        job = cl.get(f"/jobs/{r2['job_id']}")
        assert job["input"]["their_preferences"] == ["keep it shorter"] and job["input"]["busy"] == "call"
        settle(cl, w, r2["conv"])
        system = ol.requests[0]["messages"][0]["content"]
        assert "How they asked you to talk" in system and "- keep it shorter" in system
        assert '"situation": "busy"' in ol.requests[0]["messages"][-1]["content"]


def test_preferences_are_recognised():
    assert ari_mod.preference_of("call me Sas") == "How to talk to them: call me Sas"
    assert ari_mod.preference_of("please don't joke when I'm working").endswith("don't joke when I'm working")
    for not_one in ("call me at 5", "call me back later", "be quiet", "don't forget the milk", "keep going"):
        assert ari_mod.preference_of(not_one) is None, not_one


# ---------------------------------------------------------------- search my memory


def test_search_my_memory_finds_facts_and_past_chats(tmp_path):
    from test_learning_p4 import _db

    c = _db(tmp_path)
    ari_mod.remember(c, "the router admin page is 192.168.1.1")
    ari_mod.add_turn(c, "c1", "you", "what's the weather like on the router roof")
    ari_mod.add_turn(c, "c1", "ari", "Sunny up there.")
    res = ari_mod.search_memory(c, "router", time.time())
    assert [f["fact"] for f in res["remembered"]] == ["the router admin page is 192.168.1.1"]
    assert res["said_before"][0]["who"] == "you" and "router roof" in res["said_before"][0]["said"]
    assert ari_mod.search_memory(c, "router", time.time() + 400 * 86400)["said_before"] == []  # a year back at most


# ---------------------------------------------------------------- Ari's tool picking learns


def test_aris_first_pick_is_a_sample_your_correction_marks_it(tmp_path):
    replies = {"qwen2.5-coder:7b": [{"tool": "shout", "args": {"text": "jazz"}},
                                    {"tool": "", "reply": "Shouted JAZZ.", "mood": "neutral"}]}
    with FakeOllama(replies) as ol, Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop", "session"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = cl.post("/ari", {"text": "put on some jazz music for me"})
        settle(cl, w, r["conv"])
        s = cl.get(f"/jobs/{r['job_id']}/samples")
        assert len(s) == 1 and s[0]["plugin"] == "ari"  # the first pick only
        assert s[0]["output"] == {"tool": "shout", "args": {"text": "jazz"}, "need_web": False}
        assert s[0]["input"]["message"] == "put on some jazz music for me"
        cl.post("/ari", {"text": "no, I meant play it on Spotify", "conv": r["conv"]})
        s = cl.get(f"/jobs/{r['job_id']}/samples")[0]
        assert (s["verdict"], s["feedback"]) == ("wrong", "ari") and "Spotify" in s["correction"]
        assert cl.get("/guidance?plugin=ari")[0]["wrong"] == 1
        # a pick you mark Correct becomes an ari_eval case
        cl.post(f"/samples/{s['id']}/verdict", {"verdict": "correct"})
        assert cl.get("/guidance/ari-cases") == [{"say": "put on some jazz music for me", "expect": "shout"}]


def test_aris_lessons_reach_its_tool_picking(tmp_path):
    with FakeOllama({"qwen2.5-coder:7b": [{"tool": "", "reply": "Done.", "mood": "neutral"}]}) as ol, \
            Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop", "session"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = cl.post("/ari", {"text": "put on some jazz music for me"})
        settle(cl, w, r["conv"])
        key = cl.get(f"/jobs/{r['job_id']}/samples")[0]["playbook"]
        _approve_lesson(tmp_path, key, "- Music means Spotify: use media_control or open_app spotify.")
        r2 = cl.post("/ari", {"text": "play some jazz now please"})
        settle(cl, w, r2["conv"])
        system = ol.requests[-1]["messages"][0]["content"]
        assert "Lessons from earlier mistakes" in system and "Music means Spotify" in system


def _approve_lesson(tmp_path, key: str, text: str) -> None:
    import sqlite3

    from argus import guidance

    c = sqlite3.connect(tmp_path / "data" / "argus.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    lid = guidance.propose(c, time.time(), key, text, {}, None)
    guidance.decide(c, lid, True, time.time())
    c.close()


def test_a_voice_hiccup_is_retried_not_swapped_for_a_second_voice(monkeypatch, tmp_path):
    import io
    import urllib.request

    from argus.voice import Expressive

    calls = []

    def urlopen(req, timeout=None):
        calls.append(timeout)
        if len(calls) == 2:  # the second sentence times out once
            raise TimeoutError("timed out")
        return io.BytesIO(b"WAV")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    e = Expressive("http://127.0.0.1:9", None, SimpleNamespace())
    assert e.say("one.") == b"WAV" and e.recent()
    assert e.say("two.") is None and e.say("two.") is None  # down for a minute after a failure ...
    assert e.say("two.", timeout=90, force=True) == b"WAV" and calls[-1] == 90  # ... unless retried on purpose
