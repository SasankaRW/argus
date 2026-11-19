"""Today's weather for the morning brief, from Open-Meteo (free, no key, no account).

Off until you name a place (`brief.weather: Colombo`, or in Helios > Settings). argusd looks the place up once
(its coordinates are kept for the day) and asks for today's forecast when the brief is made. Only the place name
and coordinates leave the machine. If the forecast can't be reached the brief goes without it.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

GEO = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"

# WMO weather codes, in words that read well aloud
CODES: dict[int, str] = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "cloudy", 45: "foggy", 48: "foggy",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "showers", 81: "showers", 82: "heavy showers", 85: "snow showers", 86: "snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}

Fetch = Callable[[str], Any]


def fetch_json(url: str, timeout: float = 6) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "argus-brief"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


class Weather:
    def __init__(self, fetch: Fetch = fetch_json):
        self.fetch = fetch
        self._places: dict[str, tuple[float, float, str]] = {}
        self._today: dict[tuple[str, str], dict[str, Any] | None] = {}

    def place(self, name: str) -> tuple[float, float, str] | None:
        """(lat, lon, name as Open-Meteo spells it), or None when it knows no such place."""
        key = name.strip().lower()
        if key not in self._places:
            q = urllib.parse.urlencode({"name": name.split(",")[0].strip(), "count": 5, "format": "json"})
            hits = (self.fetch(f"{GEO}?{q}") or {}).get("results") or []
            want = [p.strip().lower() for p in name.split(",")[1:]]  # "Kandy, LK" or "Paris, Texas"
            for h in hits:
                where = " ".join(str(h.get(k, "")) for k in ("country_code", "country", "admin1")).lower()
                if all(w in where for w in want):
                    self._places[key] = (float(h["latitude"]), float(h["longitude"]), str(h.get("name") or name))
                    break
            else:
                return None
        return self._places[key]

    def today(self, name: str) -> dict[str, Any] | None:
        """Today's low, high, chance of rain and sky; None when the place is unknown. Kept for an hour, so
        "good morning" answers at once after the first time."""
        return self.day(name, 0)

    def day(self, name: str, offset: int = 0) -> dict[str, Any] | None:
        """Today (0) or tomorrow (1), as `today` says. Both days come with one request, kept for an hour."""
        key = (name.strip().lower(), time.strftime("%Y-%m-%d %H"))
        if key not in self._today:
            self._today = {key: self._fetch(name)}
        days = self._today[key]
        return None if days is None else days[min(max(offset, 0), len(days) - 1)]

    def _fetch(self, name: str) -> list[dict[str, Any]] | None:
        p = self.place(name)
        if p is None:
            return None
        lat, lon, spelled = p
        q = urllib.parse.urlencode({
            "latitude": f"{lat:.3f}", "longitude": f"{lon:.3f}", "timezone": "auto", "forecast_days": 2,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "hourly": "precipitation_probability,precipitation,weather_code",
            "current": "temperature_2m"})
        d = self.fetch(f"{FORECAST}?{q}") or {}
        day = d.get("daily") or {}

        def nth(k: str, i: int) -> Any:
            v = day.get(k) or []
            return v[i] if i < len(v) else None

        n = max(1, min(2, len(day.get("weather_code") or [None])))
        return [{"place": spelled, "code": nth("weather_code", i), "low": nth("temperature_2m_min", i),
                 "high": nth("temperature_2m_max", i), "rain": nth("precipitation_probability_max", i),
                 "now": (d.get("current") or {}).get("temperature_2m") if i == 0 else None,
                 "hours": hours_of(d.get("hourly"), nth("time", i))} for i in range(n)]


def hours_of(hourly: Any, date: Any) -> list[dict[str, Any]]:
    """That day's hours from Open-Meteo's `hourly` block: [{h, p (chance of rain %), mm, code}], [] when the
    forecast has none for the day."""
    if not isinstance(hourly, dict) or not date:
        return []
    out = []
    for i, t in enumerate(hourly.get("time") or []):
        if not str(t).startswith(str(date)) or len(str(t)) < 13:
            continue

        def at(k: str, i: int = i) -> Any:
            v = hourly.get(k) or []
            return v[i] if i < len(v) else None
        out.append({"h": int(str(t)[11:13]), "p": at("precipitation_probability"), "mm": at("precipitation"),
                    "code": at("weather_code")})
    return out


STRIP_FROM, STRIP_TO = 6, 22  # the strip runs 6 am to 11 pm: the hours starting 6:00 .. 22:00
WET = 40                      # a chance of rain at or above this counts as a wet hour
THUNDER, HEAVY_MM = (95, 96, 99), 4.0


def _hour(h: int) -> str:
    return "noon" if h == 12 else "midnight" if h % 24 == 0 else f"{h % 12} {'am' if h < 12 else 'pm'}"


def _range(a: int, b: int) -> str:
    """The hours a to b (b: the hour it ends): "2–5 pm", "11 am–1 pm"."""
    if (a < 12) == (b <= 12) and 12 not in (a, b):
        return f"{a % 12 or 12}–{_hour(b)}"
    return f"{_hour(a)}–{_hour(b)}"


def rain_outlook(hours: list[dict[str, Any]] | None, from_hour: int = 0) -> dict[str, Any]:
    """When it rains today: {"headline": "Rain likely 2–5 pm (80%), dry morning", "strip": [{h, p}, ...]}.

    `hours` is the day's hourly forecast (`hours_of`). The headline is about the hours from `from_hour` on (the
    morning brief asks from the first hour of the day); the strip is every hour 6 am to 11 pm. Without hourly
    data the headline is "" and the strip is empty, so the card shows neither."""
    strip = [{"h": x["h"], "p": round(x["p"])} for x in hours or []
             if x.get("p") is not None and STRIP_FROM <= x["h"] <= STRIP_TO]
    ahead = [x for x in hours or [] if x.get("p") is not None and max(from_hour, STRIP_FROM) <= x["h"] <= STRIP_TO]
    if not ahead:
        return {"headline": "", "strip": strip}
    peak = max(x["p"] for x in ahead)
    wet = [x for x in ahead if x["p"] >= WET]
    if not wet:
        return {"headline": f"Mostly dry, a small chance of rain ({round(peak)}%)" if peak >= 20 else "Dry all day",
                "strip": strip}
    runs: list[list[dict[str, Any]]] = []
    for x in wet:  # wet hours, with a one-hour gap still part of the same spell
        if runs and x["h"] - runs[-1][-1]["h"] <= 2:
            runs[-1].append(x)
        else:
            runs.append([x])
    main = max(runs, key=lambda r: (max(x["p"] for x in r), len(r)))
    top = max(x["p"] for x in main)
    a, b = main[0]["h"], main[-1]["h"] + 1
    thunder = any(x.get("code") in THUNDER for x in ahead if x["p"] >= 30 and a <= x["h"] < b)
    heavy = any((x.get("mm") or 0) >= HEAVY_MM or x.get("code") in (65, 82) for x in main)
    on_off = len(main) < 0.6 * (b - a) or (top < 60 and b - a >= 4)
    where = "after noon" if a >= 12 and b - a >= 6 else f"from {_hour(a)}" if b - a >= 8 else _range(a, b)
    if thunder:
        head = f"Thunderstorms {where} ({round(top)}%)"
    elif heavy:
        head = f"Heavy rain {where} ({round(top)}%)"
    elif on_off:
        head = f"Showers on and off {where} ({round(top)}%)"
    elif top >= 60:
        head = f"Rain likely {where} ({round(top)}%)"
    else:
        head = f"Rain possible {where} ({round(top)}%)"
    if a >= 12 and from_hour < 12 and where != "after noon" and len(runs) == 1:
        head += ", dry morning"
    elif b <= 15 and len(runs) == 1:
        head += f", clearing by {_hour(b)}"
    if len(runs) > 1:
        other = max((r for r in runs if r is not main), key=lambda r: max(x["p"] for x in r))
        head += f", also {_range(other[0]['h'], other[-1]['h'] + 1)}"
    return {"headline": head, "strip": strip}


def words(w: dict[str, Any], headline: str = "") -> str:
    """ "Colombo: 25 to 31°, thunderstorms, rain likely (80%)." With the day's rain `headline` (rain_outlook)
    that comes after the sky instead of the daily chance: "Colombo: 25 to 31°, showers. Rain likely 2–5 pm." """
    sky = CODES.get(int(w["code"]), "") if w.get("code") is not None else ""
    parts = []
    if w.get("low") is not None and w.get("high") is not None:
        parts.append(f"{round(w['low'])} to {round(w['high'])}°")
    if sky:
        parts.append(sky)
    rain = w.get("rain")
    if headline:
        pass
    elif rain is not None and rain >= 30 and "rain" not in sky and "shower" not in sky and "thunder" not in sky:
        parts.append(f"{'rain likely' if rain >= 60 else 'maybe rain'} ({round(rain)}%)")
    elif rain is not None and rain >= 60:
        parts.append(f"{round(rain)}% chance")
    out = f"{w['place']}: " + ", ".join(parts) + "." if parts else ""
    return f"{out} {headline}.".strip() if headline else out


def spoken(w: dict[str, Any], when: str = "today") -> str:
    """ "Colombo today: 25 to 31 degrees, thunderstorms, 80% chance of rain. It's 28 now." """
    sky = CODES.get(int(w["code"]), "") if w.get("code") is not None else ""
    parts = []
    if w.get("low") is not None and w.get("high") is not None:
        parts.append(f"{round(w['low'])} to {round(w['high'])} degrees")
    if sky:
        parts.append(sky)
    rain = w.get("rain")
    if rain is not None and rain >= 30:
        parts.append(f"{round(rain)}% chance of rain")
    elif rain is not None and not any(x in sky for x in ("rain", "shower", "drizzle", "thunder")):
        parts.append("no rain expected")
    out = f"{w['place']} {when}: " + ", ".join(parts) + "." if parts else f"No forecast for {w['place']} right now."
    if w.get("now") is not None and when == "today":
        out += f" It's {round(w['now'])} now."
    return out
