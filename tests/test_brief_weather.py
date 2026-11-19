"""The morning brief upgrade: today's weather (Open-Meteo), and Ari saying the brief on "good morning"."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from argus.config import load_config
from argus.context import Argus
from argus.daily import say_brief
from argus.weather import Weather, hours_of, rain_outlook, words
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
    p = {"weather": "", "reminders": [], "waiting": [], "dead": [], "dead_n": 0, "backup": None}
    return {**p, **kw}


def test_ari_says_the_weather_your_diary_and_what_needs_you():
    at7 = datetime(2026, 10, 1, 7, 5).timestamp()
    assert say_brief(parts(), at7) == "Good morning. It's 7:05 am. Nothing needs you."
    assert say_brief(parts(weather="Kandy: 21 to 30°, clear. Dry all day."), at7) == (
        "Good morning. It's 7:05 am. Kandy: 21 to 30°, clear. Dry all day. Nothing needs you.")
    said = say_brief(parts(weather="Kandy: 21 to 30°, thunderstorms. Rain likely 2–5 pm (80%), dry morning.",
                           reminders=[{"text": "Dentist", "time": "15:30"}, {"text": "Call Kasun", "time": None}],
                           dead=["notes.add"], dead_n=1,
                           waiting=["Pay the electricity bill", "Rename 3 files"], backup={"ok": False}), at7)
    assert said == ("Good morning. It's 7:05 am. Kandy: 21 to 30°, thunderstorms. Rain likely 2–5 pm (80%), dry "
                    "morning. From your diary: 3:30 pm Dentist; Call Kasun. Two things wait for you, starting "
                    "with Pay the electricity bill. Last night's backup failed. One thing failed overnight: "
                    "notes.add.")
    assert "job" not in said and "ran" not in said  # no statistics
    assert say_brief(parts(dead=["x.y"], dead_n=2), at7).endswith("Two things failed overnight: x.y.")
    assert say_brief(parts(), datetime(2026, 10, 1, 19, 0).timestamp()).startswith("Good evening.")


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
        assert "Overnight" not in b["text"] and "PC:" not in b["text"]
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
        assert cl.post("/brief", {})["text"] == "Nothing needs you."


TWO_DAYS = {"daily": {"weather_code": [95, 0], "temperature_2m_min": [21.4, 20.2], "temperature_2m_max": [29.6, 31.0],
                      "precipitation_probability_max": [80, 5]}, "current": {"temperature_2m": 23.0}}


def test_ari_answers_the_weather_at_once_today_or_tomorrow(tmp_path):
    from argus.ari.ari import weather_ask
    from argus.weather import spoken

    assert weather_ask("How's the weather today?") == ("", 0)
    assert weather_ask("will it rain tomorrow in Galle?") == ("Galle", 1)
    assert weather_ask("Explain how weather forecasts are made") is None
    assert weather_ask("open spotify") is None
    w = Weather(lambda url: GEO if "geocoding" in url else TWO_DAYS)
    assert spoken(w.day("Kandy, LK", 0)) == ("Kandy today: 21 to 30 degrees, thunderstorms, 80% chance of rain. "
                                             "It's 23 now.")
    assert spoken(w.day("Kandy, LK", 1), "tomorrow") == "Kandy tomorrow: 20 to 31 degrees, clear, no rain expected."
    a = make(tmp_path)
    a.weather = Weather(lambda url: GEO if "geocoding" in url else TWO_DAYS)
    with Server(a.open()) as srv:
        cl = client(srv.url)
        r = cl.post("/ari", {"text": "How's the weather tomorrow?"})
        assert r["reply"] == "Kandy tomorrow: 20 to 31 degrees, clear, no rain expected." and not r.get("job_id")
        assert "weather" in {t["name"] for t in cl.get("/tools")}


# ------------------------------------------------------------------ when it rains

def day(chances: dict[int, int], code: int = 80, mm: float = 1.0, other: int = 3) -> list[dict]:
    """A day's hours: the chance of rain as given (5 % otherwise); `code` and `mm` for the wet hours."""
    return [{"h": h, "p": chances.get(h, 5), "mm": mm if h in chances else 0.0, "code": code if h in chances else other}
            for h in range(24)]


