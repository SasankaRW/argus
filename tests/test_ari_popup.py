"""The Ari pill and the PC's popup: Argus says what Ari is doing (event ari.state) from every surface."""

from __future__ import annotations

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


def test_shortcut_icons_follow_what_they_do():
    from argus.ari_popup import icon_for
    assert [icon_for(a) for a in ("show:inbox", "show:map", "routine:work mode", "run:downloads-organizer:sort",
                                  "power:sleep", "power:shutdown", "url:https://x.y", "phone:ring", "odd")] == \
        ["inbox", "grid", "spark", "bolt", "moon", "power", "globe", "bell", "dot"]


def test_after_answering_the_island_shows_it_still_listens():
    from argus.ari_popup import FOLLOW_S

    st = AriState()
    st.apply([{"kind": "ari.state", "data": {"phase": "done", "text": "It's 31 degrees."}}], 0.0)
    st.apply([{"kind": "ari.state", "data": {"phase": "following"}}], 1.0)
    assert st.phase == "following" and st.text == "It's 31 degrees."  # the answer stays on show
    assert not st.fold_due(1.0 + FOLLOW_S - 1) and st.fold_due(1.0 + FOLLOW_S + 1)


def test_talk_reports_the_follow_up_window_once_the_answer_is_spoken():
    import numpy as np

    from argus import voice_live as vl

    said = []

    class P:
        busy, text = False, ""

        def say(self, parts):
            pass

        def pause(self):
            pass

        def resume(self):
            pass

        def stop(self):
            pass

    now = [0.0]
    talk = vl.Talk(turns=vl.Turns(), transcribe=lambda a: "Hey Ari, what time is it", wake_rest=lambda t: "what time",
                   ask=lambda q: {"reply": "Half past three."}, player=P(), report=said.append,
                   clock=lambda: now[0])
    talk._turn(np.zeros(10, np.float32))
    talk.tick()
    talk.tick()
    assert said[-1] == "following" and said.count("following") == 1
    now[0] = 1000
    talk.tick()
    assert said[-1] == "idle"


def test_the_stop_button_quietens_ari_and_drops_the_answer(tmp_path, monkeypatch):
    from argus import ari_listen
    from argus.ari_listen import AriClient
    from argus.ari_popup import STOPPABLE

    assert "speaking" in STOPPABLE and "thinking" in STOPPABLE and "listening" not in STOPPABLE
    with Server(make(tmp_path, monkeypatch).open()) as srv:
        cl = client(srv.url)
        r = cl.post("/ari/stop", {"job": ""})
        assert r == {"ok": True, "cancelled": False}
        assert [e["kind"] for e in cl.get("/events?kinds=ari.stop")["events"]] == ["ari.stop"]
        assert states(cl)[-1][0] == "idle"  # the island and the pill go quiet too
        # a job being worked on is cancelled
        job = cl.post("/ari", {"text": "what is a mutex in one line"})["job_id"]
        assert cl.post("/ari/stop", {"job": job})["cancelled"] is True
        assert cl.get(f"/jobs/{job}")["state"] == "cancelled"

    # the listener: an answer still being waited for is dropped when Stop was pressed meanwhile
    client_ = AriClient("http://127.0.0.1:9", None, tmp_path / "conv")
    calls = []

    def fake_req(method, path, body=None, timeout=30):
        calls.append(path)
        if method == "POST":
            return b'{"conv": "c1", "job_id": "j1", "reply": null}'
        ari_listen.STOPPED_AT[0] = ari_listen.time.monotonic() + 1  # pressed while waiting
        return b'{"events": [], "turns": []}'

    client_._req = fake_req  # type: ignore[method-assign]
    out = client_.say("explain mutex")
    assert out == {"reply": "", "stopped": True}


def test_the_island_flips_pages_and_says_whats_on_hover():
    from argus.ari_popup import hover_label, pages_of

    widgets = [{"id": "clock", "on": True}, {"id": "calendar", "on": True}, {"id": "media", "on": True},
               {"id": "system", "on": False}, {"id": "privacy", "on": True}]
    assert pages_of(widgets) == ["ari", "calendar", "media"]  # Ari's first, then in the order set in Helios
    song = {"title": "Midnight City", "artist": "M83", "playing": True}
    assert hover_label(False, False, {}, song) == "♪ Midnight City — M83"
    assert hover_label(False, False, {}, {**song, "playing": False}) == "a r i"  # paused: nothing to say
    assert hover_label(False, False, {"mic": ["Discord"]}, song) == "mic: Discord"  # who listens comes first
    assert hover_label(True, True, {"mic": ["Discord"]}, song) == "a r i · game mode"
    w, h = target_size("idle", "", True, 0, "♪ Midnight City — M83")
    assert h == 22 and w > 138  # wide enough for the song
    assert target_size("idle", "", True, 0) == (138, 22)


def test_the_island_keeps_its_keys(tmp_path):
    import sqlite3

    import pytest

    from argus import island

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, created_at REAL, updated_at REAL)")
    cfg = {**island.load(conn), "shortcuts": []}
    assert cfg["keys"] == {"ask": "ctrl+alt+space", "hide": "ctrl+alt+d"}
    assert {"media", "system", "calendar", "privacy"} <= {w["id"] for w in cfg["widgets"]}
    saved = island.check({**cfg, "keys": {"ask": "Ctrl + Shift + A", "hide": ""}}, set())
    assert saved["keys"] == {"ask": "ctrl+shift+a", "hide": ""}  # "" turns that key off
    island.save(conn, saved)
    assert island.load(conn)["keys"] == saved["keys"]
    with pytest.raises(island.IslandError):
        island.check({**cfg, "keys": {"ask": "space"}}, set())  # a global key needs ctrl / alt / shift / win
    # an older island setting (no keys, no new widgets) gets them
    conn.execute("UPDATE settings SET value = ? WHERE key = 'island'",
                 ('{"widgets": [{"id": "clock", "on": true}], "shortcuts": []}',))
    old = island.load(conn)
    assert old["keys"]["ask"] == "ctrl+alt+space" and any(w["id"] == "media" for w in old["widgets"])


def test_helios_keeps_the_island_keys_and_the_weather_page(tmp_path, monkeypatch):
    with Server(make(tmp_path, monkeypatch).open()) as srv:
        cl = client(srv.url)
        cfg = cl.get("/island")
        body = {"widgets": cfg["widgets"], "shortcuts": cfg["shortcuts"]}
        cl.call("PUT", "/island", {**body, "keys": {"ask": "ctrl+alt+k", "hide": "ctrl+alt+d"}})
        assert cl.call("PUT", "/island", body)[1]["keys"]["ask"] == "ctrl+alt+k"  # a Helios that sends no keys
        assert cl.get("/island/weather") == {}  # no town set: the calendar page goes without
