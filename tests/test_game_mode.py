"""Game mode: the PC's GPU for a game (the island's game key, Helios, or by itself while a game is open)."""

from __future__ import annotations

from pathlib import Path

from argus import ask
from argus.config import load_config
from argus.context import Argus
from argus.game import GameWatch
from argus.supervisor import Child
from test_worker import Server, client


class FakeChild(Child):
    def __init__(self, name):
        super().__init__(name, [])
        self.stopped = 0

    def stop(self, timeout: float = 15) -> None:
        self.stopped += 1


class Argusd:
    """/ari/game as the supervisor sees it."""

    def __init__(self):
        self.state = {"on": False, "auto": True, "by": None}
        self.posts: list[dict] = []

    def get(self):
        return dict(self.state)

    def post(self, body):
        self.posts.append(body)
        if "on" in body:
            if body["on"] or body.get("by") != "auto" or self.state["by"] == "auto":
                self.state.update(on=body["on"], by=body.get("by", "you"))


def watch(argusd, front, t, unloaded):
    kids = [FakeChild("ari-listen"), FakeChild("ari-voice"), FakeChild("ari-popup")]
    w = GameWatch(argusd.get, argusd.post, kids, look=lambda: front[0], unload=lambda: unloaded or ["qwen3:8b"],
                  vram=lambda: 9000 if not w.freed else 2000, on_after=20, off_after=60,
                  clock=lambda: t[0], sleep=lambda s: None)
    return w, kids


def test_a_game_turns_it_on_after_a_while_and_off_after_it_closes():
    a, front, t = Argusd(), [("game", "hades")], [0.0]
    w, kids = watch(a, front, t, [])
    w.tick()
    assert not a.state["on"]  # not yet: Alt-Tab into a game for a moment doesn't count
    t[0] = 21
    w.tick()
    assert a.state == {"on": True, "auto": True, "by": "auto"} and a.posts[0]["app"] == "hades"
    listen, voice, popup = kids
    assert listen.held and voice.held and listen.stopped == 1 and not popup.held  # the island stays
    assert a.posts[-1] == {"freed_mb": 7000, "unloaded": ["qwen3:8b"], "still": [], "used_mb": 2000}
    front[0], t[0] = (None, ""), 30
    w.tick()
    assert a.state["on"]  # just closed: wait
    t[0] = 91
    w.tick()
    assert not a.state["on"] and not listen.held and not voice.held


def test_turned_on_by_hand_it_stays_on_without_a_game():
    a, front, t = Argusd(), [(None, "")], [0.0]
    a.state.update(on=True, by="you")
    w, kids = watch(a, front, t, [])
    w.tick()
    assert kids[0].held  # freed at once
    t[0] = 500
    w.tick()
    assert a.state["on"] and kids[0].held
    a.state.update(on=False, by="you")  # off with the island's key
    w.tick()
    assert not kids[0].held


def test_auto_off_means_no_watching():
    a, front, t = Argusd(), [("game", "hades")], [0.0]
    a.state["auto"] = False
    w, _ = watch(a, front, t, [])
    t[0] = 100
    w.tick()
    w.tick()
    assert not a.state["on"] and a.posts == []


def test_a_held_child_is_not_started_again():
    c = Child("ari-voice", ["-c", "pass"])
    c.held = True
    c.check()
    assert c.proc is None


def make(tmp_path: Path) -> Argus:
    (tmp_path / "argus.yaml").write_text("logging:\n  file: null\n", encoding="utf-8")
    return Argus(load_config(tmp_path / "argus.yaml"))


def test_game_mode_on_argusd(tmp_path):
    a = make(tmp_path)
    with Server(a.open()) as srv:
        cl = client(srv.url)
        assert cl.get("/ari/game")["on"] is False
        st = cl.post("/ari/game", {"on": True, "by": "you"})
        assert st["on"] and st["by"] == "you"
        assert cl.get("/ari/listening")["listening"] is False  # Ari is off
        local = next(t for t, c in a.cfg.models.tiers.items() if c.provider == "ollama")
        p = cl.post(f"/models/{local}/permit", {"worker": "w", "job_id": None})
        assert p["allowed"] is False and "game mode" in p["reason"]
        assert cl.post("/ari/game", {"on": False, "by": "auto"})["on"] is True  # auto never ends yours
        cl.post("/ari/game", {"freed_mb": 6500, "unloaded": ["qwen3:8b"]})
        assert cl.get("/ari/game")["freed_mb"] == 6500
        st = cl.post("/ari/game", {"on": False, "by": "you"})
        assert not st["on"] and cl.get("/ari/listening")["listening"] is True
        assert cl.post(f"/models/{local}/permit", {"worker": "w", "job_id": None})["allowed"] is True
        assert cl.post("/ari/game", {"auto": False})["auto"] is False  # kept as a setting
        assert a.cfg.ari.game_mode_auto is False
        assert cl.post("/ask/do", {"action": "game:on"})["game"]["on"] is True


def test_ari_was_already_off_stays_off_after_game_mode(tmp_path):
    with Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        cl.post("/ari/power", {"on": False})
        cl.post("/ari/game", {"on": True})
        cl.post("/ari/game", {"on": False})
        assert cl.get("/ari/listening")["listening"] is False


