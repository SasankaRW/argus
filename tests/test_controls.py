"""Controls (Helios: stop, start, restart, pause) and the per-component logs: what a paused plugin or worker does,
what pause-everything restores, the restart handshake with a worker, the logs API, and that the core has no control."""

from __future__ import annotations

import hashlib
import hmac
import json
import signal
import sys
import threading
import time
from calendar import timegm
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from argus.api import create_app
from argus.config import load_config
from argus.context import Argus
from argus.controls import EXIT_RESTART, ControlError, Controls
from argus.db import Store
from argus.events import insert_event
from argus.jobs import JobStore, PluginPaused
from argus.registry import Registry
from argus.worker import Worker
from argus.worker.client import ApiError, RestartRequested
from conftest import FakeClock, run
from test_plugins import PLUGIN_PY, manifest, setup
from test_plugins_off import switch_off
from test_worker import Server, client, wait_for

MANUAL = {"manual": {"workflow": "sort", "label": "Sort now"}}


def make(tmp_path: Path, triggers=None, second: bool = False, hook: bool = False, token: str | None = None) -> Argus:
    """argusd on a temp folder: the plugin "tidy" (and "other" when `second`), a logs/ folder, optional secrets."""
    setup(tmp_path, triggers=triggers or [MANUAL])
    if second:
        folder = tmp_path / "plugins" / "other"
        folder.mkdir()
        (folder / "plugin.yaml").write_text(manifest(tmp_path / "inbox", tmp_path / "out", id="other", name="Other",
                                                     triggers=[MANUAL]))
        (folder / "plugin.py").write_text(PLUGIN_PY.replace('"tidy"', '"other"'))
    env = tmp_path / ".env"
    env.write_text(env.read_text() + ("TIDY_HOOK=0123456789abcdef-hook\n" if hook else "")
                   + (f"ARGUS_WORKER_TOKEN={token}\n" if token else ""))
    (tmp_path / "logs").mkdir(exist_ok=True)
    return Argus(load_config(tmp_path / "argus.yaml")).open()


def register(c: TestClient, wid: str, caps=("cpu",)) -> None:
    assert c.post("/workers/register", json={"id": wid, "host": "h", "capabilities": list(caps)}).status_code == 200


def claim(c: TestClient, wid: str, caps=("cpu",)):
    return c.post(f"/workers/{wid}/claim", json={"capabilities": list(caps), "wait": 0})


def queue(c: TestClient, plugin: str = "tidy") -> dict:
    r = c.post("/jobs", json={"plugin": plugin, "workflow": "sort", "input": {}})
    assert r.status_code == 201, r.text
    return r.json()


def kinds(c: TestClient, prefix: str = "control.") -> list[dict]:
    return c.get(f"/events?kinds={prefix}&limit=100").json()["events"]


# ------------------------------------------------------------------ the job store (no server)


@pytest.fixture
def ctl(store: Store, jobs: JobStore, clock: FakeClock):
    """Controls over a real store with two plugins and two workers."""
    host = SimpleNamespace(plugins={"p1": object(), "p2": object()})
    cfg = SimpleNamespace(plugins=SimpleNamespace(off=["off1"]))
    reg = Registry(store, clock)
    run(reg.register_worker("w1", "h", ["cpu"]))
    run(reg.register_worker("w2", "h", ["cpu"]))
    return Controls(store, cfg, host, jobs, clock)


def test_a_paused_plugin_is_not_claimed(ctl, jobs):
    run(jobs.enqueue("p1", "go"))
    run(jobs.enqueue("p2", "go"))
    run(ctl.stop_plugin("p1"))
    got = run(jobs.claim("w1", ["cpu"]))
    assert got is not None and got.plugin == "p2"
    assert run(jobs.claim("w1", ["cpu"])) is None and run(jobs.has_claimable()) is False  # p1's job waits
    q = run(jobs.queue([]))["queued"]
    assert [(j["plugin"], j["why"]) for j in q] == [("p1", "paused")]
    run(ctl.start_plugin("p1"))
    assert run(jobs.claim("w1", ["cpu"])).plugin == "p1"  # nothing was lost while it was stopped


def test_the_running_job_finishes_after_a_stop(ctl, jobs):
    run(jobs.enqueue("p1", "go"))
    job = run(jobs.claim("w1", ["cpu"]))
    run(jobs.start(job.id, "w1"))
    run(ctl.stop_plugin("p1"))
    run(jobs.heartbeat(job.id, "w1"))
    assert run(jobs.succeed(job.id, "w1", {"ok": True})).state.value == "succeeded"


