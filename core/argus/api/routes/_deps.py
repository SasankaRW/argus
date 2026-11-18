"""What the routers share: the Argus instance, the token guard, the tools, and a few functions one router owns
and another calls (set by the owner's build_router, read when a request comes in)."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...context import Argus
from ...jobs import Job, Step
from ...tools import Tools

HELIOS_DIR = Path(__file__).resolve().parent.parent.parent / "helios_dist"  # built by helios/ (npm run build)


def job_json(job: Job, steps: list[Step] | None = None) -> dict[str, Any]:
    d = dataclasses.asdict(job)
    d["state"] = job.state.value
    d["needs"] = list(job.needs)
    if steps is not None:
        d["steps"] = [dataclasses.asdict(s) for s in steps]
    return d


class Deps:
    def __init__(self, argus: Argus, commit: str, token_ok: Callable[..., bool], guarded: list, tools: Tools):
        self.argus = argus
        self.commit = commit  # the code this argusd runs
        self.token_ok = token_ok
        self.guarded = guarded  # dependencies=[Depends(auth)]
        self.tools = tools
        self.tokens: Any = None  # tokens.Tokens, set by create_app
        self.worker_ok: Callable[[str | None], bool] = lambda _a: True  # worker token only (create_app)
        # Owned by one router, called from another. Each is set in its owner's build_router.
        self.phone_where: Any = None  # routes/phone.py
        self.ring_phone: Any = None  # routes/phone.py
        self.do_action: Any = None  # routes/ask.py
        self.set_game: Any = None  # routes/ari.py
        self.overlays: Any = None  # routes/ari.py (host -> last ping from the PC's Ari popup)
        self.vocabulary: Any = None  # routes/ari.py
        self.able: Any = None  # routes/voice.py
        self.expressive: Any = None  # routes/voice.py
        self.remember_in_background: Any = None  # routes/ari_memory.py
        self.laptop_power_action: Any = None  # routes/settings.py
        self.power_action: Any = None  # routes/settings.py
