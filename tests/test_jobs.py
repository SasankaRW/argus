from __future__ import annotations

import itertools

import pytest

from argus.jobs import ALLOWED, InvalidTransition, JobState, LeaseLost, QueueFull
from argus.jobs.states import can_move
from conftest import run

S = JobState
W = "worker-a"
CAPS = {"cpu"}


# ------------------------------------------------------------------ the state machine table

EXPECTED_ALLOWED = {
    ("queued", "leased"), ("queued", "cancelled"),
    ("retry", "leased"), ("retry", "cancelled"),
    ("leased", "running"), ("leased", "waiting"), ("leased", "queued"), ("leased", "retry"),
    ("leased", "dead"), ("leased", "cancelled"),
    ("running", "succeeded"), ("running", "waiting"), ("running", "queued"), ("running", "retry"),
    ("running", "dead"), ("running", "cancelled"),
    ("waiting", "queued"), ("waiting", "dead"), ("waiting", "cancelled"),
    ("dead", "queued"),
}


@pytest.mark.parametrize("src,dst", list(itertools.product(list(S), list(S))))
def test_every_transition_is_exactly_as_designed(src, dst):
    assert can_move(src, dst) == ((src.value, dst.value) in EXPECTED_ALLOWED)


def test_succeeded_and_cancelled_are_final():
    assert ALLOWED[S.SUCCEEDED] == frozenset()
    assert ALLOWED[S.CANCELLED] == frozenset()


# ------------------------------------------------------------------ happy path

def test_full_lifecycle_writes_an_event_per_change(jobs):
    async def go():
        job_id, created = await jobs.enqueue("demo", "hello", {"n": 1}, needs=["cpu"])
        assert created
        job = await jobs.claim(W, CAPS)
        assert job.id == job_id and job.state is S.LEASED and job.attempt == 1
        await jobs.start(job_id, W)
        await jobs.record_step(job_id, W, 0, "read", output={"text": "hi"})
        done = await jobs.succeed(job_id, W, {"ok": True})
        assert done.state is S.SUCCEEDED and done.result == {"ok": True} and done.lease_owner is None
        kinds = [e["kind"] for e in await jobs.events(job_id)]
        assert kinds == ["job.queued", "job.leased", "job.running", "step.succeeded", "job.succeeded"]

    run(go())


def test_claim_respects_capabilities_and_priority(jobs):
    async def go():
        gpu_job, _ = await jobs.enqueue("p", "w", needs=["gpu"], priority=90)
        low, _ = await jobs.enqueue("p", "w", needs=["cpu"], priority=10)
        high, _ = await jobs.enqueue("p", "w", needs=["cpu"], priority=80)
        first = await jobs.claim(W, {"cpu"})
        second = await jobs.claim(W, {"cpu"})
        assert (first.id, second.id) == (high, low)  # the gpu job is skipped by a cpu-only worker
        assert await jobs.claim(W, {"cpu"}) is None
        got = await jobs.claim("gpu-worker", {"gpu", "cpu"})
        assert got.id == gpu_job

    run(go())


def test_equal_priority_is_first_come_first_served(jobs, clock):
    async def go():
        ids = []
        for _ in range(5):
            ids.append((await jobs.enqueue("p", "w"))[0])
            clock.advance(0.001)
        claimed = [(await jobs.claim(W, CAPS)).id for _ in range(5)]
        assert claimed == ids

    run(go())


def test_delay_holds_job_until_due(jobs, clock):
    async def go():
        await jobs.enqueue("p", "w", delay=30)
        assert await jobs.claim(W, CAPS) is None
        clock.advance(31)
        assert await jobs.claim(W, CAPS) is not None

    run(go())


# ------------------------------------------------------------------ dedupe and backpressure

