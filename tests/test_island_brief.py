"""The island's morning briefing: the sit-down decision, the card's rows and GET /brief/today."""

from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient

from argus.api import create_app
from argus.ari import island_brief as ib
from test_send_api import make

W = {"Authorization": "Bearer t0ken"}
DAY = "2026-11-19"


def at(h: int, m: int = 0) -> datetime:
    return datetime(2026, 11, 19, h, m)


# ------------------------------------------------------------------ the sit-down decision

def test_sitting_down_after_three_hours_shows_it_once():
    sit, show = ib.decide(ib.Sit(), 4 * 3600, at(8), None, False)  # the island's first look: still away
    assert not show
    sit, show = ib.decide(sit, 4 * 3600 + 5, at(8), None, False)
    assert not show
    sit, show = ib.decide(sit, 1.0, at(8, 1), None, False)  # input
    assert show and not sit.pending
    _, again = ib.decide(sit, 0.5, at(8, 2), DAY, False)  # recorded as shown today
    assert not again


def test_a_short_break_is_not_sitting_down():
    sit, _ = ib.decide(ib.Sit(prev_idle=2 * 3600), 2 * 3600 + 5, at(8), None, False)
    _, show = ib.decide(sit, 1.0, at(8), None, False)
    assert not show


def test_the_window_is_five_to_noon():
    away = ib.Sit(prev_idle=5 * 3600)
    assert not ib.decide(away, 1.0, at(4, 59), None, False)[1]
    assert ib.decide(away, 1.0, at(5), None, False)[1]
    assert ib.decide(away, 1.0, at(11, 59), None, False)[1]
    assert not ib.decide(away, 1.0, at(12), None, False)[1]


def test_a_pc_switched_on_this_morning_counts_at_the_first_input():
    assert ib.decide(ib.Sit(), 2.0, at(7), None, False)[1]
    assert not ib.decide(ib.Sit(), 2.0, at(7), DAY, False)[1]  # already shown today
    assert not ib.decide(ib.Sit(), 2.0, at(13), None, False)[1]  # too late


def test_a_blocked_moment_holds_it_back_until_one_is_free():
    away = ib.Sit(prev_idle=5 * 3600)
    sit, show = ib.decide(away, 1.0, at(9), None, True)  # a game, a fullscreen app, Ari busy, the island open
    assert not show and sit.pending
    sit, show = ib.decide(sit, 30.0, at(9, 1), None, True)
    assert not show and sit.pending
    sit, show = ib.decide(sit, 40.0, at(9, 2), None, False)
    assert show and not sit.pending
    # still owed at noon: dropped
    assert not ib.decide(ib.Sit(prev_idle=10.0, pending=True), 20.0, at(12, 1), None, False)[1]


def test_an_owed_card_waits_for_you_to_be_at_the_pc():
    sit = ib.Sit(prev_idle=100.0, pending=True)
    assert not ib.decide(sit, 2 * 3600, at(9), None, False)[1]


def test_the_timer_runs_only_in_the_window_and_stops_once_shown():
    assert ib.watching(at(5), None) and ib.watching(at(11, 59), None)
    assert not ib.watching(at(4, 59), None) and not ib.watching(at(12), None)
    assert not ib.watching(at(9), DAY)
    assert ib.seconds_to_watch(at(9), None) == 0
    assert ib.seconds_to_watch(at(4), None) == 3600
    assert ib.seconds_to_watch(at(9), DAY) == 20 * 3600  # tomorrow 05:00
    assert ib.seconds_to_watch(at(15), None) == 14 * 3600
    assert ib.row_open(at(11, 59)) and not ib.row_open(at(12))


def test_what_was_shown_is_kept_across_restarts(tmp_path):
    f = tmp_path / "data" / "island-brief.json"
    assert ib.load_shown(f) is None
    ib.save_shown(DAY, f)
    assert ib.load_shown(f) == DAY
    f.write_text("not json")
    assert ib.load_shown(f) is None


# ------------------------------------------------------------------ the card

