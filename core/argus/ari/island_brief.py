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


def row_open(now: datetime) -> bool:
    """The "Morning brief" row on the island's Ari page: until noon."""
    return now.hour < UNTIL_H


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


def backup_words(b: dict | None, now: datetime) -> tuple[str, str, str] | None:
    """(dot, text, right) for the backup row, None when no backup is known."""
    if not b:
        return None
    if not b.get("ok"):
        return CORAL, "Backup failed", str(b.get("problem") or "")[:40]
    at = datetime.fromtimestamp(b["at"])
    hhmm = at.strftime("%H:%M")
    days = (now.date() - at.date()).days
    when = "last night" if (days == 0 and at.hour < 5) or (days == 1 and at.hour >= 18) else \
        "today" if days == 0 else "yesterday" if days == 1 else at.strftime("%a")
    return GREEN, f"Backup · {when} {hhmm}", "✓"


def card_rows(data: dict, now: datetime) -> list[dict]:
    """The card's rows, most important first, from GET /brief/today: {dot, text, right, action}. `right` is a
    chevron ("›") when the row opens something. Rows that have nothing to say are left out."""
    p = data.get("parts") or {}
    rows: list[dict] = []
    waiting = p.get("waiting") or []
    if waiting:
        rows.append({"dot": AMBER, "text": f"{len(waiting)} waiting for you", "right": "›",
                     "action": "show:inbox"})
    if p.get("dead_n"):
        first = (p.get("dead") or [""])[0]
        rows.append({"dot": CORAL, "text": f"{_n(p['dead_n'], 'job', 'jobs')} failed overnight"
                     + (f" · {first}" if first else ""), "right": "›", "action": "show:runs"})
    if p.get("done"):
        rows.append({"dot": QUIET, "text": f"{_n(p['done'], 'job', 'jobs')} done overnight", "right": "",
                     "action": None})
    today = [t for t in (p.get("today") or []) if len(t) == 2][:3]
    for at, what in today:
        rows.append({"dot": ACCENT, "text": str(what), "right": str(at), "action": None})
    for task in (data.get("tasks") or [])[: max(0, 3 - len(today))]:
        rows.append({"dot": ACCENT, "text": str(task.get("title") or ""), "right": str(task.get("at") or ""),
                     "action": None})
    if not today and not data.get("tasks"):
        rows.append({"dot": QUIET, "text": "Nothing scheduled today", "right": "", "action": None})
    bk = backup_words(p.get("backup"), now)
    if bk:
        rows.append({"dot": bk[0], "text": bk[1], "right": bk[2], "action": None})
    return rows


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
    ts = now.replace(hour=2, minute=0).timestamp()
    wx = {"place": "Colombo", "now": 27.0, "low": 25.0, "high": 31.0, "rain": 70, "sky": "showers"}
    base: dict[str, Any] = {"parts": {"weather": "", "done": 12, "dead": [], "dead_n": 0, "waiting": [],
                                       "today": [["09:00", "Downloads organizer"], ["13:30", "Photo backup"]],
                                       "backup": {"ok": True, "at": ts, "problem": None}, "pc": True,
                                       "health": {}, "claude": [0, 200]}, "weather": wx, "tasks": []}
    p = base["parts"]
    if kind == "quiet":
        p.update(done=0, today=[])
        base["weather"] = {**wx, "rain": 10, "sky": "clear", "now": 26.0}
    elif kind == "trouble":
        p.update(done=9, dead=["backup.copy"], dead_n=1, waiting=["Approve email draft", "Delete old logs",
                                                                  "Install update"],
                 backup={"ok": False, "at": ts, "problem": "the target drive is full"})
    return base
