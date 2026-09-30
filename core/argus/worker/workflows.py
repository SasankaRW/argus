"""Workflows: the code a worker runs for a job.

A plugin registers workflows with the `workflow` decorator. A workflow is a plain function that takes a
`Context` and splits its work into named steps:

    from argus.worker import workflow

    @workflow("files", "rename", needs=["fs"])
    def rename(ctx):
        names = ctx.step("scan", scan_folder, ctx.input["folder"])
        ctx.step("rename", rename_all, names)
        return {"renamed": len(names)}

Each finished step is a checkpoint. If the worker dies, the job is retried and finished steps are skipped:
`ctx.step` returns the saved output instead of running the function again. Step outputs must be JSON.

Inside a step, `ctx.idempotency_key` is "<job id>:<step index>"; pass it to anything with side effects
outside Argus so a retried step does not do the same thing twice.

`ctx.approve(...)` asks you and parks the job until you answer (the worker is free meanwhile); when the job comes
back, the step runs again and `ctx.approve` returns your `Decision`. `ctx.notify(...)` sends a phone message,
once per step even if the step is retried.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from .client import LeaseLostError


class PermanentError(Exception):
    """Raise from a workflow when retrying cannot help (bad input, missing file). The job goes to dead."""


class WaitSignal(Exception):  # noqa: N818 - a signal, not an error
    """Raised by `ctx.wait()`. The job parks in `waiting` until someone resumes it."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Decision:
    """Your answer to `ctx.approve`. Truthy when approved; `fields` holds the values as approved (with edits)."""

    approved: bool
    state: str  # approved, rejected or expired
    fields: dict[str, Any]
    by: str | None
    approval_id: str

    def __bool__(self) -> bool:
        return self.approved


@dataclass(frozen=True)
class Workflow:
    plugin: str
    name: str
    fn: Callable[[Context], Any]
    needs: tuple[str, ...] = ()


@dataclass
class WorkflowRegistry:
    items: dict[tuple[str, str], Workflow] = field(default_factory=dict)

    def add(self, wf: Workflow) -> None:
        key = (wf.plugin, wf.name)
        if key in self.items and self.items[key].fn is not wf.fn:
            raise ValueError(f"workflow {wf.plugin}.{wf.name} registered twice")
        self.items[key] = wf

    def get(self, plugin: str, name: str) -> Workflow | None:
        return self.items.get((plugin, name))

    @property
    def plugins(self) -> list[str]:
        return sorted({p for p, _ in self.items})

    @property
    def needs(self) -> list[str]:
        return sorted({n for wf in self.items.values() for n in wf.needs})


REGISTRY = WorkflowRegistry()


def workflow(plugin: str, name: str, needs: Iterable[str] = (), registry: WorkflowRegistry | None = None):
    """Register a function as the workflow `plugin`.`name`."""

    def deco(fn: Callable[[Context], Any]) -> Callable[[Context], Any]:
        (registry or REGISTRY).add(Workflow(plugin, name, fn, tuple(needs)))
        return fn

    return deco


class StepReporter:
    """What the context needs from the worker: report steps and say whether the lease is still ours."""

    def step(self, idx: int, name: str, state: str, *, output: Any = None, error: str | None = None,
             tier: str | None = None) -> None: ...

    def approval(self, body: dict) -> dict: ...

    def notify(self, body: dict) -> dict: ...

    @property
    def lease_lost(self) -> bool: ...


