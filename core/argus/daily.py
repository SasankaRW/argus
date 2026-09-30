"""What Argus tells you without being asked: the morning brief, and a summary after a power cut or crash.

Morning brief (`brief.at`, 07:00): one phone message with what happened overnight, what waits for you, today's
schedules, the backup, the PC and Docker, and yesterday's Claude calls. Sent once a day (if argusd was off at 7,
when it starts, until noon).

Back after a power cut: argusd keeps a marker file while it runs and removes it when it stops cleanly. Finding it
at start means the last run ended abruptly; the phone then hears when it stopped, which jobs pick up where they
left off (from their last finished step) and which schedules run now to catch up.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .ari import clock
from .modelboard import CLAUDE_BUDGET


def _name(r) -> str:
    return f"{r['plugin']}.{r['workflow']}"


def _hhmm(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    return clock(d.hour, d.minute)


def _dur(s: float) -> str:
    m = int(s // 60)
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60} min" if m < 24 * 60 else f"{m // 1440} days"


# ------------------------------------------------------------------ morning brief

def brief_due(at: str, now: float, sent_day: str | None, until_hour: int = 12) -> str | None:
    """Today's date if the message should go now (past `at`, before `until_hour`, not sent today), else None."""
    d = datetime.fromtimestamp(now)
    h, m = map(int, at.split(":"))
    slot = d.replace(hour=h, minute=m, second=0, microsecond=0)
    day = d.strftime("%Y-%m-%d")
    last = slot.replace(hour=min(23, max(until_hour, h + 1)), minute=59 if until_hour >= 23 else 0)
    if d < slot or sent_day == day or d > last:
        return None
    return day


# ------------------------------------------------------------------ time saved and the evening summary

def fmt_minutes(seconds: float) -> str:
    m = round(seconds / 60)
    return "under a minute" if m < 1 else f"{m} min" if m < 60 else f"{m // 60} h {m % 60} min"


