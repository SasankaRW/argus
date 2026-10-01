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
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import ari as ari_mod
from .. import ask as ask_mod
from .. import daily, guidance, logview, vocab
from .. import island as island_mod
from .. import settings as settings_mod
from ..approvals import ApprovalClosed, ApprovalError, ApprovalNotFound, BadToken
from ..config import PRIORITY_INTERACTIVE
from ..context import Argus
from ..cron import next_run
from ..events import EventFilter, insert_event, read_events
from ..ids import new_id
from ..jobs import InvalidTransition, Job, JobNotFound, JobState, LeaseLost, QueueFull, Step
from ..outbox import PRIORITIES, add_message, ntfy_message
from ..power import PowerError
from ..presence import describe_phone
from ..shares import ShareError, ShareStore, kinds_of
from ..tools import Tool, ToolError, Tools
from ..triggers import BadSignature, TriggerError, UnknownTrigger
from ..voice import CATALOG, Voice, VoiceUnavailable, download, installed
from ..worker import think as think_mod
from . import approval_page, mcp
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
    min_priority: int | None = None  # a worker's fast lane: only jobs at least this urgent


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


class AriSay(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    conv: str | None = Field(None, pattern=r"^[A-Za-z0-9_-]{1,40}$")


class AriAnswer(BaseModel):
    yes: bool


class AriStateIn(BaseModel):
    phase: Literal["idle", "listening", "thinking", "working", "speaking", "done"]
    text: str = Field("", max_length=300)
    by: str = Field("", max_length=40)


class AriSpeak(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class AriVoicePick(BaseModel):
    voice: str = Field(min_length=3, max_length=80)
    speed: float = Field(1.0, ge=0.6, le=1.6)


class ToolCall(BaseModel):
    worker: str
    key: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=60)
    args: dict[str, Any] = Field(default_factory=dict)


class SampleIn(BaseModel):
    worker: str
    playbook: str = Field(min_length=1, max_length=40_000)
    schema_: dict[str, Any] | None = Field(None, alias="schema")
    input: Any = None
    output: Any = None
    tier: str | None = None
    escalated: bool = False


class Replay(BaseModel):
    tier: str = Field(min_length=1, max_length=20)


class Verdict(BaseModel):
    verdict: str | None = Field(None, pattern="^(correct|wrong)$")
    correction: Any = None


class LessonDecide(BaseModel):
    approve: bool


class PluginSettings(BaseModel):
    live: bool | None = None  # None: back to argus.yaml's plugins.live
    config: dict[str, Any] | None = None  # only the fields you changed; a null value resets that field
    reset: bool = False  # forget all your changes


class ClaudeAuth(BaseModel):
    logged_in: bool
    detail: str = Field("", max_length=300)


class ScheduleEdit(BaseModel):
    enabled: bool


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
    # a small picture to decide by (ctx.ask_me: the screenshot it couldn't name), as a data: URL
    image: str | None = Field(None, max_length=400_000, pattern=r"^data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$")


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
            "workers": [{"id": w["id"], "host": w["host"], "state": w["state"]} for w in workers
                        if not (w["id"].endswith("-now") and any(x["id"] == w["id"][:-4] for x in workers))],
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

    change_lock = asyncio.Lock()

    @app.post("/jobs/{job_id}/changes/{event_id}/wrong", dependencies=guarded)
    async def wrong_change(job_id: str, event_id: str, body: Fix) -> dict:
        """The "Wrong" button: the plugin's correction workflow puts the file where it belongs and remembers it."""
        async with change_lock:  # Undo and Wrong on the same change: one at a time
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
        if ask_mod.PHONE.search(body.text.lower()):
            where = await phone_where()
            return {"via": "rules", "reply": f"{where['text']} Ring it?", "action": "phone:ring",
                    "label": "Ring my phone"}
        snap = await ask_mod.snapshot(argus)
        hit = ask_mod.rules(body.text, actions, snap)
        labels = {a["id"]: a["label"] for a in actions}
        if hit is not None:
            return {"via": "rules", **hit, "label": labels.get(hit.get("action") or "")}
        job_id, _ = await argus.jobs.enqueue(
            "ask", "ask", {"text": body.text, "actions": actions, "snapshot": snap},
            priority=PRIORITY_INTERACTIVE, source="helios")
        return {"via": "model", "job_id": job_id}

    async def mcp_auth(request: Request) -> bool:
        return token_ok(request.headers.get("authorization"))

    tools = Tools(argus, job_json)
    app.state.tools = tools
    mcp.register(app, argus, mcp_auth, tools)

    @app.post("/jobs/{job_id}/tools", dependencies=guarded)
    async def job_tool(job_id: str, body: ToolCall) -> dict:
        """ctx.tool() from a worker: a built-in tool's answer now, or a plugin tool's child job (pending until done;
        the calling job then waits and is resumed when the child ends)."""
        return await tools.call_from_job(job_id, body.worker, body.key, body.name, body.args)

    @app.get("/tools", dependencies=guarded)
    async def list_tools() -> list[dict]:
        """Everything Ari can use (built in and from plugins)."""
        return [{**t.brief(), "plugin": t.plugin, "risky": t.risky} for t in tools.all().values() if t.for_ari]

    @app.post("/tools/selftest", dependencies=guarded)
    async def tools_selftest(body: dict | None = None) -> dict:
        """Run each read-only tool once, as Ari would (Helios > Settings). {"deep": true} adds the slow ones."""
        job_id, _ = await argus.jobs.enqueue(
            "ari", "selftest", {"deep": bool((body or {}).get("deep")),
                                "tools": [{**t.brief(), "plugin": t.plugin} for t in tools.all().values()
                                          if t.for_ari and not t.risky]},
            priority=PRIORITY_INTERACTIVE, source="helios")
        return {"job_id": job_id}

    @app.post("/ask/do", dependencies=guarded)
    async def ask_do(body: AskDo) -> dict:
        """Do a suggested action (you tapped it)."""
        return await do_action(body.action)

    async def do_action(a: str) -> dict:
        if a == "phone:ring":
            return {"phone": await ring_phone()}
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

    # -------------------------------------------------------------- Ari's island: widgets and shortcuts

    async def island_choices() -> list[dict]:
        acts = [{"action": a["id"], "label": a["label"], "group": "plugin buttons" if a["id"].startswith("run:")
                 else "power" if a["id"].startswith("power:") else "phone"}
                for a in ask_mod.catalog(argus.plugin_host) if not a["id"].startswith("show:")]
        acts += [{"action": f"show:{k}", "label": f"Open {v}", "group": "Helios pages"}
                 for k, v in island_mod.PAGES.items()]
        acts += [{"action": f"routine:{n}", "label": n, "group": "routines"}
                 for n in await argus.store.read(island_mod.routines)]
        return acts

    @app.get("/island", dependencies=guarded)
    async def island_get() -> dict:
        cfg = await argus.store.read(island_mod.load)
        return {**cfg, "widget_labels": island_mod.WIDGETS, "choices": await island_choices(),
                "max_shortcuts": island_mod.MAX_SHORTCUTS}

    @app.put("/island", dependencies=guarded)
    async def island_put(body: dict[str, Any]) -> dict:
        actions = {c["action"] for c in await island_choices()}
        try:
            cfg = island_mod.check(body, actions)
        except island_mod.IslandError as e:
            raise HTTPException(status_code=422, detail=str(e)) from None
        await argus.store.write(lambda c: island_mod.save(c, cfg))
        return await island_get()

    @app.post("/island/run", dependencies=guarded)
    async def island_run(body: AskDo) -> dict:
        """A shortcut on the island: a routine or a website here, everything else as in Ask."""
        a = body.action
        if a.startswith("url:"):
            if not re.fullmatch(r"url:https?://[^\s]{3,500}", a):
                raise HTTPException(status_code=422, detail="not a website")
            return {"open": a[4:]}
        if a.startswith("routine:"):
            try:
                tool = tools.get("run_routine")
                job_id, created = await tools.enqueue(tool, {"name": a[8:]}, parent=None, key=None)
            except ToolError as e:
                raise HTTPException(status_code=409, detail=str(e)) from None
            return {"job_id": job_id, "created": created}
        return await do_action(a)

    # -------------------------------------------------------------- find my phone

    async def ring_phone() -> dict:
        """Three urgent notifications, 20 s apart: loud even on silent if ntfy may override Do Not Disturb."""
        if not argus.outbox.senders.get("ntfy") or not argus.outbox.senders["ntfy"].enabled:
            raise HTTPException(status_code=409, detail="ntfy is off: set NTFY_TOPIC in .env")

        def fn(conn):
            now = time.time()
            for i in range(3):
                add_message(conn, now, "ntfy", ntfy_message("Here I am!", "Argus is ringing your phone.",
                                                            priority="urgent", tags=["rotating_light", "iphone"]),
                            dedupe_key=f"ring:{int(now)}:{i}", send_at=now + 20 * i)
            insert_event(conn, now, "phone.ring", src="helios", dst="phone")

        await argus.store.write(fn)
        argus.outbox.poke()
        return {"ringing": True, "times": 3}

    async def phone_where() -> dict:
        if not argus.cfg.approvals.phone:
            return {"info": None, "text": "Set approvals.phone in argus.yaml (the phone's Tailscale name) and I can "
                                          "tell where it is."}
        try:
            info = await argus.phone.info()
        except Exception as e:  # Tailscale not running here
            return {"info": None, "text": f"I can't read Tailscale right now ({str(e)[:80]}).", }
        return {"info": info, "text": describe_phone(info)}

    async def _where(_: dict) -> dict:
        return await phone_where()

    async def _ring(_: dict) -> dict:
        return await ring_phone()

    tools.builtin["where_is_my_phone"] = Tool("where_is_my_phone", "Where the user's phone is (Tailscale: online at "
                                              "home, away, or offline and when last seen).", fn=_where)
    tools.builtin["ring_phone"] = Tool("ring_phone", "Ring the user's phone loudly to find it.", fn=_ring)

    # -------------------------------------------------------------- the guidance loop

    @app.post("/jobs/{job_id}/samples", dependencies=guarded)
    async def add_sample(job_id: str, body: SampleIn) -> dict:
        """A model answer from a plugin's job (ctx.llm), kept for your verdict and the nightly review."""
        job = await argus.jobs.get(job_id)
        sid = await argus.store.write(lambda c: guidance.add_sample(
            c, time.time(), job_id=job_id, plugin=job.plugin, playbook=body.playbook, schema=body.schema_,
            input=body.input, output=body.output, tier=body.tier, escalated=body.escalated,
            keep=argus.cfg.guidance.keep_samples))
        return {"id": sid}

    @app.get("/jobs/{job_id}/samples", dependencies=guarded)
    async def job_samples(job_id: str) -> list[dict]:
        return await argus.store.read(lambda c: [guidance.sample_json(r) for r in c.execute(
            "SELECT * FROM samples WHERE job_id = ? ORDER BY id", (job_id,))])

    @app.get("/guidance", dependencies=guarded)
    async def guidance_overview(plugin: str | None = None) -> list[dict]:
        """Per playbook: answers kept, escalations, your verdicts, lessons in force and waiting."""
        return await argus.store.read(lambda c: guidance.overview(c, plugin))

    @app.get("/guidance/{key}/samples", dependencies=guarded)
    async def playbook_samples(key: str, verdict: str | None = None, limit: int = Query(50, ge=1, le=500)
                               ) -> list[dict]:
        where, args = "playbook = ?", [key]
        if verdict in ("correct", "wrong"):
            where, args = where + " AND verdict = ?", args + [verdict]
        elif verdict == "none":
            where += " AND verdict IS NULL"
        return await argus.store.read(lambda c: [guidance.sample_json(r) for r in c.execute(
            f"SELECT * FROM samples WHERE {where} ORDER BY id DESC LIMIT ?", (*args, limit))])

    @app.post("/samples/{sample_id}/verdict", dependencies=guarded)
    async def sample_verdict(sample_id: int, body: Verdict) -> dict:
        """Correct (joins the eval set) or wrong (with what it should have been; the review learns from it)."""
        if not await argus.store.write(lambda c: guidance.set_verdict(c, sample_id, body.verdict, body.correction)):
            raise HTTPException(status_code=404, detail="no such answer")
        return {"ok": True}

    @app.get("/guidance/{key}/evals", dependencies=guarded)
    async def playbook_evals(key: str) -> dict:
        """A playbook's tests (the eval set) and the history of test runs (pass rate)."""
        def fn(c):
            return {"tests": [guidance.sample_json(r) for r in guidance.eval_samples(c, key)],
                    "runs": guidance.runs(c, key)}

        return await argus.store.read(fn)

    @app.post("/guidance/{key}/evals", dependencies=guarded)
    async def playbook_evals_run(key: str) -> dict:
        """Run the tests now (the first local tier, with the lessons in force)."""
        r = await argus.queue_evals(key)
        if r is None:
            raise HTTPException(status_code=404, detail="no such playbook")
        return r

    @app.post("/samples/{sample_id}/replay", dependencies=guarded)
    async def sample_replay(sample_id: int, body: Replay) -> dict:
        """Ask another tier the same question; the job's result has both answers."""
        if body.tier not in argus.cfg.models.tiers:
            have = ", ".join(argus.cfg.models.tiers)
            raise HTTPException(status_code=422, detail=f"no tier {body.tier} (have {have})")
        r = await argus.queue_replay(sample_id, body.tier)
        if r is None:
            raise HTTPException(status_code=404, detail="no such answer")
        return r

    @app.post("/guidance/review", dependencies=guarded)
    async def guidance_review() -> dict:
        """Review now: Claude proposes lessons for playbooks with new mistakes; you approve them."""
        return await argus.guidance_review()

    @app.post("/lessons/{lesson_id}/decide", dependencies=guarded)
    async def lesson_decide(lesson_id: int, body: LessonDecide) -> dict:
        r = await argus.store.write(lambda c: guidance.decide(c, lesson_id, body.approve, time.time()))
        if r is None:
            raise HTTPException(status_code=409, detail="that lesson isn't waiting for a decision")
        return r

    @app.delete("/lessons/{lesson_id}", dependencies=guarded)
    async def lesson_drop(lesson_id: int) -> dict:
        """Stop using lessons that are in force (the playbook is used as written again)."""
        if not await argus.store.write(lambda c: guidance.drop_active(c, lesson_id)):
            raise HTTPException(status_code=404, detail="no such lessons in force")
        return {"ok": True}

    async def _propose(x: dict) -> dict:
        lid = await argus.store.write(lambda c: guidance.propose(c, time.time(), str(x["key"]), str(x["text"]),
                                                                 x.get("evals") or {}, x.get("job_id")))
        return {"lesson": lid}

    async def _decide(x: dict) -> dict:
        r = await argus.store.write(lambda c: guidance.decide(c, int(x["lesson"]), bool(x["approve"]), time.time()))
        return r or {"state": "already decided"}

    tools.builtin["propose_lessons"] = Tool(
        "propose_lessons", "(guidance loop) keep proposed lessons for a playbook",
        {"key": {"type": "string"}, "text": {"type": "string"}, "evals": {"type": "object"},
         "job_id": {"type": "string"}}, ["key", "text"], fn=_propose, for_ari=False, for_mcp=False)
    async def _record(x: dict) -> dict:
        rid = await argus.store.write(lambda c: guidance.record_run(c, time.time(), str(x["key"]), x.get("run") or {},
                                                                    str(x.get("why") or "manual"), x.get("job_id")))
        return {"run": rid}

    tools.builtin["record_evals"] = Tool(
        "record_evals", "(guidance loop) keep the result of a test run",
        {"key": {"type": "string"}, "run": {"type": "object"}, "why": {"type": "string"}, "job_id": {"type": "string"}},
        ["key", "run"], fn=_record, for_ari=False, for_mcp=False)
    tools.builtin["decide_lessons"] = Tool(
        "decide_lessons", "(guidance loop) your answer to proposed lessons",
        {"lesson": {"type": "integer"}, "approve": {"type": "boolean"}}, ["lesson", "approve"], fn=_decide,
        for_ari=False, for_mcp=False)

    @app.get("/phone", dependencies=guarded)
    async def get_phone() -> dict:
        """Where the phone is as far as Tailscale knows (online, home or away, last seen)."""
        return await phone_where()

    @app.post("/phone/ring", dependencies=guarded)
    async def post_phone_ring() -> dict:
        return await ring_phone()

    # -------------------------------------------------------------- Ari (talk to Argus)

    def _schedule_spec(action: str) -> dict:
        if action.startswith("remind:"):
            return {"plugin": "argus", "workflow": "remind", "input": {"text": action[7:]}, "needs": [],
                    "priority": 50}
        if action.startswith("power:"):
            return {"plugin": "power", "workflow": action[6:],
                    "input": {"delay": argus.cfg.power.shutdown_delay_seconds}, "needs": ["desktop"], "priority": 100}
        _, pid, wf = (action.split(":", 2) + ["", ""])[:3]
        p = argus.plugin_host.plugins.get(pid)
        if p is None:
            raise HTTPException(status_code=404, detail=f"no plugin {pid}")
        return {"plugin": pid, "workflow": wf, "input": {}, "needs": p.manifest.job_needs(), "priority": 50}

    async def _answer(conv: str, q: dict, yes: bool) -> dict:
        """You said yes or no to what Ari asked."""
        await argus.store.write(lambda c: ari_mod.close_question(c, q["turn"]))
        if not yes:
            reply, out = "Okay, I won't.", {}
        elif q["kind"] == "tool":
            try:
                res = await tools.run_now(q["name"], q.get("args") or {})
                nxt = str(res.get("next") or "") if isinstance(res, dict) else ""
                reply = f"It's ready: {nxt}." if nxt else "Done."  # "It's ready: press Enter to send."
                out = {"tool": q["name"], "result": res}
            except ToolError as e:
                reply, out = f"I couldn't: {e}.", {}
        elif q["kind"] == "remember":
            fact = str(q.get("fact") or "")[:500]
            await argus.store.write(lambda c: ari_mod.remember(c, fact))
            reply, out = "Got it, I'll remember that.", {"remembered": fact}
        elif q["kind"] == "schedule":
            when = ari_mod.When(q["cron"], q["once"], q["say"], "")
            spec = _schedule_spec(q["action"])
            label = f"{q['say']}: {q['label']}"
            s = await argus.store.write(lambda c: ari_mod.add_schedule(c, time.time(), when, q["action"], label=label,
                                                                       **spec))
            reply, out = f"Done. {q['say'][0].upper()}{q['say'][1:]}, I'll {q['label']}.", {"schedule": s}
        else:
            if q["action"].startswith("power:") and q["action"] != "power:wake":
                try:
                    out = await do_action(q["action"])
                except HTTPException as e:
                    reply, out = f"I couldn't: {e.detail}.", {}
                    await argus.store.write(lambda c: ari_mod.add_turn(c, conv, "ari", reply))
                    return {"conv": conv, "reply": reply}
            else:
                out = await do_action(q["action"])
            reply = f"Opening {out['view']}." if "view" in out else "Ringing it now." if "phone" in out else "Done."
        await argus.store.write(lambda c: (ari_mod.add_turn(c, conv, "ari", reply), ari_state(c, "done", reply)))
        return {"conv": conv, "reply": reply, **out}

    def ari_state(c, phase: str, text: str = "", job_id: str | None = None, by: str = "argus") -> None:
        """What Ari is doing (event ari.state): the Ari pill in Helios and the PC's popup follow it."""
        insert_event(c, time.time(), "ari.state", job_id=job_id, data={"phase": phase, "text": text[:300], "by": by})

    overlays: dict[str, float] = {}  # host -> last ping from the PC's Ari popup

    @app.post("/ari/state", dependencies=guarded)
    async def ari_state_set(body: AriStateIn) -> dict:
        """Report what Ari is doing where you are (listening, speaking) so every Ari pill shows it."""
        await argus.store.write(lambda c: ari_state(c, body.phase, body.text, by=body.by or "client"))
        return {"ok": True}

    listener = {"seen": 0.0}

    @app.post("/ari/listener", dependencies=guarded)
    async def ari_listener_here() -> dict:
        """The PC's "Hey Ari" listener says it is running (every 30 s)."""
        listener["seen"] = time.time()
        return {"ok": True}

    @app.get("/ari/vocabulary", dependencies=guarded)
    async def vocabulary() -> dict:
        """The words Whisper should expect (ari.vocabulary + names Ari remembers) and the fixes (ari.heard_as)."""
        facts = await argus.store.read(lambda c: [m["fact"] for m in ari_mod.memories(c)])
        names = vocab.words(argus.cfg.ari.vocabulary, facts)
        return {"words": names, "prompt": vocab.prompt(names), "heard_as": argus.cfg.ari.heard_as}

    @app.get("/ari/listener", dependencies=guarded)
    async def ari_listener_running() -> dict:
        """Is the PC's "Hey Ari" listener running? (Helios on that PC then doesn't listen for "Hey Ari" too.)"""
        return {"listener": time.time() - listener["seen"] < 75}

    @app.post("/ari/wake", dependencies=guarded)
    async def ari_wake() -> dict:
        """The island's Talk button: the PC's listener starts listening now, no "Hey Ari" needed. {listener: false}
        when no listener runs (the island then opens Helios's mic instead)."""
        here = time.time() - listener["seen"] < 75
        if here:
            await argus.store.write(lambda c: insert_event(c, time.time(), "ari.wake", src="island", dst="ari"))
        return {"listener": here}

    @app.post("/ari/popup", dependencies=guarded)
    async def ari_popup_ping(request: Request) -> dict:
        """The PC's Ari popup is running (sent every 30 s): Helios on that PC then leaves the pill to it."""
        overlays[request.client.host if request.client else "?"] = time.time()
        return {"ok": True}

    @app.post("/ari", dependencies=guarded)
    async def ari_say(body: AriSay) -> dict:
        """Say something to Ari. Returns the reply at once, or a turn that fills in (poll GET /ari/{conv})."""
        conv = body.conv or new_id().lower()
        body.text = vocab.fix(body.text, argus.cfg.ari.heard_as)  # "open what's up" -> "open WhatsApp"
        text = ari_mod.WAKE.sub("", body.text).strip() or body.text.strip()
        q = await argus.store.read(lambda c: ari_mod.open_question(c, conv))
        await argus.store.write(lambda c: ari_mod.add_turn(c, conv, "you", body.text.strip()))
        if q and (ari_mod.YES.match(text) or ari_mod.NO.match(text)):
            return await _answer(conv, q, bool(ari_mod.YES.match(text)))
        if q:  # asked something else instead: the question lapses
            await argus.store.write(lambda c: ari_mod.close_question(c, q["turn"]))

        async def reply(text: str, action: str | None = None, pending: dict | None = None) -> dict:
            tid = await argus.store.write(lambda c: (ari_mod.add_turn(c, conv, "ari", text, action=action,
                                                                      pending=pending),
                                                     ari_state(c, "done", text))[0])
            return {"conv": conv, "turn": tid, "reply": text, "action": action, "pending": pending}

        if ari_mod.BRIEF.match(text):  # "good morning": the morning brief, spoken
            return await reply(await argus.spoken_brief())
        wx = ari_mod.weather_ask(text)
        if wx is not None:  # "how's the weather?", "will it rain tomorrow in Kandy?": Open-Meteo, no model
            said = await argus.weather_say(*wx)
            if said:
                return await reply(said)
        actions = ask_mod.catalog(argus.plugin_host)
        m = ari_mod.REMEMBER.match(text)
        if m and not ari_mod.parse_when(text, time.time()):  # "remember that ..." (not "remind me at ...")
            await argus.store.write(lambda c: ari_mod.remember(c, m.group("fact")))
            return await reply("Got it, I'll remember that.")
        when = ari_mod.parse_when(text, time.time())
        if when is not None:
            what = ari_mod.action_for(when.rest, actions)
            if what is None:
                return await reply(f"I got the time ({when.say}) but not what to do. Say it like \"sort downloads "
                                   f"every morning at 7\" or \"remind me to call mum tomorrow at 5 pm\".")
            action = what[0]
            label = ari_mod.label_of(action, actions)
            return await reply(f"{when.say[0].upper()}{when.say[1:]}, I'll {label}. Shall I set that up?",
                               pending={"kind": "schedule", "action": action, "cron": when.cron, "once": when.once,
                                        "say": when.say, "label": label})
        if ask_mod.PHONE.search(text.lower()):  # "where's my phone?": where Tailscale sees it, and ring it?
            where = await phone_where()
            return await reply(f"{where['text']} Want me to ring it?", "phone:ring",
                               {"kind": "action", "action": "phone:ring"})
        snap = await ask_mod.snapshot(argus)
        # about the screen or the clipboard ("what does this error say?"): not Argus's own errors
        about_screen = think_mod.SCREEN.search(text) or think_mod.CLIPBOARD.search(text)
        hit = None if about_screen else ask_mod.rules(text, actions, snap)
        if hit is not None:
            act = hit.get("action")
            if act and not act.startswith("show:"):  # something to do: wait for your yes
                return await reply(hit["reply"], act, {"kind": "action", "action": act})
            return await reply(hit["reply"], act)
        hist = await argus.store.read(lambda c: ari_mod.history(c, conv))
        known = await argus.store.read(lambda c: ari_mod.recall(c, text))
        job_id, _ = await argus.jobs.enqueue(
            "ari", "think", {"text": text, "history": hist[:-1], "now": time.strftime("%A %d %B %Y, %H:%M"),
                             "you_remember": known,
                             "tools": [t.brief() for t in tools.all().values() if t.for_ari]},
            priority=PRIORITY_INTERACTIVE, source="helios")
        tid = await argus.store.write(lambda c: (ari_mod.add_turn(c, conv, "ari", None, job_id=job_id),
                                                 ari_state(c, "thinking", text, job_id))[0])
        return {"conv": conv, "turn": tid, "job_id": job_id, "reply": None}

    @app.get("/ari-memory", dependencies=guarded)
    async def ari_memory() -> list[dict]:
        """Everything you asked Ari to remember, newest first."""
        return await argus.store.read(ari_mod.memories)

    @app.delete("/ari-memory/{memory_id}", dependencies=guarded)
    async def ari_forget(memory_id: int) -> dict:
        if not await argus.store.write(lambda c: ari_mod.forget(c, memory_id)):
            raise HTTPException(status_code=404, detail="no such memory")
        return {"ok": True}

    async def _remember(x: dict) -> dict:
        mid = await argus.store.write(lambda c: ari_mod.remember(c, str(x["fact"])))
        return {"remembered": x["fact"], "id": mid}

    async def _recall(x: dict) -> list:
        return await argus.store.read(lambda c: ari_mod.recall(c, str(x["query"]), 10))

    async def _forget(x: dict) -> dict:
        ok = await argus.store.write(lambda c: ari_mod.forget(c, int(x["id"])))
        if not ok:
            raise ToolError(f"no memory {x['id']}")
        return {"forgotten": int(x["id"])}

    tools.builtin["remember"] = Tool("remember", "Keep a fact the user asked you to remember (one short sentence).",
                                     {"fact": {"type": "string", "description": "the fact, in the user's words"}},
                                     ["fact"], fn=_remember)
    tools.builtin["recall_memory"] = Tool("recall_memory", "Look up what the user asked you to remember earlier.",
                                          {"query": {"type": "string", "description": "words to look for"}},
                                          ["query"], fn=_recall)
    tools.builtin["forget_memory"] = Tool("forget_memory", "Forget one remembered fact (its id from recall_memory).",
                                          {"id": {"type": "integer", "description": "the memory's id"}}, ["id"],
                                          fn=_forget)

    @app.get("/ari-chats", dependencies=guarded)
    async def ari_chats() -> list[dict]:
        """Ari's past conversations, newest first (for the chat list in Helios)."""
        return await argus.store.read(lambda c: ari_mod.chats(c))

    @app.delete("/ari-chats/{conv}", dependencies=guarded)
    async def ari_delete_chat(conv: str) -> dict:
        n = await argus.store.write(lambda c: ari_mod.delete_chat(c, conv))
        if not n:
            raise HTTPException(status_code=404, detail="no such chat")
        return {"deleted": n}

    @app.get("/ari/{conv}", dependencies=guarded)
    async def ari_conv(conv: str) -> dict:
        """The conversation; a model's answer is filled in here once its job finished."""
        rows = await argus.store.read(lambda c: ari_mod.turns(c, conv))
        for t in rows:
            if t["job_id"] and t["text"] is None:
                try:
                    j = await argus.jobs.get(t["job_id"])
                except JobNotFound:
                    j = None
                if j is None or j.state.value in ("dead", "cancelled"):
                    t["text"] = "Sorry, I couldn't answer that just now."
                elif j.state.value == "succeeded":
                    r = j.result or {}
                    t["text"] = str(r.get("reply") or "…")
                    t["used"] = [{"tool": u.get("tool"), "ok": "error" not in u} for u in r.get("used") or []] + \
                        ([{"tool": "web search", "ok": True}] if r.get("via") == "web" else [])
                    t["action"] = r.get("action")
                    if r.get("pending"):  # a tool that asks first
                        t["pending"] = r["pending"]
                    elif t["action"] and not str(t["action"]).startswith("show:"):
                        t["pending"] = {"kind": "action", "action": t["action"]}
                else:
                    continue
                await argus.store.write(lambda c, t=t: c.execute(
                    "UPDATE ari_turns SET text = ?, action = ?, pending = ?, used = ? WHERE id = ?",
                    (t["text"], t["action"], json.dumps(t["pending"]) if t["pending"] else None,
                     json.dumps(t.get("used")) if t.get("used") else None, t["id"])))
        labels = {a["id"]: a["label"] for a in ask_mod.catalog(argus.plugin_host)}
        for t in rows:
            t["label"] = labels.get(t["action"] or "")
        return {"conv": conv, "turns": rows}

    # Ari's voice and ears ----------------------------------------------------

    voice = Voice(argus.cfg.ari.voice, argus.cfg.base_dir)
    audio_dir = argus.cfg.db_path.parent / "ari"
    voices_dir = voice.path.parent if voice.path is not None else argus.cfg.db_path.parent / "voices"
    picked = {"loaded": False}

    async def use_picked_voice() -> None:
        """The voice you picked in Helios (kept in the settings table) wins over ari.voice in argus.yaml."""
        if picked["loaded"]:
            return
        picked["loaded"] = True
        sql = "SELECT value FROM settings WHERE key = 'ari_voice'"
        row = await argus.store.read(lambda c: c.execute(sql).fetchone())
        if row:
            v = json.loads(row[0])
            path = voices_dir / f"{v.get('voice')}.onnx"
            if path.exists():
                voice.use(path, float(v.get("speed") or 1.0))

    async def gpu_online() -> bool:
        return any(w["state"] == "online" and "gpu" in w["capabilities"] for w in await argus.registry.workers())

    @app.get("/ari-voice", dependencies=guarded)
    async def ari_voice(request: Request) -> dict:
        """What Helios can use: Piper here, Whisper on the PC (only while a GPU worker is online)."""
        host = request.client.host if request.client else "?"
        return {"voice": voice.configured, "hearing": argus.cfg.ari.hearing,
                "whisper_ready": argus.cfg.ari.hearing == "whisper" and await gpu_online(),
                "popup_here": time.time() - overlays.get(host, 0) < 75, "pill": argus.cfg.ari.pill}

    @app.get("/ari-voice/voices", dependencies=guarded)
    async def ari_voices() -> dict:
        """Voices you can pick (Piper, English), which are on this machine, and the one in use."""
        await use_picked_voice()
        have = set(installed(voices_dir))
        known = [{"id": v, "label": label, "installed": v in have} for v, label in CATALOG]
        known += [{"id": v, "label": v, "installed": True} for v in sorted(have - {k for k, _ in CATALOG})]
        return {"current": voice.name, "speed": voice.speed, "voices": known}

    @app.put("/ari-voice/voice", dependencies=guarded)
    async def ari_pick_voice(body: AriVoicePick) -> dict:
        """Pick Ari's voice (downloaded the first time, ~60 MB) and speed; every screen and "Hey Ari" use it."""
        path = voices_dir / f"{body.voice}.onnx"
        if not path.exists():
            try:
                path = await asyncio.to_thread(download, body.voice, voices_dir)
            except VoiceUnavailable as e:
                raise HTTPException(status_code=409, detail=str(e)) from None
        voice.use(path, body.speed)
        picked["loaded"] = True
        value = json.dumps({"voice": body.voice, "speed": voice.speed})
        await argus.store.write(lambda c: c.execute(
            "INSERT INTO settings (key, value, created_at, updated_at) VALUES ('ari_voice', ?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (value, time.time(), time.time())))
        return await ari_voices()

    @app.post("/ari-voice/say", dependencies=guarded)
    async def ari_say_audio(body: AriSpeak) -> Response:
        """Ari's natural voice: WAV audio of the text (Piper). 409 when none is set up: use the browser's voice."""
        await use_picked_voice()
        try:
            wav = await asyncio.to_thread(voice.say, body.text)
        except VoiceUnavailable as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        return Response(wav, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    @app.post("/ari-voice/hear", dependencies=guarded)
    async def ari_hear(request: Request) -> dict:
        """A recording in (webm/ogg/wav, at most 5 MB), its text out: Whisper on the PC. 409 while the PC is off
        (Helios then uses the browser's recognition)."""
        if argus.cfg.ari.hearing != "whisper":
            raise HTTPException(status_code=409, detail="hearing is set to the browser (ari.hearing)")
        if not await gpu_online():
            raise HTTPException(status_code=409, detail="the PC is off")
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > 5 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="recording too long")
        if not data:
            raise HTTPException(status_code=422, detail="empty recording")
        kind = (request.headers.get("content-type") or "").split(";")[0].strip()
        ext = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/wav": "wav", "audio/x-wav": "wav",
               "audio/mp4": "m4a", "audio/mpeg": "mp3"}.get(kind, "webm")
        name = f"{new_id().lower()}.{ext}"
        audio_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread((audio_dir / name).write_bytes, bytes(data))
        try:
            voc = await vocabulary()
            job_id, _ = await argus.jobs.enqueue("ari", "transcribe", {"audio": name,
                                                                      "model": argus.cfg.ari.whisper_model,
                                                                      "prompt": voc["prompt"]},
                                                 needs=["gpu"], priority=95, source="helios")
            for _ in range(120):  # up to 60 s (the first time loads the model)
                j = await argus.jobs.get(job_id)
                if j.state.value == "succeeded":
                    return {"text": (j.result or {}).get("text", "")}
                if j.state.value in ("dead", "cancelled"):
                    raise HTTPException(status_code=502, detail=(j.error or "could not transcribe").split("\n")[0])
                await asyncio.sleep(0.5)
            await argus.jobs.cancel(job_id, "took too long")
            raise HTTPException(status_code=504, detail="the PC didn't answer in time")
        finally:
            (audio_dir / name).unlink(missing_ok=True)

    @app.get("/ari/audio/{name}", dependencies=guarded)
    async def ari_audio(name: str):
        if not re.fullmatch(r"[0-9a-z]{10,40}\.(webm|ogg|wav|m4a|mp3)", name) or not (audio_dir / name).is_file():
            raise HTTPException(status_code=404, detail="no such recording")
        return FileResponse(audio_dir / name, media_type="application/octet-stream")

    @app.post("/ari/{conv}/answer", dependencies=guarded)
    async def ari_answer(conv: str, body: AriAnswer) -> dict:
        """The Yes / No buttons under Ari's question."""
        q = await argus.store.read(lambda c: ari_mod.open_question(c, conv))
        if q is None:
            raise HTTPException(status_code=409, detail="nothing to answer")
        await argus.store.write(lambda c: ari_mod.add_turn(c, conv, "you", "Yes" if body.yes else "No"))
        return await _answer(conv, q, body.yes)

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
            parsed = await asyncio.to_thread(yaml.safe_load, body.text)
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
            raise HTTPException(status_code=404 if "no such" in str(e) or "bad" in str(e) else e.status,
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
        meta = share_or_404(shares.meta, sid)  # before reading the body
        if meta["job_id"]:
            raise HTTPException(status_code=409, detail="already sent")
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
        path, mime = share_or_404(shares.file_path, sid, name)
        # only pictures and PDFs open in the browser; anything else (html, svg, ...) downloads, never runs here
        inline = (mime.startswith("image/") and mime != "image/svg+xml") or mime == "application/pdf"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="no such file in this share")
        return FileResponse(path, media_type=mime if inline else "application/octet-stream", filename=name,
                            content_disposition_type="inline" if inline else "attachment",
                            headers={"X-Content-Type-Options": "nosniff",
                                     "Content-Security-Policy": "sandbox; default-src 'none'"})

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

    # -------------------------------------------------------------- the morning brief

    @app.post("/summary", dependencies=guarded)
    async def summary_now() -> dict:
        """Send the evening summary now (to try it)."""
        return await argus.send_summary()

    @app.get("/time-saved", dependencies=guarded)
    async def get_time_saved(days: int = Query(7, ge=1, le=366)) -> dict:
        """What the plugins saved you: total, per plugin, per day."""
        return await argus.store.read(lambda c: daily.time_saved(c, time.time(), days))

    @app.post("/brief", dependencies=guarded)
    async def brief_now() -> dict:
        """Send the morning brief now (to try it)."""
        return await argus.send_brief()

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
    async def log_read(name: str, after: int | None = Query(None, ge=0),
                       lines: int = Query(300, ge=1, le=2000)) -> dict:
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
        async with change_lock:  # Undo and Wrong on the same change: one at a time
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
                job = await argus.jobs.claim(worker_id, body.capabilities, body.plugins, body.min_priority)
            if job is not None:
                out = job_json(job, await argus.jobs.steps(job.id))
                p = argus.plugin_host.plugins.get(job.plugin)
                if p is not None:  # current settings (live, config): argusd may have restarted since register
                    out["plugin_info"] = p.info()
                    out["plugin_info"]["lessons"] = await argus.store.read(
                        lambda c, pid=job.plugin: guidance.active_lessons(c, pid))
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

    @app.post("/workers/{worker_id}/claude", dependencies=guarded)
    async def worker_claude(worker_id: str, body: ClaudeAuth) -> dict:
        """A worker's `claude auth status`: the phone hears when Claude is logged out."""
        await argus.models.auth_seen(body.logged_in, worker_id, body.detail)
        return {"ok": True}

    @app.get("/models", dependencies=guarded)
    async def models() -> dict:
        return await argus.models.snapshot()

    @app.get("/models/usage", dependencies=guarded)
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

    @app.get("/models/ollama", dependencies=guarded)
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

    # -------------------------------------------------------------- Argus's own settings (Helios > Settings)

    @app.get("/argus-settings", dependencies=guarded)
    async def argus_settings() -> dict:
        over = await argus.store.read(settings_mod.load)
        return {"settings": settings_mod.listing(argus.cfg, argus.base_settings, over)}

    @app.put("/argus-settings", dependencies=guarded)
    async def argus_settings_change(body: dict[str, Any]) -> dict:
        """{setting: value} changes it now and keeps it; {setting: null} goes back to argus.yaml."""
        try:
            await argus.change_settings(body)
        except settings_mod.SettingError as e:
            raise HTTPException(status_code=422, detail=str(e)) from None
        return await argus_settings()

    # -------------------------------------------------------------- the inbox: everything waiting for you

    @app.get("/inbox", dependencies=guarded)
    async def inbox() -> dict:
        """Approvals, lessons the nightly review proposes, and Ari's open questions, newest first."""
        approvals = await argus.approvals.list("pending", limit=100)

        def fn(c):
            lessons = [dict(r) for r in c.execute(
                "SELECT l.id, l.playbook, l.text, l.evals, l.created_at, p.plugin, p.name FROM lessons l"
                " LEFT JOIN playbooks p ON p.key = l.playbook WHERE l.state = 'proposed' ORDER BY l.created_at DESC")]
            return lessons, ari_mod.open_questions(c, time.time() - 7 * 86400)

        lessons, questions = await argus.store.read(fn)
        items = [{"kind": "approval", "id": a["id"], "title": a["title"], "plugin": a.get("plugin"),
                  "type": a.get("type"), "at": a["created_at"]} for a in approvals]
        items += [{"kind": "lesson", "id": x["id"], "title": f"A lesson for {x.get('name') or x['playbook']}",
                   "text": x["text"],
                   "plugin": x.get("plugin"), "evals": json.loads(x["evals"]) if x.get("evals") else None,
                   "at": x["created_at"]} for x in lessons]
        items += [{"kind": "question", "id": q["conv"], "title": q["text"] or "Ari asks", "chat": q["title"],
                   "plugin": "ari", "at": q["at"]} for q in questions]
        items.sort(key=lambda x: -x["at"])
        return {"items": items, "counts": {k: sum(1 for i in items if i["kind"] == k)
                                           for k in ("approval", "lesson", "question")}}

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
            summary=body.summary, link=body.link, step=body.step, image=body.image)
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
                                       " connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none';"
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

    @app.patch("/schedules/{sid}", dependencies=guarded)
    async def schedule_edit(sid: str, body: ScheduleEdit) -> dict:
        """Pause or resume one of your schedules (the ones from argus.yaml and plugins follow their files)."""
        def fn(c):
            r = c.execute("SELECT owner, cron FROM schedules WHERE id = ?", (sid,)).fetchone()
            if r is None or r["owner"] != "you":
                return False
            nxt = next_run(r["cron"], time.time()) if body.enabled else None
            c.execute("UPDATE schedules SET enabled = ?, next_run_at = COALESCE(?, next_run_at), updated_at = ?"
                      " WHERE id = ?", (int(body.enabled), nxt, time.time(), sid))
            return True
        if not await argus.store.write(fn):
            raise HTTPException(status_code=404, detail=f"no schedule of yours called {sid}")
        return {"ok": True}

    @app.delete("/schedules/{sid}", dependencies=guarded)
    async def schedule_delete(sid: str) -> dict:
        n = await argus.store.write(lambda c: c.execute("DELETE FROM schedules WHERE id = ? AND owner = 'you'",
                                                        (sid,)).rowcount)
        if not n:
            raise HTTPException(status_code=404, detail=f"no schedule of yours called {sid}")
        return {"ok": True}

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

    def plugin_or_404(pid: str):
        p = argus.plugin_host.plugins.get(pid)
        if p is None:
            raise HTTPException(status_code=404, detail=f"no plugin {pid}")
        return p

    @app.get("/plugins/{pid}", dependencies=guarded)
    async def plugin_detail(pid: str) -> dict:
        """Everything Helios's page for one plugin shows: what it is, its buttons and triggers, settings (with
        where each value comes from), Ari's tools, recent runs, time saved, learning."""
        p = plugin_or_404(pid)
        m = p.manifest
        saved = await state_get(pid, "_settings") or {}
        mine = saved.get("config") or {}
        settings = [{"name": k, "type": f.type, "label": f.label or k.replace("_", " "), "choices": f.choices,
                     "default": f.default, "value": p.config.get(k),
                     "source": "you" if k in mine else "argus.yaml" if (p.base_config or {}).get(k) != f.default
                     else "default"} for k, f in m.config.items()]
        scheds = [s for s in await argus.scheduler.list() if s["plugin"] == pid]
        runs = [job_json(j) for j in await argus.jobs.list_jobs(None, 15, pid)]
        week = await argus.store.read(lambda c: daily.time_saved(c, time.time(), 7))
        learn = await argus.store.read(lambda c: guidance.overview(c, pid))
        t = m.triggers
        return {
            "id": pid, "name": m.name, "version": m.version, "description": m.description, "runs_on": m.runs_on,
            "needs": m.job_needs(), "live": p.live,
            "live_from": "you" if saved.get("live") is not None else "argus.yaml",
            "group": m.helios.node.group, "path": str(p.path),
            "buttons": [{"workflow": x.manual.workflow, "label": x.manual.label} for x in t if x.manual],
            "schedules": [{"id": s["id"], "cron": s["cron"], "workflow": s["workflow"], "enabled": s["enabled"],
                           "next_run_at": s["next_run_at"], "last_run_at": s["last_run_at"]} for s in scheds],
            "watches": [{"workflow": x.folder_watch.workflow, "paths": x.folder_watch.paths}
                        for x in t if x.folder_watch],
            "share": [x.model_dump() for x in m.share],
            "tools": [{"name": x.name, "description": x.description, "risky": x.risky} for x in m.ari.tools],
            "rules": m.helios.rules.label if m.helios.rules else None,
            "wrong": m.helios.wrong.label if m.helios.wrong else None,
            "permissions": m.permissions.model_dump(),
            "settings": settings,
            "runs": runs,
            "saved": next((x for x in week["plugins"] if x["plugin"] == pid), {"seconds": 0, "jobs": 0}),
            "learning": learn,
        }

    @app.put("/plugins/{pid}/settings", dependencies=guarded)
    async def plugin_settings(pid: str, body: PluginSettings) -> dict:
        """Change a plugin from Helios: live or dry-run, and its settings. Takes effect with the next job."""
        from ..plugins import SettingError, check_setting

        p = plugin_or_404(pid)
        saved = {} if body.reset else dict(await state_get(pid, "_settings") or {})
        if "live" in body.model_fields_set:
            saved["live"] = body.live
        if body.config:
            mine = dict(saved.get("config") or {})
            for k, v in body.config.items():
                f = p.manifest.config.get(k)
                if f is None:
                    raise HTTPException(status_code=422, detail=f"{pid} has no setting {k!r}")
                if v is None:
                    mine.pop(k, None)
                    continue
                try:
                    mine[k] = check_setting(f, k, v)
                except SettingError as e:
                    raise HTTPException(status_code=422, detail=str(e)) from None
            saved["config"] = mine
        await state_set(pid, "_settings", saved or None)
        p.apply(saved)
        await argus.store.write(lambda c: insert_event(c, time.time(), "plugin.settings", src="helios", dst=pid,
                                                       data={"live": p.live, "changed": sorted(body.config or {})}))
        return {"live": p.live, "config": p.config}

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
                if j.state.value in ("queued", "retry") and j.workflow in ("sleep", "shutdown", "restart",
                                                                            "auto_shutdown"):
                    await argus.jobs.cancel(j.id)
                    dropped.append(j.id)
        needs = set(argus.cfg.power.pc_needs)
        if not any(w["state"] == "online" and set(w["capabilities"]) & needs for w in await argus.registry.workers()):
            if action == "cancel":  # the PC is off: nothing to cancel on it
                return {"id": None, "created": False, "dropped": dropped}
            raise HTTPException(status_code=409, detail="the PC is off")
        job_id, created = await argus.jobs.enqueue(
            "power", action, {"delay": argus.cfg.power.shutdown_delay_seconds}, needs=["desktop"], priority=100,
            dedupe_key=f"power:{action}", source="helios")
        return {"id": job_id, "created": created, "dropped": dropped}

    @app.post("/jobs/{job_id}/wait", dependencies=guarded)
    async def wait(job_id: str, body: Wait) -> dict:
        return job_json(await argus.jobs.wait(job_id, body.worker, body.reason))

    return app
