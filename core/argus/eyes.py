"""Robot eyes on the cooler's screen (DeepCool LM360): plays saved clips, light enough to run all day.

    python -m argus.eyes run [--clip NAME]             the player (`dev.ps1 up` starts it when eyes.enabled);
                                                       --clip plays one clip instead of following the front app
    python -m argus.eyes pack in.gif out.eyes          turn a GIF into a clip (needs Pillow and numpy)
    python -m argus.eyes brightness up|down [--steps]  try the screen's brightness buttons

**A clip** (`data/eyes/clips/<name>.eyes`) is stored ready to send: every frame already in the screen's format
(RGB565), compressed. Playing one is read, decompress, send; no image decoding and no numpy. A frame equal to the
one before is stored as "same again" and not sent, so the long calm holds cost almost nothing.

**Switching clips** never jumps: every clip lists its *neutral* frames (the shared resting pose; frame 0 at least).
A new clip starts only when the playing one reaches a neutral frame.

**Following you:** the app in front picks the clip (see focus.py for the keys): `clips/map.json` ({"key": "clip"})
first, then a clip named after the key (`youtube.eyes`), else `new` (curious) for an app's first two minutes and
`default` after that.

**Away:** when the PC is locked, or nobody has touched it for `eyes.away_after_s`, the screen goes black and
nothing is sent until you're back.

**Stopping** always happens between frames, never inside one (a frame cut in half leaves the screen black until
the PC loses power): Ctrl+C, closing the window and the supervisor's stop file all just ask the loop to end.

**Never in the way:** no cooler, DeepCool's app holding the screen, a USB error: one log line, then it tries again
every 30 s.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import struct
import sys
import time
import zlib
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .lm360 import BLACK, FRAME_BYTES, Screen

log = logging.getLogger("argus.eyes")

MAGIC = b"ARGUS-EYES 1\n"
RETRY_S = 30.0
AWAY_CHECK_S = 2.0
NEW_FOR_S = 120.0  # an app without a clip of its own gets the curious "new" clip for its first two minutes
KEEPALIVE_S = 1.0  # the screen gets a frame at least this often, even during a still hold or while dark


# ------------------------------------------------------------------ clips

def pack(frames: Iterable[bytes], fps: float = 20, neutral: Iterable[int] = (0,), about: str = "") -> bytes:
    """Frames (each FRAME_BYTES of RGB565) as one clip file."""
    blobs: list[bytes] = []
    prev = None
    for f in frames:
        if len(f) != FRAME_BYTES:
            raise ValueError(f"a frame must be {FRAME_BYTES} bytes, not {len(f)}")
        blobs.append(b"" if f == prev else zlib.compress(f, 6))
        prev = f
    if not blobs:
        raise ValueError("a clip needs at least one frame")
    marks = sorted({int(i) for i in neutral if 0 <= int(i) < len(blobs)} | {0})
    head = json.dumps({"fps": fps, "frames": len(blobs), "neutral": marks, "about": about}).encode()
    out = [MAGIC, struct.pack("<I", len(head)), head]
    for b in blobs:
        out += [struct.pack("<I", len(b)), b]
    return b"".join(out)


class Clip:
    def __init__(self, data: bytes, name: str = ""):
        if not data.startswith(MAGIC):
            raise ValueError("not an eyes clip")
        p = len(MAGIC)
        (n,) = struct.unpack_from("<I", data, p)
        p += 4
        head = json.loads(data[p:p + n])
        p += n
        self.name = name
        self.fps = float(head.get("fps") or 20)
        self.about = str(head.get("about") or "")
        self.blobs: list[bytes] = []
        self.src: list[int] = []  # which stored frame each frame shows
        for i in range(int(head["frames"])):
            (n,) = struct.unpack_from("<I", data, p)
            p += 4
            self.blobs.append(data[p:p + n])
            p += n
            self.src.append(i if n else (self.src[-1] if self.src else 0))
        if not self.blobs or not self.blobs[0]:
            raise ValueError("a clip must start with a full frame")
        self.neutral = set(head.get("neutral") or [0]) | {0}

    @classmethod
    def load(cls, path: Path) -> Clip:
        return cls(path.read_bytes(), path.stem)

    def __len__(self) -> int:
        return len(self.blobs)

    def changed(self, i: int) -> bool:
        """Does frame i differ from frame i-1? (frame 0 always counts: it follows the clip's last frame)"""
        return i == 0 or self.src[i] != self.src[i - 1]

    def pixels(self, i: int) -> bytes:
        return zlib.decompress(self.blobs[self.src[i]])


# ------------------------------------------------------------------ the player

class Player:
    """Plays clips on the screen. `step()` shows one frame and says how long until the next."""

    def __init__(self, screen: Any, clips_dir: Path, away: Callable[[], bool] = lambda: False,
                 clock: Callable[[], float] = time.monotonic, choose: Callable[[], str | None] | None = None):
        self.screen, self.dir, self.away, self.clock = screen, Path(clips_dir), away, clock
        self.choose = choose  # which clip now (following the front app); None: keep what was asked for
        self._chose_at = -1e9
        self.clip: Clip | None = None
        self.i = 0
        self.wanted: str | None = None
        self.cache: dict[str, Clip] = {}
        self.dark = False
        self.full = True  # the next frame must be sent whole (after a switch, a reconnect, coming back)
        self.next_try = 0.0
        self.clip_retry = 0.0
        self.said = ""
        self._away_at, self._away = -1e9, ""
        self.sent_at = -1e9

    def want(self, name: str) -> None:
        """Play this clip next (from its next neutral frame)."""
        self.wanted = name

    def _get(self, name: str) -> Clip | None:
        if name not in self.cache:
            path = self.dir / f"{name}.eyes"
            try:
                self.cache[name] = Clip.load(path)
            except (OSError, ValueError) as e:
                self._say(f"no clip {name}", error=str(e)[:120])
                return None
        return self.cache[name]

    def _say(self, what: str, **extra: Any) -> None:
        if what != self.said:  # each problem once, not 20 times a second
            self.said = what
            log.info(what, extra=extra)

    def _is_away(self) -> bool:
        now = self.clock()
        if now - self._away_at >= AWAY_CHECK_S:
            self._away_at = now
            try:
                got = self.away()
            except Exception as e:  # noqa: BLE001 - can't tell: assume you're here
                log.info("away check failed", extra={"error": str(e)[:120]})
                got = ""
            got = (got if isinstance(got, str) else ("away" if got else "")) or ""
            if got != self._away:
                log.info("screen dark: " + got if got else "playing again")
            self._away = got
        return bool(self._away)

    def step(self) -> float:
        now = self.clock()
        if not self.screen.ok:
            if now < self.next_try:
                return min(1.0, self.next_try - now)
            if not self.screen.open():
                self.next_try = now + RETRY_S
                self._say("waiting for the screen", why=getattr(self.screen, "why", ""))
                return 1.0
            self._say("screen connected")
            self.full = True
        if self._is_away():
            if (not self.dark or now - self.sent_at >= KEEPALIVE_S) and self.screen.frame(BLACK):
                self.dark, self.sent_at = True, now
            return 0.5
        if self.dark:
            self.dark, self.full = False, True
        if self.choose is not None and now - self._chose_at >= 1.0:
            self._chose_at = now
            try:
                name = self.choose()
            except Exception as e:  # noqa: BLE001 - keep playing what we have
                self._say("couldn't pick a clip", error=str(e)[:120])
                name = None
            if name:
                self.want(name)
        switch = self.wanted and (self.clip is None or self.wanted != self.clip.name)
        if switch and (self.clip is None or self.i in self.clip.neutral) and now >= self.clip_retry:
            nxt = self._get(self.wanted)
            if nxt is not None:
                self.clip, self.i, self.full = nxt, 0, True
            else:
                self.clip_retry = now + RETRY_S  # keep playing what we have; look for the file again later
        if self.clip is None:
            return 1.0
        send = self.full or self.clip.changed(self.i) or now - self.sent_at >= KEEPALIVE_S
        if send and self.screen.frame(self.clip.pixels(self.i)):
            self.full, self.sent_at = False, now
        elif not self.screen.ok:
            self.next_try = now  # lost it mid-frame: try again right away, then every 30 s
            return 0.0
        self.i = (self.i + 1) % len(self.clip)
        return 1.0 / self.clip.fps

    def run(self, stop: Callable[[], bool] = lambda: False) -> None:
        nxt = self.clock()
        while not stop():
            wait = self.step()
            nxt = max(nxt + wait, self.clock())  # keeps 20 fps steady; never tries to catch up after a pause
            time.sleep(max(0.0, nxt - self.clock()))


class Chooser:
    """The clip for the app in front: map.json, then a clip named after the key, then `new` (first two
    minutes of an app) or `default`. None until an app has been in front long enough to count."""

    def __init__(self, focus: Any, clips_dir: Path):
        self.focus, self.dir = focus, Path(clips_dir)
        self._map: dict[str, str] = {}
        self._map_mtime = -1.0
        self.last = ""

    def _mapping(self) -> dict[str, str]:
        f = self.dir / "map.json"
        try:
            m = f.stat().st_mtime
        except OSError:
            self._map, self._map_mtime = {}, -1.0
            return self._map
        if m != self._map_mtime:
            self._map_mtime = m
            try:
                got = json.loads(f.read_text())
                self._map = {str(k): str(v) for k, v in got.items()} if isinstance(got, dict) else {}
            except (OSError, ValueError, AttributeError):
                self._map = {}
        return self._map

    def __call__(self) -> str | None:
        key = self.focus.current()
        if key is None:
            return None
        name = self._mapping().get(key)
        if not name and (self.dir / f"{key}.eyes").exists():
            name = key
        if not name:
            new = self.focus.seconds(key) < NEW_FOR_S and (self.dir / "new.eyes").exists()
            name = "new" if new else "default"
        if name != self.last:
            self.last = name
            log.info("clip", extra={"key": key, "clip": name})
        return name


class Stopper:
    """Ends the player between frames. Ctrl+C, Ctrl+Break, closing the window (SIGBREAK on Windows) and SIGTERM
    only set a flag, so a frame is never cut in half; `stop_file` appearing does the same (the supervisor creates
    it, waits, and only then kills)."""

    def __init__(self, stop_file: Path | None = None, clock: Callable[[], float] = time.monotonic):
        self.file, self.clock = stop_file, clock
        self.asked = False
        self._looked = -1e9
        if self.file is not None:
            self.file.unlink(missing_ok=True)  # an old one from a run that was killed

    def install(self) -> None:  # pragma: no cover - real signals
        import signal

        for name in ("SIGINT", "SIGBREAK", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    signal.signal(sig, self._ask)
                except (OSError, ValueError):
                    pass

    def _ask(self, *_: Any) -> None:
        self.asked = True

    def __call__(self) -> bool:
        if not self.asked and self.file is not None and self.clock() - self._looked >= 0.5:
            self._looked = self.clock()
            self.asked = self.file.exists()
        return self.asked


# ------------------------------------------------------------------ the PC

def idle_s() -> float:  # pragma: no cover - Windows only
    """Seconds since the last key press or mouse move (Windows; 0 elsewhere). 32-bit tick maths, so it stays
    right after 25 or 50 days of uptime."""
    if os.name != "nt":
        return 0.0
    import ctypes
    from ctypes import wintypes

    class LastInput(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    li = LastInput()
    li.cbSize = ctypes.sizeof(li)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):  # type: ignore[attr-defined]
        return 0.0
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    k32.GetTickCount.restype = wintypes.DWORD
    return ((k32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000.0


def locked() -> bool:  # pragma: no cover - Windows only
    """True on the lock screen: the input desktop isn't ours to open then."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    user32.OpenInputDesktop.restype = ctypes.c_void_p
    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.CloseDesktop.argtypes = [ctypes.c_void_p]
    desk = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not desk:
        return True
    user32.CloseDesktop(desk)
    return False


def away_now(after_s: float) -> str:  # pragma: no cover - Windows only
    """Why the screen should be dark: "locked", "idle" (nobody for `after_s`), or "" (you're here)."""
    if locked():
        return "locked"
    return "idle" if idle_s() >= after_s else ""


def clips_dir(cfg: Any) -> Path:
    return cfg.db_path.parent / "eyes" / "clips"


def pack_gif(src: Path, dst: Path, about: str = "", neutral_every: float = 0.0) -> int:
    """A GIF (any size, resized to the screen) as a clip. Returns the number of frames."""
    from PIL import Image, ImageSequence

    from .lm360 import to_rgb565

    gif = Image.open(src)
    frames, fps = [], 20.0
    for f in ImageSequence.Iterator(gif):
        frames.append(to_rgb565(f))
        fps = 1000.0 / max(10, int(f.info.get("duration", 50)))
    step = int(round(neutral_every * fps)) if neutral_every > 0 else 0
    neutral = range(0, len(frames), step) if step else [0]
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(pack(frames, fps=round(fps, 3), neutral=neutral, about=about))
    return len(frames)


def check(cfg: Any) -> int:  # pragma: no cover - prints what it finds on this PC
    d = clips_dir(cfg)
    print(f"clips folder: {d}")
    for f in sorted(d.glob("*.eyes")):
        try:
            c = Clip.load(f)
            print(f"  {f.stem}: {len(c)} frames at {c.fps:g} fps, {len(c.neutral)} neutral, {c.about!r}")
        except (OSError, ValueError) as e:
            print(f"  {f.stem}: unreadable ({e})")
    print(f"locked: {locked()}   idle: {idle_s():.0f} s   away after: {cfg.eyes.away_after_s} s   "
          f"-> {away_now(cfg.eyes.away_after_s) or 'here'}")
    s = Screen()
    from .focus import front, key_for

    exe, title, cls = front()
    print(f"in front: {key_for(exe, title, cls, cfg.eyes.ignore) or '(passing window: ignored)'}  ({Path(exe).name})")
    print("screen: " + ("connected" if s.open() else s.why))
    s.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="argus-eyes", description="Robot eyes on the cooler's screen")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="play clips on the screen")
    r.add_argument("--clip", default=None, help="play only this clip (default: follow the app in front)")
    r.add_argument("--log-file", default="logs/eyes.log")
    r.add_argument("--awake", action="store_true", help="never go dark (for testing)")
    r.add_argument("--stop-file", type=Path, default=None, help="end (between frames) when this file appears")
    sub.add_parser("check", help="what the player sees: the clip, the screen, whether you count as away")
    k = sub.add_parser("pack", help="turn a GIF into a clip")
    k.add_argument("src", type=Path)
    k.add_argument("dst", type=Path)
    k.add_argument("--about", default="")
    k.add_argument("--neutral-every", type=float, default=0.0, help="seconds between neutral frames (0: frame 0)")
    sub.add_parser("unstick", help="try to bring back a screen left black by a frame cut in half")
    b = sub.add_parser("brightness", help="press the screen's brightness button")
    b.add_argument("way", choices=["up", "down"])
    b.add_argument("--steps", type=int, default=1)
    args = p.parse_args(argv)

    if args.cmd == "pack":
        n = pack_gif(args.src, args.dst, args.about, args.neutral_every)
        print(f"{args.dst}: {n} frames, {args.dst.stat().st_size / 1e6:.1f} MB")
        return 0
    if args.cmd == "unstick":
        s = Screen()
        if not s.open():
            print(s.why, file=sys.stderr)
            return 1
        ok = s.unstick() and s.frame(BLACK)
        s.close()
        print("sent; if it's still black, the only cure is a power cut (switch the PSU off for 10 s)" if ok
              else "couldn't send")
        return 0 if ok else 1
    if args.cmd == "brightness":
        s = Screen()
        if not s.open():
            print(s.why, file=sys.stderr)
            return 1
        ok = s.brightness(args.way == "up", args.steps)
        s.close()
        return 0 if ok else 1

    from .config import load_config

    cfg = load_config()
    if args.cmd == "check":
        return check(cfg)

    from .logs import setup_logging

    setup_logging("INFO", Path(args.log_file))
    after = cfg.eyes.away_after_s
    choose = None
    if args.clip is None:
        from .focus import Focus, watch

        focus = Focus(ignore=cfg.eyes.ignore, seen_file=clips_dir(cfg).parent / "seen.json")
        watch(focus.window)
        choose = Chooser(focus, clips_dir(cfg))
    player = Player(Screen(), clips_dir(cfg), away=(lambda: "") if args.awake else (lambda: away_now(after)),
                    choose=choose)
    player.want(args.clip or "default")
    stop = Stopper(args.stop_file)
    stop.install()
    log.info("eyes started", extra={"clip": args.clip, "clips": str(clips_dir(cfg))})
    try:
        player.run(stop)
    finally:
        player.screen.close()
        if choose is not None:
            choose.focus.save()
    log.info("eyes stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