def parts(**over):
    p = {"weather": "", "done": 0, "dead": [], "dead_n": 0, "waiting": [], "today": [],
         "backup": None, "pc": True, "health": {}, "claude": [0, 200]}
    return {"parts": {**p, **over}, "weather": {}, "tasks": []}


def test_an_empty_morning_has_one_calm_row():
    rows = ib.card_rows(parts(), at(8))
    assert [r["text"] for r in rows] == ["Nothing scheduled today"]
    assert rows[0]["action"] is None


def test_the_rows_come_in_order_of_need():
    ts = datetime(2026, 11, 19, 2, 0).timestamp()
    data = parts(waiting=["a", "b", "c"], dead=["backup.copy"], dead_n=1, done=12,
                 today=[["09:00", "One"], ["10:00", "Two"], ["11:00", "Three"], ["12:00", "Four"]],
                 backup={"ok": True, "at": ts, "problem": None})
    rows = ib.card_rows(data, at(8))
    assert [r["text"] for r in rows] == ["3 waiting for you", "1 job failed overnight · backup.copy",
                                         "12 jobs done overnight", "One", "Two", "Three",
                                         "Backup · last night 02:00"]
    assert rows[0]["action"] == "show:inbox" and rows[0]["right"] == "›"
    assert rows[1]["action"] == "show:runs" and rows[1]["dot"] == ib.CORAL
    assert rows[2]["dot"] == ib.QUIET and rows[-1]["right"] == "✓"


def test_a_failed_backup_and_plurals():
    rows = ib.card_rows(parts(dead=["a.x", "b.y"], dead_n=2, done=1,
                              backup={"ok": False, "at": 1.0, "problem": "the drive is full"}), at(8))
    assert rows[0]["text"] == "2 jobs failed overnight · a.x"
    assert "1 job done overnight" in [r["text"] for r in rows]
    assert rows[-1]["text"] == "Backup failed" and rows[-1]["right"] == "the drive is full"
    assert rows[-1]["dot"] == ib.CORAL


def test_tasks_fill_the_rest_of_today_when_there_are_any():
    data = {**parts(today=[["09:00", "One"]]), "tasks": [{"title": "Pay rent", "at": "17:00"},
                                                          {"title": "B"}, {"title": "C"}]}
    assert [r["text"] for r in ib.card_rows(data, at(8))] == ["One", "Pay rent", "B"]


def test_the_weather_on_the_card():
    assert ib.weather_line({}) == ("", "", "")
    icon, temp, line = ib.weather_line({"place": "Colombo", "now": 27.4, "low": 25, "high": 31, "rain": 70,
                                        "sky": "showers"})
    assert (icon, temp, line) == ("rainy", "27°", "25–31° · 70% rain")
    assert ib.weather_line({"place": "x", "now": 20, "low": 18, "high": 22, "rain": 29, "sky": "clear"}) == (
        "wb_sunny", "20°", "18–22°")


def test_the_preview_scenarios_have_their_rows():
    now = at(8)
    assert len(ib.card_rows(ib.sample("normal", now), now)) == 4
    assert [r["text"] for r in ib.card_rows(ib.sample("quiet", now), now)][0] == "Nothing scheduled today"
    assert ib.card_rows(ib.sample("trouble", now), now)[1]["dot"] == ib.CORAL


# ------------------------------------------------------------------ GET /brief/today

def test_brief_today_route(tmp_path):
    argus = make(tmp_path)
    with TestClient(create_app(argus)) as c:
        assert c.get("/brief/today").status_code == 401
        r = c.get("/brief/today", headers=W)
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"day", "at", "parts", "weather", "tasks"}
        assert body["day"] == datetime.fromtimestamp(body["at"]).strftime("%Y-%m-%d")
        assert set(body["parts"]) >= {"weather", "done", "dead", "dead_n", "waiting", "today", "backup", "pc",
                                      "health", "claude"}
        assert body["weather"] == {} and body["tasks"] == []  # no town and no Life Hub: the card copes
        assert body["parts"]["waiting"] == [] and body["parts"]["backup"] is None
        assert ib.card_rows(body, datetime.now())[0]["text"] == "Nothing scheduled today"
