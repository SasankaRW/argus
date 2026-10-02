"""Export an Ari conversation as one text file you can hand over when something went wrong.

It holds the chat, what Ari did for each answer (the model's steps, the tools and what they returned, errors), the
events around that time, the log lines from the same minutes (warnings and errors from every log, everything from
Ari's own), the model and Ari settings, and the health check. Secrets are removed, and what a private tool
returned (your screen, clipboard) is left out. Markdown, so it also reads fine by eye.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import ari as ari_mod
from . import logview
from .events import _COLS, event_row

PRIVATE = {"look_at_screen", "summarise_clipboard", "phone_screen_text"}  # plus every tool marked private
CLIP = 700          # one tool argument or result
EVENTS_MAX = 300
LOG_MAX = 120       # lines per log file
BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
SECRET = re.compile(r"(?i)((?:token|secret|password|api[_-]?key)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]{6,}"
                    r"|([?&]t=)[0-9a-f]{20,}")


def scrub(text: str) -> str:
    """Tokens, passwords and one-time links out of any text."""
    text = BEARER.sub(lambda m: m.group(1) + "[hidden]", text)
    return SECRET.sub(lambda m: (m.group(1) or m.group(2)) + "[hidden]", text)


def clip(v: Any, n: int = CLIP) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    s = scrub(s)
    return s if len(s) <= n else s[:n] + f"… [{len(s) - n} more characters]"


def when(t: float | None) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d %H:%M:%S") + "Z" if t else "?"


def _ts(s: Any) -> float | None:
    """A log line's time (ISO, UTC when it has no zone) as epoch seconds."""
    if not isinstance(s, str) or not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=UTC)).timestamp()


def used_lines(used: Any, private: set[str]) -> list[str]:
    out = []
    for u in used or []:
        if not isinstance(u, dict):
            continue
        name = u.get("tool") or "?"
        if u.get("private") or name in private:
            out.append(f"  - {name}: [private result, not exported]")
            continue
        err = f"  ERROR: {clip(u['error'])}" if u.get("error") else ""
        res = f"  -> {clip(u['result'])}" if "result" in u and not err else ""
        out.append(f"  - {name}({clip(u.get('args') or {}, 300)}){res}{err}")
    return out


def job_section(conn: sqlite3.Connection, job_id: str, private: set[str]) -> list[str]:
    j = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if j is None:
        return [f"  job {job_id}: no longer in the database"]
    lines = [f"  job {job_id}: {j['plugin']}.{j['workflow']} {j['state']}, attempt {j['attempt']}/{j['max_attempts']},"
             f" {when(j['created_at'])} -> {when(j['finished_at'])}"
             + (f", took {j['finished_at'] - j['created_at']:.1f} s" if j["finished_at"] else "")]
    if j["error"]:
        lines.append(f"  error: {clip(j['error'], 1500)}")
    try:
        result = json.loads(j["result"]) if j["result"] else None
    except ValueError:
        result = None
    if isinstance(result, dict):
        lines += [f"  via: {result.get('via')}  action: {result.get('action')}  mood: {result.get('mood')}"]
        lines += used_lines(result.get("used"), private)
        extra = {k: v for k, v in result.items() if k not in ("reply", "used", "via", "action", "mood", "pending")}
        if extra:
            lines.append(f"  more: {clip(extra, 500)}")
    for s in conn.execute("SELECT * FROM steps WHERE job_id = ? ORDER BY idx", (job_id,)).fetchall():
        out = ""
        if s["output"]:
            hidden = any(p in str(s["name"]) for p in private)
            out = " -> [private result, not exported]" if hidden else f" -> {clip(s['output'], 400)}"
        tier = f" tier {s['tier_used']}" if s["tier_used"] else ""
        err = f" ERROR: {clip(s['error'], 500)}" if s["error"] else ""
        lines.append(f"  step {s['idx']} {s['name']} [{s['state']}]{tier}{err}{out}")
    return lines


