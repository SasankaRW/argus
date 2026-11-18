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
    GET  /phone/inbox?wait=25         the phone app collects its notifications (long poll, token)
    POST /phone/inbox/ack             {ids}: the app showed them (token)
    GET  /phone/notifications         the notifications sent to the phone, newest first (token)
    POST /outbox/test                 send a test notification (token)
    GET  /deploy                      the running commit and the laptop updater's last check and update (token)

Scheduler and triggers (C9):
    GET  /schedules                   schedules with their next run (token)
    POST /schedules/{id}/run          run a schedule now (token)
    POST /triggers/file               {worker, trigger, path, sha256, size} from a worker's folder watcher (token)
                                      -> {status: queued|duplicate, job_id}; 429 when the plugin's queue is full
    POST /hooks/{name}                a signed webhook (no Argus token; the signature is the check)
Controls (token): POST /plugins/{id}/stop|start|restart, POST /workers/{id}/pause|resume|restart,
    GET /control/state, POST /control/pause-all|resume-all, GET /logs/{component}?level=&since=. See docs/controls.md.
Errors: 404 unknown job, 409 lease lost, transition not allowed or plugin stopped, 429 plugin queue full.
"""

from __future__ import annotations

import hmac
import mimetypes
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ..approvals import ApprovalClosed, ApprovalError, ApprovalNotFound, BadToken
from ..context import Argus
from ..jobs import InvalidTransition, JobNotFound, LeaseLost, PluginOff, PluginPaused, QueueFull
from ..tokens import Tokens
from ..tools import Tools
from ..triggers import BadSignature, TriggerError, UnknownTrigger
from . import mcp
from .routes import (
    approvals,
    ari,
    ari_memory,
    ari_timeline,
    ask,
    controls,
    deploy,
    events,
    guidance,
    island,
    jobs,
    models,
    ops,
    phone,
    plugins,
    public,
    schedules,
    send,
    settings,
    shares,
    voice,
    workers,
)
from .routes import tokens as tokens_routes
from .routes import tools as tools_routes
from .routes._deps import HELIOS_DIR, Deps, job_json

mimetypes.add_type("application/manifest+json", ".webmanifest")  # Helios as an installable app (share menu)


class _HeliosFiles(StaticFiles):
    """Hashed assets cache forever; index.html must be re-checked so a new release shows up on reload."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if path.replace("\\", "/").startswith("assets/"):  # Starlette passes an OS path (assets\x.js on Windows)
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            resp.headers["Cache-Control"] = "no-cache"
        return resp


def create_app(argus: Argus) -> FastAPI:
    """Each area's routes live in routes/<area>.py (build_router); this wires them, the exception handlers and the
    token guard. The routers are included in the order the routes were once written in, which is the order Starlette
    tries them in."""
    from ..supervisor import head

    commit = head()  # the code this argusd runs: dev.ps1 deployed waits for the laptop to report it (GET /deploy)
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await argus.start()
        # the voice's first sentence shouldn't pay for its start-up
        threading.Thread(target=lambda: deps.expressive().warm(), daemon=True, name="voice-warm").start()
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

    @app.exception_handler(PluginOff)
    async def _off(request: Request, exc: PluginOff):
        return JSONResponse({"error": "plugin_off", "detail": str(exc)}, status_code=409)

    @app.exception_handler(PluginPaused)
    async def _paused(request: Request, exc: PluginPaused):
        return JSONResponse({"error": "plugin_paused", "detail": str(exc)}, status_code=409)

    tokens = Tokens(argus.store)
    app.state.tokens = tokens

    def worker_ok(authorization: str | None) -> bool:
        """The worker token itself (all open when none is set). Only this may manage the named tokens."""
        token = argus.cfg.secrets.worker_token
        if not token:
            return True
        return authorization is not None and hmac.compare_digest(authorization, f"Bearer {token}")

    def token_ok(authorization: str | None, query_token: str | None = None) -> bool:
        """The worker token, or a live named token (header or ?token=)."""
        token = argus.cfg.secrets.worker_token
        if not token:
            return True
        if worker_ok(authorization):
            return True
        if query_token is not None and hmac.compare_digest(query_token, token):
            return True
        bearer = authorization[7:] if authorization is not None and authorization[:7].lower() == "bearer " else None
        return tokens.check(bearer) is not None or tokens.check(query_token) is not None

    def auth(authorization: str | None = Header(default=None)) -> None:
        if not token_ok(authorization):
            raise HTTPException(status_code=401, detail="missing or wrong token")

    guarded = [Depends(auth)]

    tools = Tools(argus, job_json)
    app.state.tools = tools
    deps = Deps(argus, commit, token_ok, guarded, tools)
    deps.tokens = tokens
    deps.worker_ok = worker_ok

    app.include_router(public.build_router(deps))
    if (HELIOS_DIR / "index.html").exists():
        app.mount("/helios", _HeliosFiles(directory=HELIOS_DIR, html=True), name="helios")
    app.include_router(jobs.build_router(deps))
    app.include_router(ask.build_router(deps))

    async def mcp_auth(request: Request) -> bool:
        return token_ok(request.headers.get("authorization"))

    mcp.register(app, argus, mcp_auth, tools)

    for area in (tools_routes, island, phone, guidance, models, ari, ari_timeline, ari_memory, voice, plugins,
                 shares, ops, events, workers, settings, approvals, schedules, controls, send, tokens_routes,
                 deploy):
        app.include_router(area.build_router(deps))
    return app
