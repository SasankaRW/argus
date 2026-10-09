"""Argus console: live terminal views of Argus for the laptop's screen (python -m argus.console <view>).

    python -m argus.console deck      the logo (an eye that follows Ari), the machines, jobs, models and Ari
    python -m argus.console ari       the conversation with Ari as it happens
    python -m argus.console events    everything Argus does, as it happens
    python -m argus.console logs      argusd's and the worker's logs, coloured
    python -m argus.console all       all four in one tmux window (what the laptop opens at login)

It only reads, through argusd's API (`--url`, default http://127.0.0.1:8600; the token from --token,
$ARGUS_WORKER_TOKEN or ~/.config/argus/console.env). When argusd is restarting each view says so and carries on.
deploy/linux/console-setup.sh makes it open by itself in Hyprland.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any

from .ari_popup import AriState

# ------------------------------------------------------------------ look

ACCENT, ACCENT2, DIM, FAINT, TEXT = "#5eead4", "#a78bfa", "#6b7280", "#3f4652", "#e5e7eb"
OK, WARN, BAD = "#4ade80", "#fbbf24", "#f87171"
PHASE_COLOUR = {"idle": "#64748b", "listening": "#4ade80", "following": "#4ade80", "thinking": "#fbbf24",
                "working": "#f59e0b", "speaking": "#a78bfa", "done": "#5eead4", "starting": "#94a3b8",
                "off": "#f87171"}

WORDMARK = [
    " █████╗ ██████╗  ██████╗ ██╗   ██╗███████╗",
    "██╔══██╗██╔══██╗██╔════╝ ██║   ██║██╔════╝",
    "███████║██████╔╝██║  ███╗██║   ██║███████╗",
    "██╔══██║██╔══██╗██║   ██║██║   ██║╚════██║",
    "██║  ██║██║  ██║╚██████╔╝╚██████╔╝███████║",
    "╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝  ╚═════╝ ╚══════╝",
]
GRADIENT = ["#5eead4", "#67e8f9", "#7dd3fc", "#93c5fd", "#a5b4fc", "#c4b5fd"]  # teal -> violet, top to bottom


def eye(phase: str, t: float, width: int = 15) -> list[str]:
    """Argus Panoptes's eye, 6 lines. The pupil looks around while idle, wide open when listening, moves fast while
    thinking, pulses while speaking, and the lid closes when Ari is off."""
    if phase == "off":
        return ["", "    ▄▄▄▄▄▄▄    ", "  ▄▀       ▀▄  ", "  ▀▄▄▄▄▄▄▄▄▄▀  ", "               ", ""]
    if phase in ("thinking", "working"):
        dx = round(2 * math.sin(t * 6))
    elif phase in ("listening", "following"):
        dx = 0
    else:
        dx = round(2 * math.sin(t * 0.7)) if int(t) % 9 else 0
    blink = phase == "idle" and (t % 7) < 0.15
    pupil = "◉" if phase != "speaking" else ("●" if int(t * 3) % 2 else "◉")
    mid = list("  █   ( )   █  ")
    c = 7 + dx
    mid[c - 1:c + 2] = list(f"({pupil})") if not blink else list("───")
    return ["    ▄▄▄▄▄▄▄    ", "  ▄█▀▀   ▀▀█▄  ", "".join(mid), "  ▀█▄▄   ▄▄█▀  ", "    ▀▀▀▀▀▀▀    ", ""]


def ago(sec: float) -> str:
    if sec < 60:
        return f"{int(sec)}s"
    if sec < 3600:
        return f"{int(sec // 60)}m"
    if sec < 86400:
        return f"{int(sec // 3600)}h {int(sec % 3600 // 60)}m"
    return f"{int(sec // 86400)}d {int(sec % 86400 // 3600)}h"


SPARK = "▁▂▃▄▅▆▇█"


def spark(values: list[float], top: float = 100.0) -> str:
    return "".join(SPARK[min(7, max(0, int(v / max(top, 1e-9) * 7.99)))] for v in values)


# ------------------------------------------------------------------ talking to argusd


def token_from(path: Path) -> str | None:
    """ARGUS_WORKER_TOKEN from an env file (console-setup.sh copies it to ~/.config/argus/console.env)."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("ARGUS_WORKER_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        pass
    return None


