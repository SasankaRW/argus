"""One component's recent log, for Helios's live map: GET /logs/{component}?lines=&level=&since=.

A component is a plugin id, a worker id, or "argus". The lines come from the same log files the MCP `read_log` tool
reads (logs/argus.log, worker.log, worker-laptop.log, ...), kept when they are about the component (the plugin's
logger or `plugin` field, the worker's id, the id as a word in the line), plus the component's recent events from the
database (a job that died, a step that failed, a worker that went offline, a control from Helios), so a component that
is stopped or offline still says why. Secrets are stripped and long lines cut before anything leaves argusd.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import logview
from .controls import base_worker

MAX_LINES = 500
MAX_LINE = 400  # characters kept of one line
READ_LINES = 2000  # entries read from each file before filtering
EVENT_LIMIT = 300
LEVELS = ("debug", "info", "warn", "error")

# What a line may carry that must not leave argusd: a token or password in `key=value` / `key: value` / JSON form,
# a bearer header, the one-time `t=` in an approval link, well-known key shapes, and any long secret-looking run.
SECRET_KEYS = r"(?:token|secret|password|passwd|pwd|api[_-]?key|apikey|authorization|cookie|signature|credential)"
KEYED = re.compile(rf"(?i)(\"?[\w.-]*{SECRET_KEYS}\"?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s\"',;&}}\]]+)")
BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}")
LINK_T = re.compile(r"([?&](?:t|key|sig|token|access_token)=)[^&\s\"']+")
SHAPES = re.compile(r"\b(?:ar_[A-Za-z0-9_-]{40,}|sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|"
                    r"xox[abprs]-[A-Za-z0-9-]{10,}|"
                    r"AIza[A-Za-z0-9_-]{30,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+)\b")
LONG_RUN = re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}")


def strip_secrets(text: str, known: list[str] | tuple[str, ...] = ()) -> str:
    """The text without tokens and passwords. `known` are exact secret values from the config (worker token, webhook
    secrets, .env values), blanked wherever they appear."""
    for s in known:
        if s and len(s) >= 6:
            text = text.replace(s, "***")
    text = BEARER.sub(lambda m: f"{m.group(1)} ***", text)
    text = KEYED.sub(lambda m: f"{m.group(1)}***", text)
    text = LINK_T.sub(lambda m: f"{m.group(1)}***", text)
    text = SHAPES.sub("***", text)
    return LONG_RUN.sub("***", text)


SECRET_ENV = re.compile(r"(?i)token|secret|passw|api_?key|_key$|credential|signature")


def known_secrets(cfg: Any) -> list[str]:
    """Exact values to blank: the worker token, the admin password, webhook secrets and the .env entries named like a
    secret (not the rest of .env: a setting such as ARGUS_WORKER_CAPS=laptop must not turn "worker-laptop" to "***")."""
    sec = cfg.secrets
    vals = [sec.worker_token, sec.admin_password, *sec.webhooks.values(),
            *(v for k, v in sec.env.items() if SECRET_ENV.search(k))]
    return sorted({v for v in vals if v and len(v) >= 6}, key=len, reverse=True)


def cut(text: str, limit: int = MAX_LINE) -> str:
    text = " ".join(text.split()) if "\n" in text or "\r" in text else text
    return text if len(text) <= limit else text[: limit - 1] + "…"


def epoch(ts: Any) -> float | None:
    """A log line's timestamp (ISO text, UTC when it has no zone) as epoch seconds."""
    if isinstance(ts, (int, float)):
        return float(ts)
    if not isinstance(ts, str) or not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.timestamp()


def level_of(raw: str) -> str:
    raw = (raw or "info").lower()
    raw = {"warning": "warn", "critical": "error", "fatal": "error", "trace": "debug"}.get(raw, raw)
    return raw if raw in LEVELS else "info"


def word(name: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])")


def about(entry: dict[str, Any], component: str, kind: str, names: set[str]) -> bool:
    """Is this log entry about the component?"""
    extra = entry.get("extra") or {}
    logger = str(entry.get("logger") or "")
    if kind == "plugin":
        if logger == f"argus.plugin.{component}" or logger.startswith(f"argus.plugin.{component}."):
            return True
        if extra.get("plugin") == component:
            return True
    if kind == "worker" and (extra.get("worker") in names or extra.get("worker_id") in names):
        return True
    pat = word(component) if kind != "worker" else re.compile("|".join(word(n).pattern for n in sorted(names)))
    return bool(pat.search(str(entry.get("msg") or ""))) or any(
        isinstance(v, str) and pat.search(v) for v in (extra.get("job"), extra.get("workflow"), extra.get("plugins"),
                                                        extra.get("error")))