def test_nothing_new_is_queued_for_a_paused_plugin(ctl, jobs):
    run(ctl.stop_plugin("p1"))
    with pytest.raises(PluginPaused, match="p1 is stopped"):
        run(jobs.enqueue("p1", "go"))
    assert run(jobs.enqueue("p2", "go"))[1] is True


def test_resume_all_restores_only_its_own_set(ctl):
    run(ctl.stop_plugin("p1"))  # yours
    run(ctl.pause_worker("w1"))  # yours
    out = run(ctl.pause_all())
    assert out["plugins"] == ["p2"] and out["workers"] == ["w2"]  # what it added, not what was already paused
    assert run(ctl.state()) == {"plugins_paused": ["p1", "p2"], "workers_paused": ["w1", "w2"], "paused_all": True}
    back = run(ctl.resume_all())
    assert back["plugins"] == ["p2"] and back["workers"] == ["w2"]
    assert run(ctl.state()) == {"plugins_paused": ["p1"], "workers_paused": ["w1"], "paused_all": False}


def test_a_pause_you_touch_after_pause_all_is_yours(ctl):
    run(ctl.pause_all())
    run(ctl.start_plugin("p1"))  # you started it yourself
    run(ctl.stop_plugin("p2"))  # you took over this one
    run(ctl.resume_all())
    assert run(ctl.state())["plugins_paused"] == ["p2"]  # only what pause-all still owned came back


def test_restart_requeues_dead_leases_and_bumps_the_generation(ctl, jobs, clock, store):
    for _ in range(3):
        run(jobs.enqueue("p1", "go"))
    run(jobs.enqueue("p2", "go"))
    expired = run(jobs.claim("w1", ["cpu"], ["p1"]))  # its worker went quiet
    gone = run(jobs.claim("w2", ["cpu"], ["p1"]))  # its worker is offline, the lease is still running
    healthy = run(jobs.claim("w1", ["cpu"], ["p1"]))
    other = run(jobs.claim("w1", ["cpu"], ["p2"]))
    run(store.write(lambda c: c.execute("UPDATE jobs SET lease_until = ? WHERE id = ?", (clock() - 5, expired.id))))
    run(store.write(lambda c: c.execute("UPDATE workers SET state = 'offline' WHERE id = 'w2'")))
    run(ctl.stop_plugin("p1"))
    out = run(ctl.restart_plugin("p1"))
    assert sorted(out["requeued"]) == sorted([expired.id, gone.id]) and out["paused"] is False
    assert out["generation"] == 1
    assert run(jobs.get(expired.id)).state.value == "queued" and run(jobs.get(gone.id)).state.value == "queued"
    assert run(jobs.get(healthy.id)).state.value == "leased"  # a live lease is left alone
    assert run(jobs.get(other.id)).state.value == "leased"  # and another plugin's jobs are never touched
    assert (run(ctl.restart_plugin("p1")))["generation"] == 2
    assert run(ctl.state())["plugins_paused"] == []


def test_the_core_cannot_be_controlled(ctl):
    for call in (lambda i: ctl.stop_plugin(i), lambda i: ctl.start_plugin(i), lambda i: ctl.restart_plugin(i),
                 lambda i: ctl.pause_worker(i), lambda i: ctl.resume_worker(i), lambda i: ctl.restart_worker(i)):
        for cid in ("argus", "Argus", "argusd", "core"):
            with pytest.raises(ControlError) as e:
                run(call(cid))
            assert e.value.status == 403 and "Argus itself" in e.value.detail
    with pytest.raises(ControlError) as e:
        run(ctl.stop_plugin("nope"))
    assert e.value.status == 404
    with pytest.raises(ControlError) as e:
        run(ctl.pause_worker("nope"))
    assert e.value.status == 404


def test_a_plugin_in_plugins_off_cannot_be_started_at_runtime(ctl):
    for call in (ctl.start_plugin, ctl.stop_plugin, ctl.restart_plugin):
        with pytest.raises(ControlError) as e:
            run(call("off1"))
        assert e.value.status == 409 and "plugins.off" in e.value.detail


def test_controls_survive_an_argus_restart(db_path, clock):
    host = SimpleNamespace(plugins={"p1": object()})
    cfg = SimpleNamespace(plugins=SimpleNamespace(off=[]))

    def boot():
        s = Store(db_path).open()
        j = JobStore(s, clock=clock)
        return s, j, Controls(s, cfg, host, j, clock)

    s, jobs, ctl = boot()
    run(Registry(s, clock).register_worker("w1", "h", ["cpu"]))
    run(jobs.enqueue("p1", "go"))
    run(ctl.stop_plugin("p1", "on holiday"))
    run(ctl.pause_worker("w1"))
    s.close()
    s, jobs, ctl = boot()  # a new process on the same database
    try:
        assert run(ctl.state()) == {"plugins_paused": ["p1"], "workers_paused": ["w1"], "paused_all": False}
        assert run(jobs.claim("w1", ["cpu"])) is None
        with pytest.raises(PluginPaused):
            run(jobs.enqueue("p1", "go"))
        assert run(ctl.worker_gate("w1")) == (True, False)
    finally:
        s.close()