class Api:
    def __init__(self, url: str, token: str | None):
        self.url, self.token = url.rstrip("/"), token
        self.up = True

    def get(self, path: str, timeout: float = 5) -> Any:
        req = urllib.request.Request(self.url + path,
                                     headers={"Authorization": f"Bearer {self.token}"} if self.token else {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - our own argusd
                self.up = True
                return json.loads(r.read() or b"null")
        except (OSError, ValueError):
            self.up = False
            raise


class Feed:
    """A long poll of the event stream in the background: new events go to every callback, oldest first."""

    def __init__(self, api: Api, kinds: str | None = None, backlog: int = 0):
        self.api, self.kinds, self.backlog = api, kinds, backlog
        self.subs: list[Any] = []

    def start(self) -> Feed:
        threading.Thread(target=self._run, daemon=True, name="feed").start()
        return self

    def _run(self) -> None:
        k = f"&kinds={urllib.parse.quote(self.kinds)}" if self.kinds else ""
        seq = -1
        while True:
            try:
                if seq < 0:
                    got = self.api.get(f"/events?newest=true&limit={max(1, self.backlog)}{k}")
                    evs, seq = got.get("events") or [], int(got.get("seq") or 0)
                    if self.backlog:
                        self._emit(evs)
                    continue
                got = self.api.get(f"/events?after={seq}&limit=200&wait=20{k}", timeout=30)
                evs = got.get("events") or []
                if evs:
                    seq = max(int(e["seq"]) for e in evs)
                    self._emit(evs)
            except Exception:  # noqa: BLE001 - argusd restarting: look again in a moment
                time.sleep(3)

    def _emit(self, evs: list[dict]) -> None:
        for f in self.subs:
            f(evs)


# ------------------------------------------------------------------ the views

def _rich():
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    return Console, Group, Live, Panel, Table, Text


def offline(Text: Any, api: Api) -> Any:
    return Text(f"  argusd isn't answering at {api.url} - waiting for it…", style=WARN)


class Deck:
    """The top-left view: the logo with its eye, then how everything is."""

    def __init__(self, api: Api):
        self.api = api
        self.ari = AriState()
        self.data: dict[str, Any] = {}
        self.cpu: deque = deque([0.0] * 30, maxlen=30)
        feed = Feed(api, "ari.state,ari.listening,step.running", backlog=1)
        feed.subs.append(lambda evs: self.ari.apply(evs, time.monotonic()))
        feed.start()
        threading.Thread(target=self._poll, daemon=True, name="deck").start()

    def _poll(self) -> None:
        while True:
            d: dict[str, Any] = {}
            for key, path in (("status", "/status"), ("power", "/power"), ("models", "/models/ollama"),
                              ("ears", "/ari/listener"), ("approvals", "/approvals?state=pending"),
                              ("schedules", "/schedules")):
                try:
                    d[key] = self.api.get(path)
                except Exception:  # noqa: BLE001
                    d[key] = None
            try:
                import psutil

                self.cpu.append(psutil.cpu_percent(interval=None))
                vm = psutil.virtual_memory()
                d["host"] = {"mem": vm.percent, "temp": _temp(psutil)}
            except Exception:  # noqa: BLE001 - psutil missing
                d["host"] = {}
            self.data = d
            time.sleep(2)

    def render(self, t: float, width: int = 120) -> Any:
        Console, Group, Live, Panel, Table, Text = _rich()
        phase = "off" if self.ari.off else self.ari.phase
        colour = PHASE_COLOUR.get(phase, ACCENT)
        logo = Text()
        if width < 46:  # a very narrow pane: the name alone
            logo.append("◉ ARGUS\n", style=f"bold {colour}")
        else:
            for i, (e, w) in enumerate(zip(eye(phase, t), WORDMARK, strict=False)):
                if width >= 64:  # the eye beside the name, if it fits
                    logo.append(e.ljust(17), style=f"bold {colour}")
                    logo.append("  ")
                logo.append(w, style=f"bold {GRADIENT[i]}")
                logo.append("\n")
        st = self.data.get("status") or {}
        tag = Text()
        tag.append("  the eyes and hands of your home lab", style=f"italic {DIM}")
        if st:
            up = ago(st.get("uptime_seconds", 0))
            tag.append(f"   v{st.get('version', '?')} · {st.get('instance', '')} · up {up}", style=DIM)
        if not self.api.up and not st:
            return Group(logo, tag, Text(""), offline(Text, self.api))

        grid = Table.grid(padding=(0, 2), expand=True)
        grid.add_column(style=DIM, width=10)
        grid.add_column(ratio=1)
        word = {"idle": "resting", "following": "listening for more"}.get(phase, phase)
        line = Text()
        line.append("● ", style=colour)
        line.append(word, style=f"bold {colour}")
        if self.ari.text and phase not in ("idle", "off"):
            line.append(f"  {self.ari.text[:70]}", style=TEXT)
        grid.add_row("ari", line)
        ears = self.data.get("ears") or {}
        grid.add_row("ears", Text("● the PC is listening for \"Hey Ari\"" if ears.get("listener") else
                                  "○ no listener (the PC is off or asleep)", style=OK if ears.get("listener") else DIM))
        jobs = st.get("jobs") or {}
        jl = Text()
        for name, col in (("running", ACCENT), ("queued", TEXT), ("waiting", WARN), ("retry", WARN), ("dead", BAD)):
            n = int(jobs.get(name) or 0)
            if n or name in ("running", "queued"):
                jl.append(f"{n} {name}   ", style=col if n else DIM)
        grid.add_row("jobs", jl)
        appr = self.data.get("approvals") or []
        if appr:
            grid.add_row("waiting", Text(f"{len(appr)} for your OK: " + ", ".join(a.get("title", "")[:30]
                                                                                   for a in appr[:3]), style=WARN))
        mach = Text()
        for w in st.get("workers") or []:
            on = w.get("state") == "online"
            mach.append("● " if on else "○ ", style=OK if on else DIM)
            mach.append(f"{w.get('id')}  ", style=TEXT if on else DIM)
        grid.add_row("machines", mach)
        host = self.data.get("host") or {}
        hl = Text()
        hl.append(spark(list(self.cpu)), style=ACCENT)
        hl.append(f"  cpu {self.cpu[-1]:.0f}%", style=TEXT)
        if host.get("mem") is not None:
            hl.append(f"   mem {host['mem']:.0f}%", style=TEXT)
        if host.get("temp"):
            hl.append(f"   {host['temp']:.0f}°C", style=WARN if host["temp"] > 75 else TEXT)
        grid.add_row("this box", hl)
        pw = self.data.get("power") or {}
        if pw:
            pc = pw.get("pc") or {}
            grid.add_row("pc", Text(f"{'awake' if pw.get('pc_online') or pc.get('state') == 'online' else 'asleep'}"
                                    + (f" · {pc.get('host')}" if pc.get("host") else ""), style=TEXT))
        models = self.data.get("models") or {}
        if models.get("models"):
            grid.add_row("models", Text("  ".join(m["name"] for m in models["models"][:5]), style=ACCENT2))
        nxt = [s for s in (self.data.get("schedules") or []) if s.get("enabled") and s.get("next_run_at")]
        if nxt:
            s = min(nxt, key=lambda s: s["next_run_at"])
            grid.add_row("next", Text(f"{s.get('plugin', '')}·{s.get('workflow', '')} in "
                                      f"{ago(max(0, s['next_run_at'] - time.time()))}", style=TEXT))
        clock = Text(time.strftime("  %H:%M:%S  ·  %A %d %B"), style=f"bold {TEXT}")
        return Group(logo, tag, Text(""), Panel(grid, border_style=FAINT, padding=(0, 1)), clock)


def _temp(psutil: Any) -> float | None:
    try:
        temps = psutil.sensors_temperatures()
    except Exception:  # noqa: BLE001 - not on this OS
        return None
    for key in ("coretemp", "k10temp", "acpitz", "cpu_thermal"):
        if temps.get(key):
            return float(max(t.current for t in temps[key]))
    return None


class AriView:
    """The conversation with Ari, live: what you said, what Ari answered, and what it's doing right now."""

    def __init__(self, api: Api):
        self.api = api
        self.ari = AriState()
        self.turns: list[dict] = []
        feed = Feed(api, "ari.state,ari.listening,step.running", backlog=1)
        feed.subs.append(lambda evs: self.ari.apply(evs, time.monotonic()))
        feed.start()
        threading.Thread(target=self._poll, daemon=True, name="ari").start()

    def _poll(self) -> None:
        while True:
            try:
                chats = self.api.get("/ari-chats")
                if chats:
                    self.turns = (self.api.get(f"/ari/{chats[0]['conv']}") or {}).get("turns") or []
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2)

    def render(self, t: float, height: int) -> Any:
        Console, Group, Live, Panel, Table, Text = _rich()
        phase = "off" if self.ari.off else self.ari.phase
        colour = PHASE_COLOUR.get(phase, ACCENT)
        head = Text()
        head.append(" ARI ", style=f"bold black on {colour}")
        dots = "·" * (1 + int(t * 3) % 3) if phase in ("thinking", "working") else ""
        head.append(f"  {phase}{dots}", style=f"bold {colour}")
        if self.ari.text and phase not in ("idle", "off"):
            head.append(f"  {self.ari.text[:80]}", style=TEXT)
        body = Text()
        for tr in self.turns[-40:]:
            who = tr.get("role")
            when = time.strftime("%H:%M", time.localtime(tr.get("created_at") or time.time()))
            text = tr.get("text") or "…"
            if who == "you":
                body.append(f"\n {when}  you ", style=DIM)
                body.append(f"{text}\n", style=f"bold {TEXT}")
            else:
                body.append(f" {when}  ari ", style=ACCENT2)
                body.append(f"{text}\n", style=ACCENT)
        lines = body.split("\n")
        keep = max(5, height - 4)
        if len(lines) > keep:  # the newest lines that fit
            body = Text("\n").join(lines[-keep:])
        if not self.api.up and not self.turns:
            body = offline(Text, self.api)
        return Group(head, body)


GROUP = {"job.": ACCENT, "step.": "#7dd3fc", "worker.": OK, "ari.": ACCENT2, "approval.": WARN, "file.": "#f0abfc",
         "model.": "#fde68a", "power.": "#fca5a5", "edge.": FAINT, "component.": FAINT}
ICON = {"job.succeeded": "✔", "job.dead": "✖", "job.retry": "↻", "job.waiting": "⏸", "job.queued": "＋",
        "job.running": "▶", "step.running": "›", "step.succeeded": "·", "step.failed": "✖", "worker.online": "▲",
        "worker.offline": "▼", "ari.state": "◆", "approval.requested": "?", "approval.approved": "✔",
        "file.moved": "→", "model.escalated": "⇧"}


class EventsView:
    def __init__(self, api: Api, height: int = 200):
        self.api = api
        self.rows: deque = deque(maxlen=height)
        feed = Feed(api, None, backlog=60)
        feed.subs.append(self.rows.extend)
        feed.start()

    def render(self, t: float, height: int) -> Any:
        Console, Group, Live, Panel, Table, Text = _rich()
        out = Text()
        out.append(" EVENTS ", style=f"bold black on {ACCENT}")
        out.append(f"  live · {len(self.rows)} shown\n", style=DIM)
        for e in list(self.rows)[-(max(5, height - 2)):]:
            kind = e.get("kind", "")
            col = next((c for p, c in GROUP.items() if kind.startswith(p)), TEXT)
            if kind.endswith((".dead", ".failed", ".offline")):
                col = BAD
            out.append(time.strftime(" %H:%M:%S ", time.localtime(e.get("at") or 0)), style=DIM)
            out.append(f"{ICON.get(kind, '•')} ", style=col)
            out.append(f"{kind:<18}", style=col)
            who = e.get("from_component") or ""
            if e.get("step"):
                who += f" {e['step']}"
            out.append(f" {who[:34]:<34}", style=TEXT)
            data = e.get("data") or {}
            extra = " ".join(f"{k}={v}" for k, v in data.items() if v not in (None, "", [], {}) and k != "job_id")
            out.append(f" {extra[:90]}\n", style=DIM)
        if not self.api.up and not self.rows:
            return offline(Text, self.api)
        return out


LEVEL = {"debug": (DIM, "DBG"), "info": (TEXT, "INF"), "warn": (WARN, "WRN"), "warning": (WARN, "WRN"),
         "error": (BAD, "ERR"), "critical": (BAD, "CRT")}


class LogsView:
    """The logs argusd can see (its own and this machine's worker), interleaved, newest at the bottom."""

    def __init__(self, api: Api, height: int = 300):
        self.api = api
        self.rows: deque = deque(maxlen=height)
        self.at: dict[str, int | None] = {}
        threading.Thread(target=self._poll, daemon=True, name="logs").start()

    def _poll(self) -> None:
        while True:
            try:
                for src in self.api.get("/logs") or []:
                    name = src["name"]
                    if name.endswith("-crash") or name in ("ollama",):
                        continue
                    first = name not in self.at
                    q = "?lines=40" if first else f"?after={self.at[name]}&lines=400"
                    got = self.api.get(f"/logs/{urllib.parse.quote(name)}{q}")
                    self.at[name] = got.get("offset")
                    self.rows.extend(got.get("entries") or [])
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.5)

    def render(self, t: float, height: int) -> Any:
        Console, Group, Live, Panel, Table, Text = _rich()
        out = Text()
        out.append(" LOGS ", style=f"bold black on {ACCENT2}")
        out.append(f"  {', '.join(sorted(self.at)) or 'waiting…'}\n", style=DIM)
        for r in list(self.rows)[-(max(5, height - 2)):]:
            col, lv = LEVEL.get(str(r.get("level") or "info").lower(), (TEXT, "INF"))
            ts = str(r.get("ts") or "")
            out.append(f" {ts[11:19] if len(ts) >= 19 else '        '} ", style=DIM)
            out.append(f"{lv} ", style=f"bold {col}")
            out.append(f"{str(r.get('source') or '')[:7]:<7} ", style=ACCENT2)
            out.append(f"{str(r.get('logger') or '').replace('argus.', '')[:14]:<14} ", style=DIM)
            out.append(str(r.get("msg") or "")[:110], style=col)
            extra = r.get("extra") or {}
            if extra:
                out.append("  " + " ".join(f"{k}={v}" for k, v in list(extra.items())[:4])[:80], style=DIM)
            out.append("\n")
        return out


