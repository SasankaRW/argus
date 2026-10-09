"""The terminal console (python -m argus.console): each view renders from argusd's answers, the eye follows Ari,
and the tmux layout puts the five views where they belong. A fake argusd; no network, no real terminal."""

from __future__ import annotations

import io
import time

import pytest

from argus import console as con

pytest.importorskip("rich")
from rich.console import Console  # noqa: E402


class FakeApi:
    """The answers argusd gives to the console, by path."""

    up = True
    url = "http://127.0.0.1:8600"

    def __init__(self, down: bool = False):
        self.down = down
        self.up = not down
        now = time.time()
        self.answers = {
            "/status": {"status": "ok", "version": "1.2.3", "instance": "calypso", "uptime_seconds": 7300,
                        "jobs": {"running": 2, "queued": 1, "dead": 1},
                        "workers": [{"id": "pc", "host": "sas-pc", "state": "online"},
                                    {"id": "laptop", "host": "calypso", "state": "offline"}]},
            "/power": {"pc_online": True, "pc": {"host": "sas-pc", "state": "online"}},
            "/models/ollama": {"models": [{"name": "qwen2.5-coder:7b"}, {"name": "qwen2.5vl:7b"}]},
            "/ari/listener": {"listener": True},
            "/approvals?state=pending": [{"title": "Pay the electricity bill"}],
            "/schedules": [{"enabled": True, "plugin": "backup", "workflow": "run", "next_run_at": now + 3600}],
            "/ari-chats": [{"conv": "c1"}],
            "/ari/c1": {"turns": [{"role": "you", "text": "what is running", "created_at": now},
                                  {"role": "ari", "text": "Two jobs, nothing stuck.", "created_at": now}]},
            "/logs": [{"name": "argusd"}, {"name": "ollama"}, {"name": "worker-crash"}],
            "/logs/argusd?lines=40": {"offset": 10, "entries": [
                {"source": "argusd", "ts": "2026-10-08T12:00:01Z", "level": "warning", "logger": "argus.voice",
                 "msg": "ari's voice unavailable, showing text", "extra": {"error": "timed out"}}]},
        }

    def get(self, path: str, timeout: float = 5):
        if self.down:
            raise OSError("down")
        if path.startswith("/events"):
            time.sleep(0.05)  # a long poll that found nothing new
            ev = {"seq": 1, "kind": "job.succeeded", "from_component": "backup", "step": "run", "at": time.time(),
                  "data": {"job_id": "j1", "files": 3}}
            return {"events": [ev] if "newest=true" in path else [], "seq": 1}
        if path.startswith("/logs/argusd?after"):
            return {"offset": 10, "entries": []}
        if path in self.answers:
            return self.answers[path]
        raise OSError("no such path")


def text_of(renderable, width: int = 120) -> str:
    c = Console(file=io.StringIO(), record=True, width=width, force_terminal=True, color_system="truecolor")
    c.print(renderable)
    return c.export_text()


def wait_for(fn, seconds: float = 3.0):
    end = time.time() + seconds
    while time.time() < end:
        got = fn()
        if got:
            return got
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_the_eye_changes_with_what_ari_does():
    frames = {p: con.eye(p, 1.0) for p in ("idle", "listening", "thinking", "speaking", "off", "done")}
    assert all(len(f) == 6 for f in frames.values())
    assert frames["off"] != frames["idle"] and frames["thinking"] != frames["listening"]
    assert con.eye("idle", 0.0) != con.eye("idle", 2.5)  # the pupil looks around
    assert con.PHASE_COLOUR["listening"] != con.PHASE_COLOUR["thinking"]


def test_small_helpers():
    assert con.ago(45) == "45s" and con.ago(7300).startswith("2h")
    assert len(con.spark([0, 50, 100])) == 3 and con.spark([]) == ""


def test_the_deck_shows_the_logo_and_how_everything_is():
    deck = con.Deck(FakeApi())
    wait_for(lambda: deck.data.get("status") and deck.data.get("models"))
    out = text_of(deck.render(0.5))
    assert "█████╗" in out  # the wordmark
    for want in ("calypso", "2 running", "1 dead", "pc", "qwen2.5-coder:7b", "Hey Ari", "Pay the electricity bill",
                 "backup·run"):
        assert want in out, want
    assert "sas-pc" in out


def test_the_deck_adapts_to_a_narrow_pane():
    deck = con.Deck(FakeApi())
    wait_for(lambda: deck.data.get("status"))
    wide = text_of(deck.render(0.5, 120), 120)
    narrow = text_of(deck.render(0.5, 40), 40)
    assert "█████╗" in wide and "ARGUS" in narrow and "█████╗" not in narrow


def test_the_deck_says_so_when_argusd_is_away():
    deck = con.Deck(FakeApi(down=True))
    time.sleep(0.2)
    assert "isn't answering" in text_of(deck.render(0.0))


def test_the_ari_view_follows_the_conversation():
    v = con.AriView(FakeApi())
    wait_for(lambda: v.turns)
    out = text_of(v.render(0.0, 30))
    assert "what is running" in out and "Two jobs, nothing stuck." in out and "ARI" in out


