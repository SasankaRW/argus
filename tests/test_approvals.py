"""C8: approvals, the outbox and the phone app. Gate: approving from the phone resumes the job in under 1 s, and a
crash never sends the same notification twice."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

import pytest

import argus.worker.demo  # noqa: F401 - registers the demo workflows
from argus.approvals import ApprovalClosed, Approvals, BadToken, summarize
from argus.config import Config, JobsConfig, load_config
from argus.context import Argus
from argus.jobs import JobState
from argus.outbox import Outbox
from argus.worker import Worker, WorkflowRegistry, workflow
from conftest import run
from fakes import FakePhoneApp
from test_worker import Server, client, wait_for


def cfg_with(public_url: str | None = "http://phone-reachable:8600") -> Config:
    c = Config()
    c.approvals.public_url = public_url
    return c


class Sender:
    """A push sender (a webhook, say) inside unit tests."""

    enabled = True

    def __init__(self, fail: int = 0):
        self.sent: list[dict] = []
        self.fail = fail

    def send(self, payload: dict) -> None:
        if self.fail:
            self.fail -= 1
            raise RuntimeError("sender down")
        self.sent.append(payload)


def setup(store, clock, cfg=None):
    from argus.jobs import JobStore

    cfg = cfg or cfg_with()
    jobs = JobStore(store, JobsConfig(plugin_concurrency=50), clock=clock)
    ap = Approvals(store, jobs, cfg, clock=clock)
    run(ap.start())
    return jobs, ap, cfg


async def _leased(jobs, plugin="demo"):
    jid, _ = await jobs.enqueue(plugin, "approval", {})
    await jobs.claim("w1", [])
    await jobs.start(jid, "w1")
    return jid


# ------------------------------------------------------------------ approvals (store level)


def test_request_is_idempotent_and_queues_one_message(store, clock):
    jobs, ap, _ = setup(store, clock)

    async def go():
        jid = await _leased(jobs)
        a1, c1 = await ap.request(jid, "w1", f"{jid}:1:1", "entry", "CEB bill",
                                  fields={"vendor": "CEB", "amount": "4250"}, step="approve")
        a2, c2 = await ap.request(jid, "w1", f"{jid}:1:1", "entry", "CEB bill", fields={"vendor": "X"})
        return a1, c1, a2, c2

    a1, c1, a2, c2 = run(go())
    assert c1 and not c2 and a1["id"] == a2["id"] and a2["payload"]["fields"]["vendor"] == "CEB"
    assert a1["payload"]["amount"] == "4,250.00" and "token_hash" not in a1
    rows = store.read_sync(lambda c: c.execute("SELECT * FROM outbox").fetchall())
    assert len(rows) == 1 and rows[0]["dedupe_key"] == f"approval:{a1['id']}"
    msg = json.loads(rows[0]["payload"])
    assert msg["title"] == "demo: CEB bill" and "amount: 4,250.00" in msg["message"]
    labels = [x["label"] for x in msg["actions"]]
    assert labels == ["Approve", "Reject", "Open"] and msg["actions"][0]["method"] == "POST"
    assert "body" not in msg["actions"][0]  # the buttons go straight to Argus with the one-time token
    assert msg["actions"][0]["url"] == (f"http://phone-reachable:8600/approvals/{a1['id']}/decide"
                                        f"?t={ap.token(a1['id'])}&answer=approve")
    assert msg["actions"][2]["url"].startswith("http://phone-reachable:8600/a/")


def test_buttons_are_relative_links_without_public_url(store, clock):
    """The app puts its own Argus address in front of them, so they work wherever the app reaches Argus."""
    jobs, ap, cfg = setup(store, clock, cfg_with(None))

    async def go():
        jid = await _leased(jobs)
        return (await ap.request(jid, "w1", "k0", "entry", "Rent", fields={"amount": "1"}))[0]

    a = run(go())
    msg = json.loads(store.read_sync(lambda c: c.execute("SELECT payload FROM outbox").fetchone()[0]))
    assert [x["label"] for x in msg["actions"]] == ["Approve", "Reject", "Open"]
    assert msg["actions"][0]["url"] == f"/approvals/{a['id']}/decide?t={ap.token(a['id'])}&answer=approve"
    assert msg["click"].startswith("/a/")


def test_the_phone_app_collects_acknowledges_and_expires(store, clock):
    from argus.outbox import add_message, phone_message

    cfg = cfg_with()
    ob = Outbox(store, cfg, clock=clock)  # the app's inbox is the default
    assert ob.inbox is not None and ob.pulled_kinds() == ["phone", "ntfy"]
    store.write_sync(lambda c: add_message(c, clock(), "phone", phone_message(
        "bill", "m", actions=[{"action": "http", "label": "Approve", "url": "/x?t=secret"}]), dedupe_key="a"))
    store.write_sync(lambda c: add_message(c, clock(), "ntfy", phone_message("old row", "m"), dedupe_key="b"))

    async def go():
        assert await ob.send_due() == 0  # nothing is pushed: the app collects
        first = await ob.inbox_fetch("pixel")
        again = await ob.inbox_fetch("pixel")  # not acknowledged: the same messages
        assert [m["title"] for m in first] == [m["title"] for m in again] == ["bill", "old row"]
        assert first[0]["actions"][0]["label"] == "Approve" and ob.health()["phone"] is True
        assert await ob.inbox_ack([first[0]["id"], "nope"]) == 1
        assert await ob.inbox_ack([first[0]["id"]]) == 0  # once only
        left = await ob.inbox_fetch("pixel")
        assert [m["title"] for m in left] == ["old row"]
        clock.advance(49 * 3600)  # nobody collected it in time
        assert await ob.send_due() == 0
        assert await ob.inbox_fetch("pixel") == []
        clock.advance(100)
        assert ob.health()["phone"] is False  # the app has not asked for a while

    run(go())
    rows = {r["dedupe_key"]: r for r in store.read_sync(lambda c: c.execute("SELECT * FROM outbox").fetchall())}
    assert rows["a"]["state"] == "sent" and "actions" not in json.loads(rows["a"]["payload"])  # token scrubbed
    assert rows["b"]["state"] == "skipped" and rows["b"]["last_error"] == "not collected in time"


def test_a_long_poll_hears_a_new_message_at_once(store, clock):
    from argus.outbox import add_message, phone_message

    ob = Outbox(store, cfg_with(), clock=clock)

    async def go():
        import asyncio

        task = asyncio.create_task(ob.inbox_fetch("pixel", wait=5))
        await asyncio.sleep(0.05)
        store.write_sync(lambda c: add_message(c, clock(), "phone", phone_message("hi", "m")))
        ob.poke()
        return await asyncio.wait_for(task, 2)

    assert [m["title"] for m in run(go())] == ["hi"]


def test_batch_totals_are_computed_in_code():
    items = [{"label": "a", "amount": "1,000.10"}, {"label": "b", "amount": 0.2}, {"label": "c", "amount": "3"}]
    assert summarize("batch", {}, items) == {"count": 3, "total": "1,003.30"}  # no float drift
    assert summarize("batch", {}, [{"label": "a", "amount": "n/a"}]) == {"count": 1}  # never a guessed total
    assert summarize("entry", {"amount": "12.5"}, []) == {"amount": "12.50"}


def test_decide_resumes_the_waiting_job_ahead_of_others(store, clock):
    jobs, ap, _ = setup(store, clock)

    async def go():
        jid = await _leased(jobs)
        a, _ = await ap.request(jid, "w1", "k1", "entry", "bill", fields={"vendor": "CEB", "amount": "10"})
        await jobs.wait(jid, "w1", f"approval:{a['id']}")
        assert (await jobs.get(jid)).state is JobState.WAITING
        with pytest.raises(BadToken):
            await ap.decide(a["id"], "approve", token="0" * 40)
        d = await ap.decide(a["id"], "approve", fields={"amount": "12"}, token=ap.token(a["id"]), by="phone")
        job = await jobs.get(jid)
        with pytest.raises(ApprovalClosed):  # one-time: the same button again does nothing
            await ap.decide(a["id"], "reject", token=ap.token(a["id"]))
        return d, job

    d, job = run(go())
    assert d["state"] == "approved" and d["answer"] == {"vendor": "CEB", "amount": "12.00"}
    assert d["decided_by"] == "phone"
    assert job.state is JobState.QUEUED and job.priority == 80 and job.wait_reason is None


def test_decided_before_the_worker_parks_the_job(store, clock):
    """The answer can arrive while the worker is still on its way to /wait: the job goes straight back."""
    jobs, ap, _ = setup(store, clock)

    async def go():
        jid = await _leased(jobs)
        a, _ = await ap.request(jid, "w1", "k1", "entry", "bill", fields={"x": 1})
        await ap.decide(a["id"], "reject")
        return await jobs.wait(jid, "w1", f"approval:{a['id']}")

    assert run(go()).state is JobState.QUEUED


def test_reminder_once_then_expiry_counts_as_no(store, clock):
    cfg = cfg_with()
    cfg.approvals.remind_hours, cfg.approvals.expire_hours = 1, 3
    jobs, ap, _ = setup(store, clock, cfg)

    async def go():
        jid = await _leased(jobs)
        a, _ = await ap.request(jid, "w1", "k1", "entry", "bill", fields={"x": 1})
        await jobs.wait(jid, "w1", f"approval:{a['id']}")
        clock.advance(1800)
        r0 = await ap.tick()
        clock.advance(1900)
        r1 = await ap.tick()
        r2 = await ap.tick()
        clock.advance(3 * 3600)
        r3 = await ap.tick()
        return jid, a, (r0, r1, r2, r3)

    jid, a, ticks = run(go())
    assert [t["reminded"] for t in ticks] == [0, 1, 0, 0] and ticks[3]["expired"] == 1
    got = run(ap.get(a["id"]))
    assert got["state"] == "expired" and got["decided_by"] == "timeout"
    assert run(jobs.get(jid)).state is JobState.QUEUED
    titles = store.read_sync(lambda c: [json.loads(r[0])["title"] for r in c.execute(
        "SELECT payload FROM outbox ORDER BY created_at")])
    assert titles == ["demo: bill", "Reminder: demo: bill"]


def test_only_the_lease_holder_can_ask(store, clock):
    from argus.jobs import LeaseLost

    jobs, ap, _ = setup(store, clock)

    async def go():
        jid = await _leased(jobs)
        await ap.request(jid, "w2", "k", "entry", "t")

    with pytest.raises(LeaseLost):
        run(go())


# ------------------------------------------------------------------ outbox


def test_outbox_sends_once_retries_with_backoff_and_scrubs_tokens(store, clock):
    jobs, ap, cfg = setup(store, clock)
    sender = Sender(fail=1)
    ob = Outbox(store, cfg, clock=clock, senders={"phone": sender})

    async def go():
        jid = await _leased(jobs)
        await ap.request(jid, "w1", "k", "entry", "bill", fields={"x": 1})
        assert await ob.send_due() == 1  # fails -> retry in 5 s
        assert await ob.send_due() == 0
        clock.advance(6)
        assert await ob.send_due() == 1
        assert await ob.send_due() == 0

    run(go())
    assert len(sender.sent) == 1 and sender.sent[0]["actions"][0]["label"] == "Approve"
    row = store.read_sync(lambda c: c.execute("SELECT * FROM outbox").fetchone())
    assert row["state"] == "sent" and row["attempts"] == 2 and "actions" not in json.loads(row["payload"])
    kinds = store.read_sync(lambda c: [r[0] for r in c.execute("SELECT kind FROM events WHERE kind LIKE 'outbox.%'")])
    assert kinds == ["outbox.retry", "outbox.sent"]


def test_outbox_gives_up_after_max_attempts(store, clock):
    cfg = cfg_with()
    cfg.notify.max_attempts = 2
    from argus.outbox import add_message, phone_message

    store.write_sync(lambda c: add_message(c, clock(), "phone", phone_message("t", "m")))
    ob = Outbox(store, cfg, clock=clock, senders={"phone": Sender(fail=5)})
    run(ob.send_due())
    clock.advance(10)
    run(ob.send_due())
    row = store.read_sync(lambda c: c.execute("SELECT state, last_error FROM outbox").fetchone())
    assert row["state"] == "failed" and "sender down" in row["last_error"]


def test_crash_while_sending_resends_on_restart_but_never_twice_after_sent(store, clock):
    from argus.outbox import add_message, phone_message

    store.write_sync(lambda c: add_message(c, clock(), "phone", phone_message("a", "m"), dedupe_key="a"))
    store.write_sync(lambda c: add_message(c, clock(), "phone", phone_message("b", "m"), dedupe_key="b"))
    # "b" was being sent when argusd died; "a" had been sent
    store.write_sync(lambda c: c.execute("UPDATE outbox SET state = CASE dedupe_key WHEN 'a' THEN 'sent'"
                                         " ELSE 'sending' END"))
    assert store.write_sync(lambda c: add_message(c, clock(), "phone", phone_message("a", "m"),
                                                  dedupe_key="a")) is None  # queued again: no-op
    sender = Sender()
    ob = Outbox(store, cfg_with(), clock=clock, senders={"phone": sender})

    async def go():
        await ob.start()
        await ob.stop()
        return await ob.send_due()

    run(go())
    assert [m["title"] for m in sender.sent] == ["b"]


# ------------------------------------------------------------------ end to end: worker, phone app


def make_argus(tmp_path, token: str = "tok", extra: str = "") -> Argus:
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\n"
        "jobs:\n  watchdog_interval_seconds: 0.1\n  lease_seconds: 5\n  heartbeat_seconds: 1\n" + extra,
        encoding="utf-8")
    (tmp_path / ".env").write_text(f"ARGUS_WORKER_TOKEN={token}\n", encoding="utf-8")
    return Argus(load_config(tmp_path / "argus.yaml"))


def test_approve_from_the_phone_resumes_the_job_in_under_a_second(tmp_path):
    """The app collects the notification, a tap on Approve goes to Argus, the job finishes."""
    argus = make_argus(tmp_path).open()
    with Server(argus) as srv:
        cl = client(srv.url, "tok")
        app = FakePhoneApp(srv.url)
        w = Worker(cl, "w-phone")
        w.register()
        jid = cl.post("/jobs", {"plugin": "demo", "workflow": "approval",
                                "input": {"vendor": "CEB", "amount": "4250"}})["id"]
        assert w.run_once(wait=2)
        job = cl.get(f"/jobs/{jid}")
        assert job["state"] == "waiting" and job["wait_reason"].startswith("approval:")
        msg = app.wait_messages(1)[0]
        assert msg["title"] == "demo: CEB bill" and msg["priority"] == 4

        # the Open button's page works without the Argus token and shows the bill
        page = urllib.request.urlopen(srv.url + msg["actions"][2]["url"])
        html = page.read().decode()
        assert "CEB bill" in html and "4,250.00" in html and "no-store" in page.headers["Cache-Control"]

        t0 = time.monotonic()
        assert app.tap(msg["actions"][0])[0] == 200  # Approve: straight to Argus, no Argus token
        assert w.run_once(wait=2)  # the resumed job is claimed and finished
        elapsed = time.monotonic() - t0
        job = cl.get(f"/jobs/{jid}")
        assert job["state"] == "succeeded" and job["result"]["filed"] is True
        assert job["result"]["amount"] == "4,250.00"
        limit = 6.0 if os.environ.get("CI") else 1.0  # shared CI runners are slow and noisy
        assert elapsed < limit, f"approve -> job done took {elapsed:.2f} s"

        # the parse step ran once; the approve step ran twice (asked, then answered); one approval only
        assert [s["name"] for s in job["steps"]] == ["parse", "approve", "file"]
        assert len(cl.get(f"/approvals?job={jid}")) == 1
        # the plugin's ordinary "Filed" message waits for the evening summary (quiet by default)
        held = run(argus.store.read(lambda c: [r[0] for r in c.execute("SELECT title FROM held_notes")]))
        assert held == ["Filed: CEB bill"]

        # tapping Reject afterwards changes nothing; a wrong token is refused
        assert app.tap(msg["actions"][1])[0] == 409
        assert cl.get(f"/approvals?job={jid}")[0]["state"] == "approved"
        aid = cl.get(f"/approvals?job={jid}")[0]["id"]
        wrong = {"url": f"/approvals/{aid}/decide?t={'0' * 40}&answer=reject"}
        assert app.tap(wrong)[0] in (403, 409)
        assert cl.post("/outbox/test")["queued"]
        assert app.wait_messages(2)[1]["title"] == "Argus test"
        box = wait_for(lambda: (b := cl.get("/outbox"))["counts"].get("sent") == 2 and b)
        assert box["phone"] is True and box["phone_device"] == "test-phone"
        evs = [e["kind"] for e in cl.get(f"/jobs/{jid}/events")]
        assert "approval.requested" in evs and "approval.approved" in evs and "outbox.sent" in evs
        # without the Argus token the inbox is closed
        assert FakePhoneApp(srv.url, "wrong")._call("GET", "/phone/inbox")[0] == 401


def test_reject_in_helios_and_edits(tmp_path):
    argus = make_argus(tmp_path).open()
    with Server(argus) as srv:
        cl = client(srv.url, "tok")
        w = Worker(cl, "w")
        w.register()
        j1 = cl.post("/jobs", {"plugin": "demo", "workflow": "approval", "input": {"amount": "10"}})["id"]
        j2 = cl.post("/jobs", {"plugin": "demo", "workflow": "approval", "input": {"amount": "20"}})["id"]
        assert w.run_once(wait=2) and w.run_once(wait=2)
        pending = cl.get("/approvals?state=pending")
        assert len(pending) == 2
        by_job = {a["job_id"]: a["id"] for a in pending}
        # without the Argus token and without ?t= nothing can be decided
        anon = client(srv.url)
        with pytest.raises(Exception, match="401"):
            anon.post(f"/approvals/{by_job[j1]}/decide", {"answer": "approve"})
        cl.post(f"/approvals/{by_job[j1]}/decide", {"answer": "reject"})
        cl.post(f"/approvals/{by_job[j2]}/decide", {"answer": "approve", "fields": {"amount": "25.00"}})
        with pytest.raises(Exception, match="422"):
            cl.post(f"/approvals/{by_job[j2]}/decide", {"answer": "maybe"})
        assert w.run_once(wait=2) and w.run_once(wait=2)
        assert cl.get(f"/jobs/{j1}")["result"] == {"filed": False, "because": "rejected"}
        assert cl.get(f"/jobs/{j2}")["result"]["amount"] == "25.00"


def test_worker_crash_after_asking_sends_no_second_notification(tmp_path):
    """The worker dies between creating the approval and parking the job. The lease expires, another worker
    runs the step again, gets the same approval back and parks the job: still one notification."""
    reg = WorkflowRegistry()

    @workflow("bills", "pay", registry=reg)
    def pay(ctx):
        return ctx.step("ask", lambda: ctx.approve("entry", "rent", {"amount": "100"}).state)

    argus = make_argus(tmp_path).open()
    with Server(argus) as srv:
        cl = client(srv.url, "tok")
        app = FakePhoneApp(srv.url)
        jid = cl.post("/jobs", {"plugin": "bills", "workflow": "pay"})["id"]
        # worker 1 claims, starts and asks, then "dies" (never calls /wait, no more heartbeats)
        job = cl.claim("w-dead", [], ["bills"], 2)
        cl.start(jid, "w-dead")
        key = f"{jid}:0:1"
        a = cl.approval(jid, "w-dead", {"key": key, "type": "entry", "title": "rent", "fields": {"amount": "100"}})
        assert job and a["state"] == "pending"
        wait_for(lambda: cl.get(f"/jobs/{jid}")["state"] == "queued", timeout=10)
        w2 = Worker(cl, "w2", registry=reg)
        w2.register()
        assert w2.run_once(wait=2)
        assert cl.get(f"/jobs/{jid}")["state"] == "waiting"
        time.sleep(0.5)
        app.wait_messages(1)
        app.pull()
        assert len(app.messages) == 1 and len(cl.get(f"/approvals?job={jid}")) == 1


def test_dead_job_notifies_and_the_message_waits_for_a_phone_that_is_off(tmp_path):
    """The phone is not asking (off, away from Tailscale): the message stays queued, through an argusd restart,
    and the app gets it, once, when it asks."""
    argus = make_argus(tmp_path).open()
    with Server(argus) as srv:
        cl = client(srv.url, "tok")
        jid = cl.post("/jobs", {"plugin": "demo", "workflow": "fail", "input": {"permanent": True}})["id"]
        assert Worker(cl, "w").run_once(wait=2)
        assert cl.get(f"/jobs/{jid}")["state"] == "dead"
        wait_for(lambda: cl.get("/outbox")["counts"].get("pending"))
        time.sleep(0.3)
        assert cl.get("/outbox")["counts"].get("pending") == 1  # nothing was pushed, nothing lost
    argus2 = Argus(load_config(tmp_path / "argus.yaml")).open()  # argusd restarts; the phone is back
    with Server(argus2) as srv2:
        app = FakePhoneApp(srv2.url)
        assert [m["title"] for m in app.wait_messages(1)] == ["Job failed: demo.fail"]
        app.pull()
        assert len(app.messages) == 1
        assert client(srv2.url, "tok").get("/health")["outbox"]["alive"] is True


def test_page_escapes_and_rejects_bad_links(tmp_path):
    argus = make_argus(tmp_path).open()
    with Server(argus) as srv:
        cl = client(srv.url, "tok")
        w = Worker(cl, "w")
        w.register()
        cl.post("/jobs", {"plugin": "demo", "workflow": "approval",
                          "input": {"vendor": "<script>alert(1)</script>"}})
        assert w.run_once(wait=2)
        a = cl.get("/approvals?state=pending")[0]
        good = argus.approvals.token(a["id"])
        html = urllib.request.urlopen(f"{srv.url}/a/{a['id']}?t={good}").read().decode()
        assert "<script>alert(1)" not in html and "&lt;script&gt;" in html
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"{srv.url}/a/{a['id']}?t=nope")
        assert e.value.code == 404


# ------------------------------------------------------------------ phone presence (Tailscale)


def _status(online: bool) -> dict:
    return {"Self": {"HostName": "saspc"}, "Peer": {
        "k1": {"HostName": "laptop", "DNSName": "laptop.tail1234.ts.net.", "Online": True},
        "k2": {"HostName": "Pixel-8", "DNSName": "pixel-8.tail1234.ts.net.", "Online": online}}}


def test_phone_online_matches_host_or_dns_name():
    from argus.presence import phone_online

    assert phone_online(_status(True), "pixel-8") is True
    assert phone_online(_status(False), "pixel-8.tail1234.ts.net") is False
    assert phone_online(_status(True), "iphone") is None


def test_phone_back_online_pushes_what_is_waiting_once(store, clock):
    from argus.presence import PhoneWatch

    cfg = cfg_with()
    cfg.approvals.phone, cfg.approvals.back_online_cooldown_minutes = "pixel-8", 15
    jobs, ap, _ = setup(store, clock, cfg)
    seq = iter([False, True, True, False, True, False, True])
    sender = Sender()
    ob = Outbox(store, cfg, clock=clock, senders={"phone": sender})
    watch = PhoneWatch(store, cfg, ap, ob, clock=clock, status=lambda: _status(next(seq)))

    async def go():
        jid = await _leased(jobs)
        await ap.request(jid, "w1", "k1", "entry", "CEB bill", fields={"amount": "10"})
        out = [await watch.check()]          # offline (first look)
        out.append(await watch.check())      # online: one waiting -> pushed
        out.append(await watch.check())      # still online: nothing
        clock.advance(60)
        out.append(await watch.check())      # offline
        out.append(await watch.check())      # online again within 15 min: no second push
        clock.advance(20 * 60)
        jid2 = await _leased(jobs)
        await ap.request(jid2, "w1", "k2", "batch", "3 payments", items=[{"label": "a", "amount": "1"}])
        out.append(await watch.check())      # offline
        out.append(await watch.check())      # online after the cooldown: two waiting -> one summary
        await ob.send_due()
        return out

    assert run(go()) == ["offline", "back", "online", "offline", "online", "offline", "back"]
    titles = [m["title"] for m in sender.sent]
    assert titles == ["demo: CEB bill", "Still waiting: demo: CEB bill", "demo: 3 payments",
                      "2 approvals waiting"]
    still = sender.sent[1]
    assert [a["label"] for a in still["actions"]] == ["Approve", "Reject", "Open"]  # fresh buttons
    summary = sender.sent[3]
    assert "CEB bill" in summary["message"] and summary["actions"][0]["label"] == "Open Helios"
    kinds = store.read_sync(lambda c: [r[0] for r in c.execute("SELECT kind FROM events WHERE kind LIKE 'phone.%'"
                                                               " OR kind = 'approval.pushed'")])
    assert kinds.count("approval.pushed") == 2 and kinds.count("phone.online") == 3
    meta = json.loads(store.read_sync(lambda c: c.execute("SELECT meta FROM components WHERE id = 'phone'"
                                                          ).fetchone()[0]))
    assert meta["online"] is True and meta["device"] == "pixel-8" and meta["since"] == clock()


def test_phone_watch_is_off_without_a_phone(store, clock):
    from argus.presence import PhoneWatch

    cfg = cfg_with()
    jobs, ap, _ = setup(store, clock, cfg)
    assert not PhoneWatch(store, cfg, ap, None).enabled  # no phone set
    cfg.approvals.phone = "pixel-8"
    assert PhoneWatch(store, cfg, ap, None).enabled


# ------------------------------------------------------------------ review fixes


def test_edits_keep_types_and_amounts_are_checked(store, clock):
    from argus.approvals import ApprovalError

    jobs, ap, _ = setup(store, clock)

    async def go():
        jid = await _leased(jobs)
        a, _ = await ap.request(jid, "w1", "k", "entry", "bill",
                                fields={"amount": "4,250.00", "units": 3, "rate": 1.5, "paid": False, "note": "x"})
        with pytest.raises(ApprovalError, match="not a valid amount"):
            await ap.decide(a["id"], "approve", fields={"amount": "4,25O"})
        with pytest.raises(ApprovalError, match="whole number"):
            await ap.decide(a["id"], "approve", fields={"units": "three"})
        return await ap.decide(a["id"], "approve",
                               fields={"amount": "4300", "units": "4", "rate": "2.25", "paid": "yes", "note": "ok"})

    d = run(go())
    assert d["answer"] == {"amount": "4,300.00", "units": 4, "rate": 2.25, "paid": True, "note": "ok"}


def test_late_answer_counts_as_expired_and_sticks(store, clock):
    cfg = cfg_with()
    cfg.approvals.expire_hours = 1
    jobs, ap, _ = setup(store, clock, cfg)

    async def go():
        jid = await _leased(jobs)
        a, _ = await ap.request(jid, "w1", "k", "entry", "bill", fields={"x": 1})
        await jobs.wait(jid, "w1", f"approval:{a['id']}")
        clock.advance(2 * 3600)
        d = await ap.decide(a["id"], "approve", token=ap.token(a["id"]))
        return jid, d, await ap.get(a["id"])

    jid, d, stored = run(go())
    assert d["state"] == "expired" and stored["state"] == "expired"  # not rolled back
    assert run(jobs.get(jid)).state is JobState.QUEUED


def test_cancelled_job_closes_its_approvals_and_bad_links_are_refused(store, clock):
    from argus.approvals import ApprovalError

    jobs, ap, _ = setup(store, clock)

    async def go():
        jid = await _leased(jobs)
        with pytest.raises(ApprovalError, match="http"):
            await ap.request(jid, "w1", "k0", "draft", "doc", link="javascript:alert(1)")
        a, _ = await ap.request(jid, "w1", "k", "entry", "bill", fields={"x": 1})
        await jobs.wait(jid, "w1", f"approval:{a['id']}")
        await jobs.cancel(jid)
        return await ap.get(a["id"]), await ap.push_waiting("d")

    a, pushed = run(go())
    assert a["state"] == "expired" and a["decided_by"] == "job ended" and pushed == 0


def test_approvals_do_not_use_up_attempts(store, clock):
    jobs, ap, _ = setup(store, clock)

    async def go():
        jid, _ = await jobs.enqueue("demo", "approval", {}, max_attempts=2)
        for i in range(3):  # three approvals in a row
            await jobs.claim("w1", [])
            await jobs.start(jid, "w1")
            a, _ = await ap.request(jid, "w1", f"k{i}", "entry", "t", fields={"x": 1})
            await jobs.wait(jid, "w1", f"approval:{a['id']}")
            await ap.decide(a["id"], "approve")
        await jobs.claim("w1", [])
        return await jobs.fail(jid, "w1", "network blip")

    job = run(go())
    assert job.state is JobState.RETRY and job.attempt == 1
