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