def test_duplicate_trigger_merges_into_active_job(jobs):
    async def go():
        a, created_a = await jobs.enqueue("organizer", "file", {"f": 1}, dedupe_key="hash-1")
        b, created_b = await jobs.enqueue("organizer", "file", {"f": 1}, dedupe_key="hash-1")
        assert a == b and created_a and not created_b
        job = await jobs.claim(W, CAPS)
        await jobs.start(job.id, W)
        await jobs.succeed(job.id, W)
        c, created_c = await jobs.enqueue("organizer", "file", {"f": 1}, dedupe_key="hash-1")
        assert created_c and c != a  # once finished, the same key can run again

    run(go())


def test_queue_limit_per_plugin(jobs):
    jobs.cfg.plugin_queue_limit = 3

    async def go():
        for _ in range(3):
            await jobs.enqueue("flood", "w")
        with pytest.raises(QueueFull):
            await jobs.enqueue("flood", "w")
        await jobs.enqueue("other", "w")  # other plugins are unaffected

    run(go())


# ------------------------------------------------------------------ failures, retries, dead-letter

def test_retry_with_backoff_then_dead_letter(jobs, clock):
    async def go():
        job_id, _ = await jobs.enqueue("p", "w")
        expected_backoff = [10, 60]
        for attempt in (1, 2):
            job = await jobs.claim(W, CAPS)
            assert job.attempt == attempt
            failed = await jobs.fail(job_id, W, f"boom {attempt}")
            assert failed.state is S.RETRY
            assert failed.run_after == pytest.approx(clock.now + expected_backoff[attempt - 1])
            assert await jobs.claim(W, CAPS) is None  # still backing off
            clock.advance(expected_backoff[attempt - 1] + 1)
        await jobs.claim(W, CAPS)
        dead = await jobs.fail(job_id, W, "boom 3")
        assert dead.state is S.DEAD and dead.error == "boom 3" and dead.finished_at is not None

    run(go())


def test_non_retryable_failure_goes_straight_to_dead(jobs):
    async def go():
        job_id, _ = await jobs.enqueue("p", "w")
        await jobs.claim(W, CAPS)
        assert (await jobs.fail(job_id, W, "bad input", retryable=False)).state is S.DEAD

    run(go())


def test_rerun_dead_job_resets_attempts_and_keeps_checkpoints(jobs):
    async def go():
        job_id, _ = await jobs.enqueue("p", "w", max_attempts=1)
        await jobs.claim(W, CAPS)
        await jobs.start(job_id, W)
        await jobs.record_step(job_id, W, 0, "step-a", output=1)
        await jobs.fail(job_id, W, "boom")
        again = await jobs.rerun(job_id)
        assert again.state is S.QUEUED and again.attempt == 0 and again.error is None
        assert 0 in await jobs.completed_steps(job_id)

    run(go())


# ------------------------------------------------------------------ leases and the watchdog

def test_expired_lease_is_requeued_and_resumes_at_checkpoint(jobs, clock):
    async def go():
        job_id, _ = await jobs.enqueue("p", "w")
        await jobs.claim("dying-worker", CAPS)
        await jobs.start(job_id, "dying-worker")
        await jobs.record_step(job_id, "dying-worker", 0, "download", output={"bytes": 10})
        clock.advance(61)  # no heartbeat: the worker died
        assert await jobs.expire_leases() == [job_id]
        job = await jobs.get(job_id)
        assert job.state is S.QUEUED and job.lease_owner is None
        with pytest.raises(LeaseLost):
            await jobs.heartbeat(job_id, "dying-worker")  # the old worker cannot come back
        again = await jobs.claim("new-worker", CAPS)
        assert again.id == job_id and again.attempt == 2
        done = await jobs.completed_steps(job_id)
        assert list(done) == [0] and done[0].output == {"bytes": 10}

    run(go())


def test_heartbeat_keeps_lease_alive(jobs, clock):
    async def go():
        job_id, _ = await jobs.enqueue("p", "w")
        await jobs.claim(W, CAPS)
        for _ in range(10):
            clock.advance(15)
            await jobs.heartbeat(job_id, W)
        assert await jobs.expire_leases() == []

    run(go())


