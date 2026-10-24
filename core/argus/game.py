"""Game mode: the PC's GPU and memory go to the game (the island's game key, Helios, or by itself).

argusd keeps the state (`/ari/game`: on, auto, by, since, freed_mb). While it's on, Ari is off and the model board
refuses the local (Ollama) tiers, so no job loads a model again; Ari's text answers go to Claude within its budget
and other local-model jobs wait in the queue.

On the PC you sit at, the supervisor (`GameWatch`, every few seconds) does the freeing: it stops "Hey Ari" (Whisper)
and Ari's voice (Chatterbox), unloads what Ollama has loaded and then stops Ollama itself (`ari.game_mode_stop_ollama`:
even with no model loaded it keeps a few hundred MB), and says how much video memory that freed and what else still
uses the GPU. While game mode lasts it keeps it that way (an Ollama started again is stopped again; the boot-time
supervisor doesn't restart it). The GPU worker gives its Whisper back by restarting itself (worker/runner.py
`check_game`) and hears on the CPU until game mode ends. When it ends, Ollama and Ari start again; models load on the
first request.

Automatic (`ari.game_mode_auto`): a game in front for `ari.game_on_after_s` turns it on; no game for
`ari.game_off_after_s` turns it off, but only if it turned itself on. Turned on by hand, it stays on until you turn
it off. A game is what `busy.py` calls one (fullscreen and not a browser or video player, or from a games folder).
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
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


OLLAMA_EXES = ("ollama app.exe", "ollama.exe", "ollama_llama_server.exe", "llama-server.exe")
WIN = sys.platform == "win32"


def ollama_stop() -> list[str]:  # pragma: no cover - real processes
    """Stop Ollama altogether (the tray app too, or it starts the server again). Returns what was stopped."""
    stopped = []
    if WIN:
        for exe in OLLAMA_EXES:
            p = subprocess.run(["taskkill", "/F", "/T", "/IM", exe], capture_output=True, text=True)
            if p.returncode == 0:
                stopped.append(exe)
    else:
        for pat in ("ollama serve", "ollama runner", "ollama_llama_server"):
            if subprocess.run(["pkill", "-f", pat], capture_output=True).returncode == 0:
                stopped.append(pat)
    return stopped


def ollama_up(url: str) -> bool:  # pragma: no cover - a real Ollama
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=3) as r:  # noqa: S310 - our own
            return r.status == 200
    except Exception:  # noqa: BLE001 - not answering
        return False


def ollama_start(url: str) -> str | None:  # pragma: no cover - real processes
    """Start Ollama again: its scheduled task (pc-worker.ps1 install), else `ollama serve`. Returns how."""
    if ollama_up(url):
        return None
    if WIN and subprocess.run(["schtasks", "/Run", "/TN", "Argus Ollama"], capture_output=True).returncode == 0:
        return "task"
    import shutil

    exe = shutil.which("ollama")
    if not exe:
        return None
    flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS) if WIN else 0  # type: ignore[attr-defined]
    subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,  # noqa: S603
                     creationflags=flags)
    return "serve"


def gpu_users() -> list[dict[str, Any]]:  # pragma: no cover - nvidia-smi
    """The processes using the GPU's compute side now: [{pid, name, mb}] (mb None where Windows doesn't say)."""
    try:
        p = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in p.stdout.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 3 and parts[0].isdigit():
            out.append({"pid": int(parts[0]), "name": parts[1].replace("\\", "/").rsplit("/", 1)[-1],
                        "mb": int(parts[2]) if parts[2].isdigit() else None})
    return out


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
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 stop_ollama: Callable[[], list[str]] | None = None, start_ollama: Callable[[], Any] | None = None,
                 users: Callable[[], list[dict]] = lambda: [], keep_every: float = 30):
        self.get, self.post, self.look, self.unload, self.vram = get, post, look, unload, vram
        self.stop_ollama, self.start_ollama, self.users, self.keep_every = stop_ollama, start_ollama, users, keep_every
        self.kept_at = 0.0
        self.warned_at = -1e9
        self.unreachable_since: float | None = None
        self.quiet_for = 180.0  # out of reach this long before it's worth a warning
        self.children = [c for c in children if c.name in FREES]
        self.on_after, self.off_after, self.clock, self.sleep = on_after, off_after, clock, sleep
        self.game_since: float | None = None
        self.clear_since: float | None = None
        self.freed = False  # what game mode frees is freed now

    def tick(self) -> None:
        st = self.get()
        if st is None:  # Argus out of reach: leave things as they are (and say so, now and then)
            now = self.clock()
            if self.unreachable_since is None:
                self.unreachable_since = now
            # only when it lasts: right after the PC starts, Tailscale takes a minute or two to come up
            if now - self.unreachable_since >= self.quiet_for and now - self.warned_at > 600:
                self.warned_at = now
                log.warning("game mode: can't reach Argus (/ari/game), so nothing is freed or started")
            return
        self.unreachable_since = None
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

    def _stop_ollama(self) -> list[str]:
        if self.stop_ollama is None:
            return []
        try:
            return self.stop_ollama()
        except Exception as e:  # noqa: BLE001 - never stop game mode for this
            log.warning("couldn't stop Ollama", extra={"error": str(e)[:160]})
            return []

    def holders(self) -> list[str]:
        """What still uses the GPU (by name, Argus's own marked), for the log, Helios and the console."""
        try:
            return sorted({f"{u['name']}" + (f" {u['mb']} MB" if u.get("mb") else "") for u in self.users()})
        except Exception:  # noqa: BLE001
            return []

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
            stopped = self._stop_ollama()
            self.freed, self.kept_at = True, self.clock()
            self.sleep(3)  # the GPU driver gives the memory back after a moment
            after = self.vram()
            freed = before - after if before is not None and after is not None else None
            still = self.holders()
            log.info("game mode: freed", extra={"stopped": [c.name for c in self.children] + stopped,
                                                "models": models, "freed_mb": freed, "vram_used_mb": after,
                                                "still_on_gpu": still})
            self.post({"freed_mb": freed, "unloaded": models, "still": still, "used_mb": after})
        elif on and self.freed and self.clock() - self.kept_at >= self.keep_every:
            # keep it free: something may have started Ollama again (a job, the tray app, by hand)
            self.kept_at = self.clock()
            for c in self.children:
                if c.alive():
                    c.stop()
            again = self._stop_ollama()
            if again:
                log.info("game mode: Ollama had started again; stopped it", extra={"stopped": again})
                self.post({"still": self.holders(), "used_mb": self.vram()})
        elif not on and self.freed:
            for c in self.children:
                c.held = False  # the supervisor starts them on its next check
                c.next_start = 0.0
                c.fails = 0
            self.freed = False
            how = None
            if self.start_ollama is not None:
                try:
                    how = self.start_ollama()
                except Exception as e:  # noqa: BLE001 - the boot supervisor's keeper starts it later anyway
                    log.warning("couldn't start Ollama", extra={"error": str(e)[:160]})
            log.info("game mode off: Ari back", extra={"starting": [c.name for c in self.children],
                                                       "ollama": how})
