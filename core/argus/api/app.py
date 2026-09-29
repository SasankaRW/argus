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
    POST /jobs/{id}/approvals         {worker, key, type, title, fields, items, summary, link, step}
                                      -> the approval (pending, or already decided)
    POST /jobs/{id}/notify            {worker, key, title, text, priority, tags, link} -> {queued}

Approvals (C8):
    GET  /approvals?state=pending     list (token)
    GET  /approvals/{id}              one (token)
    POST /approvals/{id}/decide       {answer: approve|reject, fields, by}; either the Argus token, or ?t=<signed
                                      one-time token>&answer=... from the phone buttons (no body needed)
    GET  /a/{id}?t=<token>            the phone page behind the notification's Open button
    GET  /outbox                      recent outgoing messages and counts (token)
    POST /outbox/test                 send a test notification (token)

Scheduler and triggers (C9):
    GET  /schedules                   schedules with their next run (token)
    POST /schedules/{id}/run          run a schedule now (token)
    POST /triggers/file               {worker, trigger, path, sha256, size} from a worker's folder watcher (token)
                                      -> {status: queued|duplicate, job_id}; 429 when the plugin's queue is full
    POST /hooks/{name}                a signed webhook (no Argus token; the signature is the check)
Errors: 404 unknown job, 409 lease lost or transition not allowed, 429 plugin queue full.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hmac
import json
import mimetypes
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import ask as ask_mod
from .. import logview
from ..approvals import ApprovalClosed, ApprovalError, ApprovalNotFound, BadToken
from ..config import PRIORITY_INTERACTIVE
from ..context import Argus
from ..events import EventFilter, insert_event, read_events
from ..jobs import InvalidTransition, Job, JobNotFound, JobState, LeaseLost, QueueFull, Step
from ..outbox import PRIORITIES, add_message, ntfy_message
from ..power import PowerError
from ..shares import ShareError, ShareStore, kinds_of
from ..triggers import BadSignature, TriggerError, UnknownTrigger
from . import approval_page
from .home import HOME_HTML

mimetypes.add_type("application/manifest+json", ".webmanifest")  # Helios as an installable app (share menu)
HELIOS_DIR = Path(__file__).resolve().parent.parent / "helios_dist"  # built by helios/ (npm run build)


