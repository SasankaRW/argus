"""What the island on the PC reads from the PC itself: what's playing, how hard the PC works, which apps use the mic
or the camera, a month's calendar, and its global keys.

Everything here is read on the PC the island runs on (nothing goes through Argus), only while it is on show: the media
and hardware pages read every second or so while open, the mic/camera dots every two seconds. Each part fails on its
own: no media session, no nvidia-smi or no psutil just leaves that part empty.
"""

from __future__ import annotations

import asyncio
import calendar
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

# ------------------------------------------------------------------ small helpers (tested everywhere)


def month_grid(year: int, month: int, first_weekday: int = 0) -> list[list[int]]:
    """The month as weeks of day numbers, 0 where a week runs into the month before or after. Only as many rows as
    the month needs (4 to 6). first_weekday: 0 = Monday, 6 = Sunday."""
    return calendar.Calendar(first_weekday).monthdayscalendar(year, month)


def weekday_letters(first_weekday: int = 0) -> list[str]:
    names = ["M", "T", "W", "T", "F", "S", "S"]
    return names[first_weekday:] + names[:first_weekday]


def level(pct: float | None) -> str:
    """How a load bar is coloured: ok, warm (over 75 %), hot (over 90 %)."""
    if pct is None:
        return "ok"
    return "hot" if pct > 90 else "warm" if pct > 75 else "ok"


def rate(bytes_per_s: float) -> str:
    """A network speed in short words: 0 B/s, 840 KB/s, 12.4 MB/s."""
    b = max(0.0, float(bytes_per_s))
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1000 or unit == "GB":
            return f"{b:.0f} {unit}/s" if unit in ("B", "KB") or b >= 100 else f"{b:.1f} {unit}/s"
        b /= 1000
    return f"{b:.1f} GB/s"


def clock(seconds: float | None) -> str:
    """3:07, 1:02:45."""
    if seconds is None or seconds < 0:
        return "–:––"
    s = int(seconds)
    h, m, s = s // 3600, (s // 60) % 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def app_name(entry: str) -> str:
    """A privacy-settings entry as a name: "C:#Program Files#Discord#Discord.exe" -> "Discord",
    "Microsoft.WindowsCamera_8wekyb3d8bbwe" -> "WindowsCamera"."""
    if "#" in entry or "\\" in entry:
        base = entry.replace("\\", "#").rsplit("#", 1)[-1]
        return base[:-4] if base.lower().endswith(".exe") else base
    name = entry.split("_", 1)[0]
    return name.rsplit(".", 1)[-1] if "." in name else name


def others(entries: list[str], own: list[str]) -> list[str]:
    """The apps in entries, without Argus's own (its listener uses the mic all day: that is not news), each once."""
    own_keys = [o.replace("\\", "#").replace("/", "#").lower() for o in own if o]
    out: list[str] = []
    for e in entries:
        if any(k and k in e.replace("\\", "#").lower() for k in own_keys):
            continue
        n = app_name(e)
        if n and n not in out:
            out.append(n)
    return out


def own_dirs() -> list[str]:
    """Where Argus's own Python lives (its venv): mic use from there is Ari listening."""
    return list({sys.prefix, os.path.dirname(sys.executable)})


def privacy() -> dict[str, list[str]]:  # pragma: no cover - Windows registry
    """{"mic": [apps], "camera": [apps]} using them now (Argus itself left out)."""
    from .busy import capability_apps

    mine = own_dirs()
    return {"mic": others(capability_apps("microphone"), mine), "camera": others(capability_apps("webcam"), mine)}


# ------------------------------------------------------------------ hardware


NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # nvidia-smi without a console flashing up


def parse_gpu(line: str) -> dict[str, Any] | None:
    """One line of nvidia-smi's csv (utilization.gpu, memory.used, memory.total, temperature.gpu)."""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 4:
        return None
    try:
        util, used, total = float(parts[0]), float(parts[1]), float(parts[2])
    except ValueError:
        return None
    temp = float(parts[3]) if parts[3].replace(".", "", 1).isdigit() else None
    return {"util": util, "mem_used": used / 1024, "mem_total": total / 1024,
            "mem": 100 * used / total if total else None, "temp": temp}


