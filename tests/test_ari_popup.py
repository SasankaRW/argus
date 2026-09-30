"""The Ari pill and the PC's popup: Argus says what Ari is doing (event ari.state) from every surface."""

from __future__ import annotations

from argus.ari_listen import Listener
from argus.ari_popup import click_area, parse_title, placement, popup_url
from argus.worker import Worker
from fakes import FakeOllama
from test_ask import make
from test_worker import Server, client, wait_for


def states(cl) -> list[tuple[str, str]]:
    evs = cl.get("/events?kinds=ari.state&limit=100")["events"]
    return [(e["data"]["phase"], e["data"]["text"]) for e in evs]


def test_the_popup_window_helpers():
    assert popup_url("http://127.0.0.1:8600/", "a b") == "http://127.0.0.1:8600/helios/popup.html?token=a+b"
    assert popup_url("http://x", None) == "http://x/helios/popup.html"
    assert placement(0, 0, 1920) == (640, 0, 640, 360)
    assert parse_title("ari:active|352x54") == ("area", (352, 54))
    assert parse_title("ari:details|9999x9999") == ("area", (640, 360))  # never bigger than the window
    assert parse_title("ari:go|ari/abc123") == ("go", "ari/abc123") and parse_title("ari:go|") == ("go", "")
    assert [parse_title(t) for t in ("Helios", "ari:idle", "ari:x|bad")] == [None, None, None]
    assert click_area(352, 54) == (144, 0, 352, 54)


def test_ari_says_what_it_is_doing(tmp_path, monkeypatch):
    replies = {"qwen2.5-coder:7b": [{"reply": "I'm Ari, your assistant."}]}
    with FakeOllama(replies) as ol, Server(make(tmp_path, monkeypatch).open()) as srv:
        cl = client(srv.url)
        cl.post("/ari/state", {"phase": "listening", "by": "pc"})
        r = cl.post("/ari", {"text": "remember that the router password is on the fridge"})
        assert states(cl) == [("listening", ""), ("done", "Got it, I'll remember that.")]
        # a model's answer: thinking (with the job), then done with the reply when the job ends
        m = cl.post("/ari", {"text": "who are you?", "conv": r["conv"]})
        ev = cl.get("/events?kinds=ari.state&limit=100")["events"][-1]
        assert ev["data"]["phase"] == "thinking" and ev["job_id"] == m["job_id"]
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        assert w.run_once(wait=2)
        wait_for(lambda: states(cl)[-1][0] == "done")
        assert states(cl)[-1] == ("done", "I'm Ari, your assistant.")


def test_helios_leaves_the_pill_to_the_popup_on_its_pc(tmp_path, monkeypatch):
    with Server(make(tmp_path, monkeypatch).open()) as srv:
        cl = client(srv.url)
        assert cl.get("/ari-voice")["popup_here"] is False
        cl.post("/ari/popup")
        assert cl.get("/ari-voice")["popup_here"] is True


def test_hey_ari_on_the_pc_reports_listening():
    told: list[str] = []
    said = iter(["Hey Ari", "sort my downloads"])
    lis = Listener(transcribe_wake=lambda a: next(said), transcribe=lambda a: next(said),
                   say=lambda text: {"reply": "Shall I?", "pending": {"kind": "action"}}, speak=lambda t: None,
                   report=told.append, clock=lambda: 0.0)
    import numpy as np

    clip = [np.zeros(480, dtype=np.float32)] * 30
    lis.clip(clip)  # the wake phrase alone: a chime, then listening
    lis.clip(clip)  # the command; Ari asks "Shall I?": listening for the answer
    assert told == ["listening", "listening"]
