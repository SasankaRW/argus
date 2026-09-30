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

from ..models import EscalationExhausted
from .client import LeaseLostError


def _data_url(b: bytes) -> str:
    kind = "png" if b.startswith(b"\x89PNG") else "webp" if b[8:12] == b"WEBP" else "jpeg"
    return f"data:image/{kind};base64,{base64.b64encode(b).decode()}"


def _advice(e: Exception) -> str:
    """The rejected answers from the local models, for Claude."""
    lines = []
    for t in getattr(e, "trail", [])[-4:]:
        if "rejected" in t:
            lines.append(f"{t['tier']} answered {t.get('reply', '')!r}, rejected: {t['rejected']}")
        elif "error" in t:
            lines.append(f"{t['tier']} failed: {t['error']}")
        elif "skipped" in t:
            lines.append(f"{t['tier']} skipped: {t['skipped']}")
    return "\n".join(lines) or "The local models could not do this."


class PermanentError(Exception):
    """Raise from a workflow when retrying cannot help (bad input, missing file). The job goes to dead."""


class ToolFailed(Exception):
    """ctx.tool(): the tool ran and failed (the message says why)."""


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

    def tool(self, body: dict) -> dict: ...

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
        self.data_dir: Any = None  # a folder on this machine that only this plugin uses (its index, caches)
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
            tiers: list[str] | None = None, attempts: int | None = None, images: list[bytes] | None = None,
            claude_last: bool = True) -> Any:
        """Ask the models, cheapest tier first, escalating when the answer fails the schema or the check.

        Returns the answer (a `schema` instance, or text without a schema). Call it inside `ctx.step`, so a
        retried job reuses the checkpointed answer instead of asking again. `ctx.last_answer` has the details
        (tier used, attempts, the trail of rejected answers).

        For every plugin: when the tiers asked for all fail and Claude wasn't among them, Claude gets one try
        (with the pictures too), within the plugin's daily Claude budget (`permissions.claude_calls_per_day`,
        default `claude.plugin_calls_per_day`). `claude_last=False` skips that (e.g. to try a cheaper route
        first). If it still fails, `EscalationExhausted`: skip the item, or ask you with `ctx.ask_me`.
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
        try:
            ans = self._router.ask(playbook, input, schema=schema, check=check, chain=tiers, attempts=attempts,
                                   images=pics)
        except EscalationExhausted as e:
            claude = self._claude_tier()
            tried = tiers or list(self._router.chain)
            if not claude_last or claude is None or claude in tried:
                raise
            self._router.board.event("model.escalated", tried[-1].lower(), claude.lower(),
                                     {"from_tier": tried[-1], "to_tier": claude, "reason": "local models failed"})
            ans = self._ask_claude(playbook, input, schema, check, pics, _advice(e), e)
        return self._took(ans)

    def _took(self, ans: Any) -> Any:
        order = list(self._router.chain) if self._router else []
        if self._tier is None or (ans.tier in order and self._tier in order
                                  and order.index(ans.tier) > order.index(self._tier)):
            self._tier = ans.tier
        self.last_answer = ans
        return ans.value

    def _ask_claude(self, playbook: str, input: Any, schema: Any, check: Any, pics: list[str] | None,
                    advice: str | None, before: EscalationExhausted | None = None, web: bool = False) -> Any:
        claude = self._claude_tier()
        if claude is None:
            raise EscalationExhausted("Claude is not available on this worker (claude CLI not found)",
                                      before.trail if before else [])
        try:
            return self._router.ask(playbook, input, schema=schema, check=check,  # type: ignore[union-attr]
                                    chain=[claude], attempts=1, images=pics, advice=advice, web=web)
        except EscalationExhausted as e2:
            if before is None:
                raise
            raise EscalationExhausted(f"{before} and {claude}: {_advice(e2)}", before.trail + e2.trail) from None

    def local_tiers(self) -> list[str]:
        """The chain without Claude (the models on your machines), within what the plugin may use."""
        if self._router is None:
            return []
        tiers = [t for t in self._router.chain if getattr(self._router.providers.get(t), "kind", "") != "claude"]
        return [t for t in tiers if self._allowed_tiers is None or t in self._allowed_tiers] or tiers[:1]

    def _claude_tier(self) -> str | None:
        if self._router is None:
            return None
        return next((t for t, p in self._router.providers.items() if getattr(p, "kind", "") == "claude"), None)

    def claude(self, prompt: str, input: Any = "", *, schema: Any = None, check: Any = None,
               images: list[bytes] | None = None, advice: str | None = None, web: bool = False) -> Any:
        """Ask Claude directly (one try, pictures too). Counts against the plugin's and the global daily cap.
        `web`: Claude may search and read the web (and nothing else), for current things."""
        if self._router is None:
            raise RuntimeError("this worker has no model configuration (is it connected to argusd?)")
        pics = [base64.b64encode(b).decode() for b in images] if images else None
        return self._took(self._ask_claude(prompt, input, schema, check, pics, advice, web=web))

    def _key(self, what: str) -> str:
        if self._step_idx is None:
            raise RuntimeError(f"call ctx.{what}() inside ctx.step(...), so a retried job does not ask twice")
        self._asks += 1
        return f"{self.job_id}:{self._step_idx}:{self._asks}"

    def approve(self, type: str, title: str, fields: dict | None = None, *, items: list[dict] | None = None,
                summary: list[str] | None = None, link: str | None = None, image: bytes | None = None) -> Decision:
        """Ask you. Returns your Decision once you answered; until then the job waits (no worker held).

        type: "entry" (one item, fields editable), "batch" (items, one decision; count and total of their
        "amount" are added up by Argus) or "draft" (summary lines and a link to review).
        """
        self._check_lease()
        body = {"key": self._key("approve"), "type": type, "title": title, "fields": fields or {},
                "items": items or [], "summary": summary or [], "link": link, "step": self._step}
        if image:
            body["image"] = _data_url(image)
        a = self._reporter.approval(body)
        if a["state"] == "pending":
            raise WaitSignal(f"approval:{a['id']}")
        return Decision(approved=a["state"] == "approved", state=a["state"], fields=a.get("answer") or {},
                        by=a.get("decided_by"), approval_id=a["id"])

    def embed(self, texts: list[str], model: str = "nomic-embed-text") -> list[list[float]] | None:
        """Vectors for search by meaning, from the local embedding model; None when it isn't available."""
        prov = next((p for p in (self._router.providers.values() if self._router else [])
                     if getattr(p, "kind", "") == "ollama" and hasattr(p, "embed")), None)
        if prov is None or not texts:
            return None
        try:
            return prov.embed(list(texts), model)
        except Exception as e:  # not pulled, Ollama down: search falls back to words
            self.log.warning("embeddings unavailable", extra={"error": str(e)[:200]})
            return None

    def tool(self, name: str, args: dict[str, Any] | None = None) -> Any:
        """Use one of Ari's tools (GET /tools): a built-in one answers at once; a plugin's tool runs as its own job,
        and this job waits (no worker held) until it is done, then this step runs again and gets the result.
        Raises ToolFailed when the tool failed. Call it inside ctx.step."""
        self._check_lease()
        r = self._reporter.tool({"key": self._key("tool"), "name": name, "args": args or {}})
        if r.get("state") == "pending":
            raise WaitSignal(f"tool:{r['job']}")
        if r.get("state") == "failed":
            raise ToolFailed(r.get("error") or "the tool failed")
        return r.get("result")

    def saved(self, seconds: float, key: str | None = None) -> None:
        """This job saved you about `seconds` of your time (Helios adds it up per week; the evening summary per
        day). Counted once per job and `key` even if the job is retried; not in dry-run."""
        if self.dry_run or seconds <= 0:
            return
        self.emit("saved", seconds=int(seconds), key=key or self._step or "job")

    def ask_me(self, title: str, fields: dict[str, Any], *, summary: list[str] | None = None,
               image: bytes | None = None) -> dict[str, Any] | None:
        """The last resort when neither the local models nor Claude could do it: ask you (Helios and the phone).
        `fields` are filled in with a guess (or empty) for you to edit; returns them as you approved them, or None
        when you rejected it (or it expired). Like `approve`, the job waits meanwhile; call it inside `ctx.step`.
        `image`: a small JPEG/PNG to decide by (keep it under ~250 KB)."""
        d = self.approve("entry", title, fields, summary=summary, image=image)
        return dict(d.fields) if d else None

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
