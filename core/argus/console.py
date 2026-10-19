"""Argus console: live terminal views of Argus for the laptop's screen (python -m argus.console <view>).

    python -m argus.console deck      the logo (an eye that follows Ari), the machines, jobs, models, Ari and
                                      what isn't working (p: the details)
    python -m argus.console ari       the conversation with Ari as it happens
    python -m argus.console events    the timeline: a lane per machine with its jobs (+/- zoom), then the events
    python -m argus.console logs      each request as one line (l: the raw logs with the model calls; f: failures;
                                      /: search; up/down and Enter: a request's full trace)
    python -m argus.console all       all four in one tmux window (what the laptop opens at login)

Greys and one accent: quiet at rest, brighter while Ari works, red only for what failed.

It only reads, through argusd's API (`--url`, default http://127.0.0.1:8600; the token from --token,
$ARGUS_WORKER_TOKEN or ~/.config/argus/console.env). When argusd is restarting each view says so and carries on.
deploy/linux/console-setup.sh makes it open by itself in Hyprland.
"""

from __future__ import annotations

import argparse
import calendar
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

# Greys and one accent (teal); red only for what failed, amber only for what waits on you. Quiet when idle,
# brighter when something happens.
ACCENT, DIM, FAINT, TEXT, MID = "#5eead4", "#6b7280", "#3f4652", "#e5e7eb", "#9ca3af"
ACCENT2 = MID  # (was a second colour)
OK, WARN, BAD = ACCENT, "#fbbf24", "#f87171"
PHASE_COLOUR = {"idle": DIM, "listening": ACCENT, "following": ACCENT, "thinking": TEXT, "working": TEXT,
                "speaking": ACCENT, "done": ACCENT, "starting": DIM, "off": BAD}

WORDMARK = [
    " █████╗ ██████╗  ██████╗ ██╗   ██╗███████╗",
    "██╔══██╗██╔══██╗██╔════╝ ██║   ██║██╔════╝",
    "███████║██████╔╝██║  ███╗██║   ██║███████╗",
    "██╔══██║██╔══██╗██║   ██║██║   ██║╚════██║",
    "██║  ██║██║  ██║╚██████╔╝╚██████╔╝███████║",
    "╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝  ╚═════╝ ╚══════╝",
]
GRADIENT = [MID] * 6  # the wordmark: grey at rest (the eye beside it carries Ari's colour)


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
        self.show_problems = False  # `p`: what isn't working, in detail
        feed = Feed(api, "ari.state,ari.listening,step.running", backlog=1)
        feed.subs.append(lambda evs: self.ari.apply(evs, time.monotonic()))
        feed.start()
        threading.Thread(target=self._poll, daemon=True, name="deck").start()

    def _poll(self) -> None:
        while True:
            d: dict[str, Any] = {}
            for key, path in (("status", "/status"), ("power", "/power"), ("models", "/models/ollama"),
                              ("ears", "/ari/listener"), ("approvals", "/approvals?state=pending"),
                              ("schedules", "/schedules"), ("problems", "/problems?hours=24")):
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
                logo.append(w, style=f"bold {TEXT if phase not in ('idle', 'off') else GRADIENT[i]}")
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
        pr = self.data.get("problems") or {}
        if pr:
            grid.add_row("problems", Text(f"{pr.get('headline', '')}" + ("   (p: details)" if pr.get("count") else ""),
                                          style=BAD if pr.get("count") else DIM))
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
        parts = [logo, tag, Text(""), Panel(grid, border_style=FAINT, padding=(0, 1))]
        if self.show_problems and pr.get("problems"):
            parts.append(Panel(problem_lines(Text, pr["problems"]), title="not working · last 24 h",
                               title_align="left", border_style=FAINT, padding=(0, 1)))
        return Group(*parts, clock)

    def key(self, k: str) -> None:
        if k == "p":
            self.show_problems = not self.show_problems