# ------------------------------------------------------------------ the API


def test_plugin_controls_over_http(tmp_path):
    argus = make(tmp_path)
    with TestClient(create_app(argus)) as c:
        assert c.get("/control/state").json() == {"plugins_paused": [], "workers_paused": [], "paused_all": False}
        r = c.post("/plugins/tidy/stop", json={"reason": "testing"})
        assert r.status_code == 200 and r.json()["paused"] is True
        assert c.get("/control/state").json()["plugins_paused"] == ["tidy"]
        refused = c.post("/jobs", json={"plugin": "tidy", "workflow": "sort", "input": {}})  # made by the API
        assert refused.status_code == 409 and refused.json()["error"] == "plugin_paused"
        assert "stopped" in refused.json()["detail"]
        assert c.post("/plugins/tidy/run", json={}).status_code == 409  # the Run button says no too
        assert c.post("/plugins/tidy/start").json()["paused"] is False
        assert queue(c)["id"]
        assert c.post("/plugins/tidy/restart").json()["generation"] == 1
        assert c.post("/plugins/nope/stop").status_code == 404
        assert c.post("/plugins/argus/stop").status_code == 403
        ev = kinds(c)
        assert [(e["kind"], e["from"], e["to"]) for e in ev] == [
            ("control.paused", "helios", "tidy"), ("control.resumed", "helios", "tidy"),
            ("control.restarted", "helios", "tidy")]
        assert ev[0]["data"]["reason"] == "testing"
        gens = c.post("/workers/register", json={"id": "w", "host": "h"}).json()["plugins"]
        assert [(p["id"], p["generation"]) for p in gens] == [("tidy", 1)]


def test_a_plugin_in_plugins_off_gets_409_over_http(tmp_path):
    setup(tmp_path)
    (tmp_path / "logs").mkdir()
    argus = Argus(load_config(switch_off(tmp_path))).open()
    with TestClient(create_app(argus)) as c:
        r = c.post("/plugins/tidy/start")
        assert r.status_code == 409 and "plugins.off" in r.json()["detail"]
        assert c.post("/plugins/tidy/restart").status_code == 409


def test_the_token_guards_every_control(tmp_path):
    argus = make(tmp_path, token="tok-123456789")
    with TestClient(create_app(argus)) as c:
        for method, path in [("post", "/plugins/tidy/stop"), ("post", "/plugins/tidy/start"),
                             ("post", "/plugins/tidy/restart"), ("post", "/workers/w/pause"),
                             ("post", "/workers/w/resume"), ("post", "/workers/w/restart"),
                             ("get", "/control/state"), ("post", "/control/pause-all"),
                             ("post", "/control/resume-all"), ("get", "/logs/argus?level=all")]:
            assert getattr(c, method)(path).status_code == 401, path
        ok = c.get("/control/state", headers={"Authorization": "Bearer tok-123456789"})
        assert ok.status_code == 200


def test_a_paused_worker_claims_nothing_but_keeps_heartbeating(tmp_path):
    argus = make(tmp_path)
    with TestClient(create_app(argus)) as c:
        register(c, "worker-a")
        register(c, "worker-a-now")  # its fast lane
        queue(c)
        assert c.post("/workers/worker-a/pause").json() == {"worker": "worker-a", "paused": True, "changed": True}
        run(argus.store.write(lambda cn: cn.execute("UPDATE workers SET last_seen = 1 WHERE id = 'worker-a'")))
        assert claim(c, "worker-a").status_code == 204
        assert claim(c, "worker-a-now").status_code == 204  # the fast lane is the same worker
        seen = [w for w in c.get("/workers").json() if w["id"] == "worker-a"][0]
        assert seen["last_seen"] > 1000 and seen["state"] == "online"  # the claim poll is its heartbeat
        assert c.get("/control/state").json()["workers_paused"] == ["worker-a"]
        assert c.post("/workers/worker-a/resume").json()["paused"] is False
        got = claim(c, "worker-a")
        assert got.status_code == 200 and got.json()["plugin"] == "tidy"
        assert c.post("/workers/nope/pause").status_code == 404
        for verb in ("pause", "resume", "restart"):
            assert c.post(f"/workers/argus/{verb}").status_code == 403


