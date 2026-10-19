"""Which app is in front, as a short key for the robot eyes (`code`, `spotify`, `youtube`, `browser` ...).

Windows tells us when the front window changes (a WinEvent hook on its own thread, no polling). In a browser the
tab's title picks the key: Gmail, YouTube and GitHub get their own; every other site is `browser`. A browser
switching tabs doesn't change the front window, so while one is in front we also listen for *its* title changes
(that browser only).

Windows that only pass by (the taskbar, the desktop, Alt+Tab, Start, search, the lock screen, dialogs) never become
a key: the eyes keep what they had. A key must stay in front `hold_s` before it counts, so Alt+Tab doesn't flicker.
Only keys are kept (seconds in front, first and last seen), never window titles.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

log = logging.getLogger("argus.focus")

BROWSERS = {"chrome", "msedge", "firefox", "brave", "opera", "vivaldi", "arc", "chromium"}
SITES = (("gmail", ("gmail", "mail.google")), ("youtube", ("youtube",)), ("github", ("github",)))
PASSING_EXES = {"lockapp", "searchhost", "searchapp", "startmenuexperiencehost", "shellexperiencehost",
                "textinputhost", "shellhost", "applicationframehost", "pythonw", "dwm", "ctfmon"}
PASSING_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW", "MultitaskingViewFrame",
                   "XamlExplorerHostIslandWindow", "ForegroundStaging", "TaskSwitcherWnd", "#32770",
                   "NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland", "Windows.UI.Core.CoreWindow"}


def key_for(exe: str, title: str = "", cls: str = "", ignore: Iterable[str] = ()) -> str | None:
    """The eyes' key for a window, or None for one that only passes by."""
    name = re.sub(r"[^a-z0-9_-]", "", Path(exe.replace("\\", "/")).stem.lower())
    if not name or cls in PASSING_CLASSES or name in PASSING_EXES:
        return None
    if name in {i.lower().removesuffix(".exe") for i in ignore}:
        return None
    if name in BROWSERS:
        t = title.lower()
        return next((key for key, words in SITES if any(w in t for w in words)), "browser")
    return name


class Focus:
    """The key in front, held `hold_s` before it counts, and how long each key has been in front in total."""

    def __init__(self, hold_s: float = 10.0, ignore: Iterable[str] = (), seen_file: Path | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.hold_s, self.ignore, self.file, self.clock = hold_s, list(ignore), seen_file, clock
        self._lock = threading.Lock()
        self._cand: str | None = None
        self._cand_at = 0.0
        self.key: str | None = None
        self._since = clock()
        self.seen: dict[str, dict[str, Any]] = self._load()
        self._saved_at = clock()
        self._dirty = False

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            got = json.loads(self.file.read_text()) if self.file else {}
            return got if isinstance(got, dict) else {}
        except (OSError, ValueError):
            return {}

    def window(self, exe: str, title: str = "", cls: str = "") -> None:
        """A window came to the front (or its title changed). Called from the watcher's thread."""
        key = key_for(exe, title, cls, self.ignore)
        if key is None:
            return
        with self._lock:
            if key != self._cand:
                self._cand, self._cand_at = key, self.clock()

    def current(self) -> str | None:
        """The key that counts now (None until the first one has been held)."""
        now = self.clock()
        with self._lock:
            cand, at = self._cand, self._cand_at
        self._count(now)
        if cand is not None and cand != self.key and now - at >= self.hold_s:
            self.key = cand
            if cand not in self.seen:
                self.seen[cand] = {"seconds": 0, "first": time.strftime("%Y-%m-%d %H:%M")}
            self.seen[cand]["last"] = time.strftime("%Y-%m-%d %H:%M")
            self._dirty = True
            log.info("in front", extra={"key": cand})
        if self._dirty and now - self._saved_at >= 60:
            self.save()
        return self.key

    def _count(self, now: float) -> None:
        if self.key is not None:
            s = self.seen.setdefault(self.key, {"seconds": 0})
            s["seconds"] = round(float(s.get("seconds", 0)) + max(0.0, now - self._since), 1)
            self._dirty = True
        self._since = now

    def seconds(self, key: str) -> float:
        return float(self.seen.get(key, {}).get("seconds", 0))

    def save(self) -> None:
        self._saved_at = self.clock()
        if self.file is None:
            return
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.seen, indent=1, sort_keys=True))
            tmp.replace(self.file)
            self._dirty = False
        except OSError as e:
            log.info("couldn't save the keys", extra={"error": str(e)[:120]})


