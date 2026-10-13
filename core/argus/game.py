"""Game mode: the PC's GPU and memory go to the game (the island's game key, Helios, or by itself).

argusd keeps the state (`/ari/game`: on, auto, by, since, freed_mb). While it's on, Ari is off and the model board
refuses the local (Ollama) tiers, so no job loads a model again; Ari's text answers go to Claude within its budget
and other local-model jobs wait in the queue.

On the PC you sit at, the supervisor (`GameWatch`, every few seconds) does the freeing: it stops "Hey Ari" (Whisper)
and Ari's voice (Chatterbox) and unloads what Ollama has loaded, then says how much video memory that freed. When
game mode ends it starts them again; models load on the first request.

Automatic (`ari.game_mode_auto`): a game in front for `ari.game_on_after_s` turns it on; no game for
`ari.game_off_after_s` turns it off, but only if it turned itself on. Turned on by hand, it stays on until you turn
it off. A game is what `busy.py` calls one (fullscreen and not a browser or video player, or from a games folder).
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
import urllib.request
from collections.abc import Callable
from typing import Any

log = logging.getLogger("argus.game")

FREES = ("ari-listen", "ari-voice")  # the supervisor's children that hold GPU memory (Whisper, Chatterbox)


def ollama_unload(url: str, timeout: float = 10) -> list[str]:  # pragma: no cover - a real Ollama
    """Unload every model Ollama has in memory (keep_alive 0). Returns their names."""
    base = url.rstrip("/")
    with urllib.request.urlopen(base + "/api/ps", timeout=timeout) as r:  # noqa: S310 - our own Ollama
        names = [m.get("name") or m.get("model") for m in json.loads(r.read() or b"{}").get("models", [])]
    for n in names:
        if not n:
            continue
        body = json.dumps({"model": n, "keep_alive": 0}).encode()
        req = urllib.request.Request(base + "/api/generate", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
                r.read()
        except OSError as e:
            log.warning("couldn't unload", extra={"model": n, "error": str(e)[:160]})
    return [n for n in names if n]


def vram_used_mb() -> int | None:  # pragma: no cover - nvidia-smi
    """Video memory in use (MB, all GPUs), or None without nvidia-smi."""
    try:
        p = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
        return sum(int(x) for x in p.stdout.split() if x.strip().isdigit()) if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


class GameWatch:
    """The PC side of game mode, ticked by the supervisor of your desktop session."""

    def __init__(self, get: Callable[[], dict | None], post: Callable[[dict], Any], children: list[Any], *,
                 look: Callable[[], tuple[str | None, str]], unload: Callable[[], list[str]],
                 vram: Callable[[], int | None] = vram_used_mb, on_after: float = 20, off_after: float = 60,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.get, self.post, self.look, self.unload, self.vram = get, post, look, unload, vram
        self.children = [c for c in children if c.name in FREES]
        self.on_after, self.off_after, self.clock, self.sleep = on_after, off_after, clock, sleep
        self.game_since: float | None = None
        self.clear_since: float | None = None
        self.freed = False  # what game mode frees is freed now

    def tick(self) -> None:
        st = self.get()
        if st is None:  # Argus out of reach: leave things as they are
            return
        on, by = bool(st.get("on")), st.get("by")
        if st.get("auto", True):
            kind, app = self.look()
            now = self.clock()
            if kind == "game":
                self.game_since, self.clear_since = (now if self.game_since is None else self.game_since), None
                if not on and now - self.game_since >= self.on_after:
                    log.info("a game is open: game mode on", extra={"app": app})
                    self.post({"on": True, "by": "auto", "app": app})
                    on = True
            else:
                self.game_since, self.clear_since = None, (now if self.clear_since is None else self.clear_since)
                if on and by == "auto" and now - self.clear_since >= self.off_after:
                    log.info("no game any more: game mode off")
                    self.post({"on": False, "by": "auto"})
                    on = False
        self.apply(on)

    def apply(self, on: bool) -> None:
        if on and not self.freed:
            before = self.vram()
            for c in self.children:
                c.held = True
                c.stop()
            try:
                models = self.unload()
            except Exception as e:  # noqa: BLE001 - Ollama not running: nothing to unload
                log.info("nothing unloaded from Ollama", extra={"error": str(e)[:160]})
                models = []
            self.freed = True
            self.sleep(3)  # the GPU driver gives the memory back after a moment
            after = self.vram()
            freed = before - after if before is not None and after is not None else None
            log.info("game mode: freed", extra={"stopped": [c.name for c in self.children], "models": models,
                                                "freed_mb": freed})
            self.post({"freed_mb": freed, "unloaded": models})
        elif not on and self.freed:
            for c in self.children:
                c.held = False  # the supervisor starts them on its next check
                c.next_start = 0.0
                c.fails = 0
            self.freed = False
            log.info("game mode off: Ari back", extra={"starting": [c.name for c in self.children]})