def test_a_job_claimed_before_the_pause_can_finish(tmp_path):
    argus = make(tmp_path)
    with TestClient(create_app(argus)) as c:
        register(c, "w")
        job = claim_after(c, "w")
        c.post("/workers/w/pause")
        c.post("/plugins/tidy/stop")
        assert c.post(f"/jobs/{job['id']}/start", json={"worker": "w"}).status_code == 200
        assert c.post(f"/jobs/{job['id']}/heartbeat", json={"worker": "w"}).status_code == 200
        assert c.post(f"/jobs/{job['id']}/succeed", json={"worker": "w", "result": {}}).json()["state"] == "succeeded"


def claim_after(c: TestClient, wid: str) -> dict:
    queue(c)
    r = claim(c, wid)
    assert r.status_code == 200
    return r.json()


def test_worker_restart_is_handed_out_once_and_never_mid_claim(tmp_path):
    argus = make(tmp_path)
    with TestClient(create_app(argus)) as c:
        register(c, "worker-a")
        register(c, "worker-a-now")
        r = c.post("/workers/worker-a-now/restart")  # asked through the lane: the box restarts
        assert r.json() == {"worker": "worker-a", "restarting": ["worker-a"], "exit_code": EXIT_RESTART} == {
            "worker": "worker-a", "restarting": ["worker-a"], "exit_code": 75}
        first = claim(c, "worker-a-now")
        assert first.status_code == 204 and first.headers["X-Argus-Control"] == "restart"
        again = claim(c, "worker-a")
        assert again.status_code == 204 and "X-Argus-Control" not in again.headers  # handed out once
        assert [e["kind"] for e in kinds(c, "worker.restarting")] == ["worker.restarting"]
        run(argus.store.write(lambda cn: cn.execute("UPDATE workers SET state = 'offline' WHERE id LIKE 'worker-a%'")))
        assert c.post("/workers/worker-a/restart").status_code == 409  # nothing running to restart


class Plain:
    """A client whose claim says "restart"."""

    def claim(self, *a, **k):
        raise RestartRequested()

    def get(self, *a, **k):
        raise ApiError(404, {})


def test_a_worker_told_to_restart_stops_only_when_supervised(monkeypatch):
    w = Worker(Plain(), "w-1")  # type: ignore[arg-type]
    monkeypatch.delenv("ARGUS_SUPERVISED", raising=False)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    assert w.run_once(wait=0) is False
    assert not w.restart.is_set() and not w.stopping.is_set()  # run by hand: nothing would start it again
    monkeypatch.setenv("ARGUS_SUPERVISED", "1")
    assert w.run_once(wait=0) is False
    assert w.restart.is_set() and w.stopping.is_set()  # run_forever now ends after the job it has
    w2 = Worker(Plain(), "w-2")  # type: ignore[arg-type]
    monkeypatch.delenv("ARGUS_SUPERVISED")
    monkeypatch.setenv("INVOCATION_ID", "0123abcd")  # systemd sets this for every service, old unit files included
    w2.run_once(wait=0)
    assert w2.restart.is_set()


def test_the_client_sees_the_restart_signal(tmp_path):
    argus = make(tmp_path)
    with Server(argus) as srv:
        cl = client(srv.url)
        cl.register("w-9", "h", ["cpu"], None)
        cl.post("/workers/w-9/restart")
        with pytest.raises(RestartRequested):
            cl.claim("w-9", ["cpu"], None, 0)
        assert cl.claim("w-9", ["cpu"], None, 0) is None


def test_the_worker_process_exits_with_the_restart_code(tmp_path, monkeypatch):
    """argus-worker under a supervisor: told to restart, it leaves with 75 (the supervisor starts it again at once)."""
    from argus.worker import cli

    argus = make(tmp_path)
    monkeypatch.setenv("ARGUS_SUPERVISED", "1")
    monkeypatch.chdir(tmp_path)
    old = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    with Server(argus) as srv:
        cl = client(srv.url)

        def ask():
            wait_for(lambda: {"w-restart", "w-restart-now"} <= {w["id"] for w in cl.get("/workers")})
            cl.post("/workers/w-restart/restart")

        t = threading.Thread(target=ask, daemon=True)
        t.start()
        try:
            code = cli.main(["--url", srv.url, "--id", "w-restart", "--no-demo", "--log-level", "WARNING"])
        finally:
            for s, h in old.items():
                signal.signal(s, h)
        t.join(5)
    assert code == EXIT_RESTART == 75


