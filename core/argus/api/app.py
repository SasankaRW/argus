"""The Argus HTTP API. In C1 it serves health and version; jobs, events and approvals come in C4-C8."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

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