class Hardware:
    """CPU, memory, the system disk, network speed and the GPU, read on demand (the system page asks every 1.5 s
    while it is open). Speeds are worked out from the change since the last read."""

    def __init__(self, run: Callable[..., Any] = subprocess.run) -> None:
        self.run = run
        self._net: tuple[float, int, int] | None = None
        self._gpu_at = 0.0
        self._gpu: dict | None = None
        self._smi = shutil.which("nvidia-smi")

    def gpu(self) -> dict | None:
        if not self._smi or time.monotonic() - self._gpu_at < 2:
            return self._gpu
        self._gpu_at = time.monotonic()
        try:
            p = self.run([self._smi, "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
                          "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=4,
                         creationflags=NO_WINDOW)
            self._gpu = parse_gpu((p.stdout or "").splitlines()[0]) if p.returncode == 0 and p.stdout else None
        except (OSError, subprocess.SubprocessError, IndexError):
            self._gpu = None
        return self._gpu

    def read(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            import psutil  # type: ignore[import-not-found]
        except ImportError:
            psutil = None
        if psutil is not None:
            out["cpu"] = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            out["ram"], out["ram_used"], out["ram_total"] = vm.percent, vm.used / 2**30, vm.total / 2**30
            try:
                d = psutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\" if sys.platform == "win32" else "/")
                out["disk"], out["disk_free"] = d.percent, d.free / 2**30
            except OSError:
                pass
            n = psutil.net_io_counters()
            now = time.monotonic()
            if self._net and now > self._net[0]:
                dt = now - self._net[0]
                out["down"] = (n.bytes_recv - self._net[1]) / dt
                out["up"] = (n.bytes_sent - self._net[2]) / dt
            self._net = (now, n.bytes_recv, n.bytes_sent)
        g = self.gpu()
        if g:
            out["gpu"] = g
        return out


# ------------------------------------------------------------------ what's playing (Windows media sessions)

PLAYING, PAUSED = 4, 5  # GlobalSystemMediaTransportControlsSessionPlaybackStatus


def position_now(pos: float | None, end: float | None, updated: float | None, playing: bool,
                 now: float | None = None) -> float | None:
    """Where the song is now: apps report the position only now and then, so add the time since, while it plays."""
    if pos is None:
        return None
    if playing and updated:
        pos += max(0.0, (now if now is not None else time.time()) - updated)
    return min(pos, end) if end else pos


class Media:  # pragma: no cover - Windows media sessions (winrt)
    """What's playing anywhere on the PC (Spotify, a browser tab, the Media Player...), through Windows' media
    sessions, and play / pause / next / previous on it. Without the winrt packages (or off Windows) read() gives
    None and control() presses the keyboard's media keys instead."""

    def __init__(self) -> None:
        self.ok = sys.platform == "win32"
        self._art_key: tuple | None = None
        self._art: bytes | None = None
        self._loop = asyncio.new_event_loop()
        self._lock = threading.Lock()

    def _run(self, coro: Any) -> Any:
        with self._lock:
            return self._loop.run_until_complete(asyncio.wait_for(coro, 4))

    async def _session(self) -> Any:
        from winrt.windows.media.control import (  # type: ignore[import-not-found]
            GlobalSystemMediaTransportControlsSessionManager as Manager,
        )

        mgr = await Manager.request_async()
        return mgr.get_current_session()

    async def _read(self) -> dict | None:
        s = await self._session()
        if s is None:
            return None
        props = await s.try_get_media_properties_async()
        info = s.get_playback_info()
        tl = s.get_timeline_properties()
        status = int(info.playback_status) if info is not None else 0

        def secs(td: Any) -> float | None:
            try:
                return td.total_seconds()
            except AttributeError:
                return td / 10_000_000 if isinstance(td, int) else None

        updated = None
        try:
            updated = tl.last_updated_time.timestamp()
        except Exception:  # noqa: BLE001
            pass
        out = {"title": props.title or "", "artist": props.artist or "", "album": props.album_title or "",
               "app": app_name(s.source_app_user_model_id or ""), "playing": status == PLAYING,
               "paused": status == PAUSED, "pos": secs(tl.position) if tl else None,
               "end": secs(tl.end_time) if tl else None, "updated": updated}
        key = (out["title"], out["artist"], out["album"])
        if key != self._art_key:
            self._art_key, self._art = key, await self._thumb(props)
        out["art"] = self._art
        return out

    async def _thumb(self, props: Any) -> bytes | None:
        try:
            from winrt.windows.storage.streams import (  # type: ignore[import-not-found]
                Buffer,
                DataReader,
                InputStreamOptions,
            )

            if props.thumbnail is None:
                return None
            stream = await props.thumbnail.open_read_async()
            size = int(stream.size)
            if not size or size > 8_000_000:
                return None
            buf = Buffer(size)
            await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
            try:
                return bytes(memoryview(buf))[: buf.length]
            except TypeError:
                reader = DataReader.from_buffer(buf)
                data = bytearray(buf.length)
                reader.read_bytes(data)
                return bytes(data)
        except Exception:  # noqa: BLE001 - no picture is fine
            return None

    def read(self) -> dict | None:
        if not self.ok:
            return None
        try:
            return self._run(self._read())
        except ImportError:
            self.ok = False  # winrt not installed: stop trying
            return None
        except Exception:  # noqa: BLE001 - nothing playing, or the app went away
            return None

    def control(self, action: str) -> bool:
        """play_pause, next or previous on what's playing. True when it went through."""
        async def go() -> bool:
            s = await self._session()
            if s is None:
                return False
            fn = {"play_pause": s.try_toggle_play_pause_async, "next": s.try_skip_next_async,
                  "previous": s.try_skip_previous_async}[action]
            return bool(await fn())

        if self.ok:
            try:
                if self._run(go()):
                    return True
            except Exception:  # noqa: BLE001
                pass
        return media_key(action)


def media_key(action: str) -> bool:  # pragma: no cover - Windows
    """The keyboard's media key for an action (works for nearly every player)."""
    if sys.platform != "win32":
        return False
    import ctypes

    vk = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1}.get(action)
    if vk is None:
        return False
    u = ctypes.windll.user32  # type: ignore[attr-defined]
    u.keybd_event(vk, 0, 1, 0)
    u.keybd_event(vk, 0, 3, 0)
    return True


# ------------------------------------------------------------------ global keys

MODS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
NAMED_KEYS = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
              "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23,
              "insert": 0x2D, "delete": 0x2E, "pageup": 0x21, "pagedown": 0x22, "`": 0xC0, ",": 0xBC,
              ".": 0xBE, "/": 0xBF, ";": 0xBA, "'": 0xDE, "[": 0xDB, "]": 0xDD, "-": 0xBD, "=": 0xBB}