def test_expired_lease_on_last_attempt_goes_dead(jobs, clock):
    async def go():
        job_id, _ = await jobs.enqueue("p", "w", max_attempts=1)
        await jobs.claim(W, CAPS)
        clock.advance(61)
        await jobs.expire_leases()
        assert (await jobs.get(job_id)).state is S.DEAD

    run(go())


def test_only_the_lease_owner_can_act(jobs):
    async def go():
        job_id, _ = await jobs.enqueue("p", "w")
        await jobs.claim(W, CAPS)
        for action in (
            jobs.start(job_id, "intruder"),
            jobs.heartbeat(job_id, "intruder"),
            jobs.succeed(job_id, "intruder"),
            jobs.fail(job_id, "intruder", "x"),
            jobs.record_step(job_id, "intruder", 0, "s"),
            jobs.wait(job_id, "intruder", "approval"),
        ):
            with pytest.raises(LeaseLost):
                await action

    run(go())


# ------------------------------------------------------------------ waiting and cancel

def test_wait_frees_worker_and_resume_requeues(jobs):
    async def go():
        job_id, _ = await jobs.enqueue("p", "w")
        await jobs.claim(W, CAPS)
        waiting = await jobs.wait(job_id, W, "approval")
        assert waiting.state is S.WAITING and waiting.wait_reason == "approval" and waiting.lease_owner is None
        assert await jobs.claim(W, CAPS) is None
        await jobs.resume(job_id)
        assert (await jobs.claim(W, CAPS)).id == job_id

    run(go())


def test_cancel_from_any_active_state_and_not_after_success(jobs):
    async def go():
        q, _ = await jobs.enqueue("p", "a")
        assert (await jobs.cancel(q)).state is S.CANCELLED
        r, _ = await jobs.enqueue("p", "b")
        await jobs.claim(W, CAPS)
        await jobs.start(r, W)
        assert (await jobs.cancel(r)).state is S.CANCELLED
        d, _ = await jobs.enqueue("p", "c")
        await jobs.claim(W, CAPS)
        await jobs.start(d, W)
        await jobs.succeed(d, W)
        with pytest.raises(InvalidTransition):
            await jobs.cancel(d)

    run(go())


def test_must_start_before_succeeding(jobs):
    async def go():
        job_id, _ = await jobs.enqueue("p", "w")
        await jobs.claim(W, CAPS)
        with pytest.raises(InvalidTransition):
            await jobs.succeed(job_id, W)

    run(go())


def test_cannot_succeed_a_queued_job(jobs):
    async def go():
        job_id, _ = await jobs.enqueue("p", "w")
        with pytest.raises(LeaseLost):
            await jobs.succeed(job_id, W)

    run(go())


def test_claim_is_not_starved_by_jobs_this_worker_cannot_run(jobs):
    async def go():
        for _ in range(250):
            await jobs.enqueue(f"gpu{_ % 3}", "big", {}, needs=["gpu"], priority=90)
        low, _ = await jobs.enqueue("cpu", "small", {}, priority=10)
        got = await jobs.claim("laptop", ["cpu"])
        other = await jobs.claim("laptop", ["gpu"], plugins=["cpu"])
        return low, got, other

    low, got, other = run(go())
    assert got is not None and got.id == low
    assert other is None  # plugin filter applies in SQL


def test_rerun_with_an_active_duplicate_is_refused(jobs):
    from argus.jobs import InvalidTransition

    async def go():
        a, _ = await jobs.enqueue("p", "w", {}, dedupe_key="file-1", max_attempts=1)
        await jobs.claim("w1", [])
        await jobs.fail(a, "w1", "boom")
        await jobs.enqueue("p", "w", {}, dedupe_key="file-1")
        await jobs.rerun(a)

    with pytest.raises(InvalidTransition, match="dedupe"):
        run(go())
