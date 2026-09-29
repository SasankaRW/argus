"""JobStore: every operation on jobs, each one a single transaction with its event.

Rules this module enforces:
- A job only changes state along ALLOWED (see states.py).
- Every state change writes an event in the same transaction, so the map and history never miss one.
- Only the worker holding a job's lease can move it forward, heartbeat it or record its steps.
- Finished steps are checkpoints: a retried job resumes after the last finished step.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from ..config import JobsConfig
from ..cron import in_window, window_opens
from ..db import Store
from ..events import insert_event
from ..ids import new_id
from ..outbox import add_message, ntfy_message
from ..registry import ensure_component
from .states import (
    ACTIVE_STATES,
    ALLOWED,
    CLAIMABLE_STATES,
    LEASED_STATES,
    JobState,
)

S = JobState


class JobError(Exception):
    """Base class for job errors."""


class JobNotFound(JobError):
    pass


class InvalidTransition(JobError):
    pass


class LeaseLost(JobError):
    """The caller no longer holds the job's lease (it expired, or another worker has it)."""


class QueueFull(JobError):
    pass


@dataclass(frozen=True)
class Job:
    id: str
    plugin: str
    workflow: str
    state: JobState
    priority: int
    needs: tuple[str, ...]
    dedupe_key: str | None
    attempt: int
    max_attempts: int
    run_after: float
    lease_owner: str | None
    lease_until: float | None
    wait_reason: str | None
    input: dict[str, Any]
    result: Any
    error: str | None
    created_at: float
    updated_at: float
    finished_at: float | None
    model_group: str | None = None
    run_window: str | None = None

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> Job:
        return cls(
            id=r["id"],
            plugin=r["plugin"],
            workflow=r["workflow"],
            state=JobState(r["state"]),
            priority=r["priority"],
            needs=tuple(json.loads(r["needs"])),
            dedupe_key=r["dedupe_key"],
            attempt=r["attempt"],
            max_attempts=r["max_attempts"],
            run_after=r["run_after"],
            lease_owner=r["lease_owner"],
            lease_until=r["lease_until"],
            wait_reason=r["wait_reason"],
            input=json.loads(r["input"]),
            result=json.loads(r["result"]) if r["result"] is not None else None,
            error=r["error"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            finished_at=r["finished_at"],
            model_group=r["model_group"] if "model_group" in r.keys() else None,
            run_window=r["run_window"] if "run_window" in r.keys() else None,
        )


@dataclass(frozen=True)
class Step:
    job_id: str
    idx: int
    name: str
    state: str
    tier_used: str | None
    output: Any
    error: str | None

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> Step:
        return cls(
            job_id=r["job_id"],
            idx=r["idx"],
            name=r["name"],
            state=r["state"],
            tier_used=r["tier_used"],
            output=json.loads(r["output"]) if r["output"] is not None else None,
            error=r["error"],
        )


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


class JobStore:
    def __init__(self, store: Store, cfg: JobsConfig | None = None, clock: Callable[[], float] = time.time,
                 windows: dict[str, str] | None = None):
        self.store = store
        self.cfg = cfg or JobsConfig()
        self.clock = clock
        self.windows = windows or {}
        self.gpu_model: str | None = None  # the model the last GPU job used: likely still loaded in Ollama

    # ------------------------------------------------------------------ helpers (writer thread)

    def _get(self, conn: sqlite3.Connection, job_id: str) -> Job:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFound(job_id)
        return Job.from_row(row)

    def _event(
        self,
        conn: sqlite3.Connection,
        now: float,
        kind: str,
        job: Job | None = None,
        *,
        step: str | None = None,
        src: str | None = None,
        dst: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        insert_event(conn, now, kind, job_id=job.id if job else None, step=step, src=src, dst=dst, data=data)

    def _move(
        self,
        conn: sqlite3.Connection,
        job: Job,
        dst: JobState,
        now: float,
        *,
        src_component: str = "argus",
        event_data: dict[str, Any] | None = None,
        **fields: Any,
    ) -> Job:
        if dst not in ALLOWED[job.state]:
            raise InvalidTransition(f"job {job.id}: {job.state} -> {dst} is not allowed")
        fields["state"] = dst.value
        fields["updated_at"] = now
        cols = ", ".join(f"{k} = ?" for k in fields)
        cur = conn.execute(
            f"UPDATE jobs SET {cols} WHERE id = ? AND state = ?",
            (*fields.values(), job.id, job.state.value),
        )
        if cur.rowcount != 1:  # someone else moved it first; the writer is single, so this is a bug guard
            raise InvalidTransition(f"job {job.id} changed state concurrently")
        if dst in (S.DEAD, S.CANCELLED):  # nobody is waiting for these answers any more
            conn.execute("UPDATE approvals SET state = 'expired', token_hash = NULL, remind_at = NULL,"
                         " decided_by = 'job ended', decided_at = ?, updated_at = ?"
                         " WHERE job_id = ? AND state = 'pending'", (now, now, job.id))
        if dst is S.DEAD:  # failures reach the phone at once (through the outbox, same transaction)
            add_message(conn, now, "ntfy", ntfy_message(
                f"Job failed: {job.plugin}.{job.workflow}",
                (fields.get("error") or "failed after retries")[:500] + f"\nJob {job.id}",
                priority="high", tags=["x"]), job_id=job.id)
        self._event(
            conn,
            now,
            f"job.{dst.value}",
            job,
            src=src_component,
            dst=job.plugin,
            data={"from": job.state.value, **(event_data or {})},
        )
        return self._get(conn, job.id)

    def _check_lease(self, job: Job, worker: str, now: float) -> None:
        if job.state not in LEASED_STATES or job.lease_owner != worker:
            raise LeaseLost(f"job {job.id}: {worker} does not hold the lease (state {job.state})")
        if job.lease_until is not None and job.lease_until < now:
            raise LeaseLost(f"job {job.id}: lease expired")

    def _backoff(self, attempt: int) -> float:
        seq = self.cfg.backoff_seconds
        return float(seq[min(max(attempt, 1) - 1, len(seq) - 1)])

    # ------------------------------------------------------------------ enqueue

    def enqueue_in(
        self,
        conn: sqlite3.Connection,
        now: float,
        plugin: str,
        workflow: str,
        input: dict[str, Any] | None = None,
        *,
        needs: Iterable[str] = (),
        priority: int = 50,
        dedupe_key: str | None = None,
        max_attempts: int | None = None,
        delay: float = 0,
        model_group: str | None = None,
        window: str | None = None,
        source: str = "argus",
    ) -> tuple[str, bool]:
        """enqueue() inside a Store write, so a schedule or trigger records its own state in the same
        transaction. Returns (job_id, created)."""
        needs_list = sorted(set(needs))
        if dedupe_key is not None:
            row = conn.execute(
                "SELECT id FROM jobs WHERE dedupe_key = ? AND state IN ({})".format(
                    ",".join("?" * len(ACTIVE_STATES))
                ),
                (dedupe_key, *[s.value for s in ACTIVE_STATES]),
            ).fetchone()
            if row is not None:
                self._event(conn, now, "job.deduped", None, src=source, dst=plugin,
                            data={"job_id": row["id"], "dedupe_key": dedupe_key})
                return row["id"], False
        limit = self.cfg.plugin_queue_limit
        active = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE plugin = ? AND state IN ({})".format(
                ",".join("?" * len(ACTIVE_STATES))
            ),
            (plugin, *[s.value for s in ACTIVE_STATES]),
        ).fetchone()[0]
        if active >= limit:
            raise QueueFull(f"plugin {plugin} already has {active} active jobs (limit {limit})")
        job_id = new_id()
        conn.execute(
            "INSERT INTO jobs (id, plugin, workflow, state, priority, needs, dedupe_key, attempt,"
            " max_attempts, run_after, input, created_at, updated_at, model_group, run_window)"
            " VALUES (?,?,?,?,?,?,?,0,?,?,?,?,?,?,?)",
            (job_id, plugin, workflow, S.QUEUED.value, priority, _dumps(needs_list), dedupe_key,
             max_attempts or self.cfg.max_attempts, now + delay, _dumps(input or {}), now, now,
             model_group, window),
        )
        ensure_component(conn, now, plugin, "plugin", plugin)
        job = self._get(conn, job_id)
        data: dict[str, Any] = {"workflow": workflow, "priority": priority}
        if model_group:
            data["model"] = model_group
        if window:
            data["window"] = window
        self._event(conn, now, "job.queued", job, src=source, dst=plugin, data=data)
        return job_id, True

    async def enqueue(
        self,
        plugin: str,
        workflow: str,
        input: dict[str, Any] | None = None,
        *,
        needs: Iterable[str] = (),
        priority: int = 50,
        dedupe_key: str | None = None,
        max_attempts: int | None = None,
        delay: float = 0,
        model_group: str | None = None,
        window: str | None = None,
        source: str = "argus",
    ) -> tuple[str, bool]:
        """Add a job. Returns (job_id, created). A duplicate of an active job returns its id, created=False."""
        return await self.store.write(lambda conn: self.enqueue_in(
            conn, self.clock(), plugin, workflow, input, needs=needs, priority=priority, dedupe_key=dedupe_key,
            max_attempts=max_attempts, delay=delay, model_group=model_group, window=window, source=source))

    # ------------------------------------------------------------------ worker side

    async def claim(self, worker: str, capabilities: Iterable[str],
                    plugins: Iterable[str] | None = None) -> Job | None:
        """Lease the best claimable job this worker can run, or return None.

        `plugins`, when given, limits the claim to jobs of those plugins (the worker has their code).
        """
        caps = set(capabilities)
        only = set(plugins) if plugins is not None else None
        lease = self.cfg.lease_seconds

        def fn(conn: sqlite3.Connection) -> Job | None:
            now = self.clock()
            sql = "SELECT * FROM jobs WHERE state IN (?, ?) AND run_after <= ?"
            args: list[Any] = [*[s.value for s in CLAIMABLE_STATES], now]
            if only is not None:
                if not only:
                    return None
                sql += " AND plugin IN ({})".format(",".join("?" * len(only)))
                args += sorted(only)
            # Backpressure: how many jobs each plugin has running, and whether the GPU is taken.
            running = {r[0]: r[1] for r in conn.execute(
                "SELECT plugin, COUNT(*) FROM jobs WHERE state IN ('leased','running') GROUP BY plugin")}
            gpu_busy = conn.execute("SELECT 1 FROM jobs WHERE state IN ('leased','running')"
                                    " AND needs LIKE '%\"gpu\"%' LIMIT 1").fetchone() is not None
            # Order: priority first; within a priority, jobs for the model already loaded, then by model (so a
            # batch of 7B jobs runs before switching to 14B once), then oldest first. Walk the whole queue with
            # a cursor until a job this worker may run turns up.
            order = (" ORDER BY priority DESC, CASE WHEN model_group = ? THEN 0 ELSE 1 END, model_group,"
                     " created_at, id")
            for r in conn.execute(sql + order, [*args, self.gpu_model]):
                job = Job.from_row(r)
                if not set(job.needs) <= caps or (only is not None and job.plugin not in only):
                    continue
                if running.get(job.plugin, 0) >= self.cfg.limit_for(job.plugin):
                    continue
                if "gpu" in job.needs and gpu_busy:
                    continue  # one GPU job at a time
                if job.run_window and job.run_window in self.windows and not in_window(
                        self.windows[job.run_window], now):
                    continue
                if "gpu" in job.needs:
                    swap = self.gpu_model is not None and job.model_group not in (None, self.gpu_model)
                    self.gpu_model = job.model_group or self.gpu_model
                else:
                    swap = False
                data: dict[str, Any] = {"worker": worker, "attempt": job.attempt + 1}
                if swap:
                    data["model_swap"] = job.model_group
                return self._move(
                    conn, job, S.LEASED, now, src_component=worker,
                    attempt=job.attempt + 1, lease_owner=worker, lease_until=now + lease,
                    wait_reason=None, event_data=data,
                )
            return None

        return await self.store.write(fn)

    async def has_claimable(self) -> bool:
        """A cheap read (no write transaction) so idle workers' long-polls don't keep the writer busy."""
        now = self.clock()

        def fn(conn: sqlite3.Connection) -> bool:
            return conn.execute("SELECT 1 FROM jobs WHERE state IN (?, ?) AND run_after <= ? LIMIT 1",
                                (*[s.value for s in CLAIMABLE_STATES], now)).fetchone() is not None

        return await self.store.read(fn)

    async def start(self, job_id: str, worker: str) -> Job:
        def fn(conn: sqlite3.Connection) -> Job:
            now = self.clock()
            job = self._get(conn, job_id)
            self._check_lease(job, worker, now)
            if job.state is S.RUNNING:
                return job
            return self._move(conn, job, S.RUNNING, now, src_component=worker,
                              lease_until=now + self.cfg.lease_seconds)

        return await self.store.write(fn)

    async def heartbeat(self, job_id: str, worker: str) -> float:
        """Extend the lease. Returns the new lease_until."""

        def fn(conn: sqlite3.Connection) -> float:
            now = self.clock()
            job = self._get(conn, job_id)
            self._check_lease(job, worker, now)
            until = now + self.cfg.lease_seconds
            conn.execute("UPDATE jobs SET lease_until = ?, updated_at = ? WHERE id = ?", (until, now, job_id))
            return until

        return await self.store.write(fn)

    async def record_step(
        self,
        job_id: str,
        worker: str,
        idx: int,
        name: str,
        *,
        state: str = "succeeded",
        output: Any = None,
        error: str | None = None,
        tier_used: str | None = None,
    ) -> None:
        """Save a step result. A succeeded step is a checkpoint: retries skip it."""
        if state not in ("running", "succeeded", "failed"):
            raise ValueError(f"bad step state {state!r}")

        def fn(conn: sqlite3.Connection) -> None:
            now = self.clock()
            job = self._get(conn, job_id)
            self._check_lease(job, worker, now)
            conn.execute(
                "INSERT INTO steps (job_id, idx, name, state, tier_used, output, error, started_at,"
                " finished_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(job_id, idx) DO UPDATE SET name=excluded.name, state=excluded.state,"
                " tier_used=excluded.tier_used, output=excluded.output, error=excluded.error,"
                " finished_at=excluded.finished_at, updated_at=excluded.updated_at",
                (job_id, idx, name, state, tier_used, _dumps(output) if output is not None else None,
                 error, now, None if state == "running" else now, now, now),
            )
            self._event(conn, now, f"step.{state}", job, step=name, src=worker, dst=job.plugin,
                        data={"idx": idx, "tier": tier_used} if tier_used else {"idx": idx})

        await self.store.write(fn)

    async def record_event(self, job_id: str, worker: str, kind: str, *, src: str | None = None,
                           dst: str | None = None, step: str | None = None,
                           data: dict[str, Any] | None = None) -> None:
        """A worker's own trace event for a job it holds (model calls, checks, escalations)."""

        def fn(conn: sqlite3.Connection) -> None:
            now = self.clock()
            job = self._get(conn, job_id)
            self._check_lease(job, worker, now)
            insert_event(conn, now, kind, job_id=job_id, step=step, src=src, dst=dst, data=data)

        await self.store.write(fn)

    async def succeed(self, job_id: str, worker: str, result: Any = None) -> Job:
        def fn(conn: sqlite3.Connection) -> Job:
            now = self.clock()
            job = self._get(conn, job_id)
            self._check_lease(job, worker, now)
            return self._move(conn, job, S.SUCCEEDED, now, src_component=worker,
                              result=_dumps(result) if result is not None else None,
                              lease_owner=None, lease_until=None, finished_at=now, error=None)

        return await self.store.write(fn)

    async def fail(self, job_id: str, worker: str, error: str, *, retryable: bool = True) -> Job:
        """A step failed. Retry after backoff while attempts remain, otherwise dead-letter."""

        def fn(conn: sqlite3.Connection) -> Job:
            now = self.clock()
            job = self._get(conn, job_id)
            self._check_lease(job, worker, now)
            if retryable and job.attempt < job.max_attempts:
                return self._move(conn, job, S.RETRY, now, src_component=worker, error=error,
                                  lease_owner=None, lease_until=None,
                                  run_after=now + self._backoff(job.attempt),
                                  event_data={"error": error[:500], "attempt": job.attempt})
            return self._move(conn, job, S.DEAD, now, src_component=worker, error=error,
                              lease_owner=None, lease_until=None, finished_at=now,
                              event_data={"error": error[:500], "attempt": job.attempt})

        return await self.store.write(fn)

    async def wait(self, job_id: str, worker: str, reason: str) -> Job:
        """Park the job (approval, GPU, PC off). Frees the worker; the job keeps its checkpoints."""

        def fn(conn: sqlite3.Connection) -> Job:
            now = self.clock()
            job = self._get(conn, job_id)
            self._check_lease(job, worker, now)
            if reason.startswith("approval:"):
                # Decided while the worker was still on its way here? Then go straight back to the queue.
                row = conn.execute("SELECT state FROM approvals WHERE id = ?", (reason[9:],)).fetchone()
                if row is not None and row["state"] != "pending":
                    return self._move(conn, job, S.QUEUED, now, src_component=worker, wait_reason=None,
                                      lease_owner=None, lease_until=None, run_after=now,
                                      attempt=max(job.attempt - 1, 0), priority=max(job.priority, 80),
                                      event_data={"reason": "approval decided"})
            # Waiting is not failing: the claim that led here does not use up one of the job's attempts.
            return self._move(conn, job, S.WAITING, now, src_component=worker, wait_reason=reason,
                              lease_owner=None, lease_until=None, attempt=max(job.attempt - 1, 0),
                              event_data={"reason": reason})

        return await self.store.write(fn)

    # ------------------------------------------------------------------ control side

    async def resume(self, job_id: str) -> Job:
        """Waiting -> queued, e.g. after an approval or when the PC is awake."""

        def fn(conn: sqlite3.Connection) -> Job:
            now = self.clock()
            job = self._get(conn, job_id)
            return self._move(conn, job, S.QUEUED, now, wait_reason=None, run_after=now)

        return await self.store.write(fn)

    async def rerun(self, job_id: str) -> Job:
        """Dead -> queued with fresh attempts (the Re-run button in Helios). Checkpoints are kept."""

        def fn(conn: sqlite3.Connection) -> Job:
            now = self.clock()
            job = self._get(conn, job_id)
            if job.dedupe_key is not None and conn.execute(
                    "SELECT 1 FROM jobs WHERE dedupe_key = ? AND id != ? AND state IN ({})".format(
                        ",".join("?" * len(ACTIVE_STATES))),
                    (job.dedupe_key, job.id, *[s.value for s in ACTIVE_STATES])).fetchone() is not None:
                raise InvalidTransition(f"job {job.id}: another job with the same dedupe key is already active")
            return self._move(conn, job, S.QUEUED, now, attempt=0, error=None, finished_at=None,
                              run_after=now, event_data={"rerun": True})

        return await self.store.write(fn)

    async def cancel(self, job_id: str, reason: str = "cancelled") -> Job:
        def fn(conn: sqlite3.Connection) -> Job:
            now = self.clock()
            job = self._get(conn, job_id)
            return self._move(conn, job, S.CANCELLED, now, lease_owner=None, lease_until=None,
                              finished_at=now, error=reason, event_data={"reason": reason})

        return await self.store.write(fn)

    async def expire_leases(self) -> list[str]:
        """Watchdog: jobs whose worker stopped heartbeating go back to the queue (or dead-letter)."""

        def fn(conn: sqlite3.Connection) -> list[str]:
            now = self.clock()
            rows = conn.execute(
                "SELECT * FROM jobs WHERE state IN (?, ?) AND lease_until IS NOT NULL AND lease_until < ?",
                (S.LEASED.value, S.RUNNING.value, now),
            ).fetchall()
            moved = []
            for r in rows:
                job = Job.from_row(r)
                if job.attempt < job.max_attempts:
                    self._move(conn, job, S.QUEUED, now, src_component="watchdog",
                               lease_owner=None, lease_until=None, run_after=now,
                               event_data={"reason": "lease expired", "worker": job.lease_owner})
                else:
                    self._move(conn, job, S.DEAD, now, src_component="watchdog",
                               lease_owner=None, lease_until=None, finished_at=now,
                               error="lease expired and no attempts left",
                               event_data={"reason": "lease expired", "worker": job.lease_owner})
                moved.append(job.id)
            return moved

        return await self.store.write(fn)

    # ------------------------------------------------------------------ reads

    async def get(self, job_id: str) -> Job:
        def fn(conn: sqlite3.Connection) -> Job:
            return self._get(conn, job_id)

        return await self.store.read(fn)

    async def steps(self, job_id: str) -> list[Step]:
        def fn(conn: sqlite3.Connection) -> list[Step]:
            rows = conn.execute("SELECT * FROM steps WHERE job_id = ? ORDER BY idx", (job_id,)).fetchall()
            return [Step.from_row(r) for r in rows]

        return await self.store.read(fn)

    async def completed_steps(self, job_id: str) -> dict[int, Step]:
        """Checkpoints a retried job can skip, by step index."""
        return {s.idx: s for s in await self.steps(job_id) if s.state == "succeeded"}

    async def list_jobs(self, state: JobState | None = None, limit: int = 100, plugin: str | None = None,
                        before: float | None = None) -> list[Job]:
        """Newest first. `before` (a created_at) pages back through older runs."""

        def fn(conn: sqlite3.Connection) -> list[Job]:
            where, args = [], []
            if state is not None:
                where.append("state = ?")
                args.append(state.value)
            if plugin:
                where.append("plugin = ?")
                args.append(plugin)
            if before is not None:
                where.append("created_at < ?")
                args.append(before)
            sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "")
            rows = conn.execute(sql + " ORDER BY created_at DESC LIMIT ?", (*args, limit))
            return [Job.from_row(r) for r in rows.fetchall()]

        return await self.store.read(fn)

    async def queue(self, workers: list[dict[str, Any]]) -> dict[str, Any]:
        """What Argus is doing and what waits, in the order it will run, each with the reason it waits.

        `workers`: the registered workers (state, capabilities), to tell "no worker can run this" apart."""
        online = [set(w.get("capabilities") or []) for w in workers if w.get("state") == "online"]

        def fn(conn: sqlite3.Connection) -> dict[str, Any]:
            now = self.clock()
            rows = [Job.from_row(r) for r in conn.execute(
                "SELECT * FROM jobs WHERE state IN ('queued','retry','leased','running','waiting')"
                " ORDER BY priority DESC, CASE WHEN model_group = ? THEN 0 ELSE 1 END, model_group, created_at, id",
                (self.gpu_model,)).fetchall()]
            steps = {r["job_id"]: r["name"] for r in conn.execute(
                "SELECT job_id, name FROM steps WHERE state = 'running'").fetchall()}
            active = [j for j in rows if j.state.value in ("leased", "running")]
            running_by_plugin: dict[str, int] = {}
            for j in active:
                running_by_plugin[j.plugin] = running_by_plugin.get(j.plugin, 0) + 1
            gpu_busy = any("gpu" in j.needs for j in active)

            def brief(j: Job) -> dict[str, Any]:
                return {"id": j.id, "plugin": j.plugin, "workflow": j.workflow, "state": j.state.value,
                        "priority": j.priority, "attempt": j.attempt, "max_attempts": j.max_attempts,
                        "created_at": j.created_at, "updated_at": j.updated_at, "needs": list(j.needs),
                        "model": j.model_group, "window": j.run_window}

            running = [{**brief(j), "worker": j.lease_owner, "step": steps.get(j.id), "since": j.updated_at}
                       for j in active]
            waiting = [{**brief(j), "reason": j.wait_reason} for j in rows if j.state.value == "waiting"]
            queued = []
            for j in rows:
                if j.state.value not in ("queued", "retry"):
                    continue
                why, until = "next", None
                if j.run_after > now:
                    why, until = ("retry" if j.state.value == "retry" else "later"), j.run_after
                elif j.run_window and j.run_window in self.windows and not in_window(self.windows[j.run_window], now):
                    why, until = "window", window_opens(self.windows[j.run_window], now)
                elif not any(set(j.needs) <= caps for caps in online):
                    why = "no_worker"
                elif running_by_plugin.get(j.plugin, 0) >= self.cfg.limit_for(j.plugin):
                    why = "plugin_busy"
                elif "gpu" in j.needs and gpu_busy:
                    why = "gpu_busy"
                queued.append({**brief(j), "position": len(queued) + 1, "why": why, "until": until,
                               "error": j.error if j.state.value == "retry" else None})
            return {"running": running, "queued": queued, "waiting": waiting, "gpu_model": self.gpu_model,
                    "workers_online": len(online)}

        return await self.store.read(fn)

    async def counts(self) -> dict[str, int]:
        def fn(conn: sqlite3.Connection) -> dict[str, int]:
            return {r[0]: r[1] for r in conn.execute("SELECT state, COUNT(*) FROM jobs GROUP BY state")}

        return await self.store.read(fn)

    async def events(self, job_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        def fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            if job_id:
                rows = conn.execute("SELECT * FROM events WHERE job_id = ? ORDER BY id", (job_id,))
            else:
                rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
            out = []
            for r in rows.fetchall():
                d = dict(r)
                d["data"] = json.loads(d["data"]) if d["data"] else None
                out.append(d)
            return out

        return await self.store.read(fn)
