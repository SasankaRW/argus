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
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from ..tools import ToolError, check_args

PROTOCOL = "2025-06-18"
KNOWN = ("2025-06-18", "2025-03-26", "2024-11-05")


def register(app: FastAPI, argus, auth_ok: Callable[[Request], Awaitable[bool]], tools) -> None:
    def listed() -> list:
        return [t for t in tools.all().values() if t.for_mcp and t.fn is not None]  # built-ins only, for now

    async def call(name: str, a: dict[str, Any]) -> Any:
        t = next((t for t in listed() if t.name == name), None)
        if t is None:
            raise ToolError(f"unknown tool {name!r}")
        return await t.fn(check_args(t, a))  # type: ignore[misc]

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
            return ok({"tools": [{"name": t.name, "description": t.description, "inputSchema": t.schema()}
                                 for t in listed()]})
        if method == "tools/call":
            p = msg.get("params") or {}
            name, args = p.get("name"), p.get("arguments") or {}
            if not any(t.name == name for t in listed()):
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