class Context:
    def __init__(self, job: dict[str, Any], reporter: StepReporter, log: logging.Logger | None = None,
                 router: Any = None):
        self.job = job
        self.job_id: str = job["id"]
        self.input: dict[str, Any] = job.get("input") or {}
        self.attempt: int = job.get("attempt", 1)
        self.log = log or logging.getLogger(f"argus.plugin.{job.get('plugin')}")
        self._reporter = reporter
        self._done = {s["idx"]: s for s in job.get("steps", []) if s.get("state") == "succeeded"}
        self._idx = 0
        self.idempotency_key: str | None = None
        self.skipped: list[str] = []
        self._router = router  # argus.models.Router bound to this job, set by the worker
        self._tier: str | None = None  # highest tier used by ctx.llm inside the current step
        self._step: str | None = None
        self._step_idx: int | None = None
        self._asks = 0  # approve/notify calls in the current step, for their idempotency keys
        self.last_answer: Any = None
        # Set by the worker for plugins loaded from a folder (argus.worker.plugins); None for built-in workflows.
        self.plugin: Any = None
        self.config: dict[str, Any] = {}
        self.dry_run = False
        self.files: Any = None
        self.http: Any = None
        self.secrets: Any = None
        self.store: Any = None
        self.emit: Callable[..., None] = lambda name, **data: None
        self._allowed_tiers: list[str] | None = None  # None: any tier
        self.shared: Callable[[str], bytes] | None = None  # a shared file's bytes, for jobs from Helios > Share
        self.shared_backup: Callable[[str], bytes] | None = None  # built-in backup copy only
        self._unsent: list = []  # plugin events waiting to be sent (set by the worker)
        self._flush_trace: Callable[[], None] = lambda: None

    def _check_lease(self) -> None:
        if self._reporter.lease_lost:
            raise LeaseLostError(409, {"error": "lease_lost", "detail": f"lease on {self.job_id} was lost"})

    def step(self, name: str, fn: Callable[..., Any], *args: Any, tier: str | None = None, **kwargs: Any) -> Any:
        """Run one checkpointed step and return its output (or the saved output if it already ran)."""
        idx = self._idx
        self._idx += 1
        self._check_lease()
        done = self._done.get(idx)
        if done is not None:
            if done["name"] != name:
                self.log.warning("step name changed since last run", extra={"idx": idx, "was": done["name"],
                                                                            "now": name})
            self.skipped.append(name)
            return done.get("output")
        self.idempotency_key = f"{self.job_id}:{idx}"
        self._tier, self._step, self._step_idx, self._asks = None, name, idx, 0
        self._reporter.step(idx, name, "running", tier=tier)
        try:
            output = fn(*args, **kwargs)
        except (WaitSignal, LeaseLostError):
            raise
        except Exception as e:
            self._check_lease()
            self._reporter.step(idx, name, "failed", error=f"{type(e).__name__}: {e}", tier=tier or self._tier)
            raise
        finally:
            self.idempotency_key = None
            self._step = self._step_idx = None
        self._check_lease()
        self._reporter.step(idx, name, "succeeded", output=output, tier=tier or self._tier)
        return output

    def llm(self, playbook: str, input: Any, *, schema: Any = None, check: Any = None,
            tiers: list[str] | None = None, attempts: int | None = None, images: list[bytes] | None = None) -> Any:
        """Ask the models, cheapest tier first, escalating when the answer fails the schema or the check.

        Returns the answer (a `schema` instance, or text without a schema). Call it inside `ctx.step`, so a
        retried job reuses the checkpointed answer instead of asking again. `ctx.last_answer` has the details
        (tier used, attempts, the trail of rejected answers).
        """
        if self._router is None:
            raise RuntimeError("this worker has no model configuration (is it connected to argusd?)")
        if self._allowed_tiers is not None:
            tiers = [t for t in (tiers or self._router.chain) if t in self._allowed_tiers]
            if not tiers:
                from .plugins import PermissionDenied
                raise PermissionDenied(f"{self.job.get('plugin')} may not use these models "
                                       "(permissions.models in plugin.yaml)")
        pics = [base64.b64encode(b).decode() for b in images] if images else None
        ans = self._router.ask(playbook, input, schema=schema, check=check, chain=tiers, attempts=attempts,
                               images=pics)
        order = list(self._router.chain)
        if self._tier is None or (ans.tier in order and self._tier in order
                                  and order.index(ans.tier) > order.index(self._tier)):
            self._tier = ans.tier
        self.last_answer = ans
        return ans.value

    def claude(self, prompt: str, input: Any = "", *, schema: Any = None, check: Any = None) -> Any:
        """Ask Claude directly (the Claude tier only). Counts against the daily cap."""
        tiers = [t for t, p in (self._router.providers.items() if self._router else []) if p.kind == "claude"]
        if not tiers:
            raise RuntimeError("Claude is not available on this worker (claude CLI not found)")
        return self.llm(prompt, input, schema=schema, check=check, tiers=tiers[:1], attempts=1)

    def _key(self, what: str) -> str:
        if self._step_idx is None:
            raise RuntimeError(f"call ctx.{what}() inside ctx.step(...), so a retried job does not ask twice")
        self._asks += 1
        return f"{self.job_id}:{self._step_idx}:{self._asks}"

    def approve(self, type: str, title: str, fields: dict | None = None, *, items: list[dict] | None = None,
                summary: list[str] | None = None, link: str | None = None) -> Decision:
        """Ask you. Returns your Decision once you answered; until then the job waits (no worker held).

        type: "entry" (one item, fields editable), "batch" (items, one decision; count and total of their
        "amount" are added up by Argus) or "draft" (summary lines and a link to review).
        """
        self._check_lease()
        body = {"key": self._key("approve"), "type": type, "title": title, "fields": fields or {},
                "items": items or [], "summary": summary or [], "link": link, "step": self._step}
        a = self._reporter.approval(body)
        if a["state"] == "pending":
            raise WaitSignal(f"approval:{a['id']}")
        return Decision(approved=a["state"] == "approved", state=a["state"], fields=a.get("answer") or {},
                        by=a.get("decided_by"), approval_id=a["id"])

    def notify(self, title: str, text: str = "", *, priority: str = "default", tags: list[str] | None = None,
               link: str | None = None) -> bool:
        """Send a phone message (ntfy). Sent once even if this step runs again. Returns True if queued now."""
        self._check_lease()
        body = {"key": self._key("notify"), "title": title, "text": text, "priority": priority,
                "tags": tags or [], "link": link}
        return bool(self._reporter.notify(body).get("queued"))

    def wait(self, reason: str) -> None:
        """Park the job until it is resumed (for example after an approval). Finished steps stay finished."""
        raise WaitSignal(reason)