# ------------------------------------------------------------------ running


def run_view(name: str, api: Api, fps: float = 6) -> int:
    Console, Group, Live, Panel, Table, Text = _rich()
    console = Console()
    view: Any = {"deck": Deck, "ari": AriView, "events": EventsView, "logs": LogsView}[name](api)
    sys.stdout.write(f"\x1b]2;{'argus' if name == 'deck' else name}\x07")  # the pane's title (tmux shows it)
    with Live(console=console, screen=True, auto_refresh=False, transient=False) as live:
        while True:
            t = time.monotonic()
            try:
                r = view.render(t, console.size.width) if name == "deck" else view.render(t, console.size.height)
            except Exception as e:  # noqa: BLE001 - never die on one bad frame
                r = Text(f"  (view error: {type(e).__name__}: {e})", style=BAD)
            live.update(r, refresh=True)
            time.sleep(1 / fps)


TMUX_CONF = """set -g mouse on
set -g status-style "bg=default,fg=#6b7280"
set -g status-left "#[bg=#5eead4,fg=#0b0f14,bold] ◉ ARGUS #[default] "
set -g status-right "#[fg=#a78bfa]%H:%M #[fg=#6b7280]· #h "
set -g status-left-length 20
set -g pane-border-style "fg=#1f2937"
set -g pane-active-border-style "fg=#5eead4"
set -g pane-border-status top
set -g pane-border-format " #[fg=#a78bfa]#{pane_title} "
# bg=default, not a colour: the terminal's own background shows through (kitty's background_opacity)
set -g window-style "bg=default"
set -g window-active-style "bg=default"
set -g default-terminal "tmux-256color"
set -ga terminal-overrides ",*:Tc"
"""