def problem_lines(Text: Any, problems: list[dict], n: int = 8) -> Any:
    """What isn't working, one cause a line: ×4  tool play_on_spotify  search box not found — the hint."""
    out = Text()
    for g in problems[:n]:
        out.append(f"×{g.get('count', 1):<3} ", style=BAD)
        out.append(f"{str(g.get('area') or '')[:22]:<22} ", style=TEXT)
        out.append(str(g.get("what") or "")[:70], style=MID)
        if g.get("hint"):
            out.append(f"  — {g['hint'][:60]}", style=DIM)
        out.append("\n")
    return out


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
    """The timeline: a lane per machine with the jobs it ran in the last hour (a bar from start to end, red when it
    failed), then the newest events in a few quiet lines."""

    def __init__(self, api: Api, height: int = 200):
        self.api = api
        self.rows: deque = deque(maxlen=height)
        self.lanes: dict[str, Any] = {}
        self.minutes = 60
        feed = Feed(api, None, backlog=60)
        feed.subs.append(self.rows.extend)
        feed.start()
        threading.Thread(target=self._poll, daemon=True, name="lanes").start()

    def _poll(self) -> None:
        while True:
            try:
                self.lanes = self.api.get(f"/timeline/lanes?minutes={self.minutes}") or {}
            except Exception:  # noqa: BLE001 - an older argusd, or restarting
                pass
            time.sleep(5)

    def key(self, k: str) -> None:
        if k in ("+", "="):
            self.minutes = max(10, self.minutes // 2)
        elif k in ("-", "_"):
            self.minutes = min(24 * 60, self.minutes * 2)

    def lane_text(self, Text: Any, width: int) -> Any:
        out = Text()
        workers = self.lanes.get("workers") or []
        now = float(self.lanes.get("now") or time.time())
        since = float(self.lanes.get("since") or now - self.minutes * 60)
        name_w, cols = 14, max(10, width - 16)
        span = max(1.0, now - since)
        axis = [" "] * cols
        for frac in (0.0, 0.25, 0.5, 0.75):
            label = time.strftime("%H:%M", time.localtime(since + span * frac))
            i = int(frac * cols)
            for j, ch in enumerate(label):
                if i + j < cols:
                    axis[i + j] = ch
        out.append(" " * (name_w + 1) + "".join(axis) + "\n", style=FAINT)
        if not workers:
            out.append(" " * (name_w + 1) + "nothing ran in this window\n", style=DIM)
        for w in workers:
            cells: list[tuple[str, str]] = [("·", FAINT)] * cols
            for j in w.get("jobs") or []:
                a = int((max(float(j.get("start") or since), since) - since) / span * cols)
                b = int((float(j.get("end") or now) - since) / span * cols)
                a, b = max(0, min(cols - 1, a)), max(0, min(cols - 1, max(a, b)))
                col = BAD if j.get("state") in ("dead", "cancelled") else (TEXT if j.get("state") == "running" else
                                                                            ACCENT)
                label = str(j.get("plugin") or "")
                for x in range(a, b + 1):
                    k = x - a
                    ch = label[k] if b - a >= len(label) + 1 and 0 <= k < len(label) else "▬"
                    cells[x] = (ch, col)
            out.append(f" {str(w.get('worker') or '?')[:name_w]:<{name_w}}", style=MID)
            for ch, col in cells:
                out.append(ch, style=col)
            out.append("\n")
        out.append(" " * (name_w + 1 + cols - 4) + "now", style=DIM)
        out.append("\n")
        return out

    def render(self, t: float, height: int, width: int = 120) -> Any:
        Console, Group, Live, Panel, Table, Text = _rich()
        out = Text()
        out.append(" TIMELINE ", style=f"bold black on {ACCENT}")
        out.append(f"  last {self.minutes} min · +/- to zoom\n", style=DIM)
        lanes = self.lane_text(Text, width)
        out.append_text(lanes)
        used = 2 + len(lanes.plain.splitlines())
        out.append("\n")
        for e in list(self.rows)[-(max(3, height - used - 2)):]:
            kind = e.get("kind", "")
            col = BAD if kind.endswith((".dead", ".failed", ".offline")) else (
                ACCENT if kind in ("job.succeeded", "worker.online") else MID)
            out.append(time.strftime(" %H:%M:%S ", time.localtime(e.get("at") or 0)), style=FAINT)
            out.append(f"{ICON.get(kind, '•')} ", style=col)
            out.append(f"{kind:<18}", style=col)
            who = e.get("from_component") or ""
            if e.get("step"):
                who += f" {e['step']}"
            out.append(f" {who[:34]:<34}", style=TEXT if col != MID else MID)
            data = e.get("data") or {}
            extra = " ".join(f"{k}={v}" for k, v in data.items() if v not in (None, "", [], {}) and k != "job_id")
            out.append(f" {extra[:90]}\n", style=DIM)
        if not self.api.up and not self.rows:
            return offline(Text, self.api)
        return out


LEVEL = {"debug": (DIM, "DBG"), "info": (TEXT, "INF"), "warn": (WARN, "WRN"), "warning": (WARN, "WRN"),
         "error": (BAD, "ERR"), "critical": (BAD, "CRT")}


class LogsView:
    """What happened, one readable line each. Two modes (`l` switches):
    - requests: each thing you asked Ari as one line (heard → what Ari did → the answer or why it failed), and
      speech that was ignored, dimmed, with why. ↑/↓ choose a line, Enter opens its full trace, Esc closes it.
    - logs: argusd's and the worker's log lines (warnings amber, errors red) with the model calls in between.
    `f`: failures and ignored only. `/`: search (type, Enter; Esc clears)."""

    def __init__(self, api: Api, height: int = 300):
        self.api = api
        self.rows: deque = deque(maxlen=height)
        self.calls: list[dict] = []
        self.stories: list[dict] = []
        self.at: dict[str, int | None] = {}
        self.mode = "requests"
        self.failures_only = False
        self.search = ""
        self.typing: str | None = None  # the search being typed
        self.sel = 0  # from the newest (0)
        self.trace: dict | None = None
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
            try:
                self.stories = (self.api.get("/ari/timeline?limit=80") or {}).get("requests") or []
            except Exception:  # noqa: BLE001 - an older argusd
                pass
            try:
                self.calls = (self.api.get("/models/calls?limit=60") or {}).get("calls") or []
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.5)

    # ---- keys

    def key(self, k: str) -> None:
        if self.typing is not None:
            if k in ("\r", "\n"):
                self.search, self.typing = self.typing, None
            elif k == "\x1b":
                self.search, self.typing = "", None
            elif k in ("\x7f", "\b"):
                self.typing = self.typing[:-1]
            elif k.isprintable():
                self.typing += k
            return
        if k == "l":
            self.mode = "logs" if self.mode == "requests" else "requests"
            self.trace, self.sel = None, 0
        elif k == "f":
            self.failures_only = not self.failures_only
        elif k == "/":
            self.typing = ""
        elif k in ("\x1b[A", "k"):
            self.sel += 1
        elif k in ("\x1b[B", "j"):
            self.sel = max(0, self.sel - 1)
        elif k == "\x1b":
            self.trace, self.search = None, self.search if self.trace else ""
        elif k in ("\r", "\n") and self.mode == "requests":
            rows = self.shown_stories()
            if rows:
                r = rows[-1 - min(self.sel, len(rows) - 1)]
                ref = r.get("job_id") or (str(r["turn"]) if r.get("turn") else None)
                if ref:
                    threading.Thread(target=self._load_trace, args=(ref, r), daemon=True).start()

    def _load_trace(self, ref: str, r: dict) -> None:
        try:
            self.trace = self.api.get(f"/ari/trace/{urllib.parse.quote(ref)}")
        except Exception:  # noqa: BLE001
            self.trace = {**r, "steps": [], "error": "couldn't load the trace"}

    # ---- what shows

    def shown_stories(self) -> list[dict]:
        rows = list(reversed(self.stories))  # oldest first, newest at the bottom
        if self.failures_only:
            rows = [r for r in rows if r.get("result") in ("failed", "ignored", "stopped")]
        if self.search:
            n = self.search.lower()
            rows = [r for r in rows if n in json.dumps(r, default=str).lower()]
        return rows

    def story_line(self, Text: Any, r: dict, selected: bool) -> Any:
        res = r.get("result") or "?"
        mark = {"ok": "✓", "failed": "✗", "stopped": "■", "ignored": "·"}.get(res, "…")
        col = {"ok": ACCENT, "failed": BAD, "stopped": WARN, "ignored": FAINT}.get(res, MID)
        line = Text()
        line.append("›" if selected else " ", style=ACCENT)
        line.append(time.strftime("%H:%M:%S ", time.localtime(r.get("at") or 0)), style=FAINT)
        line.append(f"{mark} ", style=col)
        said = str(r.get("text") or "")[:56]
        line.append(f"\"{said}\"", style=DIM if res == "ignored" else f"bold {TEXT}")
        tools = [str(x.get("tool")) for x in r.get("tools") or []]
        if tools:
            line.append(" → " + ", ".join(tools[:3]), style=MID)
        if r.get("why"):
            line.append(f"  {str(r['why'])[:80]}", style=BAD if res == "failed" else DIM)
        elif r.get("reply"):
            line.append(f"  \"{str(r['reply'])[:70]}\"", style=MID)
        ms = r.get("answer_ms") or r.get("ms")
        if ms:
            line.append(f"  {ms / 1000:.1f}s", style=FAINT)
        models = r.get("models") or []
        bad = sum(1 for m in models if m.get("verdict") != "ok")
        if models:
            line.append(f"  {models[-1].get('model')}" + (f" ({bad} rejected)" if bad else ""), style=FAINT)
        return line

    def trace_text(self, Text: Any) -> Any:
        t = self.trace or {}
        out = Text()
        out.append(f"  ┌ trace of \"{str(t.get('text') or t.get('workflow') or '')[:60]}\"  (Esc closes)\n",
                   style=ACCENT)
        h = t.get("heard") or {}
        if h:
            bits = []
            if h.get("accepted") is not None:
                bits.append(f"English {h['accepted']:.0%}" + (f", most like {h.get('top')} {h.get('top_p', 0):.0%}"
                                                              if h.get("top") not in (None, "en") else ""))
            if h.get("second_listen"):
                bits.append("\"Hey Ari\" caught on a second listen")
            out.append("  │ heard   " + " · ".join(bits) + "\n", style=MID)
        for s in t.get("steps") or []:
            ok = s.get("ok", True)
            out.append(f"  │ {s.get('kind', 'step'):<7} ", style=FAINT)
            out.append(f"{str(s.get('what') or '')[:44]:<44}", style=TEXT if ok else BAD)
            if s.get("ms") is not None:
                out.append(f" {s['ms'] / 1000:5.1f}s", style=FAINT)
            if s.get("why"):
                out.append(f"  {str(s['why'])[:70]}", style=MID if ok else BAD)
            out.append("\n")
        for x in t.get("tools") or []:
            out.append("  │ tool    ", style=FAINT)
            out.append(f"{str(x.get('tool'))[:44]:<44}", style=TEXT if x.get("ok") else BAD)
            out.append(f"  {'worked' if x.get('ok') else x.get('why')}\n", style=MID if x.get("ok") else BAD)
        if t.get("reply"):
            out.append(f"  │ said    \"{str(t['reply'])[:100]}\"\n", style=MID)
        out.append(f"  └ {t.get('result') or ''}" + (f": {t['why']}" if t.get("why") else "") + "\n",
                   style=BAD if t.get("result") == "failed" else DIM)
        return out

    def log_lines(self) -> list[tuple[float, dict]]:
        """Log entries and model calls, in time order (`logs` mode)."""
        items: list[tuple[float, dict]] = []
        for r in self.rows:
            ts = str(r.get("ts") or "")
            try:
                at = calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) if ts else 0.0
            except ValueError:
                at = 0.0
            items.append((at, {"log": r}))
        for c in self.calls:
            items.append((float(c.get("created_at") or 0), {"call": c}))
        items.sort(key=lambda x: x[0])
        return items

    def render(self, t: float, height: int, width: int = 200) -> Any:
        Console, Group, Live, Panel, Table, Text = _rich()
        out = Text()
        out.append(" LOGS ", style=f"bold black on {ACCENT}")
        flags = [self.mode] + (["failures"] if self.failures_only else []) + ([f"/{self.search}"] if self.search
                                                                              else [])
        out.append(f"  {' · '.join(flags)}", style=TEXT)
        out.append("   l switch · f failures · / search · ↑↓ Enter trace\n", style=FAINT)
        if self.typing is not None:
            out.append(f"  search: {self.typing}▏\n", style=ACCENT)
        room = max(5, height - 3)
        if self.mode == "requests":
            rows = self.shown_stories()
            if self.trace:
                tr = self.trace_text(Text)
                room -= len(tr.plain.splitlines())
            if not rows:
                out.append("  no requests yet" + (" that match" if self.search or self.failures_only else "")
                           + " (Ari's requests show here; l for the raw logs)\n", style=DIM)
            sel = min(self.sel, max(0, len(rows) - 1))
            for i, r in enumerate(rows[-max(3, room):]):
                idx_from_newest = len(rows[-max(3, room):]) - 1 - i
                line = self.story_line(Text, r, idx_from_newest == sel)
                line.truncate(max(20, width - 1), overflow="ellipsis")  # one line each, never wrapped
                out.append_text(line)
                out.append("\n")
            if self.trace:
                out.append_text(tr)
            return out
        n = self.search.lower()
        for at, item in self.log_lines()[-(room * 2):][-room:]:
            if "call" in item:
                c = item["call"]
                if self.failures_only and c.get("verdict") == "ok":
                    continue
                if n and n not in json.dumps(c, default=str).lower():
                    continue
                ok = c.get("verdict") == "ok"
                out.append(time.strftime(" %H:%M:%S ", time.localtime(at)), style=FAINT)
                out.append("MDL ", style=f"bold {ACCENT if ok else WARN}")
                out.append(f"{str(c.get('plugin') or '')}.{str(c.get('workflow') or '')}"[:22].ljust(22), style=DIM)
                out.append(f"{str(c.get('model') or c.get('tier') or '')[:20]:<20}", style=MID)
                out.append(f" {c.get('verdict')}", style=ACCENT if ok else WARN)
                if c.get("latency_ms") is not None:
                    out.append(f" {c['latency_ms'] / 1000:.1f}s", style=FAINT)
                if c.get("reason"):
                    out.append(f"  {str(c['reason'])[:70]}", style=WARN)
                out.append("\n")
                continue
            r = item["log"]
            col, lv = LEVEL.get(str(r.get("level") or "info").lower(), (TEXT, "INF"))
            if self.failures_only and lv not in ("WRN", "ERR", "CRT"):
                continue
            if n and n not in json.dumps(r, default=str).lower():
                continue
            ts = str(r.get("ts") or "")
            out.append(f" {ts[11:19] if len(ts) >= 19 else '        '} ", style=FAINT)
            out.append(f"{lv} ", style=f"bold {col if lv != 'INF' else DIM}")
            out.append(f"{str(r.get('source') or '')[:7]:<7} ", style=DIM)
            out.append(f"{str(r.get('logger') or '').replace('argus.', '')[:14]:<14} ", style=FAINT)
            out.append(str(r.get("msg") or "")[:110], style=col if lv != "INF" else TEXT)
            extra = r.get("extra") or {}
            keep = [k for k in ("heard", "problem", "error", "why", "reply", "tools", "text", "took_ms") if k in extra]
            keep += [k for k in extra if k not in keep][: max(0, 4 - len(keep))]
            if keep:
                out.append("  " + " ".join(f"{k}={extra[k]}" for k in keep)[:100], style=DIM)
            out.append("\n")
        return out