def test_the_events_and_logs_views():
    ev = con.EventsView(FakeApi())
    wait_for(lambda: ev.rows)
    out = text_of(ev.render(0.0, 20))
    assert "job.succeeded" in out and "backup" in out and "files=3" in out
    lg = con.LogsView(FakeApi())
    wait_for(lambda: lg.rows)
    out = text_of(lg.render(0.0, 20))
    assert "showing text" in out and "WRN" in out and "error=timed out" in out
    assert "ollama" not in lg.at and "worker-crash" not in lg.at  # not the noisy ones


def test_the_token_comes_from_a_file(tmp_path):
    f = tmp_path / "console.env"
    f.write_text("# argus\nARGUS_WORKER_TOKEN='abc123'\n")
    assert con.token_from(f) == "abc123"
    assert con.token_from(tmp_path / "missing") is None


def test_the_five_views_are_laid_out_in_one_tmux_window(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    panes = iter(["%0", "%1", "%2", "%3", "%4"])

    class Done:
        returncode = 0
        stdout = ""

    def run(argv, **kw):
        calls.append(argv)
        out = Done()
        if "-P" in argv:
            out.stdout = next(panes) + "\n"
        return out

    monkeypatch.setattr(con.subprocess, "run", run)
    monkeypatch.setattr(con.shutil, "which", lambda n: "/usr/bin/tmux")
    monkeypatch.setattr(con.Path, "home", classmethod(lambda cls: tmp_path))
    attached: list = []
    monkeypatch.setattr(con.os, "execvp", lambda f, a: attached.append(a))
    assert con.run_all(["--url", "http://127.0.0.1:8600"]) == 0
    flat = [" ".join(c) for c in calls]
    new = next(c for c in flat if "new-session" in c)
    assert "argus.console deck --url http://127.0.0.1:8600" in new
    assert any("split-window -h -t %0" in c and "console ari" in c for c in flat)       # Ari: the right half
    assert any("split-window -v -t %0" in c and "console events" in c for c in flat)    # events: under the deck
    assert any("split-window -v -t %1" in c and "console logs" in c for c in flat)      # logs: under Ari
    assert any("split-window -h -t %1" in c and "console map" in c for c in flat)       # the map: beside Ari
    assert [c for c in flat if "select-pane -t %0 -T argus" in c]
    assert attached == [["tmux", "attach", "-t", "argus"]]
    assert (tmp_path / ".config" / "argus" / "console.tmux.conf").read_text().startswith("set -g mouse on")


def test_without_tmux_it_says_how_to_get_it(monkeypatch, capsys):
    monkeypatch.setattr(con.shutil, "which", lambda n: None)
    assert con.run_all([]) == 2 and "pacman -S tmux" in capsys.readouterr().out


# ---- the live map

def test_workers_belong_to_machines():
    assert con.machine_of("desktop-saspc") == con.machine_of("worker-saspc") == "saspc"
    assert con.machine_of("worker-calypso-now") == con.machine_of("worker-calypso") == "calypso"
    assert con.machine_of("phone-pixel-8a") == "pixel-8a"


def map_with(workers):
    api = FakeApi()
    api.answers["/status"]["workers"] = workers
    mv = con.MapView(api)
    wait_for(lambda: mv.data.get("status"))
    return mv


def test_the_map_shows_the_machines_and_what_they_run():
    mv = map_with([{"id": "worker-calypso", "state": "online", "capabilities": ["laptop"]},
                   {"id": "desktop-saspc", "state": "online", "capabilities": ["desktop", "session"]},
                   {"id": "worker-saspc", "state": "online", "capabilities": ["gpu"]},
                   {"id": "phone-pixel-8a", "state": "offline"}])
    now = time.monotonic()
    mv.apply([{"kind": "job.queued", "from_component": "gmail", "data": {"job_id": "j9", "workflow": "new"}},
              {"kind": "job.leased", "data": {"job_id": "j9", "worker": "desktop-saspc"}}], now)
    out = text_of(mv.render(now, 24, 110), 110)
    for want in ("MAP", "calypso", "saspc", "pixel-8a", "● online", "○ offline", "▶ gmail·new", "2 workers"):
        assert want in out, want
    assert "ears" in out and "qwen2.5vl:7b" in out
    mv.apply([{"kind": "job.succeeded", "data": {"job_id": "j9"}}], now + 1)
    out = text_of(mv.render(now + 1, 24, 110), 110)
    assert "▶ gmail" not in out and "1 done" in out and "· idle" in out


def test_a_dot_travels_while_a_job_moves_and_stops_after():
    mv = map_with([{"id": "worker-saspc", "state": "online"}])
    now = time.monotonic()
    mv.apply([{"kind": "job.leased", "data": {"job_id": "j1", "worker": "worker-saspc"}}], now)
    assert mv.pulse["saspc"][1] == 1
    moving = text_of(mv.render(now, 24, 80), 80)
    assert "●" in moving.split("saspc")[0].replace("● online", "")  # a dot on the line above the machine's box
    quiet = text_of(con.MapView.render(mv, now + 60, 24, 80), 80)
    assert "●" not in quiet.split("saspc")[0]


def test_the_map_fits_a_small_pane_and_says_when_argusd_is_away():
    mv = map_with([{"id": "worker-calypso", "state": "online"}, {"id": "worker-saspc", "state": "online"}])
    out = text_of(mv.render(0.0, 10, 40), 40)
    assert "calypso" in out and "saspc" in out
    gone = con.MapView(FakeApi(down=True))
    time.sleep(0.2)
    assert "isn't answering" in text_of(gone.render(0.0, 20, 80))
