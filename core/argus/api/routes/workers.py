"""The worker protocol: register, claim, and a job's start, heartbeat, steps, result and trace."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from ... import guidance
from ...controls import plugin_generations
from ..schemas import Claim, Fail, RegisterWorker, StepReport, Succeed, TraceEvent, Wait, WorkerOnly
from ._deps import Deps, job_json

CONTROL_HEADER = "X-Argus-Control"  # on a 204 claim: "restart" = finish up and exit with code 75 (docs/controls.md)
TRACE_KINDS = ("model.", "check.", "log.", "advice.", "plugin.", "file.", "http.")
CLAIM_POLL = 0.2


def build_router(deps: Deps) -> APIRouter:
    argus = deps.argus
    router = APIRouter()
    guarded = deps.guarded

    async def with_generations(infos: list[dict]) -> list[dict]:
        """Each plugin's restart counter (Helios: Restart), so a worker knows which code it loaded."""
        gens = await argus.store.read(plugin_generations)
        return [{**i, "generation": gens.get(i["id"], 0)} for i in infos]

    @router.post("/workers/register", dependencies=guarded)
    async def register(body: RegisterWorker) -> dict:
        await argus.registry.register_worker(body.id, body.host, body.capabilities, body.version)
        return {"ok": True, "lease_seconds": argus.cfg.jobs.lease_seconds,
                "heartbeat_seconds": argus.cfg.jobs.heartbeat_seconds, "models": argus.models.worker_config(),
                "folders": argus.triggers.folders_for(body.id, body.host, body.capabilities),
                "plugins": await with_generations(argus.plugin_host.for_worker(body.capabilities)),
                "paths": argus.path_rules()}

    @router.get("/workers", dependencies=guarded)
    async def workers() -> list[dict]:
        return await argus.registry.workers()

    @router.post("/workers/{worker_id}/claim", dependencies=guarded)
    async def claim(worker_id: str, body: Claim):
        await argus.registry.touch_worker(worker_id)
        deadline = time.monotonic() + body.wait
        while True:
            job = None
            paused, restart = await argus.controls.worker_gate(worker_id)  # Helios: pause / restart this worker
            if restart and await argus.controls.take_restart(worker_id):
                # No body to carry it (a claim answers with a job or nothing): an older worker sees a plain "no job"
                return Response(status_code=204, headers={CONTROL_HEADER: "restart"})
            if not paused and await argus.jobs.has_claimable():  # a paused worker keeps heartbeating, claims nothing
                job = await argus.jobs.claim(worker_id, body.capabilities, body.plugins, body.min_priority)
            if job is not None:
                out = job_json(job, await argus.jobs.steps(job.id))
                p = argus.plugin_host.plugins.get(job.plugin)
                if p is not None:  # current settings (live, config): argusd may have restarted since register
                    out["plugin_info"] = p.info()
                    out["plugin_info"]["generation"] = (await argus.store.read(plugin_generations)).get(job.plugin, 0)
                    out["plugin_info"]["lessons"] = await argus.store.read(
                        lambda c, pid=job.plugin: guidance.active_lessons(c, pid))
                    if argus.cfg.guidance.examples:
                        out["plugin_info"]["examples"] = await argus.store.read(
                            lambda c, pid=job.plugin: guidance.examples(c, pid))
                elif job.plugin == "ari" and job.workflow == "think":  # Ari's tool picking learns too
                    out["guidance"] = {"lessons": await argus.store.read(
                        lambda c: guidance.active_lessons(c, "ari"))}
                return out
            if time.monotonic() >= deadline:
                return Response(status_code=204)
            await asyncio.sleep(CLAIM_POLL)

    @router.post("/jobs/{job_id}/start", dependencies=guarded)
    async def start(job_id: str, body: WorkerOnly) -> dict:
        return job_json(await argus.jobs.start(job_id, body.worker))

    @router.post("/jobs/{job_id}/heartbeat", dependencies=guarded)
    async def heartbeat(job_id: str, body: WorkerOnly) -> dict:
        until = await argus.jobs.heartbeat(job_id, body.worker)
        await argus.registry.touch_worker(body.worker)
        return {"lease_until": until}

    @router.post("/jobs/{job_id}/steps", dependencies=guarded)
    async def step(job_id: str, body: StepReport) -> dict:
        await argus.jobs.record_step(job_id, body.worker, body.idx, body.name, state=body.state,
                                     output=body.output, error=body.error, tier_used=body.tier)
        return {"ok": True}

    @router.post("/jobs/{job_id}/succeed", dependencies=guarded)
    async def succeed(job_id: str, body: Succeed) -> dict:
        return job_json(await argus.jobs.succeed(job_id, body.worker, body.result))

    @router.post("/jobs/{job_id}/fail", dependencies=guarded)
    async def fail(job_id: str, body: Fail) -> dict:
        return job_json(await argus.jobs.fail(job_id, body.worker, body.error, retryable=body.retryable))

    @router.post("/jobs/{job_id}/events", dependencies=guarded)
    async def trace(job_id: str, body: TraceEvent) -> dict:
        if not body.kind.startswith(TRACE_KINDS):
            raise HTTPException(status_code=422, detail=f"event kind must start with one of {TRACE_KINDS}")
        await argus.jobs.record_event(job_id, body.worker, body.kind, src=body.src, dst=body.dst, step=body.step,
                                      data=body.data)
        return {"ok": True}

    @router.post("/jobs/{job_id}/wait", dependencies=guarded)
    async def wait(job_id: str, body: Wait) -> dict:
        return job_json(await argus.jobs.wait(job_id, body.worker, body.reason))

    return router
