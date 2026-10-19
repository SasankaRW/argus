"""The robot eyes on the cooler's screen: the clip format, the player and the USB screen (all with fakes)."""

from __future__ import annotations

import argparse

import pytest

from argus import eyes, lm360, supervisor
from argus import focus as fo
from argus.eyes import Clip, Player, pack
from argus.lm360 import BLACK, FRAME_BYTES


def fr(v: int) -> bytes:
    return bytes([v]) * FRAME_BYTES


class FakeScreen:
    def __init__(self, ok: bool = True):
        self.ok_now, self.sent, self.opens = ok, [], 0
        self.fail_next = False
        self.why = "fake"

    @property
    def ok(self) -> bool:
        return self.ok_now

    def open(self) -> bool:
        self.opens += 1
        return self.ok_now

    def frame(self, pixels: bytes) -> bool:
        if self.fail_next:
            self.fail_next, self.ok_now = False, False
            return False
        self.sent.append(pixels[0] if pixels != BLACK else "black")
        return True


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def write(tmp_path, name, frames, neutral=(0,), fps=20):
    (tmp_path / f"{name}.eyes").write_bytes(pack([fr(v) for v in frames], fps=fps, neutral=neutral))


def play(p: Player, clock: Clock, n: int) -> None:
    for _ in range(n):
        clock.t += p.step() or 0.01


def test_a_clip_stores_repeats_as_same_again():
    c = Clip(pack([fr(1), fr(1), fr(2), fr(2), fr(2)], neutral=[2], about="calm"))
    assert len(c) == 5 and c.about == "calm" and c.neutral == {0, 2}
    assert [c.changed(i) for i in range(5)] == [True, False, True, False, False]
    assert c.pixels(4) == fr(2) and c.pixels(1) == fr(1)
    assert len(pack([fr(1)] * 100)) - len(pack([fr(1)])) <= 99 * 4 + 2  # 99 repeats: 4 bytes each


def test_bad_clips_are_refused():
    with pytest.raises(ValueError):
        pack([b"short"])
    with pytest.raises(ValueError):
        Clip(b"not a clip")


def test_the_player_sends_only_frames_that_change(tmp_path):
    write(tmp_path, "default", [1, 1, 1, 2, 2, 3])
    clock = Clock()
    s = FakeScreen()
    p = Player(s, tmp_path, clock=clock)
    p.want("default")
    play(p, clock, 12)  # twice round
    assert s.sent == [1, 2, 3, 1, 2, 3]


def test_switching_waits_for_a_neutral_frame(tmp_path):
    write(tmp_path, "a", [1, 2, 3, 4], neutral=[2])
    write(tmp_path, "b", [7, 8])
    clock = Clock()
    s = FakeScreen()
    p = Player(s, tmp_path, clock=clock)
    p.want("a")
    play(p, clock, 2)        # a: 1, 2 (now at frame 2)
    p.want("b")
    play(p, clock, 3)        # frame 2 is neutral: b starts there
    assert s.sent == [1, 2, 7, 8, 7]
    p.want("a")
    play(p, clock, 1)        # b's frame 1 is not neutral: one more b frame first
    play(p, clock, 2)
    assert s.sent[-3:] == [8, 1, 2]


def test_a_missing_clip_keeps_the_current_one_and_is_looked_for_again(tmp_path):
    write(tmp_path, "a", [1, 2])
    clock = Clock()
    s = FakeScreen()
    p = Player(s, tmp_path, clock=clock)
    p.want("a")
    play(p, clock, 2)
    p.want("later")
    play(p, clock, 4)
    assert s.sent == [1, 2, 1, 2, 1, 2]
    write(tmp_path, "later", [9])
    clock.t += eyes.RETRY_S
    play(p, clock, 2)
    assert s.sent[-1] == 9


def test_away_goes_dark_once_and_comes_back_whole(tmp_path):
    write(tmp_path, "a", [1, 1, 1, 1])
    clock = Clock()
    s = FakeScreen()
    away = {"on": False}
    p = Player(s, tmp_path, away=lambda: away["on"], clock=clock)
    p.want("a")
    play(p, clock, 2)
    away["on"] = True
    clock.t += eyes.AWAY_CHECK_S
    play(p, clock, 10)
    assert s.sent[:2] == [1, "black"] and set(s.sent[1:]) == {"black"}  # dark: black again now and then
    assert len(s.sent) <= 2 + 5 / eyes.KEEPALIVE_S
    away["on"] = False
    clock.t += eyes.AWAY_CHECK_S
    n = len(s.sent)
    play(p, clock, 2)
    assert s.sent[n:] == [1]  # the frame is a repeat, but the screen was black: sent whole


