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
            "current": "temperature_2m"})
        d = self.fetch(f"{FORECAST}?{q}") or {}
        day = d.get("daily") or {}

        def nth(k: str, i: int) -> Any:
            v = day.get(k) or []
            return v[i] if i < len(v) else None

        n = max(1, min(2, len(day.get("weather_code") or [None])))
        return [{"place": spelled, "code": nth("weather_code", i), "low": nth("temperature_2m_min", i),
                 "high": nth("temperature_2m_max", i), "rain": nth("precipitation_probability_max", i),
                 "now": (d.get("current") or {}).get("temperature_2m") if i == 0 else None} for i in range(n)]


def words(w: dict[str, Any]) -> str:
    """ "Colombo: 25 to 31°, thunderstorms, rain likely (80%)." """
    sky = CODES.get(int(w["code"]), "") if w.get("code") is not None else ""
    parts = []
    if w.get("low") is not None and w.get("high") is not None:
        parts.append(f"{round(w['low'])} to {round(w['high'])}°")
    if sky:
        parts.append(sky)
    rain = w.get("rain")
    if rain is not None and rain >= 30 and "rain" not in sky and "shower" not in sky and "thunder" not in sky:
        parts.append(f"{'rain likely' if rain >= 60 else 'maybe rain'} ({round(rain)}%)")
    elif rain is not None and rain >= 60:
        parts.append(f"{round(rain)}% chance")
    return f"{w['place']}: " + ", ".join(parts) + "." if parts else ""


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