def from_files(log_dir: Path, component: str, kind: str, names: set[str], lines: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name, path in logview.sources(log_dir).items():
        if name == "ollama":
            continue
        whole = (kind == "core" and name in ("argus", "argusd-crash")) or (kind == "worker" and name in names)
        try:
            got = logview.read(path, name, lines=READ_LINES)["entries"]
        except OSError:
            continue
        last: float | None = None
        for e in got:
            t = epoch(e.get("ts"))
            if t is None:
                t = last  # a traceback line: the time of the line before it
            else:
                last = t
            if whole or about(e, component, kind, names):
                extra = e.get("extra") or {}
                msg = e["msg"]
                if extra.get("error") and str(extra["error"]) not in msg:
                    msg += f" ({extra['error']})"
                out.append({"ts": t, "level": level_of(e["level"]), "source": name, "msg": msg})
    return out


# ------------------------------------------------------------------ events

PROBLEM_KINDS = ("job.dead", "job.retry", "job.cancelled", "step.failed", "worker.offline", "worker.online",
                 "worker.restarting", "control.", "plugin.settings")


def say(kind: str, d: dict[str, Any], job_id: str | None, step: str | None) -> tuple[str, str]:
    """(level, a sentence) for an event."""
    job = f" (job {job_id})" if job_id else ""
    err = str(d.get("error") or "").strip().splitlines()[0:1]
    err_s = f": {err[0]}" if err else ""
    if kind == "job.dead":
        return "error", f"job failed for good{job}{err_s}" + (f" [{d['workflow']}]" if d.get("workflow") else "")
    if kind == "job.retry":
        return "warn", f"job failed, will retry{job}{err_s}"
    if kind == "job.cancelled":
        return "info", f"job cancelled{job}" + (f": {d['reason']}" if d.get("reason") else "")
    if kind == "step.failed":
        return "error", f"step {step or '?'} failed{job}"
    if kind == "worker.offline":
        return "error", f"worker went offline (silent for {int(d.get('silent_seconds') or 0)} s)"
    if kind == "worker.online":
        return "info", "worker online" + (f" (was {d['was']})" if d.get("was") else "")
    if kind == "worker.restarting":
        return "info", "worker is restarting itself between jobs"
    if kind == "control.paused":
        why = f" ({d['reason']})" if d.get("reason") else ""
        return "info", "paused from Helios" + why
    if kind == "control.resumed":
        return "info", "started again from Helios" if d.get("kind") == "plugin" else "resumed from Helios"
    if kind == "control.restarted":
        extra = f", {len(d['requeued'])} job(s) requeued" if d.get("requeued") else ""
        return "info", "restarted from Helios" + extra
    if kind == "plugin.settings":
        return "info", "settings changed in Helios"
    return "info", kind


def from_events(conn: sqlite3.Connection, names: set[str], since: float | None) -> list[dict[str, Any]]:
    import json

    marks = ",".join("?" * len(names))
    kinds = " OR ".join("kind LIKE ?" for _ in PROBLEM_KINDS)
    args: list[Any] = [*names, *names, *[k + ("%" if k.endswith(".") else "") for k in PROBLEM_KINDS]]
    sql = (f"SELECT kind, job_id, step, data, at FROM events WHERE (from_component IN ({marks}) OR to_component IN "
           f"({marks})) AND ({kinds})")
    if since is not None:
        sql += " AND at > ?"
        args.append(since)
    out = []
    for r in conn.execute(sql + " ORDER BY at DESC, rowid DESC LIMIT ?", (*args, EVENT_LIMIT)):
        d = json.loads(r["data"]) if r["data"] else {}
        level, msg = say(r["kind"], d if isinstance(d, dict) else {}, r["job_id"], r["step"])
        out.append({"ts": r["at"], "level": level, "source": "events", "msg": msg})
    return out


# ------------------------------------------------------------------ the whole answer

class UnknownComponent(Exception):
    pass


def resolve(conn: sqlite3.Connection, plugins: set[str], component: str) -> tuple[str, set[str]]:
    """(kind, the ids it answers to) for "argus", a plugin id or a worker id; UnknownComponent otherwise."""
    if component == "argus":
        return "core", {"argus"}
    workers = [r[0] for r in conn.execute("SELECT id FROM workers")]
    if component in workers or base_worker(component) in workers:
        base = base_worker(component)
        return "worker", {base, f"{base}-now"}
    if component in plugins:
        return "plugin", {component}
    row = conn.execute("SELECT kind FROM components WHERE id = ? AND kind IN ('plugin', 'worker')",
                       (component,)).fetchone()
    if row is not None:
        return row["kind"], {component}
    raise UnknownComponent(component)


def collect(conn: sqlite3.Connection, cfg: Any, plugins: set[str], component: str, lines: int, level: str,
            since: float | None) -> list[dict[str, Any]]:
    kind, names = resolve(conn, plugins, component)
    lines = max(1, min(lines, MAX_LINES))
    rows = from_files(cfg.log_dir, component, kind, names, lines) + from_events(conn, names, since)
    if level == "error":
        rows = [r for r in rows if r["level"] == "error"]
    if since is not None:
        rows = [r for r in rows if r["ts"] is not None and r["ts"] > since]
    rows.sort(key=lambda r: r["ts"] if r["ts"] is not None else 0.0)  # stable: lines of one second keep file order
    secrets = known_secrets(cfg)
    return [{"ts": r["ts"], "level": r["level"], "source": r["source"],
             "msg": cut(strip_secrets(str(r["msg"]), secrets))} for r in rows[-lines:]]