def test_a_still_hold_still_sends_a_frame_every_second(tmp_path):
    write(tmp_path, "a", [5] * 100)  # 5 s of the same frame
    clock = Clock()
    s = FakeScreen()
    p = Player(s, tmp_path, clock=clock)
    p.want("a")
    play(p, clock, 100)
    assert s.sent == [5] * 5  # once at the start, then the keepalive each second


def test_the_stopper_ends_on_a_signal_or_the_stop_file(tmp_path):
    f = tmp_path / "eyes.stop"
    f.write_text("")  # left over from a killed run: cleared at start
    clock = Clock()
    stop = eyes.Stopper(f, clock=clock)
    assert not f.exists() and not stop()
    f.write_text("")
    assert not stop()  # looked 0 s ago: looks again after 0.5 s
    clock.t += 0.5
    assert stop()
    other = eyes.Stopper()
    other._ask(2, None)  # what Ctrl+C does
    assert other()


def test_the_supervisor_asks_the_eyes_to_stop_before_killing(tmp_path, monkeypatch):
    class Proc:
        pid, returncode, seen = 1, None, False

        def poll(self):
            self.seen = self.seen or f.exists()  # exits as soon as it sees the stop file
            return 0 if self.seen else None

        def wait(self, timeout=None):
            self.poll()
            return 0

    f = tmp_path / "eyes.stop"
    killed = []
    monkeypatch.setattr(supervisor.subprocess, "run", lambda *a, **k: killed.append(a))
    c = supervisor.Child("eyes", [], stop_file=f)
    c.proc = Proc()
    c.stop()
    assert killed == [] and not f.exists()


def test_no_screen_means_a_quiet_retry_every_30_s(tmp_path):
    write(tmp_path, "a", [1, 2])
    clock = Clock()
    s = FakeScreen(ok=False)
    p = Player(s, tmp_path, clock=clock)
    p.want("a")
    play(p, clock, 20)       # 20 steps inside 30 s: tried once
    assert s.opens == 1 and s.sent == []
    s.ok_now = True
    clock.t = eyes.RETRY_S + 0.1
    play(p, clock, 2)
    assert s.sent == [1, 2]


def test_losing_the_screen_mid_clip_resends_whole_when_it_is_back(tmp_path):
    write(tmp_path, "a", [1, 1, 1, 1, 1, 1])
    clock = Clock()
    s = FakeScreen()
    p = Player(s, tmp_path, clock=clock)
    p.want("a")
    play(p, clock, 2)
    p.full = True
    s.fail_next = True       # unplugged
    play(p, clock, 1)
    assert not s.ok
    s.ok_now = True          # plugged back in: open() succeeds at once
    play(p, clock, 1)
    assert s.sent == [1, 1]  # the repeat frame is sent whole after the reconnect


class FakeUsb:
    class core:  # noqa: N801 - mirrors pyusb's module names
        class USBError(Exception):
            pass

        dev = None

        @staticmethod
        def find(**kw):
            return FakeUsb.core.dev

    class util:  # noqa: N801
        @staticmethod
        def claim_interface(dev, n):
            dev.claimed = n

        @staticmethod
        def dispose_resources(dev):
            dev.disposed = True


class FakeDev:
    def __init__(self, fail_after: int = 99):
        self.writes: list[bytes] = []
        self.fail_after = fail_after

    def set_configuration(self):
        pass

    def write(self, ep, data, timeout=0):
        if len(self.writes) >= self.fail_after:
            raise FakeUsb.core.USBError("gone")
        self.writes.append(bytes(data))


def test_the_screen_sends_init_then_header_and_pixels():
    FakeUsb.core.dev = FakeDev()
    s = lm360.Screen(usb=FakeUsb)
    assert s.open() and s.ok
    assert s.frame(fr(5))
    w = FakeUsb.core.dev.writes
    assert w[0] == lm360.INIT and w[1] == lm360.HEADER and w[2] == fr(5)
    assert s.unstick() and w[3:] == [lm360.BLACK, lm360.INIT]  # a frame's worth of zeros, then INIT again


def test_the_screen_never_raises():
    FakeUsb.core.dev = None
    s = lm360.Screen(usb=FakeUsb)
    assert not s.open() and "isn't connected" in s.why
    FakeUsb.core.dev = FakeDev(fail_after=2)
    assert s.open()
    assert not s.frame(fr(1)) and not s.ok  # the USB error closes it; the player opens it again later


