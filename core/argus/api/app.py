"""The Argus HTTP API.

Public: /, /health, /version.
Token-protected when ARGUS_WORKER_TOKEN is set: everything under /jobs and /workers.

Worker protocol (all JSON):
    POST /workers/register            {id, host, capabilities, version}
    POST /workers/{id}/claim          {capabilities, plugins, wait} -> job with its finished steps, or 204
    POST /jobs/{id}/start             {worker}
    POST /jobs/{id}/heartbeat         {worker}                  -> {lease_until}
    POST /jobs/{id}/steps             {worker, idx, name, state, output, error, tier}
    POST /jobs/{id}/succeed           {worker, result}
    POST /jobs/{id}/fail              {worker, error, retryable}
    POST /jobs/{id}/wait              {worker, reason}
Errors: 404 unknown job, 409 lease lost or transition not allowed, 429 plugin queue full.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hmac
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from ..context import Argus
from ..jobs import InvalidTransition, Job, JobNotFound, JobState, LeaseLost, QueueFull, Step

MAX_CLAIM_WAIT = 30.0
CLAIM_POLL = 0.2


# ------------------------------------------------------------------ request bodies


class SubmitJob(BaseModel):
    plugin: str = Field(min_length=1, max_length=100)
    workflow: str = Field(min_length=1, max_length=100)
    input: dict[str, Any] = Field(default_factory=dict)
    needs: list[str] = Field(default_factory=list)
    priority: int = Field(50, ge=0, le=100)
    dedupe_key: str | None = None
    max_attempts: int | None = Field(None, ge=1, le=20)
    delay: float = Field(0, ge=0)


class RegisterWorker(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    host: str = Field(min_length=1, max_length=100)
    capabilities: list[str] = Field(default_factory=list)
    version: str | None = None


class Claim(BaseModel):
    capabilities: list[str] = Field(default_factory=list)
    plugins: list[str] | None = None
    wait: float = Field(0, ge=0, le=MAX_CLAIM_WAIT)


class WorkerOnly(BaseModel):
    worker: str


class StepReport(BaseModel):
    worker: str
    idx: int = Field(ge=0)
    name: str
    state: str = Field(pattern="^(running|succeeded|failed)$")
    output: Any = None
    error: str | None = None
    tier: str | None = None


class Succeed(BaseModel):
    worker: str
    result: Any = None


class Fail(BaseModel):
    worker: str
    error: str
    retryable: bool = True


class Wait(BaseModel):
    worker: str
    reason: str


# ------------------------------------------------------------------ helpers


def job_json(job: Job, steps: list[Step] | None = None) -> dict[str, Any]:
    d = dataclasses.asdict(job)
    d["state"] = job.state.value
    d["needs"] = list(job.needs)
    if steps is not None:
        d["steps"] = [dataclasses.asdict(s) for s in steps]
    return d


def create_app(argus: Argus) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await argus.start()
        try:
            yield
        finally:
            await argus.stop()

    app = FastAPI(title="Argus", version=argus.version, lifespan=lifespan)
    app.state.argus = argus

    @app.exception_handler(JobNotFound)
    async def _not_found(request: Request, exc: JobNotFound):
        return JSONResponse({"error": "not_found", "detail": f"no job {exc}"}, status_code=404)

    @app.exception_handler(LeaseLost)
    async def _lease_lost(request: Request, exc: LeaseLost):
        return JSONResponse({"error": "lease_lost", "detail": str(exc)}, status_code=409)

    @app.exception_handler(InvalidTransition)
    async def _invalid(request: Request, exc: InvalidTransition):
        return JSONResponse({"error": "invalid_transition", "detail": str(exc)}, status_code=409)

    @app.exception_handler(QueueFull)
    async def _full(request: Request, exc: QueueFull):
        return JSONResponse({"error": "queue_full", "detail": str(exc)}, status_code=429)

    def auth(authorization: str | None = Header(default=None)) -> None:
        token = argus.cfg.secrets.worker_token
        if not token:
            return
        expected = f"Bearer {token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="missing or wrong token")

    guarded = [Depends(auth)]

    # -------------------------------------------------------------- public

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def home() -> str:
        h = argus.health()
        counts = await argus.jobs.counts()
        workers = [w for w in await argus.registry.workers() if w["state"] == "online"]
        dot = "#3DD68C" if h["status"] == "ok" else "#FF7A6B"
        jobs_line = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "no jobs yet"
        workers_line = ", ".join(w["id"] for w in workers) or "none connected"
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Argus</title>
<meta http-equiv="refresh" content="5">
<style>body{{margin:0;background:#0B0D12;color:#E6E8EE;font:15px/1.6 system-ui,sans-serif;padding:40px 24px}}
main{{max-width:560px;margin:0 auto}}h1{{font-size:22px;margin:0 0 4px}}.m{{color:#98A0B3}}
.dot{{display:inline-block;width:9px;height:9px;border-radius:50%;background:{dot};margin-right:8px}}
table{{width:100%;border-collapse:collapse;margin:20px 0}}td{{padding:8px 0;border-bottom:1px solid #1A1F2A}}
td:last-child{{text-align:right;font-family:ui-monospace,monospace}}a{{color:#F5A524}}</style></head>
<body><main><h1>Argus <span class="m">{argus.version}</span></h1>
<div><span class="dot"></span>{h["status"]} &middot; {argus.cfg.instance.name} on {argus.cfg.instance.host}</div>
<table><tr><td>Database</td><td>{h["database"]["database"]}, schema v{h["database"]["schema_version"]}</td></tr>
<tr><td>Writer</td><td>{h["database"]["writer"]}</td></tr>
<tr><td>Watchdog</td><td>{"running" if h["watchdog"]["alive"] else "stopped"}</td></tr>
<tr><td>Workers</td><td>{workers_line}</td></tr>
<tr><td>Jobs</td><td>{jobs_line}</td></tr>
<tr><td>Uptime</td><td>{h["uptime_seconds"]} s</td></tr></table>
<p class="m">Refreshes every 5 s. Helios arrives in core step C6. Raw data: <a href="/health">/health</a> &middot;
<a href="/version">/version</a> &middot; <a href="/docs">API docs</a></p></main></body></html>"""

    @app.get("/health")
    async def health() -> JSONResponse:
        body = argus.health()
        return JSONResponse(body, status_code=200 if body["status"] == "ok" else 503)

    @app.get("/version")
    async def version() -> dict:
        return {"version": argus.version, "schema_version": argus.store.schema_version}

    # -------------------------------------------------------------- jobs (apps, Helios)

    @app.post("/jobs", status_code=201, dependencies=guarded)
    async def submit(body: SubmitJob) -> dict:
        job_id, created = await argus.jobs.enqueue(
            body.plugin, body.workflow, body.input, needs=body.needs, priority=body.priority,
            dedupe_key=body.dedupe_key, max_attempts=body.max_attempts, delay=body.delay,
        )
        return {"id": job_id, "created": created}

    @app.get("/jobs", dependencies=guarded)
    async def list_jobs(state: JobState | None = None, limit: int = 100) -> list[dict]:
        return [job_json(j) for j in await argus.jobs.list_jobs(state, min(max(limit, 1), 500))]

    @app.get("/jobs/counts", dependencies=guarded)
    async def job_counts() -> dict:
        return await argus.jobs.counts()

    @app.get("/jobs/{job_id}", dependencies=guarded)
    async def get_job(job_id: str) -> dict:
        return job_json(await argus.jobs.get(job_id), await argus.jobs.steps(job_id))

    @app.get("/jobs/{job_id}/events", dependencies=guarded)
    async def job_events(job_id: str) -> list[dict]:
        await argus.jobs.get(job_id)
        return await argus.jobs.events(job_id)

    @app.post("/jobs/{job_id}/cancel", dependencies=guarded)
    async def cancel(job_id: str) -> dict:
        return job_json(await argus.jobs.cancel(job_id))

    @app.post("/jobs/{job_id}/rerun", dependencies=guarded)
    async def rerun(job_id: str) -> dict:
        return job_json(await argus.jobs.rerun(job_id))

    @app.post("/jobs/{job_id}/resume", dependencies=guarded)
    async def resume(job_id: str) -> dict:
        return job_json(await argus.jobs.resume(job_id))

    # -------------------------------------------------------------- worker protocol

    @app.post("/workers/register", dependencies=guarded)
    async def register(body: RegisterWorker) -> dict:
        await argus.registry.register_worker(body.id, body.host, body.capabilities, body.version)
        return {"ok": True, "lease_seconds": argus.cfg.jobs.lease_seconds,
                "heartbeat_seconds": argus.cfg.jobs.heartbeat_seconds}

    @app.get("/workers", dependencies=guarded)
    async def workers() -> list[dict]:
        return await argus.registry.workers()

    @app.post("/workers/{worker_id}/claim", dependencies=guarded)
    async def claim(worker_id: str, body: Claim):
        await argus.registry.touch_worker(worker_id)
        deadline = time.monotonic() + body.wait
        while True:
            job = await argus.jobs.claim(worker_id, body.capabilities, body.plugins)
            if job is not None:
                return job_json(job, await argus.jobs.steps(job.id))
            if time.monotonic() >= deadline:
                return Response(status_code=204)
            await asyncio.sleep(CLAIM_POLL)

    @app.post("/jobs/{job_id}/start", dependencies=guarded)
    async def start(job_id: str, body: WorkerOnly) -> dict:
        return job_json(await argus.jobs.start(job_id, body.worker))

    @app.post("/jobs/{job_id}/heartbeat", dependencies=guarded)
    async def heartbeat(job_id: str, body: WorkerOnly) -> dict:
        until = await argus.jobs.heartbeat(job_id, body.worker)
        await argus.registry.touch_worker(body.worker)
        return {"lease_until": until}

    @app.post("/jobs/{job_id}/steps", dependencies=guarded)
    async def step(job_id: str, body: StepReport) -> dict:
        await argus.jobs.record_step(job_id, body.worker, body.idx, body.name, state=body.state,
                                     output=body.output, error=body.error, tier_used=body.tier)
        return {"ok": True}

    @app.post("/jobs/{job_id}/succeed", dependencies=guarded)
    async def succeed(job_id: str, body: Succeed) -> dict:
        return job_json(await argus.jobs.succeed(job_id, body.worker, body.result))

    @app.post("/jobs/{job_id}/fail", dependencies=guarded)
    async def fail(job_id: str, body: Fail) -> dict:
        return job_json(await argus.jobs.fail(job_id, body.worker, body.error, retryable=body.retryable))

    @app.post("/jobs/{job_id}/wait", dependencies=guarded)
    async def wait(job_id: str, body: Wait) -> dict:
        return job_json(await argus.jobs.wait(job_id, body.worker, body.reason))

    return app
