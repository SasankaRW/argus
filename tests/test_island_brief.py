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
    assert ib.row_open(at(11, 59), DAY) and not ib.row_open(at(12), DAY)
    assert not ib.row_open(at(8), None) and not ib.row_open(at(8), "2026-11-18")  # not before it has shown today


def test_what_was_shown_is_kept_across_restarts(tmp_path):
    f = tmp_path / "data" / "island-brief.json"
    assert ib.load_shown(f) is None
    ib.save_shown(DAY, f)
    assert ib.load_shown(f) == DAY
    f.write_text("not json")
    assert ib.load_shown(f) is None


# ------------------------------------------------------------------ the card

def parts(**over):
    p = {"weather": "", "reminders": [], "waiting": [], "dead": [], "dead_n": 0, "backup": None}
    return {"parts": {**p, **over}, "weather": {}}


def test_an_empty_morning_has_one_calm_row():
    rows = ib.card_rows(parts(), at(8))
    assert [r["text"] for r in rows] == ["Nothing needs you"]
    assert rows[0]["action"] is None and rows[0]["dot"] == ib.QUIET


def test_the_rows_come_in_order_of_need():
    rem = [{"text": "Dentist", "time": "15:30", "mentioned_on": DAY}, {"text": "Call Kasun", "time": None},
           {"text": "C", "time": None}, {"text": "D", "time": None}, {"text": "E", "time": None}]
    data = parts(waiting=["a", "b", "c"], dead=["backup.copy"], dead_n=1, reminders=rem,
                 backup={"ok": False, "at": 1.0, "problem": "the drive is full"})
    rows = ib.card_rows(data, at(8))
    assert [r.get("text") or r["head"] for r in rows] == [
        "3 waiting for you", "Backup failed", "1 thing failed overnight · backup.copy", "From your diary",
        "Dentist", "Call Kasun", "C", "D"]
    assert rows[0]["action"] == "show:inbox" and rows[0]["right"] == "›" and rows[0]["dot"] == ib.AMBER
    assert rows[2]["action"] == "show:runs" and rows[2]["dot"] == ib.CORAL
    assert rows[4]["right"] == "3:30 pm" and rows[5]["right"] == ""


def test_rows_are_hidden_when_there_is_nothing_to_say():
    assert [r["text"] for r in ib.card_rows(parts(dead=["a.x", "b.y"], dead_n=2), at(8))] == [
        "2 things failed overnight · a.x"]
    assert [r["text"] for r in ib.card_rows(parts(waiting=["x"]), at(8))] == ["1 waiting for you"]
    only = ib.card_rows(parts(reminders=[{"text": "Pay rent", "time": "09:00"}]), at(8))
    assert [r.get("text") or r["head"] for r in only] == ["From your diary", "Pay rent"]
    for r in ib.card_rows(parts(waiting=["x"], dead=["a.x"], dead_n=3, backup={"ok": False}), at(8)):
        assert "job" not in r.get("text", "")  # no job counts anywhere


def test_the_rain_strip_and_headline_on_the_card():
    assert ib.outlook({}) == ("", [])
    assert ib.outlook({"weather": {"outlook": {"headline": "Dry all day", "strip": []}}}) == ("Dry all day", [])
    data = {"weather": {"outlook": {"headline": "Rain likely", "strip": [{"h": 6, "p": 20}, {"h": 7, "p": 140},
                                                                         {"h": 8, "p": None}]}}}
    assert ib.outlook(data) == ("Rain likely", [(6, 20), (7, 100)])  # clamped; an hour without data is skipped


def test_the_weather_on_the_card():
    assert ib.weather_line({}) == ("", "", "")
    icon, temp, line = ib.weather_line({"place": "Colombo", "now": 27.4, "low": 25, "high": 31, "rain": 70,
                                        "sky": "showers"})
    assert (icon, temp, line) == ("rainy", "27°", "25–31° · 70% rain")
    assert ib.weather_line({"place": "x", "now": 20, "low": 18, "high": 22, "rain": 29, "sky": "clear"}) == (
        "wb_sunny", "20°", "18–22°")