class _HeliosFiles(StaticFiles):
    """Hashed assets cache forever; index.html must be re-checked so a new release shows up on reload."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if path.replace("\\", "/").startswith("assets/"):  # Starlette passes an OS path (assets\\x.js on Windows)
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            resp.headers["Cache-Control"] = "no-cache"
        return resp
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
    model: str | None = Field(None, max_length=100)  # groups GPU jobs by model (fewer swaps)
    window: str | None = Field(None, max_length=40)  # only start inside this window (e.g. night)


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


TRACE_KINDS = ("model.", "check.", "log.", "advice.", "plugin.", "file.", "http.")


class NewShare(BaseModel):
    title: str = Field("", max_length=300)
    text: str = Field("", max_length=20000)
    url: str = Field("", max_length=2000)
    note: str = Field("", max_length=2000)


class SendShare(BaseModel):
    plugin: str
    workflow: str


class RulesText(BaseModel):
    text: str = Field(max_length=200_000)


class Ask(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class AskDo(BaseModel):
    action: str = Field(min_length=1, max_length=200)


class Fix(BaseModel):
    value: str = Field(min_length=1, max_length=200)  # the right answer, from the Wrong button's choices


class TraceEvent(BaseModel):
    worker: str
    kind: str = Field(min_length=3, max_length=60)
    src: str | None = Field(None, max_length=100)
    dst: str | None = Field(None, max_length=100)
    step: str | None = Field(None, max_length=100)
    data: dict[str, Any] | None = None


class RunPlugin(BaseModel):
    workflow: str | None = Field(None, max_length=100)  # default: the plugin's first manual trigger
    input: dict[str, Any] = Field(default_factory=dict)


class StateValue(BaseModel):
    value: Any = None


class FileTrigger(BaseModel):
    worker: str = Field(min_length=1, max_length=100)
    trigger: str = Field(min_length=1, max_length=63)
    path: str = Field(min_length=1, max_length=2000)
    sha256: str = Field(min_length=64, max_length=64)
    size: int = Field(ge=0)


class AskApproval(BaseModel):
    worker: str
    key: str = Field(min_length=1, max_length=200)
    type: str = Field(pattern="^(entry|batch|draft)$")
    title: str = Field(min_length=1, max_length=200)
    fields: dict[str, Any] = Field(default_factory=dict)
    items: list[dict[str, Any]] = Field(default_factory=list, max_length=5000)
    summary: list[str] = Field(default_factory=list, max_length=50)
    link: str | None = Field(None, max_length=2000)
    step: str | None = Field(None, max_length=100)


class Notify(BaseModel):
    worker: str
    key: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    text: str = Field("", max_length=3500)
    priority: str = "default"
    tags: list[str] = Field(default_factory=list, max_length=5)
    link: str | None = Field(None, max_length=2000)


class Decide(BaseModel):
    answer: str | None = Field(None, pattern="^(approve|reject)$")
    fields: dict[str, Any] | None = None
    by: str | None = Field(None, max_length=60)


class ModelCall(BaseModel):
    worker: str | None = None
    job_id: str | None = None


class ModelResult(BaseModel):
    worker: str | None = None
    job_id: str | None = None
    ok: bool
    latency_ms: float | None = Field(None, ge=0)
    error: str | None = Field(None, max_length=2000)


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

    @app.exception_handler(ApprovalNotFound)
    async def _no_approval(request: Request, exc: ApprovalNotFound):
        return JSONResponse({"error": "not_found", "detail": f"no approval {exc}"}, status_code=404)

    @app.exception_handler(ApprovalClosed)
    async def _closed(request: Request, exc: ApprovalClosed):
        return JSONResponse({"error": "closed", "state": exc.state, "detail": str(exc)}, status_code=409)

    @app.exception_handler(BadToken)
    async def _bad_token(request: Request, exc: BadToken):
        return JSONResponse({"error": "bad_token", "detail": str(exc)}, status_code=403)

    @app.exception_handler(ApprovalError)
    async def _approval_error(request: Request, exc: ApprovalError):
        return JSONResponse({"error": "invalid", "detail": str(exc)}, status_code=422)

    @app.exception_handler(UnknownTrigger)
    async def _no_trigger(request: Request, exc: UnknownTrigger):
        return JSONResponse({"error": "not_found", "detail": str(exc)}, status_code=404)

    @app.exception_handler(BadSignature)
    async def _bad_sig(request: Request, exc: BadSignature):
        return JSONResponse({"error": "bad_signature", "detail": str(exc)}, status_code=401)

    @app.exception_handler(TriggerError)
    async def _trigger_error(request: Request, exc: TriggerError):
        return JSONResponse({"error": "invalid", "detail": str(exc)}, status_code=422)

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

    @app.get("/", include_in_schema=False)
    async def home(request: Request):
        """Helios when it is built in; otherwise the small status page. Keeps ?token= and friends."""
        if (HELIOS_DIR / "index.html").exists():
            q = request.url.query
            return RedirectResponse("/helios/" + (f"?{q}" if q else ""))
        return HTMLResponse(HOME_HTML)

    @app.get("/lite", response_class=HTMLResponse, include_in_schema=False)
    async def lite() -> str:
        """The small status page, always available."""
        return HOME_HTML

    if (HELIOS_DIR / "index.html").exists():
        app.mount("/helios", _HeliosFiles(directory=HELIOS_DIR, html=True), name="helios")
    else:
        @app.get("/helios", include_in_schema=False)
        @app.get("/helios/", include_in_schema=False)
        async def helios_missing():
            return HTMLResponse("<p>Helios is not built into this copy of Argus. Run <code>.\\scripts\\dev.ps1 "
                                "helios</code> or use <a href='/lite'>/lite</a>.</p>", status_code=404)

    status_cache: dict[str, Any] = {"at": 0.0, "body": None}

    @app.get("/status")
    async def status() -> dict:
        """What the home page shows. Public: counts and names only, no job contents. Cached for 2 s, since
        every open page asks every few seconds and nobody needs a token to call it."""
        if status_cache["body"] is not None and time.monotonic() - status_cache["at"] < 2.0:
            return status_cache["body"]
        body = await _status()
        status_cache.update(at=time.monotonic(), body=body)
        return body

    async def _status() -> dict:
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
            body.plugin, body.workflow, body.input, needs=[*body.needs, *argus.plugin_host.needs_for(body.plugin)],
            priority=body.priority,
            dedupe_key=body.dedupe_key, max_attempts=body.max_attempts, delay=body.delay,
            model_group=body.model, window=body.window,
        )
        return {"id": job_id, "created": created}

    @app.get("/jobs", dependencies=guarded)
    async def list_jobs(state: JobState | None = None, limit: int = 100, plugin: str | None = None,
                        before: float | None = None) -> list[dict]:
        return [job_json(j) for j in await argus.jobs.list_jobs(state, min(max(limit, 1), 500), plugin, before)]

    @app.get("/queue", dependencies=guarded)
    async def queue() -> dict:
        """Running, waiting for you, and queued in the order they will run, each with why it waits."""
        return await argus.jobs.queue(await argus.registry.workers())

    @app.get("/jobs/counts", dependencies=guarded)
    async def job_counts() -> dict:
        return await argus.jobs.counts()

    @app.get("/jobs/{job_id}", dependencies=guarded)
    async def get_job(job_id: str) -> dict:
        return job_json(await argus.jobs.get(job_id), await argus.jobs.steps(job_id))

    async def _changes(job_id: str) -> tuple[Any, list[dict]]:
        """A job's file changes (the undo log), each with the undo job made for it, if any."""
        job = await argus.jobs.get(job_id)
        evs = [e for e in await argus.jobs.events(job_id) if e["kind"].startswith("file.")]

        def undos(conn):
            keys = [f"undo:{e['id']}" for e in evs]
            if not keys:
                return {}
            keys += [f"fix:{e['id']}" for e in evs]
            rows = conn.execute("SELECT id, state, dedupe_key, input FROM jobs WHERE dedupe_key IN ({})"
                                " ORDER BY created_at".format(",".join("?" * len(keys))), keys).fetchall()
            out: dict[str, dict] = {}
            for r in rows:  # latest wins
                what, eid = r["dedupe_key"].split(":", 1)
                out[f"{what}:{eid}"] = {"job_id": r["id"], "state": r["state"],
                                        "value": json.loads(r["input"] or "{}").get("value")}
            return out

        done = await argus.store.read(undos)
        p = argus.plugin_host.plugins.get(job.plugin)
        can = p is not None and "undo" in p.manifest.all_workflows()
        wrong = p is not None and p.manifest.helios.wrong is not None
        out = []
        for e in evs:
            d = e["data"] or {}
            u, f = done.get(f"undo:{e['id']}"), done.get(f"fix:{e['id']}")
            real = e["kind"] == "file.moved" and not d.get("dry_run")
            free = all(x is None or x["state"] in ("dead", "cancelled") for x in (u, f))
            out.append({"event_id": e["id"], "kind": e["kind"], "at": e["at"], "from": d.get("from"),
                        "to": d.get("to"), "path": d.get("path"), "dry_run": bool(d.get("dry_run")), "undo": u,
                        "fix": f, "can_undo": can and real and free, "can_fix": wrong and real and free})
        return job, out

    @app.post("/jobs/{job_id}/changes/{event_id}/wrong", dependencies=guarded)
    async def wrong_change(job_id: str, event_id: str, body: Fix) -> dict:
        """The "Wrong" button: the plugin's correction workflow puts the file where it belongs and remembers it."""
        job, changes = await _changes(job_id)
        c = next((x for x in changes if x["event_id"] == event_id), None)
        if c is None:
            raise HTTPException(status_code=404, detail="no such change in this job")
        if not c["can_fix"]:
            if c["fix"]:
                return {"id": c["fix"]["job_id"], "created": False}
            raise HTTPException(status_code=409, detail="this change can't be corrected (dry-run, already undone, "
                                                        "or the plugin has no Wrong button)")
        p = argus.plugin_host.plugins[job.plugin]
        wf = p.manifest.helios.wrong.workflow  # type: ignore[union-attr]
        job_id2, created = await argus.jobs.enqueue(
            job.plugin, wf, {"from": c["to"], "orig": c["from"], "value": body.value, "fix_of": event_id,
                             "job": job_id},
            needs=p.manifest.job_needs(), priority=PRIORITY_INTERACTIVE, dedupe_key=f"fix:{event_id}",
            source="helios")
        return {"id": job_id2, "created": created}

    # -------------------------------------------------------------- Ask Argus

    @app.post("/ask", dependencies=guarded)
    async def ask(body: Ask) -> dict:
        """Plain words -> an answer and at most one suggested action (never done without POST /ask/do).
        Common asks are answered at once from data (rules); the rest go to a model as a job (poll its result)."""
        actions = ask_mod.catalog(argus.plugin_host)
        snap = await ask_mod.snapshot(argus)
        hit = ask_mod.rules(body.text, actions, snap)
        labels = {a["id"]: a["label"] for a in actions}
        if hit is not None:
            return {"via": "rules", **hit, "label": labels.get(hit.get("action") or "")}
        job_id, _ = await argus.jobs.enqueue(
            "ask", "ask", {"text": body.text, "actions": actions, "snapshot": snap},
            priority=PRIORITY_INTERACTIVE, source="helios")
        return {"via": "model", "job_id": job_id}

    @app.post("/ask/do", dependencies=guarded)
    async def ask_do(body: AskDo) -> dict:
        """Do a suggested action (you tapped it)."""
        a = body.action
        if a.startswith("show:"):
            return {"view": a[5:]}
        if a.startswith("power:"):
            return {"power": await power_action(a[6:])}
        if a.startswith("run:"):
            _, pid, wf = (a.split(":", 2) + ["", ""])[:3]
            p = argus.plugin_host.plugins.get(pid)
            if p is None or wf not in [t.manual.workflow for t in p.manifest.triggers if t.manual]:
                raise HTTPException(status_code=404, detail=f"no button {a!r}")
            job_id, created = await argus.jobs.enqueue(pid, wf, {}, needs=p.manifest.job_needs(),
                                                       priority=PRIORITY_INTERACTIVE, source="helios")
            return {"job_id": job_id, "created": created}
        raise HTTPException(status_code=404, detail=f"unknown action {a!r}")

    # -------------------------------------------------------------- plugin rules (read and edit in Helios)

    def rules_of(pid: str):
        p = argus.plugin_host.plugins.get(pid)
        if p is None or p.manifest.helios.rules is None:
            raise HTTPException(status_code=404, detail=f"{pid} has no editable rules")
        return p, p.path / p.manifest.helios.rules.file

    async def state_get(pid: str, key: str):
        def fn(conn):
            row = conn.execute("SELECT value FROM plugin_state WHERE plugin = ? AND key = ?", (pid, key)).fetchone()
            return json.loads(row[0]) if row and row[0] is not None else None
        return await argus.store.read(fn)

    async def state_set(pid: str, key: str, value) -> None:
        def fn(conn):
            now = time.time()
            conn.execute("INSERT INTO plugin_state (plugin, key, value, created_at, updated_at) VALUES (?,?,?,?,?)"
                         " ON CONFLICT(plugin, key) DO UPDATE SET value = excluded.value,"
                         " updated_at = excluded.updated_at", (pid, key, json.dumps(value), now, now))
        await argus.store.write(fn)

    @app.get("/plugins/{pid}/rules", dependencies=guarded)
    async def get_rules(pid: str) -> dict:
        p, f = rules_of(pid)
        default = f.read_text(encoding="utf-8") if f.exists() else ""
        mine = await state_get(pid, "rules")
        return {"label": p.manifest.helios.rules.label, "file": f.name, "text": mine or default,  # type: ignore
                "default": default, "custom": bool(mine), "learned": await state_get(pid, "learned")}

    @app.put("/plugins/{pid}/rules", dependencies=guarded)
    async def put_rules(pid: str, body: RulesText) -> dict:
        """Save your version. Checked here for YAML; the plugin checks the meaning at its next job."""
        rules_of(pid)
        try:
            parsed = yaml.safe_load(body.text)
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            where = f" (line {mark.line + 1})" if mark else ""
            raise HTTPException(status_code=422, detail=f"not valid YAML{where}: {getattr(e, 'problem', e)}") from None
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=422, detail="the rules must be sections like  name: ...")
        await state_set(pid, "rules", body.text)
        return {"ok": True}

    @app.delete("/plugins/{pid}/rules", dependencies=guarded)
    async def reset_rules(pid: str) -> dict:
        rules_of(pid)
        await state_set(pid, "rules", None)
        return {"ok": True}

    # -------------------------------------------------------------- share (the phone's share menu)

    shares = ShareStore(argus.cfg.db_path.parent / "shares", argus.cfg.share.max_mb, argus.cfg.share.keep_days)

    def share_targets() -> list[dict]:
        return [{"plugin": pid, "name": p.manifest.name, **t.model_dump()}
                for pid, p in sorted(argus.plugin_host.plugins.items()) for t in p.manifest.share]

    def share_or_404(fn, *a):
        try:
            return fn(*a)
        except ShareError as e:
            raise HTTPException(status_code=404 if "no such" in str(e) or "bad" in str(e) else 422,
                                detail=str(e)) from None

    @app.get("/share/targets", dependencies=guarded)
    async def get_share_targets() -> list[dict]:
        return share_targets()

    @app.post("/shares", dependencies=guarded)
    async def new_share(body: NewShare) -> dict:
        return await asyncio.to_thread(shares.create, body.title, body.text, body.url, body.note)

    @app.put("/shares/{sid}/files", dependencies=guarded)
    async def add_share_file(sid: str, request: Request, name: str = Query(..., max_length=200),
                             type: str = Query("application/octet-stream", max_length=100)) -> dict:
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > shares.max_bytes:
                raise HTTPException(status_code=413, detail=f"too big: shares are limited to "
                                                            f"{shares.max_bytes // (1024 * 1024)} MB")
        return await asyncio.to_thread(share_or_404, shares.add_file, sid, name, type, bytes(data))

    @app.get("/shares/{sid}", dependencies=guarded)
    async def get_share(sid: str) -> dict:
        return share_or_404(shares.meta, sid)

    @app.get("/shares/{sid}/files/{name}", dependencies=guarded)
    async def get_share_file(sid: str, name: str):
        path = share_or_404(shares.file_path, sid, name)
        meta = shares.meta(sid)
        mime = next((f["type"] for f in meta["files"] if f["name"] == name), "application/octet-stream")
        return FileResponse(path, media_type=mime or "application/octet-stream")

    @app.post("/shares/{sid}/send", dependencies=guarded)
    async def send_share(sid: str, body: SendShare) -> dict:
        """Queue the share for the plugin you picked (interactive priority)."""
        meta = share_or_404(shares.meta, sid)
        if meta["job_id"]:
            return {"id": meta["job_id"], "created": False}
        t = next((x for x in share_targets() if x["plugin"] == body.plugin and x["workflow"] == body.workflow), None)
        if t is None:
            raise HTTPException(status_code=422, detail=f"{body.plugin}.{body.workflow} does not take shares")
        have = kinds_of(meta)
        if not have:
            raise HTTPException(status_code=422, detail="nothing to send: add a file, a link or some text")
        if not have & set(t["accepts"]):
            raise HTTPException(status_code=422, detail=f"{t['label']} takes {', '.join(t['accepts'])}")
        p = argus.plugin_host.plugins[body.plugin]
        job_id, created = await argus.jobs.enqueue(
            body.plugin, body.workflow,
            {"share": sid, "title": meta["title"], "text": meta["text"], "url": meta["url"], "note": meta["note"],
             "files": meta["files"]},
            needs=p.manifest.job_needs(), priority=PRIORITY_INTERACTIVE, dedupe_key=f"share:{sid}", source="helios")
        await asyncio.to_thread(shares.mark_sent, sid, job_id)
        return {"id": job_id, "created": created}

    # -------------------------------------------------------------- backups

    @app.get("/backups", dependencies=guarded)
    async def backups() -> dict:
        return {"files": argus.backups.list(), "last": argus.last_backup, "at": argus.cfg.backup.at,
                "keep": argus.cfg.backup.keep, "copy_to": argus.cfg.backup.copy_to,
                "enabled": argus.cfg.backup.enabled}

    @app.post("/backups", dependencies=guarded)
    async def backup_now() -> dict:
        return await argus.backup_now()

    @app.get("/backups/files/{name}", dependencies=guarded)
    async def backup_file(name: str):
        if not re.fullmatch(r"argus-\d{8}-\d{6}\.db", name) or not (argus.backups.dir / name).is_file():
            raise HTTPException(status_code=404, detail="no such backup")
        return FileResponse(argus.backups.dir / name, media_type="application/octet-stream", filename=name)

    @app.get("/logs", dependencies=guarded)
    async def log_sources() -> list[dict]:
        """The log files Helios can show: argusd, the worker(s) on this machine, Ollama."""
        out = []
        for name, path in logview.sources(argus.cfg.log_dir).items():
            try:
                st = path.stat()
            except OSError:
                continue
            out.append({"name": name, "size": st.st_size, "modified": st.st_mtime})
        return out

    @app.get("/logs/{name}", dependencies=guarded)
    async def log_read(name: str, after: int | None = None, lines: int = Query(300, ge=1, le=2000)) -> dict:
        path = logview.sources(argus.cfg.log_dir).get(name)
        if path is None:
            raise HTTPException(status_code=404, detail=f"no log {name!r}")
        return await asyncio.to_thread(logview.read, path, name, after=after, lines=lines)

    @app.get("/jobs/{job_id}/changes", dependencies=guarded)
    async def job_changes(job_id: str) -> list[dict]:
        return (await _changes(job_id))[1]

    @app.post("/jobs/{job_id}/changes/{event_id}/undo", dependencies=guarded)
    async def undo_change(job_id: str, event_id: str) -> dict:
        """Put one moved file back, through the plugin's own `undo` workflow (so its permissions still apply)."""
        job, changes = await _changes(job_id)
        c = next((x for x in changes if x["event_id"] == event_id), None)
        if c is None:
            raise HTTPException(status_code=404, detail="no such change in this job")
        if not c["can_undo"]:
            if c["undo"]:
                return {"id": c["undo"]["job_id"], "created": False}
            raise HTTPException(status_code=409, detail="this change can't be undone (dry-run, not a move, "
                                                        "or the plugin has no undo workflow)")
        p = argus.plugin_host.plugins[job.plugin]
        job_id2, created = await argus.jobs.enqueue(
            job.plugin, "undo", {"from": c["to"], "to": c["from"], "undo_of": event_id, "job": job_id},
            needs=p.manifest.job_needs(), priority=PRIORITY_INTERACTIVE, dedupe_key=f"undo:{event_id}",
            source="helios")
        return {"id": job_id2, "created": created}

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
                     job: str | None = None, component: str | None = None, newest: bool = False) -> dict:
        """Event history. `newest=true` returns the latest `limit` matches (oldest first)."""
        flt = EventFilter.parse(kinds, job, component)
        rows = await argus.store.read(lambda c: read_events(c, after, limit=limit, flt=flt, newest=newest))
        return {"events": rows, "seq": argus.hub.cursor}

    @app.get("/registry", dependencies=guarded)
    async def registry() -> dict:
        return {"components": await argus.registry.components(), "workers": await argus.registry.workers()}

    @app.get("/map", dependencies=guarded)
    async def graph() -> dict:
        return await argus.registry.map()

    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket, since: int | None = None, kinds: str | None = None,
                        job: str | None = None, component: str | None = None, token: str | None = None):
        if not token_ok(ws.headers.get("authorization"), token):
            await ws.close(code=4401, reason="missing or wrong token")
            return
        await ws.accept()
        flt = EventFilter.parse(kinds, job, component)
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
                "heartbeat_seconds": argus.cfg.jobs.heartbeat_seconds, "models": argus.models.worker_config(),
                "folders": argus.triggers.folders_for(body.id, body.host, body.capabilities),
                "plugins": argus.plugin_host.for_worker(body.capabilities), "paths": argus.path_rules()}

    @app.get("/workers", dependencies=guarded)
    async def workers() -> list[dict]:
        return await argus.registry.workers()

    @app.post("/workers/{worker_id}/claim", dependencies=guarded)
    async def claim(worker_id: str, body: Claim):
        await argus.registry.touch_worker(worker_id)
        deadline = time.monotonic() + body.wait
        while True:
            job = None
            if await argus.jobs.has_claimable():
                job = await argus.jobs.claim(worker_id, body.capabilities, body.plugins)
            if job is not None:
                out = job_json(job, await argus.jobs.steps(job.id))
                p = argus.plugin_host.plugins.get(job.plugin)
                if p is not None:  # current settings (live, config): argusd may have restarted since register
                    out["plugin_info"] = p.info()
                return out
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

    @app.post("/jobs/{job_id}/events", dependencies=guarded)
    async def trace(job_id: str, body: TraceEvent) -> dict:
        if not body.kind.startswith(TRACE_KINDS):
            raise HTTPException(status_code=422, detail=f"event kind must start with one of {TRACE_KINDS}")
        await argus.jobs.record_event(job_id, body.worker, body.kind, src=body.src, dst=body.dst, step=body.step,
                                      data=body.data)
        return {"ok": True}

    # -------------------------------------------------------------- models (breakers, Claude budget)

    @app.get("/models", dependencies=guarded)
    async def models() -> dict:
        return await argus.models.snapshot()

    @app.post("/models/{tier}/permit", dependencies=guarded)
    async def model_permit(tier: str, body: ModelCall) -> dict:
        return (await argus.models.permit(tier, worker=body.worker, job_id=body.job_id)).as_dict()

    @app.post("/models/{tier}/report", dependencies=guarded)
    async def model_report(tier: str, body: ModelResult) -> dict:
        if tier not in argus.cfg.models.tiers:
            raise HTTPException(status_code=404, detail=f"unknown tier {tier}")
        st = await argus.models.report(tier, body.ok, latency_ms=body.latency_ms, error=body.error,
                                       job_id=body.job_id)
        return {"state": st["state"]}

    # -------------------------------------------------------------- approvals and notifications (C8)

    @app.post("/jobs/{job_id}/approvals", dependencies=guarded)
    async def ask_approval(job_id: str, body: AskApproval) -> dict:
        a, created = await argus.approvals.request(
            job_id, body.worker, body.key, body.type, body.title, fields=body.fields, items=body.items,
            summary=body.summary, link=body.link, step=body.step)
        if created:
            argus.outbox.poke()
        return {**a, "created": created}

    @app.post("/jobs/{job_id}/notify", dependencies=guarded)
    async def notify(job_id: str, body: Notify) -> dict:
        if body.priority not in PRIORITIES:
            raise HTTPException(status_code=422, detail=f"priority must be one of {', '.join(PRIORITIES)}")
        msg = ntfy_message(body.title, body.text, priority=body.priority, tags=body.tags, click=body.link)
        return {"queued": await argus.notify_from_job(job_id, body.worker, body.key, msg)}

    @app.get("/approvals", dependencies=guarded)
    async def list_approvals(state: str | None = None, job: str | None = None, limit: int = 100) -> list[dict]:
        return await argus.approvals.list(state, job, min(max(limit, 1), 500))

    @app.get("/approvals/{approval_id}", dependencies=guarded)
    async def get_approval(approval_id: str) -> dict:
        return await argus.approvals.get(approval_id)

    @app.post("/approvals/{approval_id}/decide")
    async def decide(approval_id: str, request: Request, t: str | None = None, answer: str | None = None,
                     authorization: str | None = Header(default=None)) -> dict:
        """Helios and apps send the Argus token; the phone buttons send ?t= (signed, one-time) instead."""
        raw = await request.body()
        try:
            body = Decide.model_validate_json(raw) if raw.strip() else Decide()
        except ValueError as e:
            raise HTTPException(status_code=422, detail=f"bad body: {e}") from None
        ans = body.answer or answer
        if ans not in ("approve", "reject"):
            raise HTTPException(status_code=422, detail="answer must be approve or reject")
        if t is None and not token_ok(authorization):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        by = body.by or ("phone" if t is not None else "helios")
        a = await argus.approvals.decide(approval_id, ans, fields=body.fields, by=by, token=t)
        return {"id": a["id"], "state": a["state"], "answer": a["answer"]}

    @app.get("/a/{approval_id}", response_class=HTMLResponse, include_in_schema=False)
    async def approval_page_view(approval_id: str, t: str = "") -> HTMLResponse:
        try:
            a = await argus.approvals.verify_link(approval_id, t)
        except (ApprovalNotFound, BadToken):
            return HTMLResponse(approval_page.invalid(), status_code=404)
        return HTMLResponse(approval_page.render(a, t), headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            # only this page's own inline style and script, talking to this Argus
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline';"
                                       " connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none';"
                                       " frame-ancestors 'none'"})

    @app.post("/outbox/test", dependencies=guarded)
    async def outbox_test() -> dict:
        """Send a test notification, to check the ntfy topic and the phone app."""
        ntfy = argus.outbox.senders.get("ntfy")
        if not (ntfy and ntfy.enabled):
            raise HTTPException(status_code=409, detail="ntfy is off: set NTFY_TOPIC in .env and restart argusd")

        def fn(conn):
            return add_message(conn, time.time(), "ntfy", ntfy_message(
                "Argus test", f"ntfy works. Sent by {argus.cfg.instance.name} on {argus.cfg.instance.host}.",
                tags=["wave"]))

        oid = await argus.store.write(fn)
        argus.outbox.poke()
        return {"queued": oid}

    @app.get("/outbox", dependencies=guarded)
    async def outbox(limit: int = 50) -> dict:
        return {**(await argus.outbox.stats()), "messages": await argus.outbox.recent(min(max(limit, 1), 500)),
                **argus.outbox.health()}

    # -------------------------------------------------------------- scheduler and triggers (C9)

    @app.get("/schedules", dependencies=guarded)
    async def schedules() -> list[dict]:
        return await argus.scheduler.list()

    @app.post("/schedules/{sid}/run", dependencies=guarded)
    async def schedule_run(sid: str) -> dict:
        job_id = await argus.scheduler.run_now(sid)
        if job_id is None:
            raise HTTPException(status_code=404, detail=f"no schedule {sid}")
        return {"job_id": job_id}

    @app.post("/triggers/file", dependencies=guarded)
    async def file_trigger(body: FileTrigger) -> dict:
        await argus.registry.touch_worker(body.worker)
        return await argus.triggers.file(body.worker, body.trigger, body.path, body.sha256, body.size)

    @app.post("/hooks/{name}")
    async def webhook(name: str, request: Request) -> dict:
        body = await request.body()
        return await argus.triggers.webhook(name, request.headers, body)

    # -------------------------------------------------------------- plugins and power (C10)

    @app.get("/plugins", dependencies=guarded)
    async def plugins() -> dict:
        return argus.plugin_host.list()

    @app.post("/plugins/{pid}/run", dependencies=guarded)
    async def plugin_run(pid: str, body: RunPlugin) -> dict:
        """The manual trigger (a button in Helios): runs at interactive priority."""
        p = argus.plugin_host.plugins.get(pid)
        if p is None:
            raise HTTPException(status_code=404, detail=f"no plugin {pid}")
        manual = [t.manual.workflow for t in p.manifest.triggers if t.manual]
        wf = body.workflow or (manual[0] if manual else None)
        if wf is None or wf not in p.manifest.all_workflows():
            raise HTTPException(status_code=422, detail=f"{pid} has no workflow {wf!r}")
        job_id, created = await argus.jobs.enqueue(pid, wf, body.input, needs=p.manifest.job_needs(),
                                                   priority=PRIORITY_INTERACTIVE, source="helios")
        return {"id": job_id, "created": created}

    @app.get("/plugins/{pid}/state/{key}", dependencies=guarded)
    async def plugin_state_get(pid: str, key: str) -> dict:
        def fn(conn):
            row = conn.execute("SELECT value FROM plugin_state WHERE plugin = ? AND key = ?", (pid, key)).fetchone()
            return json.loads(row[0]) if row and row[0] is not None else None

        return {"value": await argus.store.read(fn)}

    @app.put("/plugins/{pid}/state/{key}", dependencies=guarded)
    async def plugin_state_put(pid: str, key: str, body: StateValue) -> dict:
        raw = json.dumps(body.value)
        if len(raw) > 256_000:
            raise HTTPException(status_code=413, detail="value too large (256 KB)")

        def fn(conn):
            now = time.time()
            conn.execute("INSERT INTO plugin_state (plugin, key, value, created_at, updated_at) VALUES (?,?,?,?,?)"
                         " ON CONFLICT(plugin, key) DO UPDATE SET value = excluded.value,"
                         " updated_at = excluded.updated_at", (pid, key[:200], raw, now, now))

        await argus.store.write(fn)
        return {"ok": True}

    @app.get("/power", dependencies=guarded)
    async def power() -> dict:
        """The power manager's view, whether the PC (a worker with desktop) is online, and recent power actions."""
        needs = set(argus.cfg.power.pc_needs)
        pcs = [w for w in await argus.registry.workers() if set(w["capabilities"]) & needs]
        online = [w for w in pcs if w["state"] == "online"]
        recent = [job_json(j) for j in await argus.jobs.list_jobs(None, 8, "power")]
        pending = [j for j in recent if j["state"] in ("queued", "retry", "leased", "running")]
        pc = (online or pcs or [None])[0]
        return {**argus.power.status(), "pc_online": bool(online),
                "pc": {k: pc.get(k) for k in ("id", "host", "state", "last_seen")} if pc else None,
                "wol": bool(argus.cfg.power.pc_mac), "delay": argus.cfg.power.shutdown_delay_seconds,
                "recent": recent, "pending": pending}

    @app.post("/power/{action}", dependencies=guarded)
    async def power_action(action: str) -> dict:
        """The power buttons. wake: Wake-on-LAN from argusd. sleep / shutdown / restart / cancel: a job for the PC's
        worker, ahead of everything else; cancel also drops a sleep or shutdown that has not started yet."""
        if action == "wake":
            try:
                out = await asyncio.to_thread(argus.power.wake)
            except PowerError as e:
                raise HTTPException(status_code=422, detail=str(e)) from None
            except OSError as e:
                raise HTTPException(status_code=502, detail=f"could not send the wake packet: {e}") from None
            await argus.store.write(lambda c: insert_event(c, time.time(), "power.wake_sent", src="helios",
                                                           dst="power", data=out))
            return out
        if action not in ("sleep", "shutdown", "restart", "cancel"):
            raise HTTPException(status_code=404, detail=f"no power action {action!r}")
        dropped = []
        if action == "cancel":
            await argus.power.hold()  # and no automatic shutdown for this idle stretch
            for j in await argus.jobs.list_jobs(None, 20, "power"):
                if j.state.value in ("queued", "retry") and j.workflow in ("sleep", "shutdown", "restart"):
                    await argus.jobs.cancel(j.id)
                    dropped.append(j.id)
        job_id, created = await argus.jobs.enqueue(
            "power", action, {"delay": argus.cfg.power.shutdown_delay_seconds}, needs=["desktop"], priority=100,
            dedupe_key=f"power:{action}", source="helios")
        return {"id": job_id, "created": created, "dropped": dropped}

    @app.post("/jobs/{job_id}/wait", dependencies=guarded)
    async def wait(job_id: str, body: Wait) -> dict:
        return job_json(await argus.jobs.wait(job_id, body.worker, body.reason))

    return app
