"""Argus as an MCP server: Claude (Claude Code, Claude Desktop through a connector) can look into Argus and run its
buttons.

    claude mcp add --transport http argus http://127.0.0.1:8600/mcp --header "Authorization: Bearer <token>"

Streamable HTTP, JSON responses only (no event stream): POST /mcp with JSON-RPC 2.0 (initialize, tools/list,
tools/call, ping). The same token as Helios. What Claude may do is deliberately small:

- read: status and queue, jobs and their steps, logs, approvals waiting, schedules, time saved;
- act: run a plugin's button (what "Sort Downloads now" does), ask Argus/Ari a question.

It can't approve anything (money and deletes stay with you), change settings, power the PC off, or touch files.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .. import ask as ask_mod
from .. import daily, logview

PROTOCOL = "2025-06-18"
KNOWN = ("2025-06-18", "2025-03-26", "2024-11-05")


def _tool(name: str, description: str, props: dict[str, Any] | None = None,
          required: list[str] | None = None) -> dict[str, Any]:
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": props or {}, "required": required or [],
                            "additionalProperties": False}}


TOOLS = [
    _tool("argus_status", "Argus right now: health, workers, what runs, what is queued, what waits for the user."),
    _tool("list_jobs", "Recent jobs, newest first.",
          {"state": {"type": "string", "enum": ["queued", "leased", "running", "waiting", "retry", "succeeded",
                                                "dead", "cancelled"]},
           "plugin": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
    _tool("get_job", "One job: input, steps (with tier used and errors), result.",
          {"id": {"type": "string"}}, ["id"]),
    _tool("read_log", "The last lines of an Argus log (argus, worker, supervisor, ollama, ...).",
          {"name": {"type": "string"}, "lines": {"type": "integer", "minimum": 1, "maximum": 500}}, ["name"]),
    _tool("list_approvals", "What waits for the user's decision (read only: only the user can approve)."),
    _tool("list_schedules", "Schedules: from settings, plugins and the ones made by talking to Ari."),
    _tool("time_saved", "Time the plugins saved the user over the last days.",
          {"days": {"type": "integer", "minimum": 1, "maximum": 90}}),
    _tool("list_buttons", "The plugin buttons Argus can run (ids for run_button)."),
    _tool("run_button", "Run a plugin button now, e.g. run:downloads-organizer:sort. Plugins in dry-run only "
                        "report what they would do.", {"id": {"type": "string"}}, ["id"]),
    _tool("ask_argus", "Ask Argus in plain words (the Ask box): an answer and at most one suggested action "
                       "(never done by this call).", {"text": {"type": "string"}}, ["text"]),
]


class ToolError(Exception):
    pass


def register(app: FastAPI, argus, auth_ok: Callable[[Request], Awaitable[bool]],
             run_button: Callable[[str], Awaitable[dict]], job_json: Callable[..., dict]) -> None:
    async def call(name: str, a: dict[str, Any]) -> Any:
        if name == "argus_status":
            q = await argus.jobs.queue(await argus.registry.workers())
            h = argus.health()
            return {"status": h["status"], "version": h.get("version"), "workers_online": q["workers_online"],
                    "running": [f"{j['plugin']}.{j['workflow']}" for j in q["running"]],
                    "queued": [f"{j['plugin']}.{j['workflow']} ({j.get('why')})" for j in q["queued"]][:20],
                    "waiting": [f"{j['plugin']}.{j['workflow']}" for j in q["waiting"]],
                    "power": argus.power.status()}
        if name == "list_jobs":
            from ..jobs import JobState

            state = JobState(a["state"]) if a.get("state") else None
            jobs = await argus.jobs.list_jobs(state, int(a.get("limit") or 20), a.get("plugin"))
            return [{"id": j.id, "job": f"{j.plugin}.{j.workflow}", "state": j.state.value,
                     "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(j.created_at)),
                     "error": (j.error or "").split("\n")[0][:200] or None} for j in jobs]
        if name == "get_job":
            try:
                return job_json(await argus.jobs.get(str(a["id"])), await argus.jobs.steps(str(a["id"])))
            except Exception:
                raise ToolError(f"no job {a['id']!r}") from None
        if name == "read_log":
            path = logview.sources(argus.cfg.log_dir).get(str(a["name"]))
            if path is None:
                raise ToolError(f"no log {a['name']!r}; there are: {', '.join(logview.sources(argus.cfg.log_dir))}")
            import asyncio

            out = await asyncio.to_thread(logview.read, path, str(a["name"]), lines=int(a.get("lines") or 100))
            return out["entries"]
        if name == "list_approvals":
            return [{"id": x["id"], "plugin": x["plugin"], "title": x["title"], "type": x["type"]}
                    for x in await argus.approvals.list("pending", None, 100)]
        if name == "list_schedules":
            return [{"id": s["id"], "what": s.get("label") or f"{s['plugin']}.{s['workflow']}", "cron": s["cron"],
                     "enabled": s["enabled"], "owner": s.get("owner"),
                     "next": time.strftime("%Y-%m-%d %H:%M", time.localtime(s["next_run_at"]))
                     if s.get("next_run_at") else None} for s in await argus.scheduler.list()]
        if name == "time_saved":
            return await argus.store.read(lambda c: daily.time_saved(c, time.time(), int(a.get("days") or 7)))
        if name == "list_buttons":
            return [b for b in ask_mod.catalog(argus.plugin_host) if b["id"].startswith("run:")]
        if name == "run_button":
            bid = str(a["id"])
            if not bid.startswith("run:"):
                raise ToolError("only plugin buttons (run:...) can be run from here; see list_buttons")
            return await run_button(bid)
        if name == "ask_argus":
            actions = ask_mod.catalog(argus.plugin_host)
            hit = ask_mod.rules(str(a["text"]), actions, await ask_mod.snapshot(argus))
            if hit is None:
                return {"reply": "No instant answer; ask about the queue, failures, approvals, or a button.",
                        "action": None}
            return hit
        raise ToolError(f"unknown tool {name!r}")

    async def handle(msg: dict[str, Any]) -> dict[str, Any] | None:
        mid = msg.get("id")
        method = msg.get("method")

        def ok(result: Any) -> dict[str, Any]:
            return {"jsonrpc": "2.0", "id": mid, "result": result}

        def err(code: int, text: str) -> dict[str, Any]:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}

        if mid is None:  # a notification (initialized, cancelled, ...): nothing to answer
            return None
        if method == "initialize":
            asked = (msg.get("params") or {}).get("protocolVersion")
            return ok({"protocolVersion": asked if asked in KNOWN else PROTOCOL,
                       "capabilities": {"tools": {"listChanged": False}},
                       "serverInfo": {"name": "argus", "version": argus.version},
                       "instructions": "Argus is the user's home automation (jobs, plugins, schedules). Read "
                                       "freely; run_button starts a plugin's job. Approvals are the user's."})
        if method == "ping":
            return ok({})
        if method == "tools/list":
            return ok({"tools": TOOLS})
        if method == "tools/call":
            p = msg.get("params") or {}
            name, args = p.get("name"), p.get("arguments") or {}
            if not any(t["name"] == name for t in TOOLS):
                return err(-32602, f"unknown tool {name!r}")
            try:
                out = await call(str(name), args)
                return ok({"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, default=str,
                                                                            indent=1)}], "isError": False})
            except (ToolError, KeyError, ValueError) as e:
                return ok({"content": [{"type": "text", "text": str(e)}], "isError": True})
            except Exception as e:  # an HTTPException from a shared helper, for example
                detail = getattr(e, "detail", None) or str(e)
                return ok({"content": [{"type": "text", "text": str(detail)}], "isError": True})
        return err(-32601, f"method {method!r} not supported")

    @app.post("/mcp")
    async def mcp(request: Request) -> Response:
        if not await auth_ok(request):
            return JSONResponse({"error": "missing or wrong token"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}},
                                status_code=400)
        if isinstance(body, list):
            out = [r for r in [await handle(m) for m in body if isinstance(m, dict)] if r is not None]
            return JSONResponse(out) if out else Response(status_code=202)
        if not isinstance(body, dict):
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "bad request"}},
                                status_code=400)
        r = await handle(body)
        return JSONResponse(r) if r is not None else Response(status_code=202)

    @app.get("/mcp")
    async def mcp_stream() -> Response:  # no server-sent events: every answer comes in the POST's response
        return Response(status_code=405, headers={"Allow": "POST"})
