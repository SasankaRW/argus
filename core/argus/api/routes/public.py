"""Public routes: the home page, /lite, /status, /health and /version (and Helios's 'not built' page)."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..home import HOME_HTML
from ._deps import HELIOS_DIR, Deps


def build_router(deps: Deps) -> APIRouter:
    argus = deps.argus
    router = APIRouter()
    commit = deps.commit

    @router.get("/", include_in_schema=False)
    async def home(request: Request):
        """Helios when it is built in; otherwise the small status page. Keeps ?token= and friends."""
        if (HELIOS_DIR / "index.html").exists():
            q = request.url.query
            return RedirectResponse("/helios/" + (f"?{q}" if q else ""))
        return HTMLResponse(HOME_HTML)

    @router.get("/lite", response_class=HTMLResponse, include_in_schema=False)
    async def lite() -> str:
        """The small status page, always available."""
        return HOME_HTML

    if not (HELIOS_DIR / "index.html").exists():  # when built in, create_app mounts it
        @router.get("/helios", include_in_schema=False)
        @router.get("/helios/", include_in_schema=False)
        async def helios_missing():
            return HTMLResponse("<p>Helios is not built into this copy of Argus. Run <code>.\\scripts\\dev.ps1 "
                                "helios</code> or use <a href='/lite'>/lite</a>.</p>", status_code=404)


    status_cache: dict[str, Any] = {"at": 0.0, "body": None}

    @router.get("/status")
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

    @router.get("/health")
    async def health() -> JSONResponse:
        body = argus.health()
        return JSONResponse(body, status_code=200 if body["status"] == "ok" else 503)

    @router.get("/version")
    async def version() -> dict:
        return {"version": argus.version, "schema_version": argus.store.schema_version, "commit": commit}

    return router
