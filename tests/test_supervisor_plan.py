"""Which processes the supervisor runs: on the PC now, and on the PC and laptop after the move."""

from __future__ import annotations

import argparse

import pytest

from argus import supervisor


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
    assert "--cap" in argv and "gpu" not in argv and "desktop" not in argv[argv.index("--cap"):argv.index("--id")]


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
