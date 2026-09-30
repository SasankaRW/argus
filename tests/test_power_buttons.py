"""Power buttons: Wake-on-LAN from argusd; sleep / shut down / restart / cancel run on the PC's worker."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

import argus.worker.power as wpower
from argus.config import load_config
from argus.context import Argus
from argus.worker import Worker
from argus.worker.client import ApiError
from test_worker import Server, client, wait_for


def make(tmp_path: Path, extra: str = "") -> Argus:
    (tmp_path / "argus.yaml").write_text("logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.1\n" + extra,
                                         encoding="utf-8")
    return Argus(load_config(tmp_path / "argus.yaml"))


def test_mac_is_checked(tmp_path):
    a = make(tmp_path, "power:\n  pc_mac: 04-7c-16-ab-cd-ef\n")
    assert a.cfg.power.pc_mac == "04:7C:16:AB:CD:EF"
    with pytest.raises(Exception, match="MAC"):
        make(tmp_path, "power:\n  pc_mac: nope\n")


def test_wake_sends_the_magic_packet(tmp_path):
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(3)
    port = rx.getsockname()[1]
    a = make(tmp_path, f"power:\n  pc_mac: 04:7C:16:AB:CD:EF\n  wol_broadcast: 127.0.0.1\n  wol_port: {port}\n")
    with Server(a.open()) as srv:
        cl = client(srv.url)
        assert cl.post("/power/wake") == {"sent": True, "mac": "04:7C:16:AB:CD:EF"}
        assert cl.get("/power")["wol"] is True
    data = rx.recv(200)
    rx.close()
    assert data == b"\xff" * 6 + bytes.fromhex("047C16ABCDEF") * 16


def test_wake_needs_the_mac(tmp_path):
    with Server(make(tmp_path).open()) as srv:
        with pytest.raises(ApiError) as e:
            client(srv.url).post("/power/wake")
    assert e.value.status == 422 and "pc_mac" in str(e.value)


def test_shutdown_runs_on_the_pc_and_cancel_drops_what_has_not_started(tmp_path, monkeypatch):
    ran: list[list[str]] = []
    monkeypatch.setattr(wpower, "_run", lambda cmd: ran.append(cmd) or "")
    monkeypatch.setattr(wpower, "WIN", True)
    a = make(tmp_path, "power:\n  shutdown_delay_seconds: 90\n")
    with Server(a.open()) as srv:
        cl = client(srv.url)
        laptop = Worker(cl, "laptop", capabilities=["laptop"], watch_folders=False)
        pc = Worker(cl, "pc", capabilities=["desktop", "gpu"], watch_folders=False)
        laptop.register()
        pc.register()
        job = cl.post("/power/shutdown")
        assert cl.post("/power/shutdown")["id"] == job["id"]  # pressed twice: one job
        assert not laptop.run_once(wait=0.5)  # only the PC runs power jobs
        assert pc.run_once(wait=2)
        done = wait_for(lambda: (j := cl.get(f"/jobs/{job['id']}"))["state"] in ("succeeded", "dead") and j)
        assert done["state"] == "succeeded" and done["priority"] == 100
        assert ran[-1][:4] == ["shutdown", "/s", "/t", "90"]
        # a sleep that has not started yet is dropped by cancel, and cancel runs shutdown /a
        sleep = cl.post("/power/sleep")
        c = cl.post("/power/cancel")
        assert c["dropped"] == [sleep["id"]] and cl.get(f"/jobs/{sleep['id']}")["state"] == "cancelled"
        assert pc.run_once(wait=2)
        wait_for(lambda: cl.get(f"/jobs/{c['id']}")["state"] == "succeeded")
        assert ran[-1] == ["shutdown", "/a"]
        st = cl.get("/power")
        assert st["pc_online"] is True and st["pc"]["id"] == "pc" and len(st["recent"]) == 3


# ------------------------------------------------------------------ automatic power (real mode, on the laptop)

from argus.config import Config, PowerConfig  # noqa: E402
from argus.jobs import JobStore  # noqa: E402
from argus.power import PowerManager  # noqa: E402
from argus.registry import Registry  # noqa: E402
from conftest import run  # noqa: E402


def real_power(store, clock, **kw):
    cfg = Config(power=PowerConfig(mode="real", pc_mac="04:7C:16:AB:CD:EF", idle_minutes=20, warn_minutes=5, **kw))
    jobs = JobStore(store, cfg.jobs, clock=clock)
    pm = PowerManager(store, cfg, clock=clock)
    pm.jobs = jobs
    woke: list[float] = []
    pm.wake = lambda: woke.append(clock()) or {"sent": True}
    run(pm.start())
    return pm, jobs, Registry(store, clock=clock), woke


def kinds(store, prefix):
    return run(store.read(lambda c: [r[0] for r in c.execute(
        "SELECT kind FROM events WHERE kind LIKE ? ORDER BY rowid", (prefix + "%",))]))


def test_real_wake_warn_then_shutdown_only_a_pc_argus_woke(store, clock):
    pm, jobs, reg, woke = real_power(store, clock)
    job, _ = run(jobs.enqueue("vision", "name", needs=["gpu"]))
    assert run(pm.tick()) == "busy" and len(woke) == 1  # GPU work and no PC: wake it
    clock.advance(120)
    run(reg.register_worker("pc", "SasPC", ["desktop", "gpu"]))
    run(pm.tick())
    assert pm.by_argus is True
    run(jobs.cancel(job))
    assert run(pm.tick()) == "idle"
    clock.advance(20 * 60 + 1)
    assert run(pm.tick()) == "warned"
    msgs = run(store.read(lambda c: [r[0] for r in c.execute("SELECT payload FROM outbox")]))
    assert any("PC shuts down at" in m for m in msgs)
    clock.advance(4 * 60)
    assert run(pm.tick()) == "warned"
    clock.advance(61)
    assert run(pm.tick()) == "shutting_down"
    q = run(jobs.list_jobs(None, 5, "power"))
    assert [j.workflow for j in q] == ["auto_shutdown"] and q[0].priority == 100 and q[0].needs == ("desktop",)
    assert kinds(store, "power.")[:3] == ["power.wake_sent", "power.pc_online", "power.shutdown_warned"]


def test_real_never_shuts_down_a_pc_you_switched_on(store, clock):
    pm, jobs, reg, woke = real_power(store, clock)
    run(reg.register_worker("pc", "SasPC", ["desktop", "gpu"]))  # on by hand: no wake before
    run(pm.tick())
    clock.advance(3 * 3600)
    assert run(pm.tick()) == "idle" and pm.by_argus is False and not woke


def test_cancel_holds_for_this_idle_stretch(store, clock):
    pm, jobs, reg, woke = real_power(store, clock, shutdown_manual_sessions=True)
    run(reg.register_worker("pc", "SasPC", ["desktop"]))
    run(pm.tick())
    clock.advance(21 * 60)
    assert run(pm.tick()) == "warned"
    run(pm.hold())
    clock.advance(3600)
    assert run(pm.tick()) == "held" and not run(jobs.list_jobs(None, 5, "power"))
    job, _ = run(jobs.enqueue("dorg", "sort", needs=["desktop"]))  # work again: a new stretch
    assert run(pm.tick()) == "busy" and pm.held is False


def test_auto_shutdown_skips_when_someone_uses_the_pc(monkeypatch):
    from argus.worker.workflows import Context

    class R:
        lease_lost = False

        def step(self, *a, **k):
            pass

    ran = []
    monkeypatch.setattr(wpower, "_run", lambda cmd: ran.append(cmd) or "")
    monkeypatch.setattr(wpower, "WIN", True)
    monkeypatch.setattr(wpower, "input_idle_seconds", lambda: 90.0)
    ctx = Context({"id": "j1", "input": {"idle_minutes": 20, "delay": 60}}, R())
    assert "someone used the PC" in wpower.auto_shutdown(ctx)["skipped"] and not ran
    monkeypatch.setattr(wpower, "input_idle_seconds", lambda: 3000.0)
    ctx = Context({"id": "j2", "input": {"idle_minutes": 20, "delay": 60}}, R())
    assert wpower.auto_shutdown(ctx)["pc"] == "shutting down in 60 s" and ran[-1][:2] == ["shutdown", "/s"]