def test_a_restarted_plugin_is_loaded_again_by_the_worker(tmp_path, monkeypatch):
    argus = make(tmp_path)
    monkeypatch.chdir(tmp_path)
    with Server(argus) as srv:
        cl = client(srv.url)
        w = Worker(cl, "w-gen", watch_folders=False)
        w.register()
        assert w.plugin_gen == {"tidy": 0}
        before = sys.modules["argus_plugin_tidy"]
        cl.post("/jobs", {"plugin": "tidy", "workflow": "sort", "input": {}})
        assert w.run_once(wait=2) is True
        assert sys.modules["argus_plugin_tidy"] is before and w.plugin_gen == {"tidy": 0}  # nothing asked: no reload
        assert cl.post("/plugins/tidy/restart")["generation"] == 1
        cl.post("/jobs", {"plugin": "tidy", "workflow": "sort", "input": {}})
        assert w.run_once(wait=2) is True
        assert sys.modules["argus_plugin_tidy"] is not before and w.plugin_gen == {"tidy": 1}
        assert w.plugin_errors == {}


def test_a_restart_whose_code_no_longer_imports_keeps_the_old_code(tmp_path, monkeypatch):
    argus = make(tmp_path)
    monkeypatch.chdir(tmp_path)
    with Server(argus) as srv:
        cl = client(srv.url)
        w = Worker(cl, "w-gen", watch_folders=False)
        w.register()
        (tmp_path / "plugins" / "tidy" / "plugin.py").write_text("raise RuntimeError('mid-edit')\n")
        cl.post("/plugins/tidy/restart")
        job = cl.post("/jobs", {"plugin": "tidy", "workflow": "sort", "input": {}})
        assert w.run_once(wait=2) is True
        done = wait_for(lambda: (j := cl.get(f"/jobs/{job['id']}"))["state"] in ("succeeded", "dead", "retry") and j)
        assert done["state"] == "succeeded"  # the workflows loaded before still run
        assert "mid-edit" in w.plugin_errors["tidy"]


def test_pause_all_over_http_leaves_the_core_up(tmp_path):
    argus = make(tmp_path, second=True)
    with TestClient(create_app(argus)) as c:
        register(c, "w1")
        register(c, "w2")
        c.post("/plugins/tidy/stop")
        c.post("/workers/w1/pause")
        out = c.post("/control/pause-all").json()
        assert out == {"paused_all": True, "plugins": ["other"], "workers": ["w2"]}
        assert c.get("/control/state").json() == {"plugins_paused": ["other", "tidy"],
                                                  "workers_paused": ["w1", "w2"], "paused_all": True}
        assert c.get("/health").status_code == 200 and c.get("/status").json()["status"] == "ok"
        assert c.get("/approvals").status_code == 200 and c.get("/map").status_code == 200
        # The core's own jobs (backups, Ari) are not plugins' and are still queued.
        assert run(argus.jobs.enqueue("backup", "copy", {}))[1] is True
        assert claim(c, "w2").status_code == 204  # the paused worker takes nothing, not even those
        back = c.post("/control/resume-all").json()
        assert back == {"paused_all": False, "plugins": ["other"], "workers": ["w2"]}
        assert c.get("/control/state").json() == {"plugins_paused": ["tidy"], "workers_paused": ["w1"],
                                                  "paused_all": False}
        assert claim(c, "w2").status_code == 200  # the queued backup job
        summary = [e for e in kinds(c) if e["to"] == "argus"]
        assert [e["kind"] for e in summary] == ["control.paused", "control.resumed"]
        assert summary[0]["data"]["plugins"] == ["other"]


def test_pause_survives_restarting_argus(tmp_path):
    with TestClient(create_app(make(tmp_path))) as c:
        register(c, "w1")
        c.post("/plugins/tidy/stop")
        c.post("/workers/w1/pause")
    again = Argus(load_config(tmp_path / "argus.yaml")).open()
    with TestClient(create_app(again)) as c:  # argusd started again on the same database
        assert c.get("/control/state").json() == {"plugins_paused": ["tidy"], "workers_paused": ["w1"],
                                                  "paused_all": False}
        assert c.post("/jobs", json={"plugin": "tidy", "workflow": "sort", "input": {}}).status_code == 409
        assert claim(c, "w1").status_code == 204


