"""Tracker: Ari talks to your Tracker app (G:\\Projects\\tracker) through its API (header X-API-Key).

Reading needs nothing; adding or moving an issue asks you first. Only the address in the `url` setting can be
reached (permissions.network "config:url").
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from argus.worker import Context, PermanentError, workflow

PLUGIN = "tracker"
STATUSES = ("backlog", "todo", "in_progress", "review", "done")
PRIORITY = {0: "P0", 1: "P1", 2: "P2", 3: "P3"}


def call(ctx: Context, method: str, path: str, body: Any = None) -> Any:
    base = str(ctx.config.get("url") or "http://127.0.0.1:8080").rstrip("/")
    key = ctx.secrets.get("TRACKER_API_KEY")
    if not key:
        raise PermanentError("put Tracker's API_KEY in Argus's .env as TRACKER_API_KEY")
    try:
        status, raw = ctx.http.request(method, base + path, json_body=body,
                                       headers={"X-API-Key": key, "Accept": "application/json"})
    except OSError:
        raise PermanentError(f"Tracker isn't answering at {base} (docker compose up -d in its folder)") from None
    if status in (401, 403):
        raise PermanentError("Tracker refused the key: TRACKER_API_KEY must equal API_KEY in tracker/.env")
    if status >= 400:
        try:
            detail = json.loads(raw).get("detail")
        except (ValueError, AttributeError):
            detail = None
        raise PermanentError(f"Tracker: {detail or f'HTTP {status}'}")
    return json.loads(raw) if raw else None


def brief(t: dict) -> dict:
    return {"key": t.get("key"), "title": t.get("title"), "status": t.get("status"),
            "priority": PRIORITY.get(t.get("priority"), t.get("priority")), "due": t.get("due_date"),
            **({"timer": "running"} if t.get("timer_running") else {})}


def find(ctx: Context, key: str) -> dict:
    key = key.strip().upper()
    for t in call(ctx, "GET", "/api/tickets") or []:
        if str(t.get("key", "")).upper() == key:
            return t
    raise PermanentError(f"no issue {key} in Tracker")


@workflow(PLUGIN, "summary")
def summary(ctx: Context):
    def go():
        s = call(ctx, "GET", "/api/summary") or {}
        return {"open": s.get("open"), "urgent": s.get("urgent"), "overdue": s.get("overdue"),
                "due_this_week": s.get("due_week"), "hours_this_month": s.get("hours_this_month"),
                "timer": s.get("timer"),
                "focus": [{"key": f["key"], "title": f["title"], "status": f["status"],
                           "priority": PRIORITY.get(f.get("priority")), "due": f.get("due_date")}
                          for f in (s.get("focus") or [])[:8]]}

    return ctx.step("summary", go)


@workflow(PLUGIN, "issues")
def issues(ctx: Context):
    status = str(ctx.input.get("status") or "").strip().lower().replace(" ", "_")
    if status and status not in STATUSES:
        raise PermanentError(f"status is one of {', '.join(STATUSES)}")
    project = str(ctx.input.get("project") or "").strip().upper()
    query = str(ctx.input.get("query") or "").strip()

    def go():
        qs = {k: v for k, v in (("status", status), ("q", query)) if v}
        rows = call(ctx, "GET", "/api/tickets" + (f"?{urllib.parse.urlencode(qs)}" if qs else "")) or []
        if not status:
            rows = [t for t in rows if t.get("status") != "done"]
        if project:
            rows = [t for t in rows if str(t.get("key", "")).upper().startswith(project + "-")]
        return {"issues": [brief(t) for t in rows[:25]], "more": max(0, len(rows) - 25)}

    return ctx.step("list", go)


@workflow(PLUGIN, "add")
def add(ctx: Context):
    text = " ".join(str(ctx.input.get("text") or "").split())
    if not text:
        raise PermanentError("what's the issue?")

    def go():
        if ctx.dry_run:
            return {"would_add": text, "dry_run": True}
        t = call(ctx, "POST", "/api/tickets/quick", {"text": text})
        return {"added": brief(t)}

    return ctx.step("add", go)


@workflow(PLUGIN, "move")
def move(ctx: Context):
    status = str(ctx.input.get("status") or "").strip().lower().replace(" ", "_")
    if status not in STATUSES:
        raise PermanentError(f"status is one of {', '.join(STATUSES)}")

    def go():
        t = find(ctx, str(ctx.input.get("key") or ""))
        if ctx.dry_run:
            return {"would_move": t["key"], "to": status, "dry_run": True}
        t = call(ctx, "PATCH", f"/api/tickets/{t['id']}", {"status": status})
        return {"moved": brief(t)}

    return ctx.step("move", go)


@workflow(PLUGIN, "timer")
def timer(ctx: Context):
    key = str(ctx.input.get("key") or "").strip()

    def go():
        if not key:
            if ctx.dry_run:
                return {"would_stop": True, "dry_run": True}
            return {"stopped": call(ctx, "POST", "/api/timer/stop")}
        t = find(ctx, key)
        if ctx.dry_run:
            return {"would_start": t["key"], "dry_run": True}
        call(ctx, "POST", f"/api/tickets/{t['id']}/timer/start")
        return {"started": t["key"], "title": t["title"]}

    return ctx.step("timer", go)