def run_all(argv: list[str]) -> int:
    """All four views in one tmux window: the deck (logo, eye, status) top left, Ari top right, events bottom left
    and logs bottom right."""
    if shutil.which("tmux") is None:
        print("tmux isn't installed: sudo pacman -S tmux (or run one view: python -m argus.console deck)")
        return 2
    session = "argus"
    conf = Path.home() / ".config" / "argus" / "console.tmux.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(TMUX_CONF, encoding="utf-8")
    extra = " ".join(shlex.quote(a) for a in argv)

    def cmd(view: str) -> str:
        return f"{shlex.quote(sys.executable)} -m argus.console {view} {extra}".strip()

    def tmux(*args: str) -> str:
        done = subprocess.run(["tmux", *args], capture_output=True, text=True)
        if done.returncode:
            raise SystemExit(f"tmux {args[0]} failed: {(done.stderr or '').strip()}")
        return done.stdout.strip()

    subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
    deck = tmux("-f", str(conf), "new-session", "-d", "-s", session, "-x", "240", "-y", "60", "-P", "-F",
                "#{pane_id}", cmd("deck"))
    tmux("source-file", str(conf))
    ari = tmux("split-window", "-h", "-t", deck, "-l", "55%", "-P", "-F", "#{pane_id}", cmd("ari"))
    events = tmux("split-window", "-v", "-t", deck, "-l", "42%", "-P", "-F", "#{pane_id}", cmd("events"))
    logs = tmux("split-window", "-v", "-t", ari, "-l", "42%", "-P", "-F", "#{pane_id}", cmd("logs"))
    for pane, title in ((deck, "argus"), (ari, "ari"), (events, "events"), (logs, "logs")):
        tmux("select-pane", "-t", pane, "-T", title)
    tmux("select-pane", "-t", deck)
    os.execvp("tmux", ["tmux", "attach", "-t", session])
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="argus-console", description="Live terminal views of Argus.")
    ap.add_argument("view", nargs="?", default="all", choices=["all", "deck", "ari", "events", "logs"])
    ap.add_argument("--url", default=os.environ.get("ARGUS_URL", "http://127.0.0.1:8600"))
    ap.add_argument("--token", default=None)
    a = ap.parse_args(argv)
    token = a.token or os.environ.get("ARGUS_WORKER_TOKEN") or token_from(
        Path.home() / ".config" / "argus" / "console.env") or token_from(Path(".env"))
    if a.view == "all":
        return run_all(["--url", a.url])
    try:
        return run_view(a.view, Api(a.url, token))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