def test_asking_for_game_mode():
    assert ask.rules("game mode on", [], {})["action"] == "game:on"
    assert ask.rules("I'm gaming", [], {})["action"] == "game:on"
    assert ask.rules("turn off game mode", [], {})["action"] == "game:off"


def test_full_gpu_free_ollama_stopped_kept_stopped_and_started_again():
    """Game mode stops Ollama itself (not only its models), stops it again if it comes back, says what else still
    uses the GPU, and starts Ollama again when it ends."""
    a, t = Argusd(), [0.0]
    a.state.update(on=True, by="you")
    calls = {"stop": 0, "start": 0}
    back = [True]

    def stop():
        calls["stop"] += 1
        return ["ollama.exe"] if back[0] else []

    def start():
        calls["start"] += 1
        return "task"

    kids = [FakeChild("ari-listen"), FakeChild("ari-voice")]
    w = GameWatch(a.get, a.post, kids, look=lambda: (None, ""), unload=lambda: ["qwen3:8b"],
                  vram=lambda: 1800 if w.freed else 9000, clock=lambda: t[0], sleep=lambda s: None,
                  stop_ollama=stop, start_ollama=start,
                  users=lambda: [{"pid": 1, "name": "Cyberpunk2077.exe", "mb": None},
                                 {"pid": 2, "name": "chrome.exe", "mb": 300}], keep_every=30)
    w.tick()
    assert calls["stop"] == 1 and a.posts[-1]["freed_mb"] == 7200
    assert a.posts[-1]["still"] == ["Cyberpunk2077.exe", "chrome.exe 300 MB"]
    t[0] = 10
    w.tick()
    assert calls["stop"] == 1  # not every tick
    t[0] = 40
    w.tick()
    assert calls["stop"] == 2 and "still" in a.posts[-1]  # it had come back: stopped again, and said so
    a.state.update(on=False, by="you")
    w.tick()
    assert calls["start"] == 1 and not kids[0].held


def test_a_child_asking_for_a_restart_starts_at_once():
    import sys

    c = Child("worker", ["-c", "import sys; sys.exit(75)"])
    c.start()
    c.proc.wait(10)
    c.check()  # exited with 75: started again now, not after a wait, and not counted as a failure
    assert c.proc is not None and c.fails == 0
    c.proc.wait(10)
    c.stop()
    assert sys.executable


def test_the_ollama_keeper_leaves_ollama_stopped_in_game_mode():
    from argus.supervisor import OllamaKeeper

    started = []
    k = OllamaKeeper("http://127.0.0.1:11434", start=lambda: started.append(1) or True, up=lambda: False,
                     paused=lambda: True)
    for _ in range(3):
        k.check()
    assert started == []
    k.paused = lambda: False
    k.check()
    k.check()
    assert started == [1]


def test_the_worker_gives_whisper_back_in_game_mode(monkeypatch):
    from argus.worker import hear
    from argus.worker.runner import Worker

    class C:
        def __init__(self, on):
            self.on = on

        def get(self, path, **kw):
            assert path == "/ari/game"
            return {"on": self.on}

    w = Worker.__new__(Worker)
    w.id, w._game_checked = "worker-pc", 0.0
    exits = []
    w.exit_now = exits.append
    monkeypatch.setattr(hear, "_on_gpu", {"small.en"})
    monkeypatch.setitem(hear._models, "small.en", object())
    w.client = C(False)
    w.check_game(force=True)
    assert exits == [] and hear.GAME["on"] is False
    w.client = C(True)
    w.check_game(force=True)
    assert exits == [75] and hear.GAME["on"] is True and "small.en" not in hear._models
    hear.GAME["on"] = False


def test_the_supervisor_talks_to_argus_where_argus_url_says(monkeypatch):
    """The PC's supervisors run with ARGUS_URL set to the laptop; game mode asked 127.0.0.1 and was never told."""
    import argparse

    from argus import supervisor

    monkeypatch.setenv("ARGUS_URL", "https://laptop.example.ts.net")
    seen = {}

    def fake_parse(self, argv=None):
        seen["url"] = next(a.default for a in self._actions if a.dest == "url")
        raise SystemExit(0)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", fake_parse)
    try:
        supervisor.main([])
    except SystemExit:
        pass
    assert seen["url"] == "https://laptop.example.ts.net"


def test_game_watch_says_when_argus_is_out_of_reach(caplog):
    t = [0.0]
    w = GameWatch(lambda: None, lambda b: None, [], look=lambda: (None, ""), unload=lambda: [],
                  clock=lambda: t[0], sleep=lambda s: None)
    with caplog.at_level("WARNING", logger="argus.game"):
        w.tick()
        t[0] = 60.0
        w.tick()  # the first minutes after the PC starts (Tailscale coming up): quiet
        assert not any("can't reach Argus" in r.message for r in caplog.records)
        t[0] = 200.0
        w.tick()
        t[0] = 260.0
        w.tick()
    assert sum("can't reach Argus" in r.message for r in caplog.records) == 1  # once it lasts, and not every tick