def time_saved(conn: sqlite3.Connection, now: float, days: int = 7) -> dict[str, Any]:
    """Time the plugins saved you over the last `days` days (today included): total, per plugin, per day."""
    first = (datetime.fromtimestamp(now) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    rows = conn.execute("SELECT plugin, day, SUM(seconds) AS s, COUNT(DISTINCT job_id) AS jobs FROM time_saved"
                        " WHERE day >= ? GROUP BY plugin, day", (first,)).fetchall()
    by_plugin: dict[str, dict[str, int]] = {}
    by_day: dict[str, int] = {}
    for r in rows:
        p = by_plugin.setdefault(r["plugin"], {"seconds": 0, "jobs": 0})
        p["seconds"] += r["s"]
        p["jobs"] += r["jobs"]
        by_day[r["day"]] = by_day.get(r["day"], 0) + r["s"]
    total = sum(p["seconds"] for p in by_plugin.values())
    return {"days": days, "since": first, "seconds": total, "text": fmt_minutes(total),
            "plugins": sorted(({"plugin": k, **v} for k, v in by_plugin.items()), key=lambda x: -x["seconds"]),
            "by_day": dict(sorted(by_day.items()))}


def compose_summary(conn: sqlite3.Connection, now: float) -> tuple[str, str]:
    """(title, text) of the evening summary: today's work, the time it saved, what failed or waits."""
    start = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    done = conn.execute("SELECT plugin, COUNT(*) FROM jobs WHERE state = 'succeeded' AND finished_at >= ?"
                        " AND plugin NOT IN ('ask', 'health', 'ari') GROUP BY plugin ORDER BY 2 DESC",
                        (start,)).fetchall()
    dead = conn.execute("SELECT plugin, workflow FROM jobs WHERE state = 'dead' AND finished_at >= ?",
                        (start,)).fetchall()
    today = time_saved(conn, now, 1)
    lines = []
    n = sum(r[1] for r in done)
    lines.append(f"Today: {n} job{'s' if n != 1 else ''} done" + (
        " (" + ", ".join(f"{r[0]} {r[1]}" for r in done[:4]) + ")." if done else "."))
    if today["seconds"]:
        lines.append(f"Saved you about {today['text']} today"
                     + (" (" + ", ".join(f"{p['plugin']} {fmt_minutes(p['seconds'])}" for p in today["plugins"][:3])
                        + ")." if len(today["plugins"]) > 1 else "."))
    if dead:
        lines.append(f"{len(dead)} failed: " + ", ".join(sorted({_name(r) for r in dead}))[:200] + ".")
    waiting = conn.execute("SELECT COUNT(*) FROM approvals WHERE state = 'pending'").fetchone()[0]
    if waiting:
        lines.append(f"{waiting} waiting for you.")
    if datetime.fromtimestamp(now).weekday() == 6:  # Sunday: the week too
        week = time_saved(conn, now, 7)
        lines.append(f"This week: about {week['text']} saved.")
    return "Today with Argus", "\n".join(lines)


def compose_brief(conn: sqlite3.Connection, now: float, *, last_backup: dict | None, pc_online: bool,
                  health: dict | None, claude_cap: int) -> tuple[str, str]:
    """(title, text) of the morning brief. Plain lines, most important first."""
    since = now - 12 * 3600
    lines: list[str] = []
    done = conn.execute("SELECT COUNT(*) FROM jobs WHERE state = 'succeeded' AND finished_at >= ?",
                        (since,)).fetchone()[0]
    dead = conn.execute("SELECT plugin, workflow FROM jobs WHERE state = 'dead' AND finished_at >= ?"
                        " ORDER BY finished_at", (since,)).fetchall()
    s = f"Overnight: {done} job{'s' if done != 1 else ''} done"
    if dead:
        names = sorted({_name(r) for r in dead})
        s += f", {len(dead)} failed ({', '.join(names[:3])}{'…' if len(names) > 3 else ''})"
    lines.append(s + ".")
    waiting = conn.execute("SELECT title FROM approvals WHERE state = 'pending' ORDER BY created_at").fetchall()
    if waiting:
        lines.append(f"Waiting for you: {len(waiting)} (" + "; ".join(r[0] for r in waiting[:3]) + ").")
    end_of_day = datetime.fromtimestamp(now).replace(hour=23, minute=59).timestamp()
    today = conn.execute("SELECT label, plugin, workflow, next_run_at FROM schedules WHERE enabled = 1"
                         " AND next_run_at <= ? ORDER BY next_run_at", (end_of_day,)).fetchall()
    if today:
        items = [f"{_hhmm(r['next_run_at'])} {(r['label'] or _name(r)).split(': ', 1)[-1]}" for r in today[:5]]
        lines.append("Today: " + "; ".join(items) + ("…" if len(today) > 5 else "") + ".")
    if last_backup:
        if last_backup.get("ok"):
            lines.append(f"Backup at {_hhmm(last_backup['at'])}: OK.")
        else:
            lines.append(f"Backup FAILED: {str(last_backup.get('problem'))[:100]}.")
    lines.append(f"PC: {'on' if pc_online else 'off'}.")
    if health and health.get("problems"):
        lines.append("Docker/WSL: " + "; ".join(health["problems"][:3]) + ".")
    elif health and health.get("docker"):
        lines.append(f"Docker: {health['docker'].get('running', 0)} running, all fine.")
    y = (datetime.fromtimestamp(now) - timedelta(days=1)).strftime("%Y-%m-%d")
    row = conn.execute("SELECT used FROM budget WHERE day = ? AND key = ?", (y, CLAUDE_BUDGET)).fetchone()
    if row and row[0]:
        lines.append(f"Claude yesterday: {row[0]} of {claude_cap} calls.")
    title = "Good morning" + (" - something needs you" if dead or waiting or (health or {}).get("problems")
                              else "")
    return title, "\n".join(lines)


def last_health(conn: sqlite3.Connection) -> dict | None:
    r = conn.execute("SELECT result FROM jobs WHERE plugin = 'health' AND state = 'succeeded'"
                     " ORDER BY finished_at DESC LIMIT 1").fetchone()
    return json.loads(r[0]) if r and r[0] else None


# ------------------------------------------------------------------ back after a power cut

class Marker:
    """A file that exists while argusd runs. Found at start: the last run did not stop cleanly."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()  # Windows: writing and removing it at the same moment fails (file in use)

    def start(self) -> float | None:
        """Write the marker; returns when the last run was last alive if it ended abruptly, else None."""
        self._stopped = False
        was = None
        if self.path.exists():
            try:
                was = float(json.loads(self.path.read_text())["alive"])
            except (OSError, ValueError, KeyError, TypeError):
                was = self.path.stat().st_mtime
        self.touch()
        return was

    def touch(self) -> None:
        """Refreshed now and then, so the time of a power cut is known to within a minute."""
        with self._lock:
            if self._stopped:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps({"alive": time.time()}))
            except OSError:
                pass

    _stopped = False

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            for _ in range(20):  # an antivirus or indexer may hold it for a moment on Windows
                try:
                    self.path.unlink(missing_ok=True)
                    return
                except PermissionError:
                    time.sleep(0.05)
                except OSError:
                    return


def resume_summary(conn: sqlite3.Connection, now: float, stopped_at: float) -> tuple[str, str, dict[str, Any]]:
    """(title, text, data) about the abrupt stop: what was cut off, and what happens now."""
    cut = conn.execute("SELECT plugin, workflow FROM jobs WHERE state IN ('leased', 'running')").fetchall()
    missed = conn.execute("SELECT id, label, plugin, workflow FROM schedules WHERE enabled = 1 AND next_run_at < ?",
                          (now,)).fetchall()
    lines = [f"Argus stopped unexpectedly around {_hhmm(stopped_at)} and was off for {_dur(now - stopped_at)} "
             "(power cut or crash)."]
    if cut:
        lines.append(f"{len(cut)} job{'s' if len(cut) != 1 else ''} pick up from their last finished step: "
                     + ", ".join(_name(r) for r in cut[:4]) + ("…" if len(cut) > 4 else "") + ".")
    if missed:
        lines.append(f"{len(missed)} missed schedule{'s' if len(missed) != 1 else ''} run now: "
                     + ", ".join((r["label"] or _name(r)) for r in missed[:4]) + ("…" if len(missed) > 4 else "")
                     + ".")
    if not cut and not missed:
        lines.append("Nothing was running and nothing was missed.")
    data = {"stopped_at": stopped_at, "jobs": [_name(r) for r in cut], "schedules": [r["id"] for r in missed]}
    return "Argus is back", "\n".join(lines), data
