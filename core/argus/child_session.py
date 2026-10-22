"""Ari's Workstation as a real, separate Windows session: a child session (Windows 10/11 Pro).

A child session is a second session of your own user, shown in a window on your desktop, with its own mouse,
keyboard and windows (Microsoft's "picture-in-picture" for Power Automate). Ari works in there, so it never moves
your windows or pulls you to another desktop, and it can use the real mouse whenever an app needs it.

    python -m argus.child_session enable     once, as administrator (WTSEnableChildSessions); `status` to check
    python -m argus.child_session host       the window that holds the session (the desk supervisor runs it when
                                             ari.workstation is "child")

At sign-in in there, the "Argus desktop" logon task starts the supervisor again; it sees it's in the child session and
runs only the Workstation's worker (capabilities "workstation" and "childsession"), never "Hey Ari", the island or a
second desktop worker. While that worker is online, argusd sends the Workstation's jobs to it; when it isn't, they go
to your desktop worker as before (the virtual desktop).

Limits (Windows'): one child session at a time; apps set to start with Windows start in it too; some apps refuse to
run twice (Ari's Chrome has its own profile, so it is fine); the clipboard is shared. A smart-card sign-in asks for a
password in the window.
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import sys
import time

log = logging.getLogger("argus.child_session")

SM_REMOTESESSION = 0x1000
CHILD_SESSION_PROPERTY = "ConnectToChildSession"
# the Remote Desktop ActiveX control (mstscax.dll), newest first: the first one that loads is used
RDP_PROGIDS = ("MsTscAx.MsTscAx.11", "MsTscAx.MsTscAx.10", "MsTscAx.MsTscAx.9", "MsTscAx.MsTscAx")


def _wts():  # pragma: no cover - Windows only
    return ctypes.WinDLL("wtsapi32")


def enabled() -> bool | None:  # pragma: no cover - Windows only
    """Are child sessions turned on? None off Windows."""
    if os.name != "nt":
        return None
    on = ctypes.c_bool(False)
    return bool(_wts().WTSIsChildSessionsEnabled(ctypes.byref(on))) and on.value


def enable() -> bool:  # pragma: no cover - Windows only
    """Turn child sessions on (as administrator, once)."""
    return bool(_wts().WTSEnableChildSessions(ctypes.c_bool(True)))


def child_id() -> int | None:  # pragma: no cover - Windows only
    """The child session's id, seen from the parent (None: there is none)."""
    sid = ctypes.c_ulong(0)
    if _wts().WTSGetChildSessionId(ctypes.byref(sid)) and sid.value:
        return int(sid.value)
    return None


def session_id() -> int:  # pragma: no cover - Windows only
    sid = ctypes.c_ulong(0)
    ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
    return int(sid.value)


def in_child_session(env: dict | None = None) -> bool:
    """Is this process inside Ari's child session? A child session is a loopback remote session that isn't the
    console's. ARGUS_CHILD_SESSION=1 / 0 overrides (tests, and when Windows says otherwise)."""
    env = os.environ if env is None else env
    forced = str(env.get("ARGUS_CHILD_SESSION") or "").strip()
    if forced in ("0", "1"):
        return forced == "1"
    if os.name != "nt":
        return False
    try:  # pragma: no cover - Windows only
        remote = bool(ctypes.windll.user32.GetSystemMetrics(SM_REMOTESESSION))
        console = int(ctypes.windll.kernel32.WTSGetActiveConsoleSessionId())
        return remote and session_id() != console
    except (AttributeError, OSError):  # pragma: no cover
        return False


def host(width: int = 1280, height: int = 800, title: str = "Ari's Workstation") -> int:  # pragma: no cover
    """Show the child session in a window: the Remote Desktop ActiveX control with ConnectToChildSession, connected
    to this PC. Windows signs it in by itself (no password). Runs until the window is closed."""
    import tkinter as tk

    import comtypes  # type: ignore[import-not-found]
    import comtypes.client  # type: ignore[import-not-found]

    if not enabled():
        log.error("child sessions are off: run `python -m argus.child_session enable` as administrator once")
        return 2
    comtypes.CoInitialize()
    atl = ctypes.WinDLL("atl")
    atl.AtlAxWinInit()
    root = tk.Tk()
    root.title(title)
    root.geometry(f"{width}x{height}")
    root.update()
    parent = root.winfo_id()
    hwnd = 0
    used = ""
    for progid in RDP_PROGIDS:
        hwnd = ctypes.windll.user32.CreateWindowExW(0, "AtlAxWin", progid, 0x50000000, 0, 0, width, height, parent,
                                                    None, None, None)
        if hwnd:
            used = progid
            break
    if not hwnd:
        log.error("the Remote Desktop control (mstscax.dll) didn't load")
        return 2
    unk = ctypes.POINTER(comtypes.IUnknown)()
    atl.AtlAxGetControl(hwnd, ctypes.byref(unk))
    mod = comtypes.client.GetModule("mstscax.dll")
    rdp = unk.QueryInterface(mod.IMsRdpClient9) if hasattr(mod, "IMsRdpClient9") else unk.QueryInterface(mod.IMsTscAx)
    ext = unk.QueryInterface(mod.IMsRdpExtendedSettings)
    _set_property(ext, CHILD_SESSION_PROPERTY, True)  # before Connect
    rdp.Server = "localhost"
    try:
        rdp.DesktopWidth, rdp.DesktopHeight = width, height
    except Exception:  # noqa: BLE001 - older controls: the default size
        pass
    log.info("connecting to the child session", extra={"control": used})
    rdp.Connect()

    def on_resize(_e=None) -> None:
        ctypes.windll.user32.MoveWindow(hwnd, 0, 0, root.winfo_width(), root.winfo_height(), True)

    root.bind("<Configure>", on_resize)
    started = time.monotonic()

    def watch() -> None:
        cid = child_id()
        if cid and time.monotonic() - started < 120:
            log.info("child session up", extra={"session": cid})
        root.after(60_000, watch)

    root.after(5_000, watch)
    root.mainloop()
    return 0


def _set_property(ext, name: str, value) -> None:  # pragma: no cover - Windows only
    """IMsRdpExtendedSettings.Property[name] = value, however comtypes wrapped it."""
    tries = (lambda: ext.Property.__setitem__(name, value),
             lambda: ext._IMsRdpExtendedSettings__com__set_Property(name, value),
             lambda: setattr(ext, "Property", (name, value)))
    last: Exception | None = None
    for t in tries:
        try:
            t()
            return
        except Exception as e:  # noqa: BLE001 - try the next form
            last = e
    raise RuntimeError(f"couldn't set {name}: {last}")


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - Windows only
    from .logs import setup_logging

    p = argparse.ArgumentParser(prog="argus-child-session", description="Ari's Workstation in a child session")
    p.add_argument("what", choices=["status", "enable", "host"])
    p.add_argument("--log-file", default="logs/workstation-host.log")
    a = p.parse_args(argv)
    from pathlib import Path

    setup_logging("INFO", Path(a.log_file))
    if a.what == "status":
        print(f"child sessions: {'on' if enabled() else 'off'}; in one now: {in_child_session()}; "
              f"child session id: {child_id()}")
        return 0
    if a.what == "enable":
        if not enable():
            print("Couldn't turn child sessions on: run this as administrator (Windows 10/11 Pro).", file=sys.stderr)
            return 1
        print("Child sessions are on. Set ari.workstation: child in argus.yaml, then restart (dev.ps1 down, up).")
        return 0
    return host()


if __name__ == "__main__":
    sys.exit(main())
