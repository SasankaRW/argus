"""C9: scheduler, triggers and dispatcher. Gate: 100 mixed jobs run with at most 2 model swaps, and a flood of 500
files is merged and limited."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from argus.api import create_app
from argus.config import Config, JobsConfig, ScheduleConfig, load_config
from argus.context import Argus
from argus.cron import CronError, in_window, parse, window_opens
from argus.jobs import JobState, JobStore, QueueFull
from argus.scheduler import Scheduler
from conftest import run

# ------------------------------------------------------------------ cron and windows


def test_cron_fields_steps_names_and_either_day_rule():
    c = parse("*/15 9-17 * * mon-fri")
    assert c.next_after(datetime(2026, 9, 26, 18, 0)) == datetime(2026, 9, 28, 9, 0)  # Saturday -> Monday
    assert parse("@daily").next_after(datetime(2026, 9, 29, 10, 5)) == datetime(2026, 9, 30, 0, 0)
    assert parse("0 0 29 2 *").next_after(datetime(2026, 3, 1)) == datetime(2028, 2, 29)
    # day of month OR day of week when both are set: the 1st, or any Monday
    assert parse("30 8 1 * 1").next_after(datetime(2026, 9, 29, 10, 0)) == datetime(2026, 10, 1, 8, 30)
    assert parse("0 7 * * 7").next_after(datetime(2026, 9, 29)) == datetime(2026, 10, 4, 7, 0)  # 7 = Sunday
    for bad in ("99 * * * *", "* * *", "*/0 * * * *", "0 25 * * *", "0 0 * foo *"):
        with pytest.raises(CronError):
            parse(bad)


def test_windows_including_past_midnight():
    at = lambda h, m=0: datetime(2026, 9, 29, h, m).timestamp()  # noqa: E731
    assert in_window("01:00-06:00", at(3)) and not in_window("01:00-06:00", at(6))
    assert in_window("22:00-06:00", at(23)) and in_window("22:00-06:00", at(5)) and not in_window("22:00-06:00", at(12))
    assert window_opens("01:00-06:00", at(3)) == at(3)
    assert datetime.fromtimestamp(window_opens("01:00-06:00", at(12))) == datetime(2026, 9, 30, 1, 0)


# ------------------------------------------------------------------ scheduler


def sched_setup(store, clock, schedules):
    cfg = Config(schedules=schedules)
    jobs = JobStore(store, JobsConfig(plugin_concurrency=50), clock=clock)
    sch = Scheduler(store, jobs, cfg, clock=clock)
    run(sch.sync())
    return jobs, sch, cfg


def test_schedule_runs_when_due_and_catches_up_once(store, clock):
    clock.now = datetime(2026, 9, 29, 6, 59).timestamp()
    jobs, sch, _ = sched_setup(store, clock, [ScheduleConfig(id="brief", plugin="demo", workflow="echo",
                                                             cron="0 7 * * *", input={"text": "hi"})])
    assert run(sch.tick()) == 0
    clock.now = datetime(2026, 9, 29, 7, 0, 5).timestamp()
    assert run(sch.tick()) == 1 and run(sch.tick()) == 0  # once per slot
    s = run(sch.list())[0]
    assert datetime.fromtimestamp(s["next_run_at"]) == datetime(2026, 9, 30, 7, 0)
    job = run(jobs.get(s["last_job_id"]))
    assert job.input["text"] == "hi" and job.input["_schedule"] == "brief"
    # Argus was off for three days: one catch-up run, not three; the previous run still queued is merged
    clock.now = datetime(2026, 10, 3, 9, 0).timestamp()
    assert run(sch.tick()) == 0  # merged into the still-queued job (dedupe)
    assert datetime.fromtimestamp(run(sch.list())[0]["next_run_at"]) == datetime(2026, 10, 4, 7, 0)
    run(jobs.cancel(job.id))
    clock.now = datetime(2026, 10, 4, 7, 1).timestamp()
    assert run(sch.tick()) == 1


def test_schedule_config_changes_and_run_now(store, clock):
    clock.now = datetime(2026, 9, 29, 12, 0).timestamp()
    jobs, sch, _ = sched_setup(store, clock, [ScheduleConfig(id="a", plugin="demo", workflow="echo", cron="@daily"),
                                              ScheduleConfig(id="b", plugin="demo", workflow="echo", cron="@hourly")])
    run(sch.sync([ScheduleConfig(id="a", plugin="demo", workflow="echo", cron="30 * * * *")]))  # b removed
    rows = {s["id"]: s for s in run(sch.list())}
    assert not rows["b"]["enabled"]
    assert datetime.fromtimestamp(rows["a"]["next_run_at"]) == datetime(2026, 9, 29, 12, 30)
    jid = run(sch.run_now("a"))
    assert jid and run(jobs.get(jid)).state is JobState.QUEUED
    assert run(sch.list())[0]["next_run_at"] == rows["a"]["next_run_at"]  # the clock did not move
    assert run(sch.run_now("nope")) is None


# ------------------------------------------------------------------ dispatcher


def test_one_job_per_plugin_by_default_and_overrides(store, clock):
    jobs = JobStore(store, JobsConfig(concurrency={"fast": 2}), clock=clock)

    async def go():
        for p in ("slow", "slow", "fast", "fast", "fast"):
            await jobs.enqueue(p, "w", {})
        got = [await jobs.claim(f"w{i}", []) for i in range(5)]
        return [j.plugin if j else None for j in got]

    assert run(go()) == ["slow", "fast", "fast", None, None]


def test_gpu_jobs_run_one_at_a_time_grouped_by_model_gate(store, clock):
    """100 mixed jobs (two models, arriving interleaved) run with at most 2 model swaps."""
    jobs = JobStore(store, JobsConfig(plugin_concurrency=50, plugin_queue_limit=500), clock=clock)

    async def go():
        for i in range(100):
            clock.advance(1)
            model = "qwen2.5-coder:7b" if i % 3 else "qwen2.5-coder:14b"
            await jobs.enqueue(f"p{i % 4}", "w", {}, needs=["gpu"], model_group=model)
        await jobs.enqueue("cpu", "w", {})
        assert (await jobs.claim("laptop", ["cpu"])).plugin == "cpu"  # CPU work is not held up by GPU work
        a = await jobs.claim("pc", ["gpu"])
        assert await jobs.claim("pc2", ["gpu"]) is None  # the GPU is taken
        order = [a.model_group]
        await jobs.start(a.id, "pc")
        await jobs.succeed(a.id, "pc")
        while True:
            j = await jobs.claim("pc", ["gpu"])
            if j is None:
                break
            order.append(j.model_group)
            await jobs.start(j.id, "pc")
            await jobs.succeed(j.id, "pc")
        return order

    order = run(go())
    swaps = sum(1 for a, b in zip(order, order[1:], strict=False) if a != b)
    assert len(order) == 100 and swaps <= 2, f"{swaps} model swaps"
    events = store.read_sync(lambda c: [json.loads(r[0]) for r in c.execute(
        "SELECT data FROM events WHERE kind = 'job.leased' AND data LIKE '%model_swap%'")])
    assert len(events) == swaps


def test_priority_beats_model_grouping_and_windows_hold_jobs(store, clock):
    clock.now = datetime(2026, 9, 29, 12, 0).timestamp()
    jobs = JobStore(store, JobsConfig(plugin_concurrency=50), clock=clock, windows={"night": "01:00-06:00"})

    async def go():
        a = await jobs.enqueue("p", "w", {}, needs=["gpu"], model_group="7b")
        first = await jobs.claim("pc", ["gpu"])
        await jobs.start(first.id, "pc")
        await jobs.succeed(first.id, "pc")
        await jobs.enqueue("p", "w", {}, needs=["gpu"], model_group="7b", priority=50)
        urgent, _ = await jobs.enqueue("p", "w", {}, needs=["gpu"], model_group="14b", priority=90)
        night, _ = await jobs.enqueue("n", "w", {}, window="night", priority=100)
        got = await jobs.claim("pc", ["gpu"])
        held = await jobs.claim("laptop", [], plugins=["n"])
        clock.now = datetime(2026, 9, 30, 1, 30).timestamp()
        later = await jobs.claim("laptop", [], plugins=["n"])
        return a, got.id == urgent, held, later.id == night

    _, urgent_first, held, night_ok = run(go())
    assert urgent_first and held is None and night_ok


# ------------------------------------------------------------------ triggers (through the API)


def make(tmp_path: Path, extra: str = "", env: str = "") -> Argus:
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\njobs:\n  plugin_queue_limit: 100\n"
        "triggers:\n  folders:\n    - {name: downloads, plugin: organizer, workflow: sort, path: /tmp/x, worker: pc}\n"
        "  webhooks:\n    - {name: deploy, plugin: ops, workflow: deploy, secret_env: HOOK_SECRET}\n"
        "    - {name: gh, plugin: review, workflow: pr, secret_env: GH_SECRET, style: github}\n" + extra,
        encoding="utf-8")
    (tmp_path / ".env").write_text("ARGUS_WORKER_TOKEN=tok\nHOOK_SECRET=0123456789abcdef-hook\n"
                                   "GH_SECRET=0123456789abcdef-gh\n" + env, encoding="utf-8")
    return Argus(load_config(tmp_path / "argus.yaml"))


H = {"Authorization": "Bearer tok"}


def sha(n: int) -> str:
    return hashlib.sha256(str(n).encode()).hexdigest()


def test_flood_of_500_files_is_merged_and_limited_gate(tmp_path):
    argus = make(tmp_path).open()
    with TestClient(create_app(argus)) as c:
        reg = c.post("/workers/register", headers=H, json={"id": "worker-pc", "host": "PC"}).json()
        assert [f["name"] for f in reg["folders"]] == ["downloads"]
        codes = {"queued": 0, "duplicate": 0, "full": 0}
        for i in range(500):  # 450 different files; every tenth is a copy of an earlier one
            content = i - 1 if i % 10 == 9 else i
            r = c.post("/triggers/file", headers=H, json={"worker": "worker-pc", "trigger": "downloads",
                                                          "path": f"C:/Users/Sas/Downloads/f{i}.pdf",
                                                          "sha256": sha(content), "size": 10})
            if r.status_code == 429:
                codes["full"] += 1
            else:
                codes[r.json()["status"]] += 1
        assert codes["queued"] == 100  # the plugin's queue limit holds the flood back
        assert c.get("/jobs/counts", headers=H).json() == {"queued": 100}
        assert codes["duplicate"] + codes["full"] == 400
        # the same file again later (a re-download, a restarted watcher) is merged, even after its job finished
        first = c.post("/triggers/file", headers=H, json={"worker": "worker-pc", "trigger": "downloads",
                                                          "path": "C:/copy.pdf", "sha256": sha(0), "size": 10})
        assert first.json()["status"] == "duplicate"
        bad = c.post("/triggers/file", headers=H, json={"worker": "worker-pc", "trigger": "nope", "path": "x",
                                                        "sha256": sha(1), "size": 1})
        assert bad.status_code == 404


def _argus_sig(secret: str, body: bytes, ts: int) -> dict:
    mac = hmac.new(secret.encode(), str(ts).encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"X-Argus-Timestamp": str(ts), "X-Argus-Signature": f"sha256={mac}"}


def test_webhooks_need_a_valid_fresh_signature(tmp_path):
    argus = make(tmp_path).open()
    with TestClient(create_app(argus)) as c:
        body = json.dumps({"ref": "main"}).encode()
        now = int(time.time())
        ok = c.post("/hooks/deploy", content=body, headers={**_argus_sig("0123456789abcdef-hook", body, now),
                                                            "X-Argus-Id": "d1"})
        assert ok.status_code == 200 and ok.json()["status"] == "queued"
        job = c.get(f"/jobs/{ok.json()['job_id']}", headers=H).json()
        assert job["input"]["body"] == {"ref": "main"} and job["priority"] == 90
        again = c.post("/hooks/deploy", content=body, headers={**_argus_sig("0123456789abcdef-hook", body, now),
                                                               "X-Argus-Id": "d1"})
        assert again.json()["status"] == "duplicate"  # a retried delivery is merged
        assert c.post("/hooks/deploy", content=body, headers=_argus_sig("wrong-secret-0000000", body, now)
                      ).status_code == 401
        assert c.post("/hooks/deploy", content=body, headers=_argus_sig("0123456789abcdef-hook", body, now - 3600)
                      ).status_code == 401  # a replayed old request
        assert c.post("/hooks/deploy", content=body).status_code == 401
        assert c.post("/hooks/nope", content=body).status_code == 404
        gh = hmac.new(b"0123456789abcdef-gh", body, hashlib.sha256).hexdigest()
        r = c.post("/hooks/gh", content=body, headers={"X-Hub-Signature-256": f"sha256={gh}",
                                                       "X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "g1"})
        assert r.status_code == 200
        assert c.get(f"/jobs/{r.json()['job_id']}", headers=H).json()["input"]["event"] == "pull_request"


def test_webhook_secret_must_be_in_env(tmp_path):
    from argus.config import ConfigError

    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\ntriggers:\n  webhooks:\n"
        "    - {name: deploy, plugin: ops, workflow: deploy, secret_env: MISSING_SECRET}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="MISSING_SECRET"):
        load_config(tmp_path / "argus.yaml")


def test_schedules_api_and_submit_with_model_and_window(tmp_path):
    argus = make(tmp_path, "schedules:\n  - {id: brief, plugin: demo, workflow: echo, cron: '0 7 * * *'}\n").open()
    with TestClient(create_app(argus)) as c:
        s = c.get("/schedules", headers=H).json()
        assert [x["id"] for x in s] == ["brief"] and s[0]["next_run_at"] > time.time()
        jid = c.post("/schedules/brief/run", headers=H).json()["job_id"]
        evs = [e for e in c.get(f"/jobs/{jid}/events", headers=H).json() if e["kind"] == "job.queued"]
        assert evs[0]["from_component"] == "scheduler"
        r = c.post("/jobs", headers=H, json={"plugin": "p", "workflow": "w", "model": "7b", "window": "night"})
        job = c.get(f"/jobs/{r.json()['id']}", headers=H).json()
        assert job["model_group"] == "7b" and job["run_window"] == "night"


# ------------------------------------------------------------------ the folder watcher (worker side)


class FakeClient:
    def __init__(self, full_after: int | None = None):
        self.calls: list[dict] = []
        self.seen: set[str] = set()
        self.full_after = full_after

    def post(self, path, body, **kw):
        from argus.worker.client import ApiError

        assert path == "/triggers/file"
        if self.full_after is not None and len(self.calls) >= self.full_after:
            raise ApiError(429, {"error": "queue_full"})
        self.calls.append(body)
        dup = body["sha256"] in self.seen
        self.seen.add(body["sha256"])
        return {"status": "duplicate" if dup else "queued", "job_id": "j"}


def test_watcher_waits_for_files_to_settle_ignores_temp_files_and_backs_off(tmp_path):
    from argus.worker.watcher import FolderWatcher

    t = [0.0]
    trig = {"name": "downloads", "path": str(tmp_path), "patterns": ["*"], "settle_seconds": 10,
            "ignore": ["*.crdownload", "~$*"]}
    cl = FakeClient()
    w = FolderWatcher(cl, "worker-pc", trig, clock=lambda: t[0])
    (tmp_path / "a.pdf").write_bytes(b"A")
    (tmp_path / "big.iso.crdownload").write_bytes(b"...")
    assert w.scan_once()["reported"] == 0  # just appeared: wait for it to settle
    t[0] = 5
    (tmp_path / "a.pdf").write_bytes(b"AA")  # still being written
    assert w.scan_once()["reported"] == 0
    t[0] = 16
    assert w.scan_once() == {"reported": 1, "duplicate": 0, "waiting": 0}
    assert [c["path"].endswith("a.pdf") for c in cl.calls] == [True]
    assert w.scan_once()["reported"] == 0  # reported once
    (tmp_path / "copy.pdf").write_bytes(b"AA")
    t[0] = 30
    w.scan_once()
    t[0] = 41
    assert w.scan_once()["duplicate"] == 1
    # queue full: hold back, then try again after the back-off
    cl.full_after = len(cl.calls)
    for i in range(3):
        (tmp_path / f"n{i}.pdf").write_bytes(bytes([i]))
    t[0] = 50
    w.scan_once()
    t[0] = 61
    assert w.scan_once()["waiting"] == 3 and w.backoff_until == 61 + 30
    cl.full_after = None
    t[0] = 80
    assert w.scan_once()["reported"] == 0  # still backing off
    t[0] = 92
    assert w.scan_once()["reported"] == 3


def test_queue_full_rolls_back_the_seen_file(store, clock):
    """A file refused because the queue is full must be offered again later, not remembered as done."""
    from argus.triggers import Triggers

    cfg = Config.model_validate({"jobs": {"plugin_queue_limit": 1}, "triggers": {"folders": [
        {"name": "d", "plugin": "org", "workflow": "sort", "path": "/x", "worker": "pc"}]}})
    jobs = JobStore(store, cfg.jobs, clock=clock)
    tr = Triggers(store, jobs, cfg, clock=clock)
    assert run(tr.file("pc", "d", "/x/1", sha(1), 1))["status"] == "queued"
    with pytest.raises(QueueFull):
        run(tr.file("pc", "d", "/x/2", sha(2), 1))
    j = run(jobs.claim("w", []))
    run(jobs.start(j.id, "w"))
    run(jobs.succeed(j.id, "w"))
    assert run(tr.file("pc", "d", "/x/2", sha(2), 1))["status"] == "queued"