def parse_hotkey(text: str) -> tuple[int, int] | None:
    """ "ctrl+alt+space" -> (modifiers, virtual key) for RegisterHotKey; None when it can't be a global key (no
    modifier, or a key it doesn't know)."""
    parts = [p.strip().lower() for p in (text or "").replace(" ", "").split("+") if p.strip()]
    if len(parts) < 2:
        return None
    mods = 0
    for p in parts[:-1]:
        if p not in MODS:
            return None
        mods |= MODS[p]
    k = parts[-1]
    if len(k) == 1 and (k.isalpha() or k.isdigit()):
        vk = ord(k.upper())
    elif k in NAMED_KEYS:
        vk = NAMED_KEYS[k]
    elif k.startswith("f") and k[1:].isdigit() and 1 <= int(k[1:]) <= 24:
        vk = 0x6F + int(k[1:])
    else:
        return None
    return mods, vk


def listen_hotkeys(keys: dict[str, str], fire: Callable[[str], None],
                   failed: Callable[[str], None] = lambda _n: None) -> threading.Thread | None:  # pragma: no cover
    """Register each global key ({"ask": "ctrl+alt+space", ...}) and call fire(name) when it is pressed. Windows only;
    a key another app holds already is reported through failed(name)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    def run() -> None:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        names: dict[int, str] = {}
        for i, (name, text) in enumerate(keys.items(), start=1):
            hk = parse_hotkey(text)
            if hk is None or not user32.RegisterHotKey(None, i, hk[0] | 0x4000, hk[1]):  # 0x4000: no auto-repeat
                failed(name)
                continue
            names[i] = name
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312 and msg.wParam in names:  # WM_HOTKEY
                fire(names[msg.wParam])

    t = threading.Thread(target=run, daemon=True, name="island-keys")
    t.start()
    return t