def test_the_map_says_what_is_paused(tmp_path):
    argus = make(tmp_path)
    with TestClient(create_app(argus)) as c:
        register(c, "w1")

        def nodes():
            return {n["id"]: n for n in c.get("/map").json()["nodes"]}

        n = nodes()
        assert n["tidy"]["paused"] is False and n["w1"]["paused"] is False and "since" not in n["tidy"]
        assert "paused" not in n["argus"]  # the core has no such state
        c.post("/plugins/tidy/stop")
        c.post("/workers/w1/pause")
        n = nodes()
        assert n["tidy"]["paused"] is True and n["tidy"]["since"] > 0
        assert n["w1"]["paused"] is True and n["w1"]["since"] > 0


def test_a_stopped_plugins_schedule_skips_its_slot(tmp_path):
    argus = make(tmp_path, triggers=[MANUAL, {"schedule": {"workflow": "sort", "cron": "0 3 * * *"}}])
    with TestClient(create_app(argus)) as c:
        def due():
            run(argus.store.write(lambda cn: cn.execute("UPDATE schedules SET next_run_at = 1 WHERE plugin = 'tidy'")))
            return run(argus.scheduler.tick())

        c.post("/plugins/tidy/stop")
        assert due() == 0 and c.get("/jobs?plugin=tidy").json() == []
        nxt = run(argus.store.read(lambda cn: cn.execute("SELECT next_run_at FROM schedules").fetchone()[0]))
        assert nxt > time.time()  # the slot is skipped, the next one is waiting
        c.post("/plugins/tidy/start")
        assert due() == 1
        assert [j["plugin"] for j in c.get("/jobs?plugin=tidy").json()] == ["tidy"]


def sig(body: bytes) -> dict:
    ts = int(time.time())
    mac = hmac.new(b"0123456789abcdef-hook", str(ts).encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"X-Argus-Timestamp": str(ts), "X-Argus-Signature": f"sha256={mac}"}


def test_a_stopped_plugins_triggers_and_hooks_queue_nothing(tmp_path):
    inbox = tmp_path / "inbox"
    argus = make(tmp_path, hook=True, triggers=[
        MANUAL, {"folder_watch": {"workflow": "sort", "paths": [str(inbox)], "match": "*.txt", "stable_for": "1s"}},
        {"webhook": {"workflow": "sort", "secret_env": "TIDY_HOOK"}}])
    with TestClient(create_app(argus)) as c:
        folder = argus.cfg.triggers.folders[0].name
        file = {"worker": "w", "trigger": folder, "path": str(inbox / "a.txt"), "sha256": "a" * 64, "size": 1}
        body = json.dumps({"x": 1}).encode()
        c.post("/plugins/tidy/stop")
        r = c.post("/triggers/file", json=file)
        assert r.status_code == 409 and r.json()["error"] == "plugin_paused"
        assert c.post("/hooks/tidy", content=body, headers=sig(body)).status_code == 409
        assert c.get("/jobs?plugin=tidy").json() == []
        c.post("/plugins/tidy/start")
        assert c.post("/triggers/file", json=file).json()["status"] == "queued"  # the file was not "seen" meanwhile
        assert c.post("/hooks/tidy", content=body, headers=sig(body)).json()["status"] == "queued"


class Refusing:
    def __init__(self):
        self.calls = 0

    def post(self, path, body, **kw):
        self.calls += 1
        raise ApiError(409, {"error": "plugin_paused", "detail": "stopped"})


def test_the_folder_watcher_offers_files_again_while_the_plugin_is_stopped(tmp_path):
    from argus.worker.watcher import FolderWatcher

    t = [0.0]
    trig = {"name": "d", "path": str(tmp_path), "patterns": ["*"], "settle_seconds": 1}
    cl = Refusing()
    w = FolderWatcher(cl, "w", trig, clock=lambda: t[0])  # type: ignore[arg-type]
    (tmp_path / "a.pdf").write_bytes(b"A")
    w.scan_once()
    t[0] = 5
    assert w.scan_once()["waiting"] == 1 and w.backoff_until == 5 + 30
    t[0] = 40  # later, still stopped: it is asked again (not written off like a refused file)
    assert w.scan_once()["waiting"] == 1 and cl.calls == 2


# ------------------------------------------------------------------ the logs


def stamp(minute: int, sec: int = 0) -> str:
    return f"2026-11-14T10:{minute:02d}:{sec:02d}"


def at(minute: int, sec: int = 0) -> float:
    return float(timegm((2026, 11, 14, 10, minute, sec)))


def jl(minute: int, level: str, logger: str, msg: str, **extra) -> str:
    return json.dumps({"ts": stamp(minute), "level": level, "logger": logger, "msg": msg, **extra}) + "\n"


