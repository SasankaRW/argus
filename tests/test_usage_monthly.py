"""Month-by-month use per plugin (F1): the rollup, its retention, and `GET /models/usage/monthly`."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from argus import usage
from argus.events import insert_event, prune_events
from test_timeline import job, make
from test_worker import Server, client

NOW = dt.datetime(2026, 11, 14, 12, 0).timestamp()


def at(y, m, d=10):
    return dt.datetime(y, m, d, 12, 0).timestamp()


def seed(c):
    job(c, "j1", "succeeded", plugin="mail", at=at(2026, 9))
    job(c, "j2", "dead", plugin="mail", at=at(2026, 10))
    job(c, "j3", "succeeded", plugin="files", at=at(2026, 10))
    job(c, "j4", "queued", plugin="files", at=at(2026, 10))
    c.execute("UPDATE jobs SET finished_at = NULL WHERE id = 'j4'")
    for n in range(3):
        insert_event(c, at(2026, 10) + n, "model.request", job_id="j2", src="worker", dst="local")
    insert_event(c, at(2026, 10), "model.escalated", job_id="j2", src="worker", dst="claude")
    insert_event(c, at(2026, 9), "model.request", job_id="j1", src="worker", dst="local")
    insert_event(c, at(2026, 11), "model.request", src="ari", dst="local")  # no job: counted under the sender


def test_rollup_counts_per_month_and_plugin(tmp_path: Path):
    store = make(tmp_path).open().store
    try:
        store.write_sync(seed)
        out = store.write_sync(lambda c: (usage.rollup(c, NOW), usage.monthly(c, 3, NOW))[1])
    finally:
        store.close()
    assert out["from"] == "2026-09" and [m["month"] for m in out["data"]] == ["2026-09", "2026-10", "2026-11"]
    sep, octo, nov = out["data"]
    assert sep["model_calls"] == 1 and sep["job_runs"] == 1
    assert octo["model_calls"] == 3 and octo["job_runs"] == 2
    assert [p["plugin"] for p in octo["plugins"]] == ["mail", "files"]
    mail = octo["plugins"][0]
    assert mail == {"plugin": "mail", "model_calls": 3, "escalations": 1, "job_runs": 1, "job_failed": 1}
    assert nov["plugins"] == [{"plugin": "ari", "model_calls": 1, "escalations": 0, "job_runs": 0, "job_failed": 0}]


def test_counts_survive_event_pruning_and_a_rerun_does_not_double(tmp_path: Path):
    store = make(tmp_path).open().store
    try:
        store.write_sync(seed)
        store.write_sync(lambda c: usage.rollup(c, NOW))
        store.write_sync(lambda c: usage.rollup(c, NOW))  # twice: same numbers
        assert store.write_sync(lambda c: prune_events(c, at(2026, 11, 1))) > 0
        out = store.write_sync(lambda c: (usage.rollup(c, NOW), usage.monthly(c, 12, NOW))[1])
    finally:
        store.close()
    octo = next(m for m in out["data"] if m["month"] == "2026-10")
    assert octo["model_calls"] == 3 and octo["job_runs"] == 2


def test_old_months_are_dropped_and_months_are_bounded(tmp_path: Path):
    store = make(tmp_path).open().store
    try:
        def fn(c):
            c.execute("INSERT INTO usage_monthly (month, plugin, model_calls) VALUES ('2000-01', 'old', 5)")
            c.execute("INSERT INTO usage_monthly (month, plugin, model_calls) VALUES ('2026-11', 'new', 2)")
            usage.rollup(c, NOW)
            return usage.monthly(c, 1000, NOW)

        out = store.write_sync(fn)
    finally:
        store.close()
    assert out["months"] == usage.MAX_MONTHS
    assert [m["month"] for m in out["data"]] == ["2026-11"]


def test_endpoint_and_existing_usage_route_unchanged(tmp_path: Path, monkeypatch):
    from test_ask import make as make_ask

    with Server(make_ask(tmp_path, monkeypatch).open()) as srv:
        cl = client(srv.url)
        assert cl.get("/models/usage") == {"days": 7, "daily": [], "plugins": []}
        out = cl.get("/models/usage/monthly")
        assert out["months"] == 12 and out["data"] == []
        assert cl.get("/models/usage/monthly?months=3")["months"] == 3
