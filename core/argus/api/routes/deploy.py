"""GET /deploy: what this argusd runs and how the laptop's updater (deploy/linux/argus-update.sh) is doing.
The updater leaves last-check.json and last-update.json in argusd's data dir (the database's folder)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from ...supervisor import head_subject
from ._deps import Deps

STALE_AFTER = 15 * 60  # the updater runs every 5 minutes; a check older than this means it isn't running


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return v if isinstance(v, dict) else None


def build_router(deps: Deps) -> APIRouter:
    argus = deps.argus
    router = APIRouter()
    subject = head_subject()  # once, at start-up: it is the code that is running

    @router.get("/deploy", dependencies=deps.guarded)
    async def deploy() -> dict:
        data = argus.cfg.db_path.parent
        last_check = read_json(data / "last-check.json")
        last_update = read_json(data / "last-update.json")
        checked = last_check.get("checked_at") if last_check else None
        stale = not isinstance(checked, int | float) or time.time() - checked > STALE_AFTER
        return {
            "commit": deps.commit,
            "commit_subject": subject,
            "started_at": argus.started_at,
            "last_check": last_check,
            "last_update": last_update,
            "stale": stale,
        }

    return router
