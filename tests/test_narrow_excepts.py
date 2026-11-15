"""The narrowed handlers: the expected failures still fall back quietly, a programming fault is no longer hidden."""
from __future__ import annotations

import http.client
import urllib.error
import urllib.request

import pytest

from argus import supervisor
from argus.ari import ari_listen
from argus.config import ConfigError


def _urlopen_raising(exc: BaseException):
    def fake(*a, **k):
        raise exc
    return fake


@pytest.mark.parametrize("exc", [urllib.error.URLError("down"), TimeoutError(), ConnectionResetError(),
                                 http.client.IncompleteRead(b""), ValueError("not json")])
def test_supervisor_calls_fall_back_on_the_expected_failures(monkeypatch, exc):
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raising(exc))
    assert supervisor.api_call("http://x", None, "/ari/game") is None
    assert supervisor.busy("http://x", None) is False


def test_supervisor_calls_do_not_hide_a_bug(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raising(RuntimeError("bug")))
    with pytest.raises(RuntimeError):
        supervisor.api_call("http://x", None, "/ari/game")
    with pytest.raises(RuntimeError):
        supervisor.busy("http://x", None)


def test_supervisor_config_switches_fall_back_on_a_bad_config(monkeypatch):
    import argus.config as config

    def broken(*a, **k):
        raise ConfigError("bad yaml")
    monkeypatch.setattr(config, "load_config", broken)
    assert supervisor.ari_on("listen") is False
    assert supervisor.eyes_on() is False
    assert supervisor.workstation_where() == "desktop"
    assert supervisor.expressive_on() is False
    assert supervisor.marker_path().name == "argus.running"


def test_supervisor_config_switches_do_not_hide_a_bug(monkeypatch):
    import argus.config as config

    def bug(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(config, "load_config", bug)
    with pytest.raises(RuntimeError):
        supervisor.eyes_on()


def test_listener_client_quiet_when_argusd_is_away(monkeypatch):
    c = ari_listen.AriClient.__new__(ari_listen.AriClient)

    def away(*a, **k):
        raise urllib.error.URLError("down")
    monkeypatch.setattr(c, "_req", away, raising=False)
    assert c.wakes(5) == ([], 5)
    c.here()
    c.ears()
    c.vocabulary()


def test_listener_client_does_not_hide_a_bug(monkeypatch):
    c = ari_listen.AriClient.__new__(ari_listen.AriClient)

    def bug(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(c, "_req", bug, raising=False)
    with pytest.raises(RuntimeError):
        c.wakes(5)
