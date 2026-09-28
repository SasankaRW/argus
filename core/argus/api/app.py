"""The Argus HTTP API. In C1 it serves health and version; jobs, events and approvals come in C4-C8."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from ..context import Argus


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

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def home() -> str:
        h = argus.health()
        counts = await argus.jobs.counts()
        dot = "#3DD68C" if h["status"] == "ok" else "#FF7A6B"
        jobs_line = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "no jobs yet"
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Argus</title>
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
<tr><td>Jobs</td><td>{jobs_line}</td></tr>
<tr><td>Uptime</td><td>{h["uptime_seconds"]} s</td></tr></table>
<p class="m">Helios arrives in core step C6. Raw data: <a href="/health">/health</a> &middot;
<a href="/version">/version</a> &middot; <a href="/docs">API docs</a></p></main></body></html>"""

    @app.get("/health")
    async def health() -> JSONResponse:
        body = argus.health()
        return JSONResponse(body, status_code=200 if body["status"] == "ok" else 503)

    @app.get("/version")
    async def version() -> dict:
        return {"version": argus.version, "schema_version": argus.store.schema_version}

    @app.get("/jobs/counts")
    async def job_counts() -> dict:
        return await argus.jobs.counts()

    return app
