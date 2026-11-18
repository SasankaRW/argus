"""Named API tokens: POST /tokens {name} (the token is shown once), GET /tokens, DELETE /tokens/{name}.

These accept only the worker token: a named token cannot make or revoke tokens."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from ...tokens import TokenError
from ._deps import Deps


class NewToken(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)


def build_router(deps: Deps) -> APIRouter:
    router = APIRouter()

    def worker_only(authorization: str | None = Header(default=None)) -> None:
        if not deps.token_ok(authorization):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        if not deps.worker_ok(authorization):
            raise HTTPException(status_code=403, detail="only the worker token manages tokens")

    only = [Depends(worker_only)]

    @router.post("/tokens", dependencies=only)
    async def create_token(body: NewToken) -> dict:
        try:
            token = await deps.tokens.create(body.name)
        except TokenError as e:
            raise HTTPException(status_code=409 if "exists" in str(e) else 422, detail=str(e)) from None
        return {"name": body.name.strip(), "token": token}

    @router.get("/tokens", dependencies=only)
    async def list_tokens() -> list[dict]:
        return await deps.tokens.list()

    @router.delete("/tokens/{name}", dependencies=only)
    async def revoke_token(name: str) -> dict:
        if not await deps.tokens.revoke(name):
            raise HTTPException(status_code=404, detail="no such active token")
        return {"ok": True, "name": name}

    return router
