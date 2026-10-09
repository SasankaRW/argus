"""Ari's Workstation (P7 part 1): Ari's own virtual desktop. A fake Windows here: windows, desktops, the keyboard."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from argus.config import load_config
from argus.plugins import PluginHost
from argus.worker.think import straight_to

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("t_workstation", ROOT / "plugins" / "workstation" / "plugin.py")
ws = importlib.util.module_from_spec(spec)
sys.modules["t_workstation"] = ws  # as the worker loads it (its dataclass needs it)
spec.loader.exec_module(ws)

NEVER = ["directfn", "1password"]


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch):
    """On the PC Playwright is installed: without this, a test would start the real Chrome (and time out)."""
    monkeypatch.setattr(ws, "have_playwright", lambda: False)


class FakeDesk:
    """Two desktops ("you" and Ari's, made on first use), windows on them, and which one has the keyboard."""

    def __init__(self, wins: list[tuple[int, str, str, str]]):
        self.wins = {h: ws.Win(h, title, exe) for h, title, exe, _ in wins}
        self.on = {h: d for h, _, _, d in wins}
        self.fg = wins[0][0] if wins else 0
        self.made = False
        self.launched: list[list[str]] = []
        self.opens: list[tuple[int, str, str]] = []  # what a launch makes appear (and grab the keyboard)
        self.renames: dict[int, str] = {}  # titles that change after a while (a page loading)

    def windows(self):
        for h, t in list(self.renames.items()):
            if h in self.wins:
                self.wins[h].title = t
        return list(self.wins.values())

    def foreground(self):
        return self.fg

    def current(self):
        return "you"

    def workstation(self):
        self.made = True
        return "ari"

    def desktop_of(self, h):
        return self.on.get(h)

    def move(self, h, d):
        self.on[h] = d

    def show_quietly(self, h):
        pass

    def bring_front(self, h):
        self.fg = h

    def launch(self, argv):
        self.launched.append(argv)
        for h, title, exe in self.opens:
            self.wins[h] = ws.Win(h, title, exe)
            self.on[h] = "you"  # new windows open where you are
            self.fg = h  # and take the keyboard


def ctx(desk, inp=None, store=None, **config):
    st = dict(store or {})
    return SimpleNamespace(
        input=inp or {}, desk=desk, dry_run=False, config={"never_touch": NEVER, "browser": "chrome", **config},
        store=SimpleNamespace(get=lambda k, d=None: st.get(k, d), set=lambda k, v: st.__setitem__(k, v), data=st),
        step=lambda name, fn, *a, **k: fn(*a, **k))


def instant(monkeypatch):
    t = [0.0]
    monkeypatch.setattr(ws.time, "monotonic", lambda: t[0])
    monkeypatch.setattr(ws.time, "sleep", lambda s: t.__setitem__(0, t[0] + s))


def test_a_search_opens_on_the_workstation_and_you_keep_the_keyboard(monkeypatch):
    instant(monkeypatch)
    monkeypatch.setattr(ws, "browser_exe", lambda which: r"C:\Chrome\chrome.exe")
    d = FakeDesk([(1, "report.docx - Word", "winword", "you")])
    d.opens = [(9, "New Tab - Google Chrome", "chrome")]
    d.renames = {9: "cheap flights bangkok - Google Search - Google Chrome"}
    c = ctx(d, {"query": "cheap flights Bangkok"})
    out = ws.search(c)
    assert out["done"] and out["on"] == ws.NAME and "Google Search" in out["window"]
    assert d.on[9] == "ari" and d.fg == 1  # on Ari's desktop; your Word still has the keyboard
    argv = d.launched[0]
    assert "--new-window" in argv and any(a.startswith("--user-data-dir=") and "Argus" in a for a in argv)
    assert argv[-1] == "https://www.google.com/search?q=cheap+flights+Bangkok"
    assert c.store.data["last"]["hwnd"] == 9


def test_the_result_is_checked(monkeypatch):
    instant(monkeypatch)
    d = FakeDesk([(9, "Oops - Google Chrome", "chrome", "ari")])
    assert ws.settled(d, 9, ["flights"]) == {"ok": False, "title": "Oops - Google Chrome",
                                             "why": "the page shows 'Oops - Google Chrome'"}
    d.on[9] = "you"
    assert ws.settled(d, 9, [])["why"] == "it isn't on the Workstation"
    assert ws.settled(d, 7, [])["why"] == "the window closed"


def test_move_that_window_to_me_and_take_this():
    d = FakeDesk([(1, "inbox - Outlook", "outlook", "you"), (9, "flights - Google Chrome", "chrome", "ari"),
                  (5, "Spotify Premium", "spotify", "ari")])
    c = ctx(d, {}, store={"last": {"hwnd": 9}})
    out = ws.give(c)  # "move that window to me": the one Ari last worked in
    assert out["done"] and d.on[9] == "you" and d.fg == 9
    assert ws.give(ctx(d, {"name": "spotify"}))["done"] and d.on[5] == "you"
    d.fg = 1
    c2 = ctx(d, {})
    out = ws.take(c2)  # "take this": the window in front
    assert out["done"] and d.on[1] == "ari" and c2.store.data["last"]["hwnd"] == 1


def test_office_and_password_windows_are_never_touched():
    d = FakeDesk([(3, "DirectFN Pro - Market Watch", "directfn", "you"), (4, "1Password", "1password", "you")])
    d.fg = 3
    assert not ws.take(ctx(d, {}))["done"] and d.on[3] == "you"
    assert not ws.take(ctx(d, {"name": "directfn"}))["done"]
    assert not ws.give(ctx(d, {"name": "1password"}))["done"]
    assert ws.open_app(ctx(d, {"name": "DirectFN"}))["problem"] == "I don't work in DirectFN"


def test_an_app_already_open_on_your_desktop_stays_yours(monkeypatch):
    instant(monkeypatch)
    d = FakeDesk([(5, "Spotify Premium", "spotify", "you")])
    out = ws.open_app(ctx(d, {"name": "spotify"}))
    assert not out["done"] and "take this" in out["problem"] and d.on[5] == "you"
    d2 = FakeDesk([(1, "notes - Notepad", "notepad", "you")])
    d2.opens = [(8, "Calculator", "calculatorapp")]
    c = ctx(d2, {"name": "calculator"})
    c.find_app = lambda name: {"name": "Calculator", "id": "Microsoft.WindowsCalculator!App"}
    out = ws.open_app(c)
    assert out["done"] and d2.on[8] == "ari" and d2.fg == 1
    assert d2.launched == [["explorer.exe", "shell:AppsFolder\\Microsoft.WindowsCalculator!App"]]


def test_what_is_on_the_workstation():
    d = FakeDesk([(1, "Word", "winword", "you"), (9, "flights - Google Chrome", "chrome", "ari")])
    assert ws.status(ctx(d))["windows"] == ["flights - Google Chrome"]


def test_web_addresses():
    assert ws.page_url("example.com/a") == "https://example.com/a"
    assert ws.page_url("https://x.org") == "https://x.org"
    with pytest.raises(Exception, match="isn't a web address"):
        ws.page_url("cat videos")
    assert ws.search_words("is it a cat or a dog") == ["cat", "dog"]


@pytest.mark.parametrize("said,tool,args", [
    ("move that window to me", "move_window_to_me", {}),
    ("Hey Ari, give me that", "move_window_to_me", {}),
    ("move the spotify window to me", "move_window_to_me", {"name": "spotify"}),
    ("take this", "take_window", {}),
    ("take the chrome window", "take_window", {"name": "chrome"}),
    ("google cheap flights to Bangkok", "search_in_browser", {"query": "cheap flights to Bangkok"}),
    ("search for laptops in the browser", "search_in_browser", {"query": "laptops"}),
])
def test_said_plainly_goes_straight_to_the_tool(said, tool, args):
    tools = {t: {} for t in ("move_window_to_me", "take_window", "search_in_browser", "web_search", "open_app")}
    assert straight_to(tools, said) == (tool, args)


WS_TOOLS = ("move_window_to_me", "take_window", "search_in_browser", "web_search", "open_app", "do_in_browser",
            "open_on_workstation", "switch_to_window")


@pytest.mark.parametrize("said,tool,args", [
    # what you said on 9 Oct (from logs/ari.log), and the like
    ("Take this screen", "take_window", {}),
    ("Take the Spotify screen", "take_window", {"name": "spotify"}),
    ("Take the Chrome window to your workstation", "take_window", {"name": "chrome"}),
    ("Can you bring Claude to this workstation", "move_window_to_me", {"name": "claude"}),
    ("Now move it to this workstation", "move_window_to_me", {}),
    ("bring the claude window to my desktop", "move_window_to_me", {"name": "claude"}),
    ("take the claude window off my screen", "take_window", {"name": "claude"}),
    ("send spotify to your desktop", "take_window", {"name": "spotify"}),
    ("bring me the vs code window", "move_window_to_me", {"name": "vs code"}),
    ("put this on your desktop", "take_window", {}),
    ("Can you open Chrome and check for my emails", "do_in_browser",
     {"goal": "check for my emails", "url": "https://mail.google.com/"}),
    ("open the browser and find the opening hours of Keells", "do_in_browser",
     {"goal": "find the opening hours of Keells"}),
    ("open notepad on your workstation", "open_on_workstation", {"name": "notepad"}),
    ("open spotify", "open_app", {"name": "spotify"}),  # plain "open X": on your desktop, as before
])
def test_moving_windows_and_browser_tasks_said_loosely(said, tool, args):
    assert straight_to({t: {} for t in WS_TOOLS}, said) == (tool, args)


@pytest.mark.parametrize("said", ["take a note", "take me to the settings", "bring up my calendar", "get the weather",
                                  "give me a joke", "send the report to Kaancha", "put on some music",
                                  "take a screenshot", "take the bins out", "move the chrome window"])
def test_not_a_window_move(said):
    got = straight_to({t: {} for t in WS_TOOLS}, said)
    assert got is None or got[0] not in ("take_window", "move_window_to_me"), got


def test_argus_own_island_is_never_taken_and_app_nicknames_work():
    d = FakeDesk([(2, "Ari", "pythonw", "you"), (3, "main.py - Visual Studio Code", "code", "you")])
    d.fg = 2
    assert not ws.take(ctx(d, {}))["done"] and d.on[2] == "you"  # the island in front: not moved
    assert ws.take(ctx(d, {"name": "vs code"}))["done"] and d.on[3] == "ari"


def test_search_for_alone_is_still_a_question():
    tools = {t: {} for t in ("search_in_browser", "web_search")}
    assert straight_to(tools, "search for the latest python release") is None


def test_the_manifest_loads(tmp_path):
    (tmp_path / "argus.yaml").write_text(f"plugins:\n  dirs: ['{(ROOT / 'plugins').as_posix()}']\n")
    host = PluginHost(load_config(tmp_path / "argus.yaml"))
    host.load()
    assert not host.errors, host.errors
    m = host.plugins["workstation"].manifest
    assert {t.name for t in m.ari.tools} == {"search_in_browser", "browse_on_workstation", "open_on_workstation",
                                            "move_window_to_me", "take_window", "workstation_windows",
                                            "do_in_browser", "play_on_spotify", "do_in_app", "fill_form",
                                            "check_email", "read_email"}
    assert m.job_needs() == ["desktop", "session"]
