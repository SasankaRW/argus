"""C4: the worker protocol, the worker process, and crash recovery across workers."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

import argus.worker.demo  # noqa: F401 - registers the demo workflows
from argus.api import create_app
from argus.config import load_config
from argus.context import Argus
from argus.worker import ArgusClient, Context, Worker, WorkflowRegistry, workflow
from argus.worker.client import LeaseLostError, Unreachable

CORE = Path(__file__).resolve().parents[1] / "core"


def make(tmp_path: Path, token: str | None = None, lease: int = 60, heartbeat: int = 15,
         queue_limit: int = 100) -> Argus:
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\n"
        f"jobs:\n  watchdog_interval_seconds: 0.1\n  lease_seconds: {lease}\n  heartbeat_seconds: {heartbeat}\n"
        f"  plugin_queue_limit: {queue_limit}\n",
        encoding="utf-8",
    )
    if token:
        (tmp_path / ".env").write_text(f"ARGUS_WORKER_TOKEN={token}\n", encoding="utf-8")
    return Argus(load_config(tmp_path / "argus.yaml"))


def wait_for(cond, timeout: float = 10.0, every: float = 0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = cond()
        if v:
            return v
        time.sleep(every)
    raise AssertionError("condition not met in time")


class Server:
    """A real argusd (uvicorn) in a background thread."""

    def __init__(self, argus: Argus):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        cfg = uvicorn.Config(create_app(argus), host="127.0.0.1", port=self.port, log_config=None, lifespan="on",
                             ws="websockets-sansio")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> Server:
        self.thread.start()
        wait_for(lambda: self.server.started)
        return self

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(10)


def client(url: str, token: str | None = None) -> ArgusClient:
    return ArgusClient(url, token, retry_delays=(0.1, 0.2))


# ------------------------------------------------------------------ Context (no server)


class FakeReporter:
    def __init__(self):
        self.calls: list[tuple] = []
        self.lost = False

    def step(self, idx, name, state, *, output=None, error=None, tier=None):
        self.calls.append((idx, name, state, output, error))

    @property
    def lease_lost(self):
        return self.lost


def test_context_skips_finished_steps_and_sets_idempotency_key():
    job = {"id": "J1", "plugin": "p", "input": {"x": 2},
           "steps": [{"idx": 0, "name": "a", "state": "succeeded", "output": 10}]}
    rep = FakeReporter()
    ctx = Context(job, rep)
    ran = []
    assert ctx.step("a", lambda: ran.append("a") or 99) == 10  # saved output, not re-run
    keys = []
    assert ctx.step("b", lambda: keys.append(ctx.idempotency_key) or ctx.input["x"] * 3) == 6
    assert ran == [] and keys == ["J1:1"] and ctx.skipped == ["a"]
    assert [c[:3] for c in rep.calls] == [(1, "b", "running"), (1, "b", "succeeded")]


def test_context_reports_failed_step_and_stops_when_lease_lost():
    rep = FakeReporter()
    ctx = Context({"id": "J2", "plugin": "p"}, rep)
    with pytest.raises(ZeroDivisionError):
        ctx.step("div", lambda: 1 / 0)
    assert rep.calls[-1][2] == "failed" and "ZeroDivisionError" in rep.calls[-1][4]
    rep.lost = True
    with pytest.raises(LeaseLostError):
        ctx.step("next", lambda: 1)


def test_registry_rejects_duplicates_and_collects_needs():
    reg = WorkflowRegistry()

    @workflow("a", "one", needs=["gpu"], registry=reg)
    def one(ctx):
        return 1

    with pytest.raises(ValueError):
        workflow("a", "one", registry=reg)(lambda ctx: 2)
    assert reg.plugins == ["a"] and reg.needs == ["gpu"]


# ------------------------------------------------------------------ API contract (TestClient)


def test_worker_protocol_round_trip(tmp_path):
    argus = make(tmp_path).open()
    with TestClient(create_app(argus)) as c:
        r = c.post("/workers/register", json={"id": "w1", "host": "pc", "capabilities": ["gpu"]})
        assert r.status_code == 200 and r.json()["lease_seconds"] == 60
        assert c.post("/workers/w1/claim", json={"capabilities": ["gpu"]}).status_code == 204

        job_id = c.post("/jobs", json={"plugin": "demo", "workflow": "echo", "input": {"t": 1}}).json()["id"]
        # a worker without the plugin does not get it
        assert c.post("/workers/w1/claim", json={"capabilities": [], "plugins": ["other"]}).status_code == 204
        job = c.post("/workers/w1/claim", json={"capabilities": [], "plugins": ["demo"]}).json()
        assert job["id"] == job_id and job["state"] == "leased" and job["steps"] == []

        assert c.post(f"/jobs/{job_id}/start", json={"worker": "w1"}).json()["state"] == "running"
        assert c.post(f"/jobs/{job_id}/heartbeat", json={"worker": "w1"}).json()["lease_until"] > 0
        r = c.post(f"/jobs/{job_id}/steps", json={"worker": "w1", "idx": 0, "name": "a", "state": "succeeded",
                                                  "output": {"n": 1}})
        assert r.status_code == 200
        # someone else cannot report on this job
        r = c.post(f"/jobs/{job_id}/succeed", json={"worker": "intruder"})
        assert r.status_code == 409 and r.json()["error"] == "lease_lost"
        done = c.post(f"/jobs/{job_id}/succeed", json={"worker": "w1", "result": {"ok": 1}}).json()
        assert done["state"] == "succeeded" and done["result"] == {"ok": 1}

        full = c.get(f"/jobs/{job_id}").json()
        assert full["steps"][0]["output"] == {"n": 1}
        kinds = [e["kind"] for e in c.get(f"/jobs/{job_id}/events").json()]
        assert kinds[0] == "job.queued" and "step.succeeded" in kinds and kinds[-1] == "job.succeeded"
        assert c.get("/jobs/counts").json() == {"succeeded": 1}
        assert [w["id"] for w in c.get("/workers").json()] == ["w1"]
        assert c.get("/jobs/nope").status_code == 404


def test_queue_full_is_429(tmp_path):
    argus = make(tmp_path, queue_limit=1).open()
    with TestClient(create_app(argus)) as c:
        assert c.post("/jobs", json={"plugin": "p", "workflow": "w"}).status_code == 201
        r = c.post("/jobs", json={"plugin": "p", "workflow": "w"})
        assert r.status_code == 429 and r.json()["error"] == "queue_full"


def test_token_required_when_set(tmp_path):
    argus = make(tmp_path, token="s3cret").open()
    with TestClient(create_app(argus)) as c:
        assert c.get("/health").status_code == 200  # public
        assert c.get("/jobs").status_code == 401
        assert c.get("/jobs", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/jobs", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert c.post("/workers/register", json={"id": "w", "host": "h"}).status_code == 401


def test_cancel_rerun_resume_endpoints(tmp_path):
    argus = make(tmp_path).open()
    with TestClient(create_app(argus)) as c:
        jid = c.post("/jobs", json={"plugin": "p", "workflow": "w"}).json()["id"]
        assert c.post(f"/jobs/{jid}/cancel").json()["state"] == "cancelled"
        r = c.post(f"/jobs/{jid}/resume")
        assert r.status_code == 409 and r.json()["error"] == "invalid_transition"


# ------------------------------------------------------------------ real server + worker


def test_worker_runs_demo_jobs_end_to_end(tmp_path):
    argus = make(tmp_path, token="tok").open()
    with Server(argus) as srv:
        cl = client(srv.url, "tok")
        w = Worker(cl, "w-e2e")
        w.register()
        jid = cl.post("/jobs", {"plugin": "demo", "workflow": "echo", "input": {"text": "hello big world"}})["id"]
        assert w.run_once(wait=2)
        job = cl.get(f"/jobs/{jid}")
        assert job["state"] == "succeeded" and job["result"] == {"text": "hello big world", "words": 3}
        assert [s["name"] for s in job["steps"]] == ["read", "count"]

        # retryable failure -> retry with backoff; permanent -> dead
        r1 = cl.post("/jobs", {"plugin": "demo", "workflow": "fail", "input": {}})["id"]
        assert w.run_once(wait=2)
        assert cl.get(f"/jobs/{r1}")["state"] == "retry"
        r2 = cl.post("/jobs", {"plugin": "demo", "workflow": "fail", "input": {"permanent": True}})["id"]
        assert w.run_once(wait=2)
        dead = cl.get(f"/jobs/{r2}")
        assert dead["state"] == "dead" and "for good" in dead["error"]
        assert [x["id"] for x in cl.get("/workers")] == ["w-e2e"]


def test_unknown_workflow_goes_dead(tmp_path):
    argus = make(tmp_path).open()
    with Server(argus) as srv:
        cl = client(srv.url)
        jid = cl.post("/jobs", {"plugin": "demo", "workflow": "missing"})["id"]
        assert Worker(cl, "w").run_once(wait=2)
        job = cl.get(f"/jobs/{jid}")
        assert job["state"] == "dead" and "unknown workflow" in job["error"]


def test_wait_then_resume_skips_finished_steps(tmp_path):
    reg = WorkflowRegistry()
    runs = {"prepare": 0, "pay": 0}

    @workflow("approvals", "pay", registry=reg)
    def pay(ctx):
        runs["pay"] += 1
        ctx.step("prepare", lambda: runs.__setitem__("prepare", runs["prepare"] + 1) or "ready")
        if not ctx.input.get("approved") and runs["pay"] == 1:
            assert ctx.attempt == 1
            ctx.wait("needs approval")
        assert ctx.attempt == 1  # waiting did not use up an attempt
        return ctx.step("pay", lambda: "paid")

    argus = make(tmp_path).open()
    with Server(argus) as srv:
        cl = client(srv.url)
        w = Worker(cl, "w", registry=reg)
        jid = cl.post("/jobs", {"plugin": "approvals", "workflow": "pay"})["id"]
        assert w.run_once(wait=2)
        job = cl.get(f"/jobs/{jid}")
        assert job["state"] == "waiting" and job["wait_reason"] == "needs approval"
        cl.post(f"/jobs/{jid}/resume")
        assert w.run_once(wait=2)
        job = cl.get(f"/jobs/{jid}")
        assert job["state"] == "succeeded" and job["result"] == "paid"
        assert runs["prepare"] == 1  # checkpoint held across the wait


def test_client_gives_up_cleanly_when_argus_is_down(tmp_path):
    argus = make(tmp_path).open()
    with Server(argus) as srv:
        url = srv.url
    cl = ArgusClient(url, retry_delays=(0.05,))
    with pytest.raises(Unreachable):
        cl.get("/health")
    w = Worker(cl, "w")
    w.stopping.set()  # skip the 5 s back-off
    assert w.run_once(wait=0) is False


# ------------------------------------------------------------------ crash: kill -9 a worker mid-step

PLUGIN = textwrap.dedent(
    """
    import os, time
    from pathlib import Path
    from argus.worker import workflow

    MARKS = Path(os.environ["ARGUS_TEST_MARKS"])

    def mark(name):
        with open(MARKS / name, "a") as f:
            f.write("x")

    @workflow("crash", "two_steps")
    def two_steps(ctx):
        ctx.step("first", lambda: mark("first") or "one")
        def second():
            mark("second")
            if os.environ.get("ARGUS_TEST_SLOW"):
                time.sleep(60)
            return "two"
        ctx.step("second", second)
        return "done"
    """
)


def test_killed_worker_job_resumes_on_another_worker_without_redoing_steps(tmp_path, monkeypatch):
    plug_dir = tmp_path / "plug"
    plug_dir.mkdir()
    (plug_dir / "crashplug.py").write_text(PLUGIN, encoding="utf-8")
    marks = tmp_path / "marks"
    marks.mkdir()
    monkeypatch.setenv("ARGUS_TEST_MARKS", str(marks))

    argus = make(tmp_path, lease=5, heartbeat=1).open()
    with Server(argus) as srv:
        cl = client(srv.url)
        jid = cl.post("/jobs", {"plugin": "crash", "workflow": "two_steps"})["id"]

        env = dict(os.environ, ARGUS_TEST_SLOW="1",
                   PYTHONPATH=os.pathsep.join([str(CORE), str(plug_dir), os.environ.get("PYTHONPATH", "")]))
        proc = subprocess.Popen(
            [sys.executable, "-m", "argus.worker.cli", "--url", srv.url, "--id", "doomed", "--no-demo",
             "--plugin", "crashplug", "--log-level", "WARNING"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            wait_for(lambda: (marks / "second").exists(), timeout=20)
            assert cl.get(f"/jobs/{jid}")["lease_owner"] == "doomed"
        finally:
            proc.kill()  # SIGKILL on Linux, TerminateProcess on Windows: no cleanup at all
            proc.wait(10)

        # the watchdog requeues the job once the lease runs out
        wait_for(lambda: cl.get(f"/jobs/{jid}")["state"] == "queued", timeout=15)

        sys.path.insert(0, str(plug_dir))
        try:
            import crashplug  # noqa: F401 - registers crash.two_steps in this process
        finally:
            sys.path.remove(str(plug_dir))
        assert Worker(cl, "rescuer").run_once(wait=2)

        job = cl.get(f"/jobs/{jid}")
        assert job["state"] == "succeeded" and job["result"] == "done" and job["attempt"] == 2
        assert (marks / "first").read_text() == "x"  # finished step was not redone
        assert (marks / "second").read_text() == "xx"  # interrupted step ran again
        events = [e["kind"] for e in cl.get(f"/jobs/{jid}/events")]
        assert events.count("job.leased") == 2 and events.count("job.queued") == 2