def test_the_supervisor_runs_the_eyes_only_when_on(monkeypatch):
    monkeypatch.setattr(supervisor, "ari_on", lambda what: False)
    monkeypatch.setattr(supervisor, "expressive_on", lambda: False)
    args = argparse.Namespace(no_worker=False, no_session=False, no_argusd=False, desk_only=False)
    monkeypatch.setattr(supervisor, "eyes_on", lambda: True)
    assert [n for n, _ in supervisor.plan(args)] == ["argusd", "eyes", "worker"]
    assert "--stop-file" in dict(supervisor.plan(args))["eyes"]
    monkeypatch.setattr(supervisor, "eyes_on", lambda: False)
    assert [n for n, _ in supervisor.plan(args)] == ["argusd", "worker"]


# ------------------------------------------------------------------ following the app in front


@pytest.mark.parametrize("exe, title, cls, want", [
    (r"C:\Program Files\Microsoft VS Code\Code.exe", "eyes.py - argus", "Chrome_WidgetWin_1", "code"),
    (r"C:\x\Spotify.exe", "Spotify Premium", "Chrome_WidgetWin_0", "spotify"),
    (r"C:\x\chrome.exe", "lofi beats - YouTube - Google Chrome", "Chrome_WidgetWin_1", "youtube"),
    (r"C:\x\msedge.exe", "SasankaRW/argus: Pull requests · GitHub - Microsoft Edge", "", "github"),
    (r"C:\x\firefox.exe", "Inbox (3) - me@gmail.com - Gmail — Mozilla Firefox", "", "gmail"),
    (r"C:\x\chrome.exe", "Stack Overflow - Google Chrome", "", "browser"),
    (r"C:\Windows\explorer.exe", "", "Shell_TrayWnd", None),      # the taskbar
    (r"C:\Windows\explorer.exe", "", "WorkerW", None),            # the desktop
    (r"C:\Windows\explorer.exe", "Downloads", "CabinetWClass", "explorer"),
    (r"C:\x\notepad.exe", "Save as", "#32770", None),             # a dialog
    (r"C:\Windows\SystemApps\LockApp.exe", "", "", None),          # the lock screen
    ("", "", "", None),                                            # couldn't read it
])
def test_a_window_becomes_a_key(exe, title, cls, want):
    assert fo.key_for(exe, title, cls) == want


def test_the_ignore_list():
    assert fo.key_for(r"C:\x\obs64.exe", "OBS", "", ignore=["OBS64.exe"]) is None


def test_a_key_counts_only_after_it_is_held(tmp_path):
    clock = Clock()
    f = fo.Focus(hold_s=10, seen_file=tmp_path / "seen.json", clock=clock)
    f.window(r"C:\x\Code.exe")
    assert f.current() is None
    clock.t = 9
    assert f.current() is None
    clock.t = 10
    assert f.current() == "code"
    f.window(r"C:\x\chrome.exe", "x - YouTube - Google Chrome")   # Alt+Tab for a moment ...
    clock.t = 15
    f.window(r"C:\x\Code.exe")                                      # ... and back
    clock.t = 30
    assert f.current() == "code"
    assert f.seconds("code") == 20
    f.save()
    again = fo.Focus(seen_file=tmp_path / "seen.json", clock=clock)
    assert again.seconds("code") == 20 and "title" not in (tmp_path / "seen.json").read_text()


def test_passing_windows_keep_the_key(tmp_path):
    clock = Clock()
    f = fo.Focus(hold_s=0, clock=clock)
    f.window(r"C:\x\Spotify.exe")
    assert f.current() == "spotify"
    f.window(r"C:\Windows\explorer.exe", "", "Shell_TrayWnd")
    clock.t = 60
    assert f.current() == "spotify"


class FakeFocus:
    def __init__(self, key=None, seconds=0.0):
        self.key, self.secs = key, seconds

    def current(self):
        return self.key

    def seconds(self, key):
        return self.secs


def test_the_chooser_picks_map_then_own_clip_then_new_then_default(tmp_path):
    f = FakeFocus()
    c = eyes.Chooser(f, tmp_path)
    assert c() is None                         # nothing held yet: keep playing
    f.key = "spotify"
    assert c() == "default"                    # no new.eyes yet
    write(tmp_path, "new", [1])
    assert c() == "new"                        # an app's first two minutes
    f.secs = eyes.NEW_FOR_S
    assert c() == "default"
    write(tmp_path, "spotify", [2])
    assert c() == "spotify"
    (tmp_path / "map.json").write_text('{"spotify": "music"}')
    assert c() == "music"


def test_the_player_follows_the_chooser(tmp_path):
    write(tmp_path, "default", [1, 1])
    write(tmp_path, "code", [5, 5])
    clock = Clock()
    s = FakeScreen()
    pick = {"name": None}
    p = Player(s, tmp_path, clock=clock, choose=lambda: pick["name"])
    p.want("default")
    play(p, clock, 2)
    pick["name"] = "code"
    clock.t += 1.0
    play(p, clock, 2)
    assert s.sent[0] == 1 and s.sent[-1] == 5
