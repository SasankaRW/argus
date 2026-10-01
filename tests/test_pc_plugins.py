"""pc-apps and pc-media: finding the app you meant, the key presses for a volume, and the manifests load."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from argus.config import load_config
from argus.plugins import PluginHost

ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"t_{name}", ROOT / "plugins" / name / "plugin.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


apps = load("pc-apps")
media = load("pc-media")
wins = load("pc-windows")
system = load("pc-system")
APPS = [{"name": n, "id": n.lower()} for n in
        ["Spotify", "Google Chrome", "Visual Studio Code", "Calculator", "Settings", "File Explorer",
         "Microsoft Teams", "Steam", "Steam Support Center", "Notepad"]]


@pytest.mark.parametrize("asked,got", [
    ("spotify", "Spotify"), ("Chrome", "Google Chrome"), ("vs code", "Visual Studio Code"),
    ("code", "Visual Studio Code"), ("calc", "Calculator"), ("teams", "Microsoft Teams"), ("steam", "Steam"),
    ("files", "File Explorer"), ("notpad", "Notepad"), ("photoshop", None), ("", None),
])
def test_the_app_you_meant(asked, got):
    m = apps.best_match(asked, APPS)
    assert (m["name"] if m else None) == got


def test_volume_presses():
    assert media.plan_volume(40, None) == [("down", 50), ("up", 20)]
    assert media.plan_volume(None, "louder") == [("up", 5)]
    assert media.plan_volume(None, "mute") == [("mute", 1)]
    with pytest.raises(Exception, match="level 0-100"):
        media.plan_volume(None, "sideways")


def test_manifests_load_with_their_tools(tmp_path, monkeypatch):
    (tmp_path / "argus.yaml").write_text(f"plugins:\n  dirs: ['{(ROOT / 'plugins').as_posix()}']\n")
    host = PluginHost(load_config(tmp_path / "argus.yaml"))
    host.load()
    assert not host.errors, host.errors
    tools = {t.name for pid in ("pc-apps", "pc-media", "pc-windows", "pc-system")
             for t in host.plugins[pid].manifest.ari.tools}
    assert {"open_app", "open_file", "open_website", "close_app", "set_volume", "media_control", "switch_to_window",
            "lock_pc", "take_screenshot", "pc_status", "read_clipboard", "copy_to_clipboard"} <= tools
    assert host.plugins["pc-apps"].manifest.job_needs() == ["desktop", "session"]
    assert [t.risky for t in host.plugins["pc-apps"].manifest.ari.tools if t.name == "close_app"] == [True]


def test_window_picking():
    rows = [{"Id": 1, "ProcessName": "chrome", "MainWindowTitle": "Argus - Google Chrome"},
            {"Id": 2, "ProcessName": "Code", "MainWindowTitle": "plugin.py - argus - Visual Studio Code"},
            {"Id": 3, "ProcessName": "Spotify", "MainWindowTitle": "Spotify Premium"}]
    assert wins.pick("Chrome", rows)["Id"] == 1
    assert wins.pick("vs code", rows)["Id"] == 2
    assert wins.pick("spotify", rows)["Id"] == 3
    assert wins.pick("steam", rows) is None


def test_pc_status_reads_this_machine():
    pytest.importorskip("psutil")
    s = system.status()
    assert 0 <= s["cpu_percent"] <= 100 and s["memory_total_gb"] > 0 and s["disks"]
    assert len(s["most_memory"]) >= 1


keys = load("pc-keys")


def test_shortcuts_parse_and_passwords_are_refused():
    assert keys.parse_keys("ctrl+shift+t") == [0x11, 0x10, ord("T")]
    assert keys.parse_keys("Alt + Tab") == [0x12, 0x09]
    assert keys.parse_keys("f5") == [0x74]
    for bad in ("ctrl+shift", "ctrl+banana", ""):
        with pytest.raises(Exception):  # noqa: B017 - PermanentError from the plugin module
            keys.parse_keys(bad)
    assert keys.SECRET.search("my password is hunter2") and not keys.SECRET.search("Dear Sam, thanks!")


def test_whatsapp_link():
    assert apps.whatsapp_url("hi there", "+94 77 123 4567") == "whatsapp://send?phone=94771234567&text=hi%20there"
    assert apps.whatsapp_url("hi") == "whatsapp://send?text=hi"