def write_logs(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "argus.log").write_text(
        jl(0, "info", "argus.plugin.tidy", "moved a.txt", plugin="tidy")
        + jl(1, "error", "argus.worker", "job failed", plugin="tidy", job="J1", error="boom token=abc123SECRETxyz")
        + jl(2, "info", "argus.worker", "job started", plugin="other")
        + jl(3, "info", "argus", "sent Authorization: Bearer abcdef123456789 to worker-a")
        + jl(4, "warning", "argus.jobs", "tidy is slow today")
        + jl(5, "info", "argus", "password=hunter2 and url https://x/a?t=ONETIMETOKEN&b=1 and "
             "key sk-ant-api03-AAAAAAAAAAAAAAAAAA and 0123456789abcdef0123456789abcdef0123456789abcdef")
        + jl(6, "info", "argus.plugin.tidy", "word " * 400, plugin="tidy"))
    (logs / "worker.log").write_text(
        jl(0, "info", "argus.worker", "worker registered", worker="worker-a")
        + jl(1, "info", "argus.worker", "job started", plugin="other", worker="worker-a-now")
        + jl(2, "info", "argus.worker", "tidy job started", plugin="tidy", worker="worker-b"))
    (logs / "worker-laptop.log").write_text(jl(0, "info", "argus.worker", "everything this worker says"))


def test_logs_for_a_plugin_are_filtered_and_stripped(tmp_path):
    write_logs(tmp_path)
    argus = make(tmp_path)
    with TestClient(create_app(argus)) as c:
        rows = c.get("/logs/tidy?level=all").json()
        assert isinstance(rows, list) and all(set(r) == {"ts", "level", "source", "msg"} for r in rows)
        msgs = [r["msg"] for r in rows]
        assert "moved a.txt" in msgs and any(m.startswith("job failed") for m in msgs)
        assert not any("job started" == m for m in msgs)  # "other"'s line is not about tidy
        assert any(m.startswith("tidy is slow today") for m in msgs) and any("tidy job started" in m for m in msgs)
        assert [r["ts"] for r in rows] == sorted(r["ts"] for r in rows)  # newest last
        assert rows[-1]["ts"] == at(6) and rows[0]["ts"] == at(0)
        assert {r["source"] for r in rows} <= {"argus", "worker"}
        assert all(len(r["msg"]) <= 400 for r in rows) and rows[-1]["msg"].endswith("…")
        text = json.dumps(rows)
        assert "abc123SECRETxyz" not in text and "token=***" in text


def test_logs_strip_secrets_everywhere(tmp_path):
    write_logs(tmp_path)
    argus = make(tmp_path, token="tok-123456789")
    env = tmp_path / ".env"
    extra = "ARGUS_WORKER_CAPS=laptop" + chr(10) + "MY_API_SECRET=zebra-secret-value" + chr(10)
    env.write_text(env.read_text() + extra)
    argus = Argus(load_config(tmp_path / "argus.yaml")).open()
    (tmp_path / "logs" / "argus.log").write_text(
        (tmp_path / "logs" / "argus.log").read_text() + jl(7, "info", "argus", "the worker said tok-123456789 once")
        + jl(8, "info", "argus", "worker-laptop said zebra-secret-value to the laptop"))
    with TestClient(create_app(argus)) as c:
        rows = c.get("/logs/argus?level=all&lines=500", headers={"Authorization": "Bearer tok-123456789"}).json()
        text = json.dumps(rows)
        for secret in ("abcdef123456789", "hunter2", "ONETIMETOKEN", "sk-ant-api03", "0123456789abcdef0123456789abcdef",
                       "tok-123456789"):
            assert secret not in text, secret
        assert "Authorization: ***" in text and "password=***" in text and "t=***" in text
        assert "zebra-secret-value" not in text  # a .env entry named like a secret
        assert "worker-laptop said *** to the laptop" in text  # a plain setting (caps=laptop) is not blanked
        assert len(rows) == 9  # the core's own log, all of it