def events_section(conn: sqlite3.Connection, t0: float, t1: float, job_ids: list[str]) -> list[str]:
    marks = ",".join("?" * len(job_ids)) or "''"
    rows = conn.execute(
        f"SELECT {_COLS} FROM events WHERE at BETWEEN ? AND ? AND (kind LIKE 'ari.%' OR kind LIKE 'model.%'"
        f" OR kind LIKE 'job.%' OR job_id IN ({marks})) ORDER BY rowid DESC LIMIT ?",
        (t0, t1, *job_ids, EVENTS_MAX)).fetchall()
    out = []
    for r in reversed(rows):
        e = event_row(r)
        data = clip(e["data"], 300) if e.get("data") else ""
        path = f" {e['from']}->{e['to']}" if e.get("from") or e.get("to") else ""
        out.append(f"{when(e['at'])[11:]} {e['kind']}{path} {data}".rstrip())
    return out


def logs_section(log_dir: Path, t0: float, t1: float) -> list[str]:
    out: list[str] = []
    for name, path in logview.sources(log_dir).items():
        try:
            entries = logview.read(path, name, lines=1500)["entries"]
        except OSError:
            continue
        own = name.startswith(("ari", "voice", "listener", "island"))
        keep = []
        for e in entries:
            t = _ts(e.get("ts"))
            if t is not None and not (t0 <= t <= t1):
                continue
            if t is None and not own:
                continue
            if own or e["level"] in ("warn", "warning", "error", "critical"):
                keep.append(e)
        if not keep:
            continue
        more = f", last {LOG_MAX}" if len(keep) > LOG_MAX else ""
        out.append(f"--- {name}.log ({len(keep)} lines in this time{more})")
        for e in keep[-LOG_MAX:]:
            extra = f" {clip(e['extra'], 200)}" if e.get("extra") else ""
            out.append(f"{(e.get('ts') or '')[11:19]} {e['level'].upper():5} {clip(e['msg'], 400)}{extra}")
    return out


def build(conn: sqlite3.Connection, conv: str, *, cfg: Any, version: str, log_dir: Path,
          health: dict[str, Any] | None = None, last: int = 40, private: set[str] | None = None) -> str | None:
    """The report for one conversation (its last `last` turns), or None when there is no such conversation."""
    turns = ari_mod.turns(conn, conv, last)
    if not turns:
        return None
    private = PRIVATE | (private or set())
    now = time.time()
    t0 = float(turns[0]["created_at"] or now) - 60
    t1 = min(now, float(turns[-1]["created_at"] or now) + 180)
    L: list[str] = []
    L += ["# Ari conversation export", "",
          f"- conversation: {conv} (last {len(turns)} turns)",
          f"- exported: {when(now)}  Argus {version}  host {cfg.instance.name} ({cfg.instance.host})",
          f"- ollama: {clip(cfg.ollama.model_dump(mode='json'), 300)}",
          f"- model tiers: {clip({k: v.model_dump(mode='json') for k, v in cfg.models.tiers.items()}, 600)}",
          f"- ari settings: {clip(cfg.ari.model_dump(mode='json'), 1500)}", ""]
    if health:
        L += ["## Health check (last run)", ""] + [
            f"- {c.get('name')}: {c.get('level')} - {clip(c.get('detail') or '', 200)}"
            for c in (health.get("checks") or [])] + [""]
    L += ["## Conversation", ""]
    jobs: list[str] = []
    for t in turns:
        who = "YOU" if t["role"] == "you" else "ARI"
        text = t["text"] if t["text"] is not None else "(no answer was written)"
        L.append(f"[{when(t['created_at'])[11:]}] {who}: {scrub(str(text))}")
        if t.get("action") or t.get("pending"):
            L.append(f"  action: {t.get('action')}  asked first: {clip(t['pending'], 300) if t['pending'] else '-'}")
        if t.get("used"):
            L.append("  tools: " + ", ".join(f"{u.get('tool')}{'' if u.get('ok', True) else ' (failed)'}"
                                               for u in t["used"] if isinstance(u, dict)))
        if t.get("job_id"):
            jobs.append(t["job_id"])
            L += job_section(conn, t["job_id"], private)
        L.append("")
    L += ["## Events around then", ""] + (events_section(conn, t0, t1, jobs) or ["(none)"]) + [""]
    L += ["## Log lines from the same time (warnings and errors, and Ari's own logs)", ""]
    L += (logs_section(log_dir, t0, t1) or ["(none)"]) + [""]
    return "\n".join(L)