# ------------------------------------------------------------------ Windows

def describe(hwnd: int) -> tuple[str, str, str]:  # pragma: no cover - Windows only
    """(exe path, title, window class) of a window; empty strings when it can't be read."""
    import ctypes
    from ctypes import wintypes

    user32, k32 = ctypes.windll.user32, ctypes.windll.kernel32  # type: ignore[attr-defined]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    exe = ""
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        buf = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(1024)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            exe = buf.value
        k32.CloseHandle(h)
    title = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(wintypes.HWND(hwnd), title, 512)
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(wintypes.HWND(hwnd), cls, 256)
    return exe, title.value, cls.value


def front() -> tuple[str, str, str]:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    user32.GetForegroundWindow.restype = wintypes.HWND
    hwnd = user32.GetForegroundWindow()
    return describe(hwnd) if hwnd else ("", "", "")


def watch(on_window: Callable[[str, str, str], None]) -> threading.Thread | None:  # pragma: no cover - Windows
    """Call on_window(exe, title, class) whenever the front window changes, or the front browser's title does.
    Runs on its own daemon thread (a WinEvent hook needs a message loop). None when not on Windows."""
    import sys

    if sys.platform != "win32":
        return None

    def loop() -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        proc_t = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG,
                                    wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
        user32.SetWinEventHook.restype = wintypes.HANDLE
        user32.SetWinEventHook.argtypes = [wintypes.UINT, wintypes.UINT, wintypes.HMODULE, proc_t,
                                           wintypes.DWORD, wintypes.DWORD, wintypes.UINT]
        user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        FOREGROUND, NAMECHANGE, FLAGS = 0x0003, 0x800C, 0x0000 | 0x0002  # out of context, skip our own process
        state: dict[str, Any] = {"pid": 0, "hook": None}

        def tell(hwnd: int) -> None:
            try:
                on_window(*describe(hwnd))
            except Exception as e:  # noqa: BLE001 - never let a window kill the watcher
                log.info("couldn't read a window", extra={"error": str(e)[:120]})

        def on_name(_h, _ev, hwnd, obj, _child, _thread, _t) -> None:
            if obj == 0 and hwnd and hwnd == user32.GetForegroundWindow():
                tell(hwnd)

        def on_front(_h, _ev, hwnd, _obj, _child, _thread, _t) -> None:
            if not hwnd:
                return
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            exe, title, cls = describe(hwnd)
            browser = Path(exe.replace("\\", "/")).stem.lower() in BROWSERS
            if pid.value != state["pid"]:  # follow this browser's tab titles, and only this browser's
                if state["hook"]:
                    user32.UnhookWinEvent(state["hook"])
                state["hook"] = user32.SetWinEventHook(NAMECHANGE, NAMECHANGE, None, name_cb, pid.value, 0,
                                                       FLAGS) if browser else None
                state["pid"] = pid.value if browser else 0
            try:
                on_window(exe, title, cls)
            except Exception as e:  # noqa: BLE001
                log.info("couldn't read a window", extra={"error": str(e)[:120]})

        name_cb, front_cb = proc_t(on_name), proc_t(on_front)  # kept alive for the hooks' lifetime
        if not user32.SetWinEventHook(FOREGROUND, FOREGROUND, None, front_cb, 0, 0, FLAGS):
            log.warning("couldn't watch the front window: the eyes stay on one clip")
            return
        hwnd = user32.GetForegroundWindow()
        if hwnd:
            on_front(None, FOREGROUND, hwnd, 0, 0, 0, 0)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    t = threading.Thread(target=loop, name="eyes-focus", daemon=True)
    t.start()
    return t