def test_logs_for_a_worker_and_the_events_that_explain_it(tmp_path):
    write_logs(tmp_path)
    argus = make(tmp_path)
    with TestClient(create_app(argus)) as c:
        register(c, "worker-a")
        register(c, "worker-laptop")
        run(argus.store.write(lambda cn: [
            insert_event(cn, at(2, 30), "worker.offline", src="worker-a", data={"silent_seconds": 120}),
            insert_event(cn, at(3), "job.dead", job_id="J1", src="worker-a", dst="tidy",
                         data={"error": "Traceback...\nboom", "workflow": "sort"}),
            insert_event(cn, at(4), "job.retry", job_id="J2", src="worker-a", dst="other",
                         data={"error": "net down"}),
            insert_event(cn, at(5), "job.succeeded", job_id="J3", src="worker-a", dst="tidy")]))  # not a problem
        rows = c.get("/logs/worker-a?level=all").json()
        by = {r["msg"]: r for r in rows}
        assert "worker registered" in by and "job started" in by  # its id in the line, or its fast lane's
        assert by["worker went offline (silent for 120 s)"]["source"] == "events"
        assert by["worker went offline (silent for 120 s)"]["level"] == "error"
        assert by["job failed for good (job J1): Traceback... [sort]"]["ts"] == at(3)
        assert "tidy job started" not in by  # that is worker-b's
        assert not any("succeeded" in m for m in by)
        # a whole file named like the worker
        laptop = [r for r in c.get("/logs/worker-laptop?level=all").json() if r["source"] != "events"]
        assert [r["msg"] for r in laptop] == ["everything this worker says"]
        # a plugin explains itself with the same events
        tidy = c.get("/logs/tidy?level=all").json()
        assert any(r["source"] == "events" and r["msg"].startswith("job failed for good") for r in tidy)


def test_the_cap_and_the_order(tmp_path):
    write_logs(tmp_path)
    (tmp_path / "logs" / "argus.log").write_text("".join(json.dumps(
        {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(at(0) + i)), "level": "info", "logger": "argus",
         "msg": f"line {i}"}) + chr(10) for i in range(700)))
    with TestClient(create_app(make(tmp_path))) as c:
        assert len(c.get("/logs/argus?level=all&lines=9999").json()) == 500  # the cap
        assert len(c.get("/logs/argus?level=all").json()) == 200  # the default
        last = c.get("/logs/argus?level=all&lines=3").json()
        assert [r["msg"] for r in last] == ["line 697", "line 698", "line 699"]  # newest last


def test_log_levels_and_since(tmp_path):
    write_logs(tmp_path)
    with TestClient(create_app(make(tmp_path))) as c:
        errs = c.get("/logs/tidy?level=error").json()
        assert [r["level"] for r in errs] == ["error"] and errs[0]["msg"].startswith("job failed")
        newer = c.get(f"/logs/tidy?level=all&since={at(1)}").json()
        assert newer and all(r["ts"] > at(1) for r in newer) and not any(r["msg"] == "moved a.txt" for r in newer)
        assert c.get("/logs/tidy?level=bogus").status_code == 422


def test_unknown_component_404_and_the_old_log_view_is_unchanged(tmp_path):
    write_logs(tmp_path)
    argus = make(tmp_path)
    with TestClient(create_app(argus)) as c:
        assert c.get("/logs/nope?level=all").status_code == 404
        assert c.get("/logs/..%2f.env?level=all").status_code == 404
        assert c.get("/logs/nope").status_code == 404
        old = c.get("/logs/worker").json()  # Helios's Logs page: no level / since, a file name
        assert set(old) == {"entries", "offset"} and old["entries"][0]["msg"] == "worker registered"
        assert c.get("/logs/worker?after=0&lines=5").json()["entries"][0]["extra"]["worker"] == "worker-a"
        assert isinstance(c.get("/logs/argus?level=all").json(), list)  # the new shape, asked for with level
        assert {"argus", "worker", "worker-laptop"} <= {s["name"] for s in c.get("/logs").json()}
        assert isinstance(c.get("/logs/tidy").json(), list)  # not a file: a component


def test_the_supervisor_and_systemd_bring_a_restarted_worker_back(tmp_path, monkeypatch):
    import argus.supervisor as sup

    started = []
    monkeypatch.setattr(sup.Child, "start", lambda self: started.append(self.name))
    ch = sup.Child("worker", ["-m", "argus.worker.cli"])
    ch.proc = SimpleNamespace(poll=lambda: EXIT_RESTART, returncode=EXIT_RESTART)  # type: ignore[assignment]
    ch.started = time.monotonic()
    ch.check()
    assert started == ["worker"] and ch.fails == 0  # at once, not counted as a crash
    unit = (Path(__file__).resolve().parents[1] / "deploy" / "linux" / "argus-worker.service").read_text()
    assert "Restart=always" in unit and "SuccessExitStatus=75" in unit and "ARGUS_SUPERVISED=1" in unit
    # and the supervisor tells every child it is supervised (a worker only restarts itself when something will)
    seen = {}
    monkeypatch.undo()
    monkeypatch.setattr(sup, "ROOT", tmp_path)
    monkeypatch.setattr(sup.subprocess, "Popen", lambda argv, **kw: seen.update(kw) or SimpleNamespace(pid=1))
    sup.Child("worker", ["-m", "argus.worker.cli"]).start()
    assert seen["env"]["ARGUS_SUPERVISED"] == "1"
