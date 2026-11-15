"""Tools: the list, the self-test, and a job's tool calls."""

from __future__ import annotations

from fastapi import APIRouter

from ...config import PRIORITY_INTERACTIVE
from ..schemas import ToolCall
from ._deps import Deps


def build_router(deps: Deps) -> APIRouter:
    argus = deps.argus
    router = APIRouter()
    guarded = deps.guarded
    tools = deps.tools

    @router.post("/jobs/{job_id}/tools", dependencies=guarded)
    async def job_tool(job_id: str, body: ToolCall) -> dict:
        """ctx.tool() from a worker: a built-in tool's answer now, or a plugin tool's child job (pending until done;
        the calling job then waits and is resumed when the child ends)."""
        return await tools.call_from_job(job_id, body.worker, body.key, body.name, body.args)

    @router.get("/tools", dependencies=guarded)
    async def list_tools() -> list[dict]:
        """Everything Ari can use (built in and from plugins)."""
        return [{**t.brief(), "plugin": t.plugin, "risky": t.risky} for t in tools.all().values() if t.for_ari]

    @router.post("/tools/selftest", dependencies=guarded)
    async def tools_selftest(body: dict | None = None) -> dict:
        """Run each read-only tool once, as Ari would (Helios > Settings). {"deep": true} adds the slow ones."""
        job_id, _ = await argus.jobs.enqueue(
            "ari", "selftest", {"deep": bool((body or {}).get("deep")),
                                "tools": [{**t.brief(), "plugin": t.plugin} for t in tools.all().values()
                                          if t.for_ari and not t.risky]},
            priority=PRIORITY_INTERACTIVE, source="helios")
        return {"job_id": job_id}

    return router
