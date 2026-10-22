"""Which processes the supervisor runs: on the PC now, and on the PC and laptop after the move."""

from __future__ import annotations

import argparse

import pytest

from argus import supervisor


@pytest.fixture(autouse=True)
def _eyes_off(monkeypatch):
    """The robot eyes follow argus.yaml on this PC: off here, so the plans below don't depend on it
    (tests/test_eyes.py checks them on and off)."""
    monkeypatch.setattr(supervisor, "eyes_on", lambda: False)
    monkeypatch.setattr(supervisor, "workstation_where", lambda: "desktop")


def names(monkeypatch, listen=True, popup=True, voice=False, **flags):
    monkeypatch.setattr(supervisor, "ari_on", lambda what: {"listen": listen, "popup": popup}[what])
    monkeypatch.setattr(supervisor, "expressive_on", lambda: voice)
    args = argparse.Namespace(no_worker=False, no_session=False, no_argusd=False, desk_only=False)
    for k, v in flags.items():
        setattr(args, k, v)
    return [n for n, _ in supervisor.plan(args)]


def test_the_pc_today_runs_everything(monkeypatch):
    assert names(monkeypatch) == ["argusd", "ari-listen", "ari-popup", "worker"]
    assert names(monkeypatch, listen=False, popup=False) == ["argusd", "worker"]


@pytest.mark.parametrize("flags, want", [
    ({"no_worker": True}, ["argusd"]),                                       # the laptop
    ({"no_argusd": True, "no_session": True}, ["worker"]),                   # the PC at boot, after the move
    ({"desk_only": True}, ["desktop", "ari-listen", "ari-popup"]),           # the PC at logon, after the move
])
def test_after_the_move(monkeypatch, flags, want):
    assert names(monkeypatch, **flags) == want


def test_the_desk_worker_has_its_own_id_and_only_the_session(monkeypatch):
    monkeypatch.setattr(supervisor.socket, "gethostname", lambda: "SAS-PC")
    monkeypatch.setattr(supervisor, "ari_on", lambda what: False)
    monkeypatch.setattr(supervisor, "expressive_on", lambda: False)
    args = argparse.Namespace(no_worker=False, no_session=False, no_argusd=False, desk_only=True)
    (name, argv), = supervisor.plan(args)
    assert argv[argv.index("--id") + 1] == "desktop-sas-pc"
    caps = [argv[i + 1] for i, a in enumerate(argv) if a == "--cap"]
    # the PC tools need desktop + session; the Workstation (a virtual desktop here); the GPU stays with the boot one
    assert sorted(caps) == ["desktop", "session", "workstation"]


def test_a_second_supervisor_in_the_same_mode_stops(monkeypatch):
    held = supervisor.only_one(0)  # port 0: any free port, to show the lock works
    port = held.getsockname()[1]
    try:
        assert supervisor.only_one(port) is None
    finally:
        held.close()
    assert supervisor.LOCK_PORTS["main"] != supervisor.LOCK_PORTS["desk"]


def test_the_expressive_voice_runs_in_its_own_python(monkeypatch):
    from argus.config import AriConfig

    monkeypatch.setattr(supervisor, "load_ari", lambda: AriConfig())
    assert names(monkeypatch, voice=True) == ["argusd", "ari-listen", "ari-voice", "ari-popup", "worker"]
    args = argparse.Namespace(no_worker=False, no_session=False, no_argusd=False, desk_only=False)
    monkeypatch.setattr(supervisor, "expressive_on", lambda: True)
    argv = dict(supervisor.plan(args))["ari-voice"]
    assert argv[0] == "exe:.venv-voice/Scripts/python.exe" and argv[1] == "core/argus/voice_server.py"
    assert argv[argv.index("--port") + 1] == "8611"
    assert "--clip" not in argv  # no ari.voice_clip: the server's own data/voices/ari-clip.wav
    monkeypatch.setattr(supervisor, "load_ari", lambda: AriConfig(voice_clip="data/voices/me.wav"))
    argv = dict(supervisor.plan(args))["ari-voice"]
    assert argv[argv.index("--clip") + 1] == "data/voices/me.wav"


def test_the_child_session_runs_only_the_workstations_worker(monkeypatch):
    monkeypatch.setattr(supervisor.socket, "gethostname", lambda: "SAS-PC")
    args = argparse.Namespace(no_worker=False, no_session=False, no_argusd=False, desk_only=True, child=True)
    (name, argv), = supervisor.plan(args)
    caps = [argv[i + 1] for i, a in enumerate(argv) if a == "--cap"]
    assert name == "workstation" and sorted(caps) == ["childsession", "workstation"]  # no "Hey Ari", no island
    assert argv[argv.index("--id") + 1] == "workstation-sas-pc"
    assert len(set(supervisor.LOCK_PORTS.values())) == 3


def test_with_the_child_session_on_the_desk_holds_its_window(monkeypatch):
    monkeypatch.setattr(supervisor, "workstation_where", lambda: "child")
    assert names(monkeypatch, desk_only=True) == ["desktop", "ari-listen", "ari-popup", "workstation-host"]


def test_in_a_child_session_is_found_or_told():
    from argus.child_session import in_child_session

    assert in_child_session({"ARGUS_CHILD_SESSION": "1"}) is True
    assert in_child_session({"ARGUS_CHILD_SESSION": "0"}) is False


def test_the_startup_entry_does_nothing_in_your_own_session(monkeypatch):
    monkeypatch.setenv("ARGUS_CHILD_SESSION", "0")
    assert supervisor.main(["--no-argusd", "--only-in-child"]) == 0