# ------------------------------------------------------------------ running


class Keys:
    """Keys pressed in this pane (a view's `key(k)`: arrows come as "\\x1b[A" / "\\x1b[B"). Only in a real
    terminal; the terminal is put back as it was on exit."""

    def __init__(self) -> None:
        import queue

        self.q: Any = queue.Queue()
        if not sys.stdin.isatty():
            return
        try:
            import atexit
            import termios
            import tty

            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            atexit.register(lambda: termios.tcsetattr(fd, termios.TCSADRAIN, old))
        except Exception:  # noqa: BLE001 - not a terminal we can read keys from
            return
        threading.Thread(target=self._run, args=(fd,), daemon=True, name="keys").start()

    def _run(self, fd: int) -> None:
        while True:
            try:
                chunk = os.read(fd, 8).decode(errors="ignore")
            except OSError:
                return
            while chunk:
                if chunk.startswith("\x1b[") and len(chunk) >= 3:
                    self.q.put(chunk[:3])
                    chunk = chunk[3:]
                else:
                    self.q.put(chunk[0])
                    chunk = chunk[1:]

    def drain(self) -> list[str]:
        out = []
        while not self.q.empty():
            out.append(self.q.get_nowait())
        return out


def run_view(name: str, api: Api, fps: float = 6) -> int:
    Console, Group, Live, Panel, Table, Text = _rich()
    console = Console()
    view: Any = {"deck": Deck, "ari": AriView, "events": EventsView, "logs": LogsView}[name](api)
    keys = Keys()
    sys.stdout.write(f"\x1b]2;{'argus' if name == 'deck' else name}\x07")  # the pane's title (tmux shows it)
    with Live(console=console, screen=True, auto_refresh=False, transient=False) as live:
        while True:
            t = time.monotonic()
            for k in keys.drain():
                if hasattr(view, "key"):
                    try:
                        view.key(k)
                    except Exception:  # noqa: BLE001 - a key never breaks the view
                        pass
            try:
                if name == "deck":
                    r = view.render(t, console.size.width)
                elif name in ("events", "logs"):
                    r = view.render(t, console.size.height, console.size.width)
                else:
                    r = view.render(t, console.size.height)
            except Exception as e:  # noqa: BLE001 - never die on one bad frame
                r = Text(f"  (view error: {type(e).__name__}: {e})", style=BAD)
            live.update(r, refresh=True)
            time.sleep(1 / fps)


TMUX_CONF = """set -g mouse on
set -g status-style "bg=default,fg=#6b7280"
set -g status-left "#[bg=#5eead4,fg=#0b0f14,bold] ◉ ARGUS #[default] "
set -g status-right "#[fg=#9ca3af]%H:%M #[fg=#6b7280]· #h "
set -g status-left-length 20
set -g pane-border-style "fg=#1f2937"
set -g pane-active-border-style "fg=#5eead4"
set -g pane-border-status top
set -g pane-border-format " #[fg=#9ca3af]#{pane_title} "
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
    for pane, title in ((deck, "argus  · p problems"), (ari, "ari"), (events, "timeline  · +/- zoom"),
                        (logs, "logs  · l f / ↑↓ Enter")):
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
