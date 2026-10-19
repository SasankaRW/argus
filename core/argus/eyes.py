"""Robot eyes on the cooler's screen (DeepCool LM360): plays saved clips, light enough to run all day.

    python -m argus.eyes run [--clip default]          the player (`dev.ps1 up` starts it when eyes.enabled)
    python -m argus.eyes pack in.gif out.eyes          turn a GIF into a clip (needs Pillow and numpy)
    python -m argus.eyes brightness up|down [--steps]  try the screen's brightness buttons

**A clip** (`data/eyes/clips/<name>.eyes`) is stored ready to send: every frame already in the screen's format
(RGB565), compressed. Playing one is read, decompress, send; no image decoding and no numpy. A frame equal to the
one before is stored as "same again" and not sent, so the long calm holds cost almost nothing.

**Switching clips** never jumps: every clip lists its *neutral* frames (the shared resting pose; frame 0 at least).
A new clip starts only when the playing one reaches a neutral frame.

**Away:** when the PC is locked, or nobody has touched it for `eyes.away_after_s`, the screen goes black and
nothing is sent until you're back.

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
                 clock: Callable[[], float] = time.monotonic):
        self.screen, self.dir, self.away, self.clock = screen, Path(clips_dir), away, clock
        self.clip: Clip | None = None
        self.i = 0
        self.wanted: str | None = None
        self.cache: dict[str, Clip] = {}
        self.dark = False
        self.full = True  # the next frame must be sent whole (after a switch, a reconnect, coming back)
        self.next_try = 0.0
        self.clip_retry = 0.0
        self.said = ""
        self._away_at, self._away = -1e9, False

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
                self._away = bool(self.away())
            except Exception:  # noqa: BLE001
                self._away = False
        return self._away

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
            if not self.dark and self.screen.frame(BLACK):
                self.dark = True
            return 0.5
        if self.dark:
            self.dark, self.full = False, True
        switch = self.wanted and (self.clip is None or self.wanted != self.clip.name)
        if switch and (self.clip is None or self.i in self.clip.neutral) and now >= self.clip_retry:
            nxt = self._get(self.wanted)
            if nxt is not None:
                self.clip, self.i, self.full = nxt, 0, True
            else:
                self.clip_retry = now + RETRY_S  # keep playing what we have; look for the file again later
        if self.clip is None:
            return 1.0
        if (self.full or self.clip.changed(self.i)) and self.screen.frame(self.clip.pixels(self.i)):
            self.full = False
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


# ------------------------------------------------------------------ the PC

def away_now(after_s: float) -> bool:  # pragma: no cover - Windows only
    """Locked (the input desktop isn't ours) or nobody has touched the PC for `after_s` seconds."""
    if os.name != "nt":
        return False
    import ctypes

    from .ari_listen import idle_seconds

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    desk = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP: fails on the lock screen
    if not desk:
        return True
    user32.CloseDesktop(desk)
    return idle_seconds() >= after_s


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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="argus-eyes", description="Robot eyes on the cooler's screen")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="play clips on the screen")
    r.add_argument("--clip", default="default")
    r.add_argument("--log-file", default="logs/eyes.log")
    k = sub.add_parser("pack", help="turn a GIF into a clip")
    k.add_argument("src", type=Path)
    k.add_argument("dst", type=Path)
    k.add_argument("--about", default="")
    k.add_argument("--neutral-every", type=float, default=0.0, help="seconds between neutral frames (0: frame 0)")
    b = sub.add_parser("brightness", help="press the screen's brightness button")
    b.add_argument("way", choices=["up", "down"])
    b.add_argument("--steps", type=int, default=1)
    args = p.parse_args(argv)

    if args.cmd == "pack":
        n = pack_gif(args.src, args.dst, args.about, args.neutral_every)
        print(f"{args.dst}: {n} frames, {args.dst.stat().st_size / 1e6:.1f} MB")
        return 0
    if args.cmd == "brightness":
        s = Screen()
        if not s.open():
            print(s.why, file=sys.stderr)
            return 1
        ok = s.brightness(args.way == "up", args.steps)
        s.close()
        return 0 if ok else 1

    from .config import load_config
    from .logs import setup_logging

    setup_logging("INFO", Path(args.log_file))
    cfg = load_config()
    player = Player(Screen(), clips_dir(cfg), away=lambda: away_now(cfg.eyes.away_after_s))
    player.want(args.clip)
    log.info("eyes started", extra={"clip": args.clip, "clips": str(clips_dir(cfg))})
    try:
        player.run()
    except KeyboardInterrupt:
        pass
    finally:
        player.screen.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
