"""The island's morning briefing: when to show it, and what its card says (no Qt in here, so it is testable).

The island shows the card once a day when you sit down: the PC has had no input for 3 hours, then input comes,
between 05:00 and 12:00, and it hasn't shown today (data/island-brief.json). The data is `GET /brief/today`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

AWAY_S = 3 * 3600      # no input for this long, then input: you sat down
PRESENT_S = 60.0       # "input arrived" / "you're here": the last input was this recent
EVERY_S = 5.0          # how often the sit-down check runs (while the window is open)
FROM_H, UNTIL_H = 5, 12
HIDE_S = 25.0          # the card closes itself after this long if the pointer never entered it
QUIET = "#8c8c92"      # the island's quiet dot
ACCENT, AMBER, CORAL, GREEN = "#a6c8ff", "#ffc58f", "#ff9d94", "#8fe3ad"
STORE = Path("data") / "island-brief.json"


@dataclass(frozen=True)
class Sit:
    """What the sit-down check remembers between two looks."""
    prev_idle: float | None = None  # the idle time at the last look (None: the island just started)
    pending: bool = False           # someone sat down and the card is owed (a block held it back)


def in_window(now: datetime) -> bool:
    return FROM_H <= now.hour < UNTIL_H


def today_key(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def decide(sit: Sit, idle: float, now: datetime, shown_day: str | None, blocked: bool) -> tuple[Sit, bool]:
    """(the memory for the next look, show the card now). `idle` is seconds since the last input; `blocked` is
    game mode, a fullscreen app in front, Ari busy, or the island already open. A block holds the card back (it
    stays owed) until the next look where nothing blocks and you are still at the PC, before noon."""
    if not in_window(now) or shown_day == today_key(now):
        return Sit(idle, False), False
    pending = sit.pending
    if sit.prev_idle is None:  # the island just started: the first input counts, as if you had just sat down
        pending = pending or idle <= PRESENT_S
    elif sit.prev_idle >= AWAY_S and idle < sit.prev_idle:  # input after a long quiet
        pending = True
    if pending and not blocked and idle <= 300:
        return Sit(idle, False), True
    return Sit(idle, pending), False


def seconds_to_watch(now: datetime, shown_day: str | None) -> float:
    """0 when the sit-down check should be running, else how long until the window next opens (05:00 today, or
    tomorrow once the card has been shown today or noon has passed)."""
    if watching(now, shown_day):
        return 0.0
    start = now.replace(hour=FROM_H, minute=0, second=0, microsecond=0)
    if now >= start:
        start += timedelta(days=1)
    return (start - now).total_seconds()


def watching(now: datetime, shown_day: str | None) -> bool:
    """Whether the 5 s timer should run: inside the window and not shown today."""
    return in_window(now) and shown_day != today_key(now)


def row_open(now: datetime, shown_day: str | None) -> bool:
    """The "Morning brief" row on the island's Ari page: once the card has shown today, until noon."""
    return now.hour < UNTIL_H and shown_day == today_key(now)


def load_shown(path: Path = STORE) -> str | None:
    try:
        day = json.loads(path.read_text(encoding="utf-8")).get("day")
    except (OSError, ValueError, AttributeError):
        return None
    return day if isinstance(day, str) else None


def save_shown(day: str, path: Path = STORE) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"day": day}), encoding="utf-8")
    except OSError:
        pass  # the card may show again after a restart; that is all


