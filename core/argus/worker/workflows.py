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
"""

from __future__ import annotations

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

    @property
    def lease_lost(self) -> bool: ...


class Context:
    def __init__(self, job: dict[str, Any], reporter: StepReporter, log: logging.Logger | None = None):
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
        self._reporter.step(idx, name, "running", tier=tier)
        try:
            output = fn(*args, **kwargs)
        except (WaitSignal, LeaseLostError):
            raise
        except Exception as e:
            self._check_lease()
            self._reporter.step(idx, name, "failed", error=f"{type(e).__name__}: {e}", tier=tier)
            raise
        finally:
            self.idempotency_key = None
        self._check_lease()
        self._reporter.step(idx, name, "succeeded", output=output, tier=tier)
        return output

    def wait(self, reason: str) -> None:
        """Park the job until it is resumed (for example after an approval). Finished steps stay finished."""
        raise WaitSignal(reason)
