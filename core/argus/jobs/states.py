"""The job state machine. Every state change in Argus goes through ALLOWED; nothing else moves a job.

    queued -> leased -> running -> succeeded
                 |         |  \\-> waiting -> queued (resume)
                 |         \\-> retry -> leased (after backoff)
                 \\-> (lease expired) -> queued, or dead when attempts are used up
    dead -> queued        (re-run by hand from Helios)
    any non-terminal -> cancelled
"""

from __future__ import annotations

from enum import StrEnum


class JobState(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    WAITING = "waiting"
    RETRY = "retry"
    SUCCEEDED = "succeeded"
    DEAD = "dead"
    CANCELLED = "cancelled"


S = JobState

ALLOWED: dict[JobState, frozenset[JobState]] = {
    S.QUEUED: frozenset({S.LEASED, S.CANCELLED}),
    S.RETRY: frozenset({S.LEASED, S.CANCELLED}),
    S.LEASED: frozenset({S.RUNNING, S.WAITING, S.QUEUED, S.RETRY, S.DEAD, S.CANCELLED}),
    S.RUNNING: frozenset({S.SUCCEEDED, S.WAITING, S.QUEUED, S.RETRY, S.DEAD, S.CANCELLED}),
    S.WAITING: frozenset({S.QUEUED, S.DEAD, S.CANCELLED}),
    S.SUCCEEDED: frozenset(),
    S.DEAD: frozenset({S.QUEUED}),
    S.CANCELLED: frozenset(),
}

# States in which a job still counts against queue limits and dedupe.
ACTIVE_STATES = frozenset({S.QUEUED, S.LEASED, S.RUNNING, S.WAITING, S.RETRY})
# States a worker can claim from.
CLAIMABLE_STATES = frozenset({S.QUEUED, S.RETRY})
# States in which a worker holds a lease.
LEASED_STATES = frozenset({S.LEASED, S.RUNNING})
TERMINAL_STATES = frozenset({S.SUCCEEDED, S.CANCELLED})


def can_move(src: JobState, dst: JobState) -> bool:
    return dst in ALLOWED[src]
