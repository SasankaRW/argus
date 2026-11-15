"""Models: the call log, breakers, the Claude budget, usage and the Ollama view."""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request

from fastapi import APIRouter, HTTPException, Query

from ... import calls as calls_mod
from ... import usage as usage_mod
from ..schemas import ClaudeAuth, ModelCall, ModelResult
from ._deps import Deps


def build_router(deps: Deps) -> APIRouter:
    argus = deps.argus
    router = APIRouter()
    guarded = deps.guarded

    @router.post("/jobs/{job_id}/calls", dependencies=guarded)
    async def add_model_call(job_id: str, body: dict) -> dict:
        """One model call from a worker (the router): model, prompt, raw reply, verdict, time."""
        m = argus.cfg.models
        if not m.call_log:
            return {"id": None}
        cid = await argus.store.write(lambda c: calls_mod.add_call(
            c, time.time(), job_id, body, keep_days=m.call_log_days, max_mb=m.call_log_mb,
            prompts=m.call_log_prompts))
        return {"id": cid}

    @router.get("/models/calls", dependencies=guarded)
    async def model_calls(limit: int = Query(100, ge=1, le=500), before: int | None = None,
                          model: str | None = None, plugin: str | None = None, verdict: str | None = None,
                          q: str | None = None) -> dict:
        """Model calls, newest first (filters: model or tier, plugin, verdict ok / rejected / error / problems,
        text in the prompt or reply). `before`: the next page."""
        return await argus.store.read(lambda c: {
            "calls": calls_mod.list_calls(c, limit=limit, before=before, model=model, plugin=plugin,
                                          verdict=verdict, q=(q or "").strip() or None),
            **calls_mod.summary(c), "enabled": argus.cfg.models.call_log})

    @router.get("/models/calls/{call_id}", dependencies=guarded)
    async def model_call(call_id: int) -> dict:
        r = await argus.store.read(lambda c: c.execute("SELECT * FROM model_calls WHERE id = ?",
                                                       (call_id,)).fetchone())
        if r is None:
            raise HTTPException(status_code=404, detail="no such call (pruned?)")
        return calls_mod.row_json(r, full=True)

    @router.get("/jobs/{job_id}/calls", dependencies=guarded)
    async def job_calls(job_id: str) -> list[dict]:
        """A job's model calls in order, in full (the Inspector's "Model calls")."""
        return await argus.store.read(lambda c: calls_mod.list_calls(c, job=job_id, limit=200))


    @router.post("/workers/{worker_id}/claude", dependencies=guarded)
    async def worker_claude(worker_id: str, body: ClaudeAuth) -> dict:
        """A worker's `claude auth status`: the phone hears when Claude is logged out."""
        await argus.models.auth_seen(body.logged_in, worker_id, body.detail)
        return {"ok": True}

    @router.get("/models", dependencies=guarded)
    async def models() -> dict:
        return await argus.models.snapshot()

    @router.get("/models/usage", dependencies=guarded)
    async def models_usage(days: int = Query(7, ge=1, le=90)) -> dict:
        """Model calls per tier per day, and per plugin: calls and how often a local model had to hand up."""
        since = time.time() - days * 86400

        def fn(c):
            daily_rows = c.execute(
                "SELECT date(at, 'unixepoch', 'localtime') AS day, to_component AS tier, COUNT(*) AS n FROM events"
                " WHERE kind = 'model.request' AND at >= ? GROUP BY day, tier ORDER BY day", (since,)).fetchall()
            per = c.execute(
                "SELECT COALESCE(j.plugin, e.from_component) AS plugin,"
                " SUM(e.kind = 'model.request') AS calls, SUM(e.kind = 'model.escalated') AS escalations"
                " FROM events e LEFT JOIN jobs j ON j.id = e.job_id"
                " WHERE e.kind IN ('model.request', 'model.escalated') AND e.at >= ?"
                " GROUP BY 1 ORDER BY calls DESC", (since,)).fetchall()
            return {"days": days, "daily": [dict(r) for r in daily_rows], "plugins": [dict(r) for r in per]}

        return await argus.store.read(fn)

    @router.get("/models/usage/monthly", dependencies=guarded)
    async def models_usage_monthly(months: int = Query(12, ge=1, le=usage_mod.MAX_MONTHS)) -> dict:
        """Month by month, per plugin: model calls, hand-ups and job runs. Kept far longer than `/models/usage`."""
        def fn(c):
            usage_mod.rollup(c)
            return usage_mod.monthly(c, months)

        return await argus.store.write(fn)

    @router.get("/models/ollama", dependencies=guarded)
    async def models_ollama() -> dict:
        """The models pulled in Ollama (as argusd sees it; on the laptop the PC's Ollama may be out of reach)."""
        def fetch():
            with urllib.request.urlopen(argus.cfg.ollama.url.rstrip("/") + "/api/tags", timeout=3) as r:
                return json.loads(r.read())

        try:
            tags = await asyncio.to_thread(fetch)
        except Exception as e:  # Ollama off or on another machine
            return {"reachable": False, "error": str(e)[:200], "models": []}
        return {"reachable": True, "models": [
            {"name": m.get("name"), "size_gb": round((m.get("size") or 0) / 1e9, 1),
             "family": (m.get("details") or {}).get("family"),
             "params": (m.get("details") or {}).get("parameter_size")}
            for m in tags.get("models", [])]}

    @router.post("/models/{tier}/permit", dependencies=guarded)
    async def model_permit(tier: str, body: ModelCall) -> dict:
        return (await argus.models.permit(tier, worker=body.worker, job_id=body.job_id)).as_dict()

    @router.post("/models/{tier}/report", dependencies=guarded)
    async def model_report(tier: str, body: ModelResult) -> dict:
        if tier not in argus.cfg.models.tiers:
            raise HTTPException(status_code=404, detail=f"unknown tier {tier}")
        st = await argus.models.report(tier, body.ok, latency_ms=body.latency_ms, error=body.error,
                                       job_id=body.job_id)
        return {"state": st["state"]}

    return router
