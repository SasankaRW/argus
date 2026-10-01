"""The morning brief upgrade: today's weather (Open-Meteo), and Ari saying the brief on "good morning"."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from argus.config import load_config
from argus.context import Argus
from argus.daily import say_brief
from argus.weather import Weather, words
from test_worker import Server, client

GEO = {"results": [
    {"name": "Kandy", "latitude": 40.1, "longitude": -80.0, "country_code": "US", "country": "United States"},
    {"name": "Kandy", "latitude": 7.29, "longitude": 80.63, "country_code": "LK", "country": "Sri Lanka",
     "admin1": "Central"}]}
FORECAST = {"daily": {"weather_code": [95], "temperature_2m_min": [21.4], "temperature_2m_max": [29.6],
                      "precipitation_probability_max": [80]}, "current": {"temperature_2m": 23.0}}


class Fake:
    def __init__(self):
        self.urls: list[str] = []

    def __call__(self, url):
        self.urls.append(url)
        return GEO if "geocoding" in url else FORECAST


def test_the_place_is_looked_up_once_and_the_country_picks_the_right_one():
    f = Fake()
    w = Weather(f)
    today = w.today("Kandy, LK")
    assert today["place"] == "Kandy" and today["high"] == 29.6
    assert "latitude=7.290" in f.urls[1]
    w.today("kandy, lk")
    assert sum("geocoding" in u for u in f.urls) == 1
    assert Weather(lambda url: {"results": []}).today("Nowhere") is None


def test_the_weather_in_words():
    assert words({"place": "Kandy", "code": 95, "low": 21.4, "high": 29.6, "rain": 80}) == \
        "Kandy: 21 to 30°, thunderstorms, 80% chance."
    assert words({"place": "Colombo", "code": 2, "low": 25, "high": 31, "rain": 65}) == \
        "Colombo: 25 to 31°, partly cloudy, rain likely (65%)."
    assert words({"place": "Colombo", "code": 0, "low": 25, "high": 31, "rain": 10}) == "Colombo: 25 to 31°, clear."


def parts(**kw):
    p = {"weather": "", "done": 0, "dead": [], "dead_n": 0, "waiting": [], "today": [], "backup": {"ok": True},
         "pc": True, "health": {}, "claude": (0, 30)}
    return {**p, **kw}


def test_ari_says_the_brief_needs_first():
    at7 = datetime(2026, 10, 1, 7, 5).timestamp()
    assert say_brief(parts(), at7) == "Good morning. It's 7:05 am. Nothing needs you."
    said = say_brief(parts(weather="Kandy: 21 to 30°, thunderstorms.", done=4, dead=["notes.add"], dead_n=1,
                           waiting=["Pay the electricity bill", "Rename 3 files"],
                           today=[("9 am", "sort downloads"), ("5 pm", "call mum")],
                           backup={"ok": False}), at7)
    assert said == ("Good morning. It's 7:05 am. Kandy: 21 to 30°, thunderstorms. Two things wait for you, starting "
                    "with Pay the electricity bill. Overnight four jobs ran and one failed: notes.add. Today: sort "
                    "downloads at 9 am, and one more. Last night's backup failed.")
    assert say_brief(parts(dead=["x.y"], dead_n=2), at7).endswith("Overnight two jobs failed: x.y.")
    assert say_brief(parts(done=3), datetime(2026, 10, 1, 19, 0).timestamp()).startswith("Good evening.")


def make(tmp_path: Path) -> Argus:
    (tmp_path / "argus.yaml").write_text("logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 30\n"
                                         "brief:\n  weather: Kandy, LK\n", encoding="utf-8")
    a = Argus(load_config(tmp_path / "argus.yaml"))
    a.weather = Weather(Fake())
    return a


def test_good_morning_to_ari_and_the_phone_brief_have_the_weather(tmp_path):
    with Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        r = cl.post("/ari", {"text": "Good morning, Ari!"})
        assert r["reply"].startswith("Good ") and "Kandy: 21 to 30°, thunderstorms, 80% chance." in r["reply"]
        assert r["pending"] is None
        b = cl.post("/brief", {})
        assert b["text"].splitlines()[0] == "Kandy: 21 to 30°, thunderstorms, 80% chance."
        s = {x["key"]: x for x in cl.get("/argus-settings")["settings"]}
        assert s["brief.weather"]["type"] == "text" and s["brief.weather"]["value"] == "Kandy, LK"
        cl.call("PUT", "/argus-settings", {"brief.weather": ""})
        assert "Kandy" not in cl.post("/brief", {})["text"]


def test_no_connection_no_weather_but_the_brief_still_goes(tmp_path):
    a = make(tmp_path)

    def down(url):
        raise OSError("no network")

    a.weather = Weather(down)
    with Server(a.open()) as srv:
        cl = client(srv.url)
        assert cl.post("/brief", {})["text"].startswith("Overnight:")
