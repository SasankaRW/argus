"""The terminal console (python -m argus.console): each view renders from argusd's answers, the eye follows Ari,
and the tmux layout puts the four views where they belong. A fake argusd; no network, no real terminal."""

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
            "/problems?hours=24": {"count": 3, "headline": "3 problems: tool play_on_spotify ×2, hearing",
                                   "problems": [{"area": "tool play_on_spotify", "what": "search box not found",
                                                 "count": 2, "hint": None},
                                                {"area": "hearing", "what": "another language", "count": 1,
                                                 "hint": "speech that wasn't English was ignored"}]},
            "/timeline/lanes?minutes=60": {"since": now - 3600, "now": now, "workers": [
                {"worker": "desktop-saspc", "jobs": [{"start": now - 1800, "end": now - 1500, "plugin": "workstation",
                                                      "state": "dead"},
                                                     {"start": now - 600, "end": now - 300, "plugin": "ari",
                                                      "state": "succeeded"}]}]},
            "/ari/timeline?limit=80": {"requests": [
                {"at": now - 60, "text": "play famous on spotify", "result": "failed", "job_id": "j9",
                 "why": "search box not found", "tools": [{"tool": "play_on_spotify", "ok": False}],
                 "ms": 8200, "models": [{"model": "qwen3:8b", "verdict": "ok"}]},
                {"at": now - 90, "text": "kohomada", "result": "ignored", "why": "another language (si 80%)"},
                {"at": now - 120, "text": "what's the time", "result": "ok", "reply": "It's 7 pm.", "ms": 900}]},
            "/ari/trace/j9": {"text": "play famous on spotify", "result": "failed", "why": "search box not found",
                              "heard": {"accepted": 0.4, "top": "si", "top_p": 0.45, "second_listen": True},
                              "steps": [{"kind": "model", "what": "qwen3:8b (decide 1)", "ok": True, "ms": 900},
                                        {"kind": "step", "what": "tool 1: play_on_spotify", "ok": False,
                                         "ms": 7000, "why": "search box not found"}]},
            "/models/calls?limit=60": {"calls": [{"created_at": now - 30, "plugin": "ari", "workflow": "think",
                                                  "model": "qwen3:8b", "verdict": "rejected", "latency_ms": 1200,
                                                  "reason": "not one of the tools"}]},
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
    assert "what is running" in out and "Two jobs, nothing stuck." in out and "idle" in out


def test_the_timeline_and_events():
    ev = con.EventsView(FakeApi())
    wait_for(lambda: ev.rows and ev.lanes, 6)
    out = text_of(ev.render(0.0, 30, 120))
    assert "last 60 min" in out and "desktop-saspc" in out and "ari━" in out and "now" in out
    assert "job.succeeded" in out and "backup" in out and "files=3" in out
    ev.key("+")
    assert ev.minutes == 30


def test_the_logs_view_tells_each_request_as_a_line_and_opens_its_trace():
    lg = con.LogsView(FakeApi())
    wait_for(lambda: lg.stories and lg.rows and lg.calls, 6)
    out = text_of(lg.render(0.0, 30))
    assert "play famous on spotify" in out and "search box not found" in out and "kohomada" in out
    assert "what's the time" in out and "It's 7 pm." in out and "8.2s" in out
    lg.key("f")  # failures and ignored only
    out = text_of(lg.render(0.0, 30))
    assert "what's the time" not in out and "kohomada" in out
    lg.key("f")
    lg.key("\x1b[A")  # up, then down again: the newest line, the failed request
    lg.key("\x1b[B")
    lg.key("\r")
    wait_for(lambda: lg.trace)
    out = text_of(lg.render(0.0, 40))
    assert "trace of" in out and "second listen" in out and "tool 1: play_on_spotify" in out
    lg.key("\x1b")
    assert lg.trace is None
    lg.key("l")  # the raw logs, with the model calls in between
    out = text_of(lg.render(0.0, 30))
    assert "showing text" in out and "WRN" in out and "error=timed out" in out
    assert "MDL" in out and "qwen3:8b" in out and "rejected" in out
    lg.key("/")
    for ch in "voice":
        lg.key(ch)
    lg.key("\r")
    out = text_of(lg.render(0.0, 30))
    assert "showing text" in out and "qwen3:8b" not in out
    assert "ollama" not in lg.at and "worker-crash" not in lg.at  # not the noisy ones


def test_the_deck_says_what_isnt_working():
    deck = con.Deck(FakeApi())
    wait_for(lambda: deck.data.get("problems"))
    out = text_of(deck.render(0.5))
    assert "3 problems" in out and "p: details" in out
    deck.key("p")
    out = text_of(deck.render(0.5))
    assert "search box not found" in out and "not working" in out


def test_the_token_comes_from_a_file(tmp_path):
    f = tmp_path / "console.env"
    f.write_text("# argus\nARGUS_WORKER_TOKEN='abc123'\n")
    assert con.token_from(f) == "abc123"
    assert con.token_from(tmp_path / "missing") is None


