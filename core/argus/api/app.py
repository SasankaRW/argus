"""The Argus HTTP API.

Public: /, /health, /version, /status.
Token-protected when ARGUS_WORKER_TOKEN is set: /jobs, /workers, /events, /registry, /map and /ws/events
(the WebSocket also accepts ?token=, since browsers cannot set headers on it).

Live events (WebSocket /ws/events?since=<seq>&kinds=job.,worker.&job=<id>):
    server -> {"type": "hello", "seq": N}                      first message; N = newest event now
              {"type": "events", "events": [...], "replay": true}   missed events after `since`, oldest first
              {"type": "events", "events": [...]}              live batches
              {"type": "reset", "reason": ...}                 too far behind: reload /map, then carry on
              {"type": "ping", "seq": N}                       every 20 s when quiet
    Close 4000 = you fell behind: reconnect with since=<last seq you saw>. Close 1001 = Argus is stopping.

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

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from ..context import Argus
from ..events import EventFilter, read_events
from ..jobs import InvalidTransition, Job, JobNotFound, JobState, LeaseLost, QueueFull, Step
from .home import HOME_HTML

MAX_CLAIM_WAIT = 30.0
CLAIM_POLL = 0.2
WS_PING_SECONDS = 20.0
MAX_REPLAY = 10_000


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

    def token_ok(authorization: str | None, query_token: str | None = None) -> bool:
        token = argus.cfg.secrets.worker_token
        if not token:
            return True
        if authorization is not None and hmac.compare_digest(authorization, f"Bearer {token}"):
            return True
        return query_token is not None and hmac.compare_digest(query_token, token)

    def auth(authorization: str | None = Header(default=None)) -> None:
        if not token_ok(authorization):
            raise HTTPException(status_code=401, detail="missing or wrong token")

    guarded = [Depends(auth)]

    # -------------------------------------------------------------- public

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def home() -> str:
        return HOME_HTML

    @app.get("/status")
    async def status() -> dict:
        """What the home page shows. Public: counts and names only, no job contents."""
        h = argus.health()
        workers = await argus.registry.workers()
        m = await argus.registry.map()
        return {
            "status": h["status"], "version": argus.version, "instance": argus.cfg.instance.name,
            "host": argus.cfg.instance.host, "uptime_seconds": h["uptime_seconds"],
            "database": h["database"], "watchdog": h["watchdog"], "events": h["events"],
            "jobs": await argus.jobs.counts(),
            "workers": [{"id": w["id"], "host": w["host"], "state": w["state"]} for w in workers],
            "map": {"nodes": len(m["nodes"]), "edges": len(m["edges"])},
            "token_required": bool(argus.cfg.secrets.worker_token),
        }

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

    # -------------------------------------------------------------- events, registry, map (Helios)

    @app.get("/events", dependencies=guarded)
    async def events(after: int = 0, limit: int = Query(200, ge=1, le=1000), kinds: str | None = None,
                     job: str | None = None) -> dict:
        flt = EventFilter.parse(kinds, job)
        rows = await argus.store.read(lambda c: read_events(c, after, limit=limit, flt=flt))
        return {"events": rows, "seq": argus.hub.cursor}

    @app.get("/registry", dependencies=guarded)
    async def registry() -> dict:
        return {"components": await argus.registry.components(), "workers": await argus.registry.workers()}

    @app.get("/map", dependencies=guarded)
    async def graph() -> dict:
        return await argus.registry.map()

    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket, since: int | None = None, kinds: str | None = None,
                        job: str | None = None, token: str | None = None):
        if not token_ok(ws.headers.get("authorization"), token):
            await ws.close(code=4401, reason="missing or wrong token")
            return
        await ws.accept()
        flt = EventFilter.parse(kinds, job)
        sub = argus.hub.subscribe(flt)
        try:
            await ws.send_json({"type": "hello", "version": argus.version, "seq": sub.after})
            if since is not None:
                cursor, sent = min(max(since, 0), sub.after), 0
                while cursor < sub.after:
                    rows = await argus.store.read(
                        lambda c, a=cursor: read_events(c, a, until=sub.after, limit=500, flt=flt))
                    if not rows:
                        break
                    await ws.send_json({"type": "events", "replay": True, "events": rows})
                    cursor, sent = rows[-1]["seq"], sent + len(rows)
                    if sent >= MAX_REPLAY and cursor < sub.after:
                        await ws.send_json({"type": "reset", "reason": "too far behind; reload the map"})
                        break
            # Wait for events and for the viewer leaving at the same time, so a closed tab (or Argus shutting
            # down) ends this handler at once instead of at the next ping.
            recv = asyncio.ensure_future(ws.receive())
            try:
                while True:
                    get = asyncio.ensure_future(sub.queue.get())
                    done, _ = await asyncio.wait({get, recv}, timeout=WS_PING_SECONDS,
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if recv in done:
                        get.cancel()
                        if recv.result()["type"] == "websocket.disconnect":
                            return
                        recv = asyncio.ensure_future(ws.receive())  # viewers have nothing to say; ignore it
                        continue
                    if get not in done:
                        get.cancel()
                        await ws.send_json({"type": "ping", "seq": argus.hub.cursor})
                        continue
                    batch = get.result()
                    if batch is None:
                        if sub.dropped == "shutdown":
                            await ws.close(code=1001, reason="argus is stopping")
                        else:
                            await ws.close(code=4000, reason="fell behind; reconnect with since")
                        return
                    await ws.send_json({"type": "events", "events": batch})
            finally:
                recv.cancel()
        except (WebSocketDisconnect, RuntimeError, ConnectionError):
            pass  # the viewer went away
        finally:
            argus.hub.unsubscribe(sub)

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
