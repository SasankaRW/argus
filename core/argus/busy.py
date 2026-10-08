"""Are you in a call or a game? (the PC's listener asks every few seconds; Ari then answers in text, not aloud)

- **A call:** another app is using the microphone right now. Windows keeps this per app under
  HKCU\\...\\CapabilityAccessManager\\ConsentStore\\microphone: an entry with a start time and no stop time is in use.
  Argus's own listener (Python) doesn't count.
- **A game:** the window in front fills its whole screen and isn't a browser, a video player or Explorer (a
  fullscreen video is not a game), or its program lives in a games folder (Steam, Epic, Riot, ...).

Windows only; elsewhere nothing is detected. Only names are read, never what is said or shown.
"""

from __future__ import annotations

import os
import sys

NOT_CALLS = ("python", "pythonw", "argus", "soundrecorder", "voicerecorder")
NOT_GAMES = ("chrome", "msedge", "firefox", "brave", "opera", "vlc", "mpc-hc", "mpc-be", "potplayer", "spotify",
             "explorer", "applicationframehost", "searchhost", "shellexperiencehost", "powerpnt", "code",
             "windowsterminal", "mpv", "netflix", "lockapp")
GAME_DIRS = ("steamapps", "epic games", "riot games", "gog galaxy", "ubisoft game launcher", "ea games",
             "xboxgames", "battle.net", "rockstar games")


def _short(name: str) -> str:
    """"C:#Program Files#Zoom#bin#Zoom.exe" / "MSTeams_8wekyb3d8bbwe" -> "zoom" / "msteams"."""
    base = name.replace("#", "\\").rstrip("\\").split("\\")[-1].lower()
    return base.removesuffix(".exe").split("_")[0]


def classify(mic_apps: list[str], front: dict | None) -> str | None:
    """"call", "game" or None, from the apps using the mic and the window in front ({exe, fullscreen})."""
    for app in mic_apps:  # Teams, Zoom, Discord, a browser with Meet: any other app on the mic
        s = _short(app)
        if s and not any(n in s for n in NOT_CALLS):
            return "call"
    if front and front.get("fullscreen"):
        exe = str(front.get("exe") or "").lower()
        name = _short(exe)
        if any(d in exe for d in GAME_DIRS):
            return "game"
        if name and not any(n == name or name.startswith(n) for n in NOT_GAMES):
            return "game"
    return None


def mic_apps() -> list[str]:  # pragma: no cover - Windows registry
    if sys.platform != "win32":
        return []
    import winreg

    root = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
    out: list[str] = []

    def scan(path: str) -> None:
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            return
        with k:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                if sub == "NonPackaged":
                    continue
                try:
                    with winreg.OpenKey(k, sub) as s:
                        start = winreg.QueryValueEx(s, "LastUsedTimeStart")[0]
                        stop = winreg.QueryValueEx(s, "LastUsedTimeStop")[0]
                except OSError:
                    continue
                if start and not stop:
                    out.append(sub)

    scan(root)
    scan(root + r"\NonPackaged")
    return out


def front_window() -> dict | None:  # pragma: no cover - Windows only
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]

    mon = user32.MonitorFromWindow(hwnd, 2)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(mon, ctypes.byref(mi))
    m = mi.rcMonitor
    full = rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = ""
    try:
        import psutil

        exe = psutil.Process(pid.value).exe()
    except Exception:  # noqa: BLE001 - gone, or not ours to look at
        pass
    return {"exe": exe, "fullscreen": bool(full)}


def now() -> str | None:
    """In a call, a game, or neither (None). Never raises."""
    if os.environ.get("ARGUS_BUSY"):  # for trying it out: ARGUS_BUSY=call
        return os.environ["ARGUS_BUSY"]
    try:
        return classify(mic_apps(), front_window())
    except Exception:  # noqa: BLE001
        return None
