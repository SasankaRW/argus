"""The Ari pill and the PC's popup: Argus says what Ari is doing (event ari.state) from every surface."""

from __future__ import annotations

from argus.ari_listen import Listener
from argus.ari_popup import (
    AriState,
    Spring,
    W,
    next_schedule,
    open_target,
    outline,
    placement,
    shoulder_of,
    target_size,
)
from argus.worker import Worker
from fakes import FakeOllama
from test_ask import make
from test_worker import Server, client, wait_for


def states(cl) -> list[tuple[str, str]]:
    evs = cl.get("/events?kinds=ari.state&limit=100")["events"]
    return [(e["data"]["phase"], e["data"]["text"]) for e in evs]


def test_the_island_helpers():
    assert placement(0, 0, 1920)[1] == 0 and placement(0, 0, 1920)[2] == W  # centred, touching the top edge
    assert placement(0, 0, 1920)[0] == (1920 - W) // 2
    # the outline starts and ends at the screen's edge, and spans the shoulders
    pts = outline(100, 36, False)
    assert pts[0] == ("M", 0, 0) and pts[1][1] == 100 + 2 * shoulder_of(36) and pts[-1][-2:] == (0, 0)
    sp = Spring(96, 8)
    frames = 0
    while sp.step(300, 36, 1 / 60):
        frames += 1
    assert (sp.w, sp.h) == (300, 36) and 10 < frames < 120  # settles in well under two seconds
    st = AriState()
    assert st.apply([{"kind": "ari.state", "job_id": "j1", "data": {"phase": "thinking", "text": "hi"}}], 0.0)
    st.apply([{"kind": "step.running", "job_id": "j1", "step": "tool 1: search_my_files", "data": {}}], 1.0)
    assert (st.phase, st.text) == ("working", "searching your files")
    assert not st.apply([{"kind": "step.running", "job_id": "other", "step": "tool 1: x", "data": {}}], 2.0)
    st.apply([{"kind": "ari.state", "job_id": "j1", "data": {"phase": "done", "text": "Done."}}], 3.0)
    assert not st.fold_due(4.0) and st.fold_due(10.0)
    assert target_size("idle", "", False, 0) == (84, 10) and target_size("details", "", False, 999)[1] == 300
    assert next_schedule([{"enabled": True, "next_run_at": 9}, {"enabled": True, "next_run_at": 5},
                          {"enabled": False, "next_run_at": 1}])["next_run_at"] == 5
    assert open_target("http://x:8600", "show:inbox") == "http://x:8600/helios/#inbox"
    assert open_target("http://x:8600", "show:map") == "http://x:8600/helios/"
    assert open_target("http://x", "url:https://github.com") == "https://github.com"
    assert open_target("http://x", "url:file:///c:/windows") is None
    assert open_target("http://x", "run:downloads-organizer:sort") is None  # Argus runs it


def test_the_talk_button_wakes_the_pcs_listener(tmp_path, monkeypatch):
    with Server(make(tmp_path, monkeypatch).open()) as srv:
        cl = client(srv.url)
        assert cl.post("/ari/wake") == {"listener": False}  # nobody listening: the island uses Helios's mic
        cl.post("/ari/listener")
        assert cl.post("/ari/wake") == {"listener": True}
        assert [e["kind"] for e in cl.get("/events?kinds=ari.wake")["events"]] == ["ari.wake"]
    told: list[str] = []
    lis = Listener(transcribe_wake=lambda a: "", transcribe=lambda a: "", say=lambda t: {}, speak=lambda t: None,
                   report=told.append, clock=lambda: 100.0)
    lis.wake()
    assert told == ["listening"] and lis.armed_until > 100.0


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


def test_shortcut_icons_follow_what_they_do():
    from argus.ari_popup import icon_for
    assert [icon_for(a) for a in ("show:inbox", "show:map", "routine:work mode", "run:downloads-organizer:sort",
                                  "power:sleep", "power:shutdown", "url:https://x.y", "phone:ring", "odd")] == \
        ["inbox", "grid", "spark", "bolt", "moon", "power", "globe", "bell", "dot"]