def test_a_dry_day():
    o = rain_outlook(day({}))
    assert o["headline"] == "Dry all day"
    assert [x["h"] for x in o["strip"]] == list(range(6, 23)) and {x["p"] for x in o["strip"]} == {5}
    assert rain_outlook(day({9: 25}))["headline"] == "Mostly dry, a small chance of rain (25%)"


def test_morning_rain_clears():
    o = rain_outlook(day({7: 60, 8: 80, 9: 70, 10: 45}))
    assert o["headline"] == "Rain likely 7–11 am (80%), clearing by 11 am"


def test_afternoon_rain_after_a_dry_morning():
    assert rain_outlook(day({14: 70, 15: 80, 16: 80, 17: 60}))["headline"] == "Rain likely 2–6 pm (80%), dry morning"


def test_afternoon_showers_on_and_off():
    o = rain_outlook(day({13: 45, 15: 50, 17: 45, 19: 40, 21: 40}))
    assert o["headline"] == "Showers on and off after noon (50%)"


def test_thunder_and_heavy_rain():
    assert rain_outlook(day({15: 70, 16: 90, 17: 60}, code=95))["headline"] == "Thunderstorms 3–6 pm (90%), dry morning"
    assert rain_outlook(day({8: 80, 9: 90}, code=65, mm=6.0))["headline"].startswith("Heavy rain 8–10 am (90%)")


def test_a_second_spell_and_hours_already_gone():
    o = rain_outlook(day({7: 70, 8: 70, 18: 80, 19: 80}))
    assert o["headline"] == "Rain likely 6–8 pm (80%), also 7–9 am"
    late = rain_outlook(day({7: 70, 8: 70, 18: 80}), from_hour=12)  # asked at noon: the morning is over
    assert late["headline"] == "Rain likely 6–7 pm (80%)"
    assert len(late["strip"]) == 17  # the strip is still the whole day


def test_missing_hourly_data_gives_no_headline_and_no_strip():
    assert rain_outlook(None) == {"headline": "", "strip": []}
    assert rain_outlook([]) == {"headline": "", "strip": []}
    assert rain_outlook([{"h": 9, "p": None, "mm": None, "code": None}]) == {"headline": "", "strip": []}
    assert hours_of(None, "2026-11-19") == [] and hours_of({"time": []}, None) == []
    assert rain_outlook(day({}), from_hour=23)["headline"] == ""  # nothing left of today to say


def test_hours_of_picks_that_days_hours():
    h = {"time": ["2026-11-19T06:00", "2026-11-19T07:00", "2026-11-20T06:00"],
         "precipitation_probability": [10, 90, 50], "precipitation": [0, 3.2, 1], "weather_code": [3, 63, 61]}
    assert hours_of(h, "2026-11-19") == [{"h": 6, "p": 10, "mm": 0, "code": 3},
                                         {"h": 7, "p": 90, "mm": 3.2, "code": 63}]
    assert hours_of(h, "2026-11-20") == [{"h": 6, "p": 50, "mm": 1, "code": 61}]


def test_the_forecast_asks_for_hourly_rain_and_the_headline_reaches_the_brief_line():
    f = Fake()
    hourly = {"time": [f"2026-11-19T{h:02d}:00" for h in range(24)],
              "precipitation_probability": [70 if 14 <= h <= 16 else 5 for h in range(24)],
              "precipitation": [1.0] * 24, "weather_code": [80] * 24}
    data = {**FORECAST, "daily": {**FORECAST["daily"], "time": ["2026-11-19"]}, "hourly": hourly}
    w = Weather(lambda url: GEO if "geocoding" in url else (f.urls.append(url), data)[1])
    d = w.today("Kandy, LK")
    assert "hourly=precipitation_probability%2Cprecipitation%2Cweather_code" in f.urls[-1]
    assert len(d["hours"]) == 24
    assert words(d, rain_outlook(d["hours"])["headline"]) == (
        "Kandy: 21 to 30°, thunderstorms. Rain likely 2–5 pm (70%), dry morning.")