def test_the_preview_scenarios_have_their_rows():
    now = at(8)
    normal = ib.sample("normal", now)
    assert [r.get("text") or r["head"] for r in ib.card_rows(normal, now)] == [
        "From your diary", "Dentist appointment", "Call Kasun about the trip"]
    head, strip = ib.outlook(normal)
    assert head.startswith("Rain likely") and len(strip) == 17 and max(p for _h, p in strip) == 80
    quiet = ib.sample("quiet", now)
    assert [r["text"] for r in ib.card_rows(quiet, now)] == ["Nothing needs you"]
    assert ib.outlook(quiet)[0] == "Dry all day"
    trouble = ib.sample("trouble", now)
    assert [r.get("text") or r["head"] for r in ib.card_rows(trouble, now)][:3] == [
        "3 waiting for you", "Backup failed", "1 thing failed overnight · backup.copy"]
    assert ib.outlook(trouble)[0].startswith("Thunderstorms")


# ------------------------------------------------------------------ GET /brief/today

def test_brief_today_route(tmp_path):
    argus = make(tmp_path)
    with TestClient(create_app(argus)) as c:
        assert c.get("/brief/today").status_code == 401
        r = c.get("/brief/today", headers=W)
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"day", "at", "parts", "weather"}
        assert body["day"] == datetime.fromtimestamp(body["at"]).strftime("%Y-%m-%d")
        assert set(body["parts"]) == {"weather", "reminders", "waiting", "dead", "dead_n", "backup"}  # no job counts
        assert body["weather"] == {}  # no town: the card copes
        assert body["parts"]["waiting"] == [] and body["parts"]["backup"] is None
        assert body["parts"]["reminders"] == [] and body["parts"]["weather"] == ""
        assert ib.card_rows(body, datetime.now())[0]["text"] == "Nothing needs you"


def test_brief_today_has_the_hourly_rain_and_the_diary(tmp_path, monkeypatch):
    import json
    import time

    import argus.context as context
    from argus.weather import Weather
    from conftest import run
    from test_brief_weather import GEO

    class Morning(datetime):  # the headline is about the hours still to come: it is 7 am
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 19, 7, 0)

    monkeypatch.setattr(context, "datetime", Morning)
    hourly = {"time": [f"{DAY}T{h:02d}:00" for h in range(24)],
              "precipitation_probability": [80 if 14 <= h <= 16 else 5 for h in range(24)],
              "precipitation": [1.0] * 24, "weather_code": [80] * 24}
    fc = {"daily": {"time": [DAY], "weather_code": [80], "temperature_2m_min": [25], "temperature_2m_max": [31],
                    "precipitation_probability_max": [80]}, "current": {"temperature_2m": 27}, "hourly": hourly}
    argus = make(tmp_path)
    argus.cfg.brief.weather = "Kandy, LK"
    argus.weather = Weather(lambda url: GEO if "geocoding" in url else fc)
    jid, _ = run(argus.jobs.enqueue("diary", "reminders"))
    now = time.time()
    res = {"for": datetime.fromtimestamp(now).strftime("%Y-%m-%d"),
           "reminders": [{"text": "Dentist", "time": "15:30", "mentioned_on": DAY}]}
    run(argus.store.write(lambda c: c.execute("UPDATE jobs SET state = 'succeeded', result = ?, finished_at = ?"
                                              " WHERE id = ?", (json.dumps(res), now - 100, jid))))
    with TestClient(create_app(argus)) as c:
        body = c.get("/brief/today", headers=W).json()
    assert body["weather"]["outlook"]["headline"] == "Rain likely 2–5 pm (80%), dry morning"
    assert len(body["weather"]["outlook"]["strip"]) == 17
    assert body["parts"]["reminders"] == [{"text": "Dentist", "time": "15:30", "mentioned_on": DAY}]
    assert body["parts"]["weather"] == "Kandy: 25 to 31°, showers. Rain likely 2–5 pm (80%), dry morning."


def test_read_it_to_me_has_the_listener_say_the_brief(tmp_path):
    argus = make(tmp_path)
    with TestClient(create_app(argus)) as c:
        body = {"text": "good morning", "source": "island-read"}
        r = c.post("/ari", json=body, headers=W).json()  # no listener on this PC: the answer is only shown
        assert r["reply"].startswith("Good ")
        assert c.get("/events?kinds=ari.say", headers=W).json()["events"] == []
        c.post("/ari/listener", headers=W)
        c.post("/ari", json=body, headers=W)
        said = c.get("/events?kinds=ari.say", headers=W).json()["events"]
        assert len(said) == 1 and said[0]["data"]["text"].startswith("Good ")
        c.post("/ari", json={"text": "good morning", "source": "island"}, headers=W)  # typed: not read aloud
        assert len(c.get("/events?kinds=ari.say", headers=W).json()["events"]) == 1