def _n(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _hm(t: Any) -> str:
    """ "14:30" as "2:30 pm"; whatever isn't a time stays as it is."""
    try:
        h, m = map(int, str(t).split(":"))
    except ValueError:
        return str(t or "")
    return f"{h % 12 or 12}:{m:02d} {'am' if h < 12 else 'pm'}"


def card_rows(data: dict, now: datetime) -> list[dict]:
    """The card's rows from GET /brief/today: {dot, text, right, action} (`right` is "›" when the row opens
    something), and {"head": "From your diary"} for a small heading. What needs you comes first (approvals
    waiting, a failed backup, things that failed overnight), then what your diary says is today (up to 4). A row
    with nothing to say is left out; a quiet morning gets the one line "Nothing needs you"."""
    p = data.get("parts") or {}
    rows: list[dict] = []
    waiting = p.get("waiting") or []
    if waiting:
        rows.append({"dot": AMBER, "text": f"{len(waiting)} waiting for you", "right": "›",
                     "action": "show:inbox"})
    if p.get("backup"):
        rows.append({"dot": CORAL, "text": "Backup failed", "right": "›", "action": "show:runs"})
    if p.get("dead_n"):
        first = (p.get("dead") or [""])[0]
        rows.append({"dot": CORAL, "text": f"{_n(p['dead_n'], 'thing', 'things')} failed overnight"
                     + (f" · {first}" if first else ""), "right": "›", "action": "show:runs"})
    rem = [r for r in p.get("reminders") or [] if isinstance(r, dict) and r.get("text")]
    if rem:
        rows.append({"head": "From your diary"})
        for r in rem[:4]:
            rows.append({"dot": ACCENT, "text": str(r["text"]), "right": _hm(r["time"]) if r.get("time") else "",
                         "action": None})
    if not rows:  # a quiet morning: one calm line, so the card isn't just a weather report with a gap
        rows.append({"dot": QUIET, "text": "Nothing needs you", "right": "", "action": None})
    return rows


def outlook(data: dict) -> tuple[str, list[tuple[int, int]]]:
    """(the rain headline, [(hour, chance %)] for the strip) from GET /brief/today; ("", []) without hourly data."""
    o = (data.get("weather") or {}).get("outlook") or {}
    strip = [(int(x["h"]), max(0, min(100, int(x["p"])))) for x in o.get("strip") or []
             if isinstance(x, dict) and x.get("h") is not None and x.get("p") is not None]
    return str(o.get("headline") or ""), strip


def weather_line(w: dict | None) -> tuple[str, str, str]:
    """(symbol name, the temperature, the line under it) for the card's top right; ("", "", "") without weather."""
    if not w or not w.get("place"):
        return "", "", ""
    sky = str(w.get("sky") or "")
    icon = ("thunderstorm" if "thunder" in sky else "weather_snowy" if "snow" in sky else
            "rainy" if any(k in sky for k in ("rain", "drizzle", "shower")) else "foggy" if "fog" in sky else
            "cloud" if sky == "cloudy" else "partly_cloudy_day" if sky in ("partly cloudy", "mostly clear")
            else "wb_sunny")
    temp = f"{round(w['now'])}°" if w.get("now") is not None else ""
    bits = []
    if w.get("low") is not None and w.get("high") is not None:
        bits.append(f"{round(w['low'])}–{round(w['high'])}°")
    if (w.get("rain") or 0) >= 30:
        bits.append(f"{round(w['rain'])}% rain")
    return icon, temp, " · ".join(bits)


def sample(kind: str, now: datetime) -> dict[str, Any]:
    """Fake GET /brief/today bodies for `--preview --brief normal|quiet|trouble`."""
    from ..weather import rain_outlook

    def hours(chances: dict[int, int], code: int = 80, mm: float = 1.0) -> list[dict[str, Any]]:
        return [{"h": h, "p": chances.get(h, 5), "mm": mm if h in chances else 0.0,
                 "code": code if h in chances else 3} for h in range(24)]

    wx = {"place": "Colombo", "now": 27.0, "low": 25.0, "high": 31.0, "rain": 80, "sky": "showers"}
    day = hours({13: 40, 14: 70, 15: 80, 16: 80, 17: 60, 18: 30})
    parts: dict[str, Any] = {
        "weather": "", "waiting": [], "dead": [], "dead_n": 0, "backup": None,
        "reminders": [{"text": "Dentist appointment", "time": "15:30", "mentioned_on": "2026-11-17"},
                      {"text": "Call Kasun about the trip", "time": None, "mentioned_on": "2026-11-18"}]}
    if kind == "quiet":
        wx = {**wx, "rain": 10, "sky": "clear", "now": 26.0}
        parts["reminders"] = []
        day = hours({})
    elif kind == "trouble":
        parts.update(dead=["backup.copy"], dead_n=1, waiting=["Approve email draft", "Delete old logs",
                                                              "Install update"],
                     backup={"ok": False, "at": now.timestamp(), "problem": "the target drive is full"})
        wx = {**wx, "sky": "thunderstorms"}
        day = hours({11: 50, 12: 70, 13: 90, 14: 90, 15: 70, 16: 40}, code=95, mm=5.0)
    return {"day": today_key(now), "at": now.timestamp(), "parts": parts,
            "weather": {**wx, "outlook": rain_outlook(day)}}
