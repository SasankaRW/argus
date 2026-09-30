"""The morning brief, the "Argus is back" message after a power cut, and the PC health check."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import argus.worker.health as health
from argus.config import load_config
from argus.context import Argus
from argus.daily import Marker, brief_due, compose_brief
from conftest import run


def make(tmp_path: Path, extra: str = "") -> Argus:
    (tmp_path / "argus.yaml").write_text("logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 30\n" + extra,
                                         encoding="utf-8")
    return Argus(load_config(tmp_path / "argus.yaml"))


def ts(h, m=0, day=30):
    return datetime(2026, 9, day, h, m).timestamp()


def test_brief_once_a_morning():
    assert brief_due("07:00", ts(6, 59), None) is None
    assert brief_due("07:00", ts(7, 0), None) == "2026-09-30"
    assert brief_due("07:00", ts(9, 30), "2026-09-29") == "2026-09-30"  # argusd was off at 7
    assert brief_due("07:00", ts(7, 5), "2026-09-30") is None  # sent already
    assert brief_due("07:00", ts(13, 0), None) is None  # too late in the day


def test_brief_says_what_matters(tmp_path):
    a = make(tmp_path).open()
    now = ts(7)
    j1, _ = run(a.jobs.enqueue("demo", "echo"))
    j2, _ = run(a.jobs.enqueue("screenshot-renamer", "name"))

    def fn(c):
        c.execute("UPDATE jobs SET state = 'succeeded', finished_at = ? WHERE id = ?", (now - 3600, j1))
        c.execute("UPDATE jobs SET state = 'dead', finished_at = ? WHERE id = ?", (now - 600, j2))
        c.execute("INSERT INTO schedules (id, plugin, workflow, cron, spec, enabled, next_run_at, created_at,"
                  " updated_at, owner, label) VALUES ('you-1','argus','remind','0 17 30 9 *','{}',1,?,?,?,'you',?)",
                  (ts(17), now, now, 'today at 5 pm: remind you: "call mum"'))
        c.execute("INSERT INTO budget (day, key, used, created_at, updated_at) VALUES ('2026-09-29','claude_calls',"
                  "3,?,?)", (now, now))
        return compose_brief(c, now, last_backup={"ok": True, "at": ts(2, 30)}, pc_online=False,
                             health={"problems": ["eclaire-db is exited"]}, claude_cap=30)

    title, text = run(a.store.write(fn))
    assert title == "Good morning - something needs you"
    assert "Overnight: 1 job done, 1 failed (screenshot-renamer.name)." in text
    assert 'Today: 5 pm remind you: "call mum".' in text
    assert "Backup at 2:30 am: OK." in text and "PC: off." in text
    assert "eclaire-db is exited" in text and "Claude yesterday: 3 of 30 calls." in text
    a.store.close()


def test_back_after_a_power_cut(tmp_path):
    a = make(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    Marker(a.cfg.db_path.parent / "argus.running").path.write_text(json.dumps({"alive": ts(2, 14)}))
    a.open()
    jid, _ = run(a.jobs.enqueue("downloads-organizer", "sort"))
    run(a.store.write(lambda c: c.execute("UPDATE jobs SET state = 'running' WHERE id = ?", (jid,))))

    async def go():
        await a.start()
        await a.stop()

    run(go())
    assert a.resumed["jobs"] == ["downloads-organizer.sort"]
    assert not (a.cfg.db_path.parent / "argus.running").exists()  # a clean stop removes it
    a2 = make(tmp_path).open()
    msgs = run(a2.store.read(lambda c: [json.loads(r[0]) for r in c.execute("SELECT payload FROM outbox")]))
    back = [m for m in msgs if m.get("title") == "Argus is back"]
    assert len(back) == 1 and "around 2:14 am" in back[0]["message"]
    assert "downloads-organizer.sort" in back[0]["message"]
    run(a2.start())
    assert a2.resumed is None  # stopped cleanly last time
    run(a2.stop())


def test_health_problems():
    d = {"up": True, "running": 1, "containers": [
        {"name": "eclaire-app", "state": "running", "status": "Up 2 hours (unhealthy)"},
        {"name": "eclaire-db", "state": "exited", "status": "Exited (1) 3 minutes ago"}]}
    assert health.problems(d, ["eclaire-app", "eclaire-db", "redis"]) == [
        "eclaire-db is exited", "redis is missing", "eclaire-app is unhealthy"]
    assert health.problems({"up": False}, []) == ["Docker is not answering (Docker Desktop stopped?)"]
    assert health.problems(None, []) == []


def test_health_tells_the_phone_only_about_changes(tmp_path, monkeypatch):
    from argus.worker.workflows import Context

    monkeypatch.chdir(tmp_path)
    notes = []
    state = {"d": {"up": True, "running": 0, "containers": [{"name": "db", "state": "exited", "status": "x"}]}}
    monkeypatch.setattr(health, "docker", lambda: state["d"])
    monkeypatch.setattr(health, "wsl", lambda: None)

    class R:
        lease_lost = False

        def step(self, *a, **k):
            pass

        def notify(self, body):
            notes.append(body["title"])
            return {"queued": True}

    def once():
        return health.check(Context({"id": f"j{len(notes)}-{id(state)}", "input": {"containers": ["db"]}}, R()))

    assert once()["problems"] == ["db is exited"] and notes == ["PC health: something stopped"]
    once()
    assert len(notes) == 1  # the same problem again: quiet
    state["d"] = {"up": True, "running": 1, "containers": [{"name": "db", "state": "running", "status": "Up"}]}
    once()
    assert notes[-1] == "PC health: all fine again"


def test_health_schedule_only_when_on(tmp_path):
    a = make(tmp_path, "health:\n  enabled: true\n  every_minutes: 30\n  containers: [eclaire-db]\n").open()
    run(a.start())
    s = [x for x in run(a.scheduler.list()) if x["id"] == "argus-health"]
    assert s and s[0]["cron"] == "*/30 * * * *" and s[0]["spec"]["input"] == {"containers": ["eclaire-db"]}
    run(a.stop())


def test_time_saved_counts_each_job_once_and_the_evening_summary(tmp_path):
    from argus.daily import compose_summary, time_saved

    a = make(tmp_path).open()
    now = ts(20)
    j1, _ = run(a.jobs.enqueue("downloads-organizer", "sort"))
    j2, _ = run(a.jobs.enqueue("screenshot-renamer", "name"))

    def fn(c):
        day = "2026-09-30"
        for job, key, secs in ((j1, "moved", 600), (j1, "moved", 600), (j2, "named", 90)):  # retried: same key
            c.execute("INSERT OR REPLACE INTO time_saved VALUES (?,?,?,?,?,?)",
                      (job, key, "downloads-organizer" if job == j1 else "screenshot-renamer", day, secs, now))
        c.execute("UPDATE jobs SET state = 'succeeded', finished_at = ?", (now - 60,))
        return time_saved(c, now, 7), compose_summary(c, now)

    week, (title, text) = run(a.store.write(fn))
    assert week["seconds"] == 690 and week["text"] == "12 min"
    assert [p["plugin"] for p in week["plugins"]] == ["downloads-organizer", "screenshot-renamer"]
    assert "Today: 2 jobs done" in text and "Saved you about 12 min today" in text
    a.store.close()



def test_held_messages_come_with_the_evening_summary_once(tmp_path):
    from argus.daily import compose_summary

    a = make(tmp_path).open()
    now = ts(20)

    def fn(c):
        c.execute("INSERT INTO held_notes (plugin, title, text, created_at) VALUES ('demo', 'Filed: CEB bill', '', ?)",
                  (now - 100,))
        return compose_summary(c, now)[1], compose_summary(c, now + 60)[1]

    first, second = run(a.store.write(fn))
    assert "Also: Filed: CEB bill." in first and "Filed" not in second
    a.store.close()
