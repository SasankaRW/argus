"""The queue view: running, waiting for you, and queued in run order with the reason each one waits."""

from __future__ import annotations

from datetime import datetime

from argus.config import JobsConfig
from argus.jobs import JobStore
from conftest import run


def test_queue_order_and_reasons(store, clock):
    clock.now = datetime(2026, 9, 29, 12, 0).timestamp()
    jobs = JobStore(store, JobsConfig(plugin_concurrency=1), clock=clock, windows={"night": "01:00-06:00"})
    busy, _ = run(jobs.enqueue("dorg", "sort", needs=["desktop"]))
    run(jobs.claim("pc", ["desktop", "gpu"]))
    run(jobs.start(busy, "pc"))
    gpu, _ = run(jobs.enqueue("vision", "name", needs=["gpu"], priority=90))
    run(jobs.claim("pc", ["desktop", "gpu"], plugins=["vision"]))
    run(jobs.start(gpu, "pc"))
    same_plugin, _ = run(jobs.enqueue("dorg", "file", needs=["desktop"]))
    night, _ = run(jobs.enqueue("research", "run", window="night"))
    gpu2, _ = run(jobs.enqueue("ocr", "read", needs=["gpu"], priority=50))
    laptop, _ = run(jobs.enqueue("cashly", "add", needs=["laptop"]))
    free, _ = run(jobs.enqueue("demo", "echo", priority=20))
    workers = [{"id": "pc", "state": "online", "capabilities": ["desktop", "gpu"]},
               {"id": "old", "state": "offline", "capabilities": ["laptop"]}]
    q = run(jobs.queue(workers))
    assert {r["id"] for r in q["running"]} == {busy, gpu} and all(r["worker"] == "pc" for r in q["running"])
    why = {r["id"]: r["why"] for r in q["queued"]}
    assert why == {same_plugin: "plugin_busy", night: "window", gpu2: "gpu_busy", laptop: "no_worker",
                   free: "next"}
    assert [r["position"] for r in q["queued"]] == [1, 2, 3, 4, 5]
    opens = next(r for r in q["queued"] if r["id"] == night)["until"]
    assert datetime.fromtimestamp(opens) == datetime(2026, 9, 30, 1, 0)