def test_the_four_views_are_laid_out_in_one_tmux_window(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    panes = iter(["%0", "%1", "%2", "%3"])

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
    assert [c for c in flat if "select-pane -t %0 -T argus" in c]
    assert attached == [["tmux", "attach", "-t", "argus"]]
    assert (tmp_path / ".config" / "argus" / "console.tmux.conf").read_text().startswith("set -g mouse on")


def test_without_tmux_it_says_how_to_get_it(monkeypatch, capsys):
    monkeypatch.setattr(con.shutil, "which", lambda n: None)
    assert con.run_all([]) == 2 and "pacman -S tmux" in capsys.readouterr().out


def test_the_panes_fill_their_space():
    """No half-empty panes: the deck adds Ari's day as a chart, what runs now and the models; the logs pane puts
    the newest log lines under a short list of requests; the chart helper stacks and scales."""
    api = FakeApi()
    now = time.time()
    start = (int(now) // 3600 - 23) * 3600
    api.answers["/timeline/activity?hours=24"] = {
        "start": start, "jobs": [{"ok": 5, "failed": i % 7 == 0} for i in range(24)],
        "ari": [{"ok": i % 4, "failed": int(i == 20), "ignored": int(i == 21)} for i in range(24)],
        "total": {"ok": 30, "failed": 1, "ignored": 1, "jobs_ok": 120, "jobs_failed": 4, "answer_ms": 1400}}
    api.answers["/jobs?state=running&limit=6"] = [{"plugin": "ari", "workflow": "think", "created_at": now - 5,
                                                   "lease_owner": "worker-saspc"}]
    api.answers["/models/calls?limit=80"] = {"calls": [{"model": "qwen3:8b", "verdict": "ok", "latency_ms": 900},
                                                       {"model": "qwen3:8b", "verdict": "rejected",
                                                        "latency_ms": 1200}]}
    deck = con.Deck(api)
    deck._activity_at = -1e9
    wait_for(lambda: deck.data.get("running") and deck.activity)
    text = text_of(deck.render(1.0, 104, 34), 104)
    assert "ari · last 24 h" in text and "30 answered" in text and "ari·think" in text and "qwen3:8b" in text
    assert len(text.rstrip().splitlines()) >= 30  # the pane is used to the bottom

    logs = con.LogsView(api)
    wait_for(lambda: logs.stories and logs.rows)
    text = text_of(logs.render(1.0, 30, 160), 160)
    assert "latest log lines" in text and "voice unavailable" in text

    from rich.text import Text
    chart = con.bars(Text, [{"ok": 1}, {"ok": 4, "failed": 2}, {}], [("ok", "x"), ("failed", "y")], 4, 12, start)
    rows = chart.plain.splitlines()
    assert len(rows) == 5 and rows[0].strip() and "█" in rows[-2]


def test_ari_strip_shows_the_voice_then_the_request_as_it_happens():
    api = FakeApi()
    now = time.time()
    api.answers["/live"] = {"voice": {"mic": [0.1, 0.5, 0.9, 0.3], "out": [], "speaking": False, "english": 0.94,
                                      "at": now},
                            "model": {"state": "text", "job_id": "j9", "step": "decide 1", "model": "qwen3:8b",
                                      "text": '{"tool": "play_on_spotify"', "ms": 900}, "model_at": now}
    api.answers["/ari/trace/j9"] = {"steps": [
        {"kind": "step", "what": "decide 1", "ok": True, "ms": 900},
        {"kind": "model", "what": "qwen3:8b (decide 1)", "ok": True, "ms": 880},
        {"kind": "step", "what": "tool 1: play_on_spotify", "ok": True, "ms": None}]}
    v = con.AriView(api)
    wait_for(lambda: v.live.get("voice"))
    out = text_of(v.render(1.0, 16, 90), 94)
    assert "English 94%" in out and "▁" in out or "█" in out  # the waveform
    v.ari.phase, v.ari.think_job, v.ari.text = "working", "j9", "play famous on spotify"
    wait_for(lambda: v.trace)
    out = text_of(v.render(1.0, 16, 110), 114)
    assert "heard" in out and "understood" in out and "play on spotify" in out and "speak" in out
    assert "qwen3:8b" in out and "tok/s" in out and "play_on_spotify" in out  # the model, live
    assert v.typed("hello there", 0.0) and v.typed("hello there ari", 10.0) == "hello there ari"


def test_the_deck_becomes_the_game_mode_screen():
    api = FakeApi()
    api.answers["/ari/game"] = {"on": True, "by": "auto", "since": time.time() - 600, "app": "Cyberpunk2077",
                                "freed_mb": 9300, "used_mb": 900, "unloaded": ["qwen3:8b"], "still": ["chrome.exe"]}
    deck = con.Deck(api)
    wait_for(lambda: deck.data.get("game"))
    out = text_of(deck.render(1.0, 90, 30), 90)
    assert "▄▀▀" in out and "Cyberpunk2077" in out and "9.1 GB" in out and "qwen3:8b" in out
    assert "chrome.exe" in out and "a minute after the game closes" in out
