"""Google Calendar events and holidays for the island's calendar page: the plain, tested logic.

The `calendar` plugin fetches (on the PC, with its own Google key) and keeps the last good answer per calendar in its
state in Argus; the island asks `GET /island/calendar`, which uses `for_island` here. Nothing in this file touches
the network.

An event is {title, start, end, all_day, calendar: "events" | "holidays"}. All-day events carry dates
("2026-12-25"), timed ones carry Google's date-times with their offset; like Google, an all-day `end` is the day
after the last day.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta

KINDS = ("events", "holidays")
HORIZON_DAYS = 30
DEFAULT_CALENDARS = ["all", "en.lk#holiday@group.v.calendar.google.com"]  # every ticked calendar, Sri Lanka's holidays


def expand_calendars(ids: list[str], listed: list[dict] | None) -> list[str]:
    """"all" -> every calendar ticked (selected, not hidden) in your Google Calendar list, in its order; the other ids
    stay as written, without repeats. `listed` None (the list couldn't be read): "all" falls back to primary."""
    out: list[str] = []
    for i in ids:
        if i != "all":
            got = [i]
        elif listed is None:
            got = ["primary"]
        else:
            got = [str(c["id"]) for c in listed if c.get("id") and c.get("selected") and not c.get("hidden")]
        out += [x for x in got if x not in out]
    return out


def kind_of(cal_id: str) -> str:
    """Google's holiday calendars have ids like en.lk#holiday@group.v.calendar.google.com."""
    return "holidays" if "#holiday@" in cal_id else "events"


def normalize(item: dict, kind: str) -> dict | None:
    """One item of Google's events list -> an event; None for a cancelled or malformed one."""
    if not isinstance(item, dict) or item.get("status") == "cancelled":
        return None
    s, e = item.get("start") or {}, item.get("end") or {}
    all_day = "date" in s
    start = s.get("date") if all_day else s.get("dateTime")
    end = (e.get("date") if all_day else e.get("dateTime")) or start
    if not start:
        return None
    title = str(item.get("summary") or "").strip() or ("Holiday" if kind == "holidays" else "(no title)")
    return {"title": title, "start": str(start), "end": str(end), "all_day": all_day,
            "calendar": kind if kind in KINDS else "events"}


def normalize_all(items: list[dict], kind: str) -> list[dict]:
    return [ev for ev in (normalize(i, kind) for i in items or []) if ev]


def _local(s: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.astimezone() if d.tzinfo else d


def days_of(ev: dict) -> list[date]:
    """The calendar days an event touches, on this PC's clock."""
    try:
        if ev.get("all_day"):
            a, b = date.fromisoformat(ev["start"][:10]), date.fromisoformat(str(ev.get("end") or ev["start"])[:10])
            return [a + timedelta(n) for n in range(max(1, (b - a).days))]
    except (KeyError, ValueError, TypeError):
        return []
    a, b = _local(ev.get("start", "")), _local(ev.get("end") or ev.get("start", ""))
    if a is None:
        return []
    last = a.date() if b is None or b <= a else (b - timedelta(microseconds=1)).date()
    return [a.date() + timedelta(n) for n in range((last - a.date()).days + 1)]


def sort_key(ev: dict) -> tuple:
    d = days_of(ev)
    t = "" if ev.get("all_day") else (_local(ev.get("start", "")) or datetime.min).strftime("%H:%M")
    return (d[0] if d else date.max, 0 if ev.get("all_day") else 1, t, ev.get("title", ""))


def merge(*lists: list[dict]) -> list[dict]:
    """Several calendars' events as one list, without repeats, all-day first within a day, in time order."""
    seen, out = set(), []
    for ev in (e for lst in lists for e in lst):
        k = (ev["calendar"], ev["title"], ev["start"], ev["end"])
        if k not in seen:
            seen.add(k)
            out.append(ev)
    return sorted(out, key=sort_key)


def window(events: list[dict], today: date, days: int = HORIZON_DAYS) -> list[dict]:
    """Only what touches today .. today + days."""
    last = today + timedelta(days)
    return [ev for ev in events if any(today <= d <= last for d in days_of(ev))]


# ---------------------------------------------------------------- the cache (last good answer per calendar)

def update_cache(cache: dict | None, cal_id: str, events: list[dict] | None, now: float | None = None,
                 error: str | None = None) -> dict:
    """A fetch's outcome for one calendar. A good one replaces what was kept; a failed one keeps the last good
    events (so the island works offline) and only records why."""
    out = {k: dict(v) for k, v in (cache or {}).items() if isinstance(v, dict)}
    cur = out.get(cal_id, {})
    if error is None and events is not None:
        out[cal_id] = {"events": events, "at": now if now is not None else time.time(), "error": None}
    else:
        out[cal_id] = {"events": cur.get("events", []), "at": cur.get("at"), "error": error or "no answer"}
    return out


def cached_events(cache: dict | None) -> list[dict]:
    return merge(*[v.get("events") or [] for v in (cache or {}).values() if isinstance(v, dict)])


def _read(body: bytes | str | dict | None) -> tuple[str, set[str]]:
    """Google's error answer -> (its message, its reasons)."""
    try:
        got = body if isinstance(body, dict) else json.loads(body or b"{}")
    except ValueError:
        got = {}
    got = got if isinstance(got, dict) else {}
    err = got.get("error")
    err = err if isinstance(err, dict) else {}
    plain = got.get("error") if isinstance(got.get("error"), str) else ""
    text = str(err.get("message") or got.get("error_description") or plain or "")
    return text, {str(x.get("reason", "")) for x in err.get("errors") or [] if isinstance(x, dict)}


def needs_new_key(status: int, body: bytes | str | dict | None) -> bool:
    """Only a new "Allow" fixes it: the key was taken back or ran out (401), or lacks the calendar scope (403)."""
    text, reasons = _read(body)
    low = text.lower()
    if "accessNotConfigured" in reasons or "has not been used" in low:
        return False
    return status == 401 or (status == 403 and ("insufficient" in low or "scope" in low
                                                or "insufficientPermissions" in reasons))


def explain(status: int, body: bytes | str | dict | None, calendar_id: str = "") -> str:
    """Why Google refused, in plain words."""
    text, reasons = _read(body)
    low = text.lower()
    if "accessNotConfigured" in reasons or "has not been used" in low or "is disabled" in low or "not enabled" in low:
        return ("The Google Calendar API isn't switched on in your Google Cloud project: Cloud console > APIs & "
                "Services > Library > Google Calendar API > Enable, then Refresh.")
    if status == 401 or "invalid_grant" in low or "revoked" in low or "expired" in low:
        return "Google took the OK back or it ran out: press Connect Google Calendar and click Allow again."
    if needs_new_key(status, body):
        return ("Google didn't give Argus calendar access: add the calendar.readonly scope to the consent screen, "
                "then Connect Google Calendar again and tick it.")
    if status == 404:
        return f"Google has no calendar called {calendar_id or 'that'}: check the ids in the plugin's settings."
    if status == 429 or reasons & {"rateLimitExceeded", "userRateLimitExceeded"}:
        return "Google says too many requests: it will try again in 15 minutes."
    return f"Google answered {status}{': ' + text[:120] if text else ''}."


# ---------------------------------------------------------------- what the island asks for

def for_island(state: dict | None, today: date | None = None) -> dict:
    """GET /island/calendar: {connected, updated, events: [...merged, today..+30 days]}. `state` is the plugin's
    saved {status, cache}. Nothing connected (or nothing cached yet) gives connected False and no events: the island
    then shows nothing extra, and never an error."""
    state = state or {}
    cache = state.get("cache") or {}
    status = state.get("status") or {}
    stamps = [v.get("at") for v in cache.values() if isinstance(v, dict) and v.get("at")]
    if not stamps or status.get("state") == "needs_connect":
        return {"connected": False, "updated": None, "events": []}
    return {"connected": True, "updated": max(stamps),
            "events": window(cached_events(cache), today or date.today())}


def marks(events: list[dict], year: int, month: int) -> dict[int, set[str]]:
    """For the month grid: day number -> {"events", "holidays"} present that day."""
    out: dict[int, set[str]] = {}
    for ev in events or []:
        for d in days_of(ev):
            if d.year == year and d.month == month:
                out.setdefault(d.day, set()).add(ev.get("calendar") if ev.get("calendar") in KINDS else "events")
    return out


def on_day(events: list[dict], day: date) -> list[dict]:
    return [ev for ev in events or [] if day in days_of(ev)]


def clock(ev: dict) -> str:
    """A timed event's start as "14:30" on this PC's clock; "" for an all-day one."""
    if ev.get("all_day"):
        return ""
    t = _local(ev.get("start", ""))
    return t.strftime("%H:%M") if t else ""
