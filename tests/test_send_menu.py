"""The right-click "Send to ..." path: `argus send` started from anywhere (Explorer's folder), never silent, never a
console window."""

from __future__ import annotations

import subprocess
from pathlib import Path

from argus.send import agent as agent_mod
from argus.send import cli, notify


def test_config_is_found_from_any_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("ARGUS_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)  # like Explorer starting argus.exe in System32: no argus.yaml here
    found = cli.config_path(None)
    assert found and Path(found).name == "argus.yaml" and Path(found).parent == Path(cli.__file__).resolve().parents[3]


def test_an_explicit_config_still_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_CONFIG", str(tmp_path / "other.yaml"))
    assert cli.config_path(None) is None  # load_config reads ARGUS_CONFIG itself
    assert cli.config_path("x.yaml") == "x.yaml"


def test_a_failed_send_is_logged_and_shown(tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr(cli, "toast", lambda title, body="": shown.append((title, body)))
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    cli._problem("argus send: not found: C:\\nope.txt")
    assert "not found" in (tmp_path / ".argus-send" / "send-cli.log").read_text(encoding="utf-8")
    assert shown == [("Argus couldn't send that", "argus send: not found: C:\\nope.txt")]


def test_toast_text_is_escaped():
    x = notify.toast_xml("Sent <a&b>.txt to my phone", 'say "hi"')
    assert "&lt;a&amp;b&gt;.txt" in x and "<text>say \"hi\"</text>" in x


def test_events_that_notify(monkeypatch):
    shown = []
    monkeypatch.setattr(notify, "toast", lambda title, body="": shown.append(title) or True)
    assert notify.toast_for_event("sent", "my phone", "a.txt")
    assert notify.toast_for_event("received", "Calypso", "b.pdf")
    assert not notify.toast_for_event("held", "Calypso", "b.pdf")  # the quiet ones show nothing
    assert shown == ["Sent a.txt to my phone", "Got b.pdf from Calypso"]


def test_tailscale_check_opens_no_window(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, stdout=b'{"Peer": {}}')

    monkeypatch.setattr(agent_mod.subprocess, "run", fake_run)
    assert agent_mod.tailscale_online() == set()
    assert seen["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
