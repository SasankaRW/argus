"""Tracker: Ari talks to your Tracker app (G:\\Projects\\tracker) through its API (header X-API-Key).

Reading needs nothing; adding or moving an issue asks you first. Only the address in the `url` setting can be
reached (permissions.network "config:url").
"""

from __future__ import annotations

import json
import time
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
    raise PermanentError(f"no issue {key or '(no key given)'} in Tracker")


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


def clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


@workflow(PLUGIN, "show")
def show(ctx: Context):
    key = str(ctx.input.get("key") or "").strip()
    if not key:
        raise PermanentError("which issue? (e.g. ACME-12)")

    def go():
        t = find(ctx, key)
        comments = call(ctx, "GET", f"/api/tickets/{t['id']}/comments") or []
        files = call(ctx, "GET", f"/api/tickets/{t['id']}/attachments") or []
        checklist = t.get("checklist") or []
        return {**brief(t), "type": t.get("type"), "labels": t.get("labels") or [],
                "description": clip(t.get("description"), 1500),
                "checklist": {"done": sum(1 for c in checklist if c.get("done")), "total": len(checklist),
                              "items": [("[x] " if c.get("done") else "[ ] ") + clip(c.get("text"), 120)
                                        for c in checklist[:12]]},
                "comments": [{"at": str(c.get("created_at", ""))[:16], "text": clip(c.get("body"), 400)}
                             for c in comments[-5:]],
                "attachments": [f["filename"] for f in files][:20]}

    return ctx.step("show", go)


@workflow(PLUGIN, "projects")
def projects(ctx: Context):
    def go():
        rows = [p for p in call(ctx, "GET", "/api/projects") or [] if not p.get("archived")]
        return {"projects": [{"key": p["key"], "name": p["name"], "kind": p.get("kind"),
                              "open": p.get("open_count", 0)} for p in rows]}

    return ctx.step("projects", go)


@workflow(PLUGIN, "comment")
def comment(ctx: Context):
    text = str(ctx.input.get("text") or "").strip()
    if not text:
        raise PermanentError("what should the comment say?")

    def go():
        t = find(ctx, str(ctx.input.get("key") or ""))
        if ctx.dry_run:
            return {"would_comment": t["key"], "text": text, "dry_run": True}
        call(ctx, "POST", f"/api/tickets/{t['id']}/comments", {"body": text})
        return {"commented": t["key"]}

    return ctx.step("comment", go)


def changes(inp: dict, labels: list[str]) -> dict:
    """The PATCH body for edit: title, priority (P0-P3 or 0-3), due (YYYY-MM-DD or none), labels to add/remove."""
    out: dict = {}
    if str(inp.get("title") or "").strip():
        out["title"] = str(inp["title"]).strip()[:200]
    pr = str(inp.get("priority") or "").strip().upper().lstrip("P")
    if pr:
        if pr not in ("0", "1", "2", "3"):
            raise PermanentError("priority is P0, P1, P2 or P3")
        out["priority"] = int(pr)
    due = str(inp.get("due") or "").strip().lower()
    if due:
        if due in ("none", "no", "clear", "-"):
            out["due_date"] = None
        elif len(due) == 10 and due[4] == due[7] == "-" and due.replace("-", "").isdigit():
            out["due_date"] = due
        else:
            raise PermanentError("due is a date like 2026-10-31, or 'none'")
    add = [x.strip().lstrip("#").lower() for x in str(inp.get("add_labels") or "").split(",") if x.strip()]
    drop = {x.strip().lstrip("#").lower() for x in str(inp.get("remove_labels") or "").split(",") if x.strip()}
    if add or drop:
        out["labels"] = [x for x in labels if x.lower() not in drop] + [x for x in add if x not in labels]
    if not out:
        raise PermanentError("nothing to change: title, priority, due or labels")
    return out


@workflow(PLUGIN, "edit")
def edit(ctx: Context):
    def go():
        t = find(ctx, str(ctx.input.get("key") or ""))
        body = changes(ctx.input, list(t.get("labels") or []))
        if ctx.dry_run:
            return {"would_change": t["key"], "changes": body, "dry_run": True}
        return {"changed": brief(call(ctx, "PATCH", f"/api/tickets/{t['id']}", body)), "fields": sorted(body)}

    return ctx.step("edit", go)


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


@workflow(PLUGIN, "check")
def check(ctx: Context):
    """Issues that just became overdue: one phone message each (important, so Ari may also say it at the PC)."""
    today = time.strftime("%Y-%m-%d")

    def go():
        s = call(ctx, "GET", "/api/summary") or {}
        overdue = [f for f in s.get("focus") or [] if f.get("due_date") and str(f["due_date"]) < today
                   and f.get("status") != "done"]
        told = ctx.store.get("told") or {}
        fresh = [f for f in overdue if f["key"] not in told]
        for f in fresh:
            told[f["key"]] = today
        if fresh and not ctx.dry_run:
            names = "; ".join(f"{f['key']} {f['title']}" for f in fresh[:3])
            ctx.notify("Overdue in Tracker" if len(fresh) == 1 else f"{len(fresh)} issues overdue in Tracker",
                       names + ("…" if len(fresh) > 3 else ""), priority="high", tags=["alarm_clock"])
        keep = {f["key"] for f in overdue}
        ctx.store.set("told", {k: v for k, v in told.items() if k in keep})  # done ones can be told again later
        return {"overdue": [f["key"] for f in overdue], "told_now": [f["key"] for f in fresh]}

    return ctx.step("check", go)
