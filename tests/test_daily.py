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


def reminders_job(a, now, result, state="succeeded", finished=None):
    """A diary.reminders job with `result` (what the 04:00 run leaves for the brief)."""
    jid, _ = run(a.jobs.enqueue("diary", "reminders"))
    run(a.store.write(lambda c: c.execute("UPDATE jobs SET state = ?, result = ?, finished_at = ? WHERE id = ?",
                                          (state, json.dumps(result), finished or now - 3 * 3600, jid))))


def test_brief_says_only_what_needs_you(tmp_path):
    a = make(tmp_path).open()
    now = ts(7)
    j1, _ = run(a.jobs.enqueue("demo", "echo"))
    j2, _ = run(a.jobs.enqueue("screenshot-renamer", "name"))
    run(a.store.write(lambda c: c.execute("INSERT INTO schedules (id, plugin, workflow, cron, spec, enabled,"
                                          " next_run_at, created_at, updated_at, owner, label) VALUES ('you-1',"
                                          "'argus','remind','0 17 30 9 *','{}',1,?,?,?,'you','call mum')",
                                          (ts(17), now, now))))
    reminders_job(a, now, {"for": "2026-09-30", "reminders": [
        {"text": "Call Kasun", "time": None, "mentioned_on": "2026-09-28"},
        {"text": "Dentist", "time": "15:30", "mentioned_on": "2026-09-27"}]})

    def fn(c):
        c.execute("UPDATE jobs SET state = 'succeeded', finished_at = ? WHERE id = ?", (now - 3600, j1))
        c.execute("UPDATE jobs SET state = 'dead', finished_at = ? WHERE id = ?", (now - 600, j2))
        c.execute("INSERT INTO approvals (id, job_id, key, plugin, type, title, state, expires_at, created_at,"
                  " updated_at) VALUES ('ap1', ?, 'k1', 'demo', 'entry', 'Approve draft', 'pending', ?, ?, ?)",
                  (j1, now + 3600, now, now))
        return compose_brief(c, now, last_backup={"ok": False, "at": ts(2, 30), "problem": "drive full"},
                             weather="Colombo: 25 to 31°, showers. Rain likely 2–5 pm (80%), dry morning.")

    title, text = run(a.store.write(fn))
    assert title == "Good morning - something needs you"
    assert text.splitlines() == [
        "Colombo: 25 to 31°, showers. Rain likely 2–5 pm (80%), dry morning.",
        "From your diary: 3:30 pm Dentist; Call Kasun.",
        "Waiting for you: 1 (Approve draft).",
        "Backup failed: drive full.",
        "1 thing failed overnight (screenshot-renamer.name)."]
    for gone in ("job", "done", "Today:", "PC:", "Docker", "Claude", "OK"):  # no statistics, nothing that is fine
        assert gone not in text, gone
    a.store.close()


def test_a_quiet_brief_and_stale_or_failed_reminders_are_left_out(tmp_path):
    a = make(tmp_path).open()
    now = ts(7)
    assert run(a.store.write(lambda c: compose_brief(c, now, last_backup={"ok": True, "at": ts(2, 30)}))) == (
        "Good morning", "Nothing needs you.")
    reminders_job(a, now, {"for": "2026-09-29", "reminders": [{"text": "Yesterday's", "time": None}]})  # not today
    reminders_job(a, now, {"for": "2026-09-30", "reminders": [{"text": "Never ran", "time": None}]},
                  state="dead", finished=now - 20 * 3600)
    title, text = run(a.store.write(lambda c: compose_brief(c, now, last_backup=None, weather="Kandy: clear.")))
    assert (title, text) == ("Good morning", "Kandy: clear.")  # a dead diary job (old) is neither shown nor counted
    reminders_job(a, now, {"for": "2026-09-30", "reminders": [{"text": "Pay rent", "time": "09:00"}]},
                  finished=now - 60)  # the newest succeeded one for today wins
    assert run(a.store.write(lambda c: compose_brief(c, now, last_backup=None)))[1] == "From your diary: 9 am Pay rent."
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


def test_the_week_and_what_to_schedule(tmp_path):
    from argus.daily import repeats, week_review
    from argus.events import insert_event

    a = make(tmp_path).open()
    now = datetime(2026, 10, 4, 20, 0).timestamp()  # a Sunday evening
    mondays = [datetime(2026, 9, d, 9, 10).timestamp() for d in (7, 14, 21, 28)]
    ids = [run(a.jobs.enqueue("downloads-organizer", "sort"))[0] for _ in mondays]
    sched, _ = run(a.jobs.enqueue("screenshot-renamer", "sweep"))

    def fn(c):
        for jid, at in zip(ids, mondays, strict=True):
            c.execute("UPDATE jobs SET state = 'succeeded', finished_at = ? WHERE id = ?", (at + 60, jid))
            c.execute("UPDATE events SET from_component = 'helios', at = ? WHERE job_id = ? AND kind = 'job.queued'",
                      (at, jid))
        insert_event(c, now - 3600, "job.queued", job_id=sched, src="scheduler", dst="screenshot-renamer")
        labels = {"run:downloads-organizer:sort": "Sort Downloads now"}
        return repeats(c, now, labels), week_review(c, now, labels)

    sugg, week = run(a.store.write(fn))
    assert sugg[0]["text"] == "You ran Sort Downloads now by hand 4 times in 4 weeks, mostly on Mondays around 9 am."
    assert sugg[0]["say"] == "sort downloads every Monday at 9 am"
    assert week["text"].startswith("This week: 1 job done (downloads-organizer 1)")
    assert 'Say "sort downloads every Monday at 9 am" to Ari' in week["text"]

    def scheduled(c):
        c.execute("INSERT INTO schedules (id, plugin, workflow, cron, spec, enabled, next_run_at, created_at,"
                  " updated_at, owner, label) VALUES ('s1','downloads-organizer','sort','0 9 * * 1','{}',1,?,?,?,"
                  "'you','x')", (now, now, now))
        return repeats(c, now)

    assert run(a.store.write(scheduled)) == []  # already scheduled: nothing to suggest
    a.store.close()
