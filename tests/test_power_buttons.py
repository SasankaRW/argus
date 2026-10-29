"""Power buttons: Wake-on-LAN from argusd; sleep / shut down / restart / cancel run on the PC's worker."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

import argus.worker.power as wpower
from argus import laptop_power, power
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
        assert cl.post("/power/wake") == {"sent": True, "mac": "04:7C:16:AB:CD:EF", "to": ["127.0.0.1"]}
        st = cl.get("/power")
        assert st["wol"] is True and st["wake_check"]["result"] == "waiting" and st["wake_check"]["to"] == ["127.0.0.1"]
    got = [rx.recv(200) for _ in range(power.WAKE_COPIES)]  # sent a few times: UDP may drop one
    rx.close()
    assert got == [b"\xff" * 6 + bytes.fromhex("047C16ABCDEF") * 16] * power.WAKE_COPIES


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


def card(name, local, prefix, up=True, **extra):
    return {"ifname": name, "flags": ["BROADCAST", "UP"] if up else ["BROADCAST"],
            "addr_info": [{"family": "inet", "local": local, "prefixlen": prefix, **extra}]}


IP_JSON = [card("lo", "127.0.0.1", 8), card("wlan0", "192.168.1.20", 24, broadcast="192.168.1.255"),
           card("enp3s0", "10.0.0.5", 16), card("tailscale0", "100.101.1.2", 32), card("docker0", "172.17.0.1", 16),
           card("eth1", "192.168.5.2", 24, up=False)]


def test_wake_goes_out_of_the_home_network_cards_only():
    # not Tailscale, Docker, loopback or a card that is down; the broadcast is the network's own
    assert power.broadcasts_from(IP_JSON) == [("192.168.1.20", "192.168.1.255"), ("10.0.0.5", "10.0.255.255")]


def test_wake_check_says_whether_the_pc_came_online(tmp_path):
    import asyncio

    a = make(tmp_path, "power:\n  pc_mac: 04:7C:16:AB:CD:EF\n  wol_broadcast: 127.0.0.1\n  wol_port: 9\n")
    a.open()
    t = [1000.0]
    a.power.clock = lambda: t[0]
    a.power.wake()
    assert a.power.status()["wake_check"]["result"] == "waiting"
    t[0] += power.WAKE_ANSWER_S + 1
    asyncio.run(a.power.tick())
    assert a.power.status()["wake_check"]["result"] == "no_answer"


def test_the_default_broadcast_also_uses_each_home_card(tmp_path, monkeypatch):
    sent: list[tuple] = []

    class Sock:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def setsockopt(self, *a):
            pass

        def bind(self, addr):
            sent.append(("bind", addr[0]))

        def sendto(self, data, addr):
            sent.append(("to", addr[0]))

    monkeypatch.setattr(power.socket, "socket", Sock)
    a = make(tmp_path, "power:\n  pc_mac: 04:7C:16:AB:CD:EF\n")
    a.open()
    a.power.find_lans = lambda: [("192.168.1.20", "192.168.1.255")]
    out = a.power.wake()
    assert out["to"] == ["255.255.255.255", "192.168.1.255 (from 192.168.1.20)"]
    assert ("bind", "192.168.1.20") in sent and sent.count(("to", "192.168.1.255")) == power.WAKE_COPIES


# ---- the laptop's own buttons (Argus's server)

class Done:
    def __init__(self, code=0, err=""):
        self.returncode, self.stderr, self.stdout = code, err, ""


def test_laptop_reboot_is_scheduled_and_shutdown_needs_confirm(monkeypatch):
    monkeypatch.setattr(laptop_power, "available", lambda: True)
    ran: list[list[str]] = []
    out = laptop_power.run("reboot", runner=lambda cmd, **k: ran.append(cmd) or Done())
    assert out["ok"] and ran[-1][:3] == ["/usr/bin/shutdown", "-r", "+1"]
    with pytest.raises(laptop_power.LaptopPowerError, match="confirm"):
        laptop_power.run("shutdown", runner=lambda cmd, **k: Done())
    laptop_power.run("shutdown", confirm=True, runner=lambda cmd, **k: ran.append(cmd) or Done())
    assert ran[-1][:3] == ["/usr/bin/shutdown", "-h", "+1"]
    laptop_power.run("cancel", runner=lambda cmd, **k: ran.append(cmd) or Done())
    assert ran[-1] == ["/usr/bin/shutdown", "-c"]


def test_laptop_without_the_polkit_rule_says_how_to_allow_it(monkeypatch):
    monkeypatch.setattr(laptop_power, "available", lambda: True)
    with pytest.raises(laptop_power.LaptopPowerError, match="power-setup.sh"):
        laptop_power.run("reboot", runner=lambda cmd, **k: Done(1, "Access denied"))


def test_restart_argus_runs_a_moment_later(monkeypatch):
    monkeypatch.setattr(laptop_power, "available", lambda: True)
    spawned: list = []
    out = laptop_power.run("restart-argus", spawn=lambda cmd, **k: spawned.append(cmd))
    assert out["ok"] and "systemctl restart argusd argus-worker" in spawned[0][-1] and "sleep" in spawned[0][-1]


def test_laptop_buttons_only_on_linux(monkeypatch):
    monkeypatch.setattr(laptop_power, "available", lambda: False)
    with pytest.raises(laptop_power.LaptopPowerError, match="Linux"):
        laptop_power.run("reboot", runner=lambda cmd, **k: Done())


def test_a_scheduled_restart_is_read_from_logind(tmp_path):
    f = tmp_path / "scheduled"
    f.write_text("USEC=2000000000000000\nWARN_WALL=1\nMODE=reboot\n", encoding="utf-8")
    assert laptop_power.scheduled(f, clock=lambda: 1999999000) == {"mode": "reboot", "at": 2000000000.0}
    assert laptop_power.scheduled(f, clock=lambda: 2000000001) is None
    assert laptop_power.scheduled(tmp_path / "none") is None


def test_laptop_endpoint_and_island_actions(tmp_path, monkeypatch):
    ran: list[str] = []
    monkeypatch.setattr(laptop_power, "run", lambda action, confirm=False: ran.append(action) or {"ok": True})
    with Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        assert cl.post("/power/laptop/reboot") == {"ok": True}
        assert "laptop" in cl.get("/power")
        assert cl.post("/ask/do", {"action": "laptop:restart-argus"})["power"] == {"ok": True}
        with pytest.raises(ApiError):  # shutting the laptop down: Helios only, where it asks twice
            cl.post("/ask/do", {"action": "laptop:shutdown"})
    assert ran == ["reboot", "restart-argus"]


def test_wake_also_goes_straight_to_the_pc(tmp_path):
    """A laptop on Wi-Fi: the router may not pass the broadcast, so the packet also goes to the PC's own address,
    and the wake says whether the laptop keeps the PC's card known (pc-wake-setup.sh) while the PC is off."""
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(3)
    port = rx.getsockname()[1]
    a = make(tmp_path, f"power:\n  pc_mac: 04:7C:16:AB:CD:EF\n  pc_ip: 127.0.0.1\n  wol_broadcast: 127.0.0.2\n"
                       f"  wol_port: {port}\n")
    assert a.cfg.power.pc_ip == "127.0.0.1"
    a.power.neighbour = lambda ip: None
    with Server(a.open()) as srv:
        cl = client(srv.url)
        r = cl.post("/power/wake")
        assert r["to"] == ["127.0.0.2", "127.0.0.1"] and r["direct"].startswith("not kept")
        a.power.neighbour = lambda ip: {"mac": "04:7C:16:AB:CD:EF", "state": "PERMANENT", "dev": "wlan0"}
        assert cl.post("/power/wake")["direct"] == "kept"
        assert cl.get("/power")["wake_check"]["direct"] == "kept"
    got = [rx.recv(200) for _ in range(power.WAKE_COPIES * 2)]  # two wakes, straight to the PC each time
    rx.close()
    assert set(got) == {b"\xff" * 6 + bytes.fromhex("047C16ABCDEF") * 16}
    with pytest.raises(Exception, match="pc_ip"):
        make(tmp_path, "power:\n  pc_ip: 192.168.8\n")
