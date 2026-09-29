"""Cron expressions and time windows, in the computer's local time. No dependencies.

Cron: five fields, `minute hour day-of-month month day-of-week`, each `*`, `n`, `a-b`, a list `a,b` and a step
`*/n` or `a-b/n`. Day of week 0-7 (0 and 7 are Sunday) or sun..sat; months 1-12 or jan..dec. As in classic cron,
when both day fields are restricted a day matches if either does. Shortcuts: @hourly @daily @weekly @monthly
@yearly (and @midnight).

Windows: "HH:MM-HH:MM", for example the night window "01:00-06:00"; it may wrap past midnight ("22:00-06:00").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@weekly": "0 0 * * 0",
           "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *"}
DOW_NAMES = {n: i for i, n in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}
MON_NAMES = {n: i + 1 for i, n in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


class CronError(ValueError):
    pass


@dataclass(frozen=True)
class Cron:
    text: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    dom_any: bool
    dow_any: bool

    def _day_ok(self, d: datetime) -> bool:
        if d.month not in self.months:
            return False
        dom = d.day in self.days
        dow = (d.isoweekday() % 7) in self.weekdays
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return dow
        if self.dow_any:
            return dom
        return dom or dow

    def next_after(self, t: datetime) -> datetime:
        """The first matching minute strictly after `t` (naive local time)."""
        t = t.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(366 * 5):  # every rule matches within a few years (Feb 29 at worst)
            if self._day_ok(t):
                for h in sorted(x for x in self.hours if x >= t.hour):
                    first = t.minute if h == t.hour else 0
                    m = next((x for x in sorted(self.minutes) if x >= first), None)
                    if m is not None:
                        return t.replace(hour=h, minute=m)
            t = (t + timedelta(days=1)).replace(hour=0, minute=0)
        raise CronError(f"{self.text!r} never matches")


def _field(text: str, lo: int, hi: int, names: dict[str, int] | None = None) -> tuple[frozenset[int], bool]:
    out: set[int] = set()
    for part in text.lower().split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) == 0:
                raise CronError(f"bad step in {text!r}")
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            a, b = _num(x, names), _num(y, names)
        else:
            a = b = _num(part, names)
            if step > 1:
                b = hi
        if not (lo <= a <= hi and lo <= b <= hi) or a > b:
            raise CronError(f"{text!r} is outside {lo}-{hi}")
        out.update(range(a, b + 1, step))
    return frozenset(out), text == "*"


def _num(x: str, names: dict[str, int] | None) -> int:
    if names and x in names:
        return names[x]
    if not x.isdigit():
        raise CronError(f"{x!r} is not a number")
    return int(x)


def parse(expr: str) -> Cron:
    text = ALIASES.get(expr.strip().lower(), expr.strip())
    parts = text.split()
    if len(parts) != 5:
        raise CronError(f"{expr!r}: a cron needs 5 fields (minute hour day month weekday)")
    mi, _ = _field(parts[0], 0, 59)
    ho, _ = _field(parts[1], 0, 23)
    do, dom_any = _field(parts[2], 1, 31)
    mo, _ = _field(parts[3], 1, 12, MON_NAMES)
    dw, dow_any = _field(parts[4], 0, 7, DOW_NAMES)
    dw = frozenset(d % 7 for d in dw)
    return Cron(expr, mi, ho, do, mo, dw, dom_any, dow_any)


def next_run(expr: str, after_ts: float) -> float:
    """Unix time of the next run of `expr` after `after_ts`, in local time."""
    return parse(expr).next_after(datetime.fromtimestamp(after_ts)).timestamp()


# ------------------------------------------------------------------ windows

_WINDOW = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def parse_window(text: str) -> tuple[int, int]:
    """Minutes after midnight (start, end)."""
    m = _WINDOW.match(text)
    if not m:
        raise CronError(f"window {text!r} must look like 01:00-06:00")
    h1, m1, h2, m2 = map(int, m.groups())
    if h1 > 23 or h2 > 24 or m1 > 59 or m2 > 59 or (h1, m1) == (h2, m2):
        raise CronError(f"window {text!r} is not a valid time range")
    return h1 * 60 + m1, h2 * 60 + m2


def in_window(text: str, ts: float) -> bool:
    start, end = parse_window(text)
    t = datetime.fromtimestamp(ts)
    now = t.hour * 60 + t.minute
    return start <= now < end if start < end else (now >= start or now < end)


def window_opens(text: str, ts: float) -> float:
    """When the window next opens (`ts` itself if it is open now)."""
    if in_window(text, ts):
        return ts
    start, _ = parse_window(text)
    t = datetime.fromtimestamp(ts)
    opening = t.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
    if opening <= t:
        opening += timedelta(days=1)
    return opening.timestamp()
