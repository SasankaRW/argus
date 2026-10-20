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
GRADIENT = ["#d1d5db", "#b4b9c1", "#979da7", "#7b828d", "#5f6673", "#4b5563"]  # the wordmark at rest: light to dim
GRADIENT_ON = ["#99f6e4", "#7ef0dc", "#5eead4", "#3fbfae", "#2a9488", "#1f6f66"]  # while Ari works


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


def blend(a: str, b: str, f: float) -> str:
    """The colour between hex `a` and `b` at `f` (0..1)."""
    f = max(0.0, min(1.0, f))
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * f):02x}" for x, y in zip(pa, pb, strict=True))


def ramp(a: str, b: str, n: int = 8) -> list[str]:
    return [blend(a, b, i / max(1, n - 1)) for i in range(n)]


DEEP = "#134e4a"  # the accent at its darkest, where the gradients start
TEAL_RAMP = ramp(DEEP, ACCENT)
GREY_RAMP = ramp("#2a313b", MID)
LINE = "#334155"  # the frames: just visible


def gspark(Text: Any, values: list[float], top: float = 100.0, colours: list[str] | None = None) -> Any:
    """A sparkline whose bars shade from dark (low) to bright (high)."""
    colours = colours or TEAL_RAMP
    t = Text()
    for v in values:
        f = max(0.0, min(1.0, v / max(top, 1e-9)))
        t.append(SPARK[min(7, int(f * 7.99))], style=colours[min(len(colours) - 1, 2 + int(f * (len(colours) - 2.01)))])
    return t


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def chip(Text: Any, label: str, colour: str = ACCENT, fg: str = "#0b0f14") -> Any:
    """A rounded label: ▐ label ▌ (half blocks make the ends round)."""
    t = Text()
    t.append("▐", style=colour)
    t.append(f" {label} ", style=f"bold {fg} on {colour}")
    t.append("▌", style=colour)
    return t


def meter(Text: Any, frac: float, width: int, colours: list[str] | None = None) -> Any:
    """A thin gauge, ━━━━━━━━──── with the filled part shading from dark to bright."""
    colours = colours or TEAL_RAMP
    t = Text()
    filled = round(max(0.0, min(1.0, frac)) * width)
    for i in range(width):
        if i < filled:
            t.append("━", style=colours[min(len(colours) - 1, int(i / max(1, width) * len(colours)))])
        else:
            t.append("─", style=LINE)
    return t


def card(body: Any, title: str | None = None, height: int | None = None, hint: str | None = None,
         main: bool = False) -> Any:
    """A rounded panel with a quiet title (the modern look of every box in the console). `main`: a pane's own
    frame, its title led by an accent dot."""
    from rich import box
    from rich.panel import Panel
    from rich.text import Text

    head = None
    if title:
        head = Text(" ")
        if main:
            head.append("● ", style=ACCENT)
        head.append(f"{title} ", style=f"bold {TEXT if main else MID}")
    return Panel(body, box=box.ROUNDED, border_style=LINE, padding=(0, 1), height=height,
                 title=head, title_align="left",
                 subtitle=Text(f" {hint} ", style=FAINT) if hint else None, subtitle_align="right")


EIGHTHS = " ▁▂▃▄▅▆▇█"


def bars(Text: Any, buckets: list[dict], stack: list[tuple[str, str]], height: int, width: int,
         start: float | None = None) -> Any:
    """A stacked bar chart, one bar per bucket (an hour), `stack` bottom to top as (key, colour), scaled to the
    tallest bar, with the hours under it when `start` is given. Partial cells use eighth blocks."""
    out = Text()
    n = max(1, len(buckets))
    cw = max(1, width // n)
    pad = " " * max(0, width - cw * n)
    top = max([sum(int(b.get(k) or 0) for k, _ in stack) for b in buckets] + [1])
    scale = height * 8 / top
    cols = []
    for b in buckets:
        cum, segs = 0.0, []
        for k, colour in stack:
            v = int(b.get(k) or 0)
            if v:
                cum += v * scale
                segs.append((max(1.0, cum) if not segs else cum, colour))  # anything at all shows at least 1/8
        cols.append(segs)
    for row in range(height - 1, -1, -1):
        out.append(pad)
        lo, hi = row * 8, (row + 1) * 8
        for segs in cols:
            total = segs[-1][0] if segs else 0
            if total <= lo:
                out.append(("·" if row == 0 else " ").ljust(cw), style=FAINT)
                continue
            mid = lo + 4 if total >= hi else total - 0.01
            colour = next((c for top_, c in segs if top_ > mid), segs[-1][1])
            shade = TEAL_RAMP if colour == ACCENT else GREY_RAMP if colour == MID else None
            if shade:  # the bars shade from dark at the bottom to bright at the top
                colour = shade[min(7, 2 + int(row / max(1, height - 1) * 5.99))]
            ch = "█" if total >= hi else EIGHTHS[max(1, int(round(total - lo)))]
            out.append((ch * cw)[:max(1, cw - 1)] + (" " if cw > 1 else ""), style=colour)
        out.append("\n")
    if start is not None:
        axis = [" "] * (cw * n)
        for i in range(n):
            hr = time.localtime(start + i * 3600).tm_hour
            if hr % (3 if cw >= 2 else 6) == 0:
                for j, ch in enumerate(f"{hr:02d}"):
                    if i * cw + j < len(axis):
                        axis[i * cw + j] = ch
        out.append(pad + "".join(axis) + "\n", style=FAINT)
    return out


def lines_of(renderable: Any, width: int) -> int:
    """How many terminal lines `renderable` takes at `width`."""
    import io

    from rich.console import Console

    c = Console(width=max(10, width), file=io.StringIO(), color_system=None)
    return len(c.render_lines(renderable, c.options, pad=False))


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
        self.activity: dict[str, Any] = {}
        self._activity_at = -1e9
        feed = Feed(api, "ari.state,ari.listening,step.running", backlog=1)
        feed.subs.append(lambda evs: self.ari.apply(evs, time.monotonic()))
        feed.start()
        threading.Thread(target=self._poll, daemon=True, name="deck").start()

    def _poll(self) -> None:
        while True:
            d: dict[str, Any] = {}
            for key, path in (("status", "/status"), ("power", "/power"), ("models", "/models/ollama"),
                              ("ears", "/ari/listener"), ("approvals", "/approvals?state=pending"),
                              ("schedules", "/schedules"), ("problems", "/problems?hours=24"),
                              ("running", "/jobs?state=running&limit=6"), ("calls", "/models/calls?limit=80"),
                              ("game", "/ari/game")):
                try:
                    d[key] = self.api.get(path)
                except Exception:  # noqa: BLE001
                    d[key] = None
            if time.monotonic() - self._activity_at > 30:  # the hour-by-hour chart: every half minute is plenty
                try:
                    self.activity = self.api.get("/timeline/activity?hours=24") or {}
                    self._activity_at = time.monotonic()
                except Exception:  # noqa: BLE001 - an older argusd
                    pass
            try:
                import psutil

                self.cpu.append(psutil.cpu_percent(interval=None))
                vm = psutil.virtual_memory()
                d["host"] = {"mem": vm.percent, "temp": _temp(psutil)}
            except Exception:  # noqa: BLE001 - psutil missing
                d["host"] = {}
            self.data = d
            time.sleep(2)

    def render(self, t: float, width: int = 120, height: int | None = None) -> Any:
        """The logo and the status; with `height`, the rest of the pane is filled: Ari's last 24 hours as a chart
        (it grows to fill), what runs now beside the models, and what isn't working beside what comes next."""
        Console, Group, Live, Panel, Table, Text = _rich()
        self._t = t
        game = self.data.get("game") or {}
        if game.get("on") and width >= 46:
            return self.game_screen(Text, t, width, height, game)
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
                logo.append(w, style=f"bold {GRADIENT_ON[i] if phase not in ('idle', 'off') else GRADIENT[i]}")
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
        hl.append_text(gspark(Text, list(self.cpu)[-24:]))  # the cpu over the last minute
        hl.append(f" cpu {self.cpu[-1]:>3.0f}%  ", style=TEXT)
        if host.get("mem") is not None:
            hl.append_text(meter(Text, host["mem"] / 100, 14))
            hl.append(f" mem {host['mem']:.0f}%", style=TEXT)
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
        if height:  # the clock goes on the tag line, to leave room for the panels under the status
            tag.append(time.strftime("   %H:%M:%S · %a %d %b"), style=f"bold {TEXT}")
        parts = [logo, tag] + ([] if height else [Text("")]) + [card(grid, "status")]
        if self.show_problems and pr.get("problems"):
            parts.append(card(problem_lines(Text, pr["problems"]), "not working · last 24 h", hint="p closes"))
        if not height:
            parts.append(clock)
        if height:
            parts += self.fill(height - lines_of(Group(*parts), width), width, pr)
        return Group(*parts)

    # ---- game mode: the whole deck while you play

    GAME_WORD = ["▄▀▀ ▄▀▄ █▄ ▄█ █▀▀   █▄ ▄█ ▄▀▄ █▀▄ █▀▀",
                 "█ ▄ █▀█ █ ▀ █ █▀▀   █ ▀ █ █ █ █ █ █▀▀",
                 "▀▀▀ ▀ ▀ ▀   ▀ ▀▀▀   ▀   ▀  ▀  ▀▀  ▀▀▀"]
    PURPLE = ["#ddd6fe", "#c4b5fd", "#a78bfa", "#8b5cf6", "#7c3aed", "#6d28d9"]

    def game_screen(self, Text: Any, t: float, width: int, height: int | None, g: dict) -> Any:
        out = Text("\n")
        for i, line in enumerate(self.GAME_WORD):
            out.append("  ")
            for j, ch in enumerate(line):  # a slow shimmer across the word
                k = int((j / len(line) * 4 + t * 0.8 + i) % len(self.PURPLE))
                out.append(ch, style=f"bold {self.PURPLE[k]}")
            out.append("\n")
        since = g.get("since")
        out.append("\n  ")
        out.append(g.get("app") or "game mode", style=f"bold {TEXT}")
        if since:
            out.append(f"  ·  on for {ago(max(0, time.time() - float(since)))}", style=MID)
        out.append(f"  ·  {'by itself' if g.get('by') == 'auto' else 'turned on by you'}\n\n", style=MID)
        freed, used = g.get("freed_mb"), g.get("used_mb")
        if freed is not None:
            total = float(freed) + float(used or 0)
            out.append("  vram freed  ", style=MID)
            out.append_text(meter(Text, float(freed) / max(1.0, total), max(12, min(36, width - 40)),
                                  list(reversed(self.PURPLE))))
            out.append(f"  {freed / 1024:.1f} GB", style=f"bold {TEXT}")
            if used is not None:
                out.append(f"  ·  {used / 1024:.1f} GB still in use", style=DIM)
            out.append("\n\n")
        else:
            out.append("  freeing the GPU…\n\n", style=DIM)
        rows = [("ari listening", "paused"), ("ari's voice", "stopped"), ("ollama", "stopped")]
        rows += [(m, "unloaded") for m in (g.get("unloaded") or [])[:4]]
        for name, st in rows:
            out.append("  ◌ ", style=self.PURPLE[4])
            out.append(f"{name[:24]:<26}", style=DIM)
            out.append(f"{st}\n", style=self.PURPLE[2])
        still = g.get("still") or []
        if still:
            out.append("\n  still on the GPU  ", style=MID)
            out.append(", ".join(still[:5]) + "\n", style=TEXT)
        lines = len(out.plain.splitlines()) + 2
        if height and freed is not None and height - lines >= 5:  # the GPU's memory, big: free for the game vs in use
            total = float(freed) + float(used or 0)
            cells = max(10, width - 6)
            free_n = round(float(freed) / max(1.0, total) * cells)
            tall = min(6, height - lines - 3)
            out.append("\n")
            for r in range(tall):
                out.append("  ")
                for c in range(cells):
                    col = self.PURPLE[min(5, 1 + (c * 4) // max(1, free_n) + (r * 2) // max(1, tall))] \
                        if c < free_n else "#374151"
                    out.append("█", style=col)
                out.append("\n")
            out.append(f"  {'free for the game':<{max(18, free_n - 2)}}", style=self.PURPLE[2])
            out.append("in use\n", style=DIM)
        when = "ends a minute after the game closes" if g.get("by") == "auto" else "on until you turn it off"
        out.append(f"\n  {when} · the island's game key ends it now", style=FAINT)
        return out

    # ---- the rest of the pane

    def fill(self, room: int, width: int, pr: dict) -> list[Any]:
        """Panels for the space under the status, so the pane is never half empty."""
        Console, Group, Live, Panel, Table, Text = _rich()
        out: list[Any] = []
        if room < 6:
            return out
        chart_min = 6  # the chart's panel at its smallest: totals, 2 rows of bars, the hours

        def pair(a: tuple[str, Any], b: tuple[str, Any], rows: int) -> Any:
            g = Table.grid(expand=True, padding=(0, 1))
            g.add_column(ratio=1)
            g.add_column(ratio=1)
            for body in (a[1], b[1]):
                body.no_wrap, body.overflow = True, "ellipsis"
            g.add_row(*[card(body, title, height=rows + 2) for title, body in (a, b)])
            return g

        half = max(20, (width - 1) // 2 - 4)
        spare = room - chart_min
        rows = 3 if spare < 20 else min(6, 3 + (spare - 20) // 4)
        lower = []
        if spare >= rows + 2:
            lower.append(pair(("now", self.now_text(Text, rows, half)),
                              ("models · last 80 calls", self.models_text(Text, rows, half)), rows))
            spare -= rows + 2
        if spare >= rows + 2 + 2:  # the second row only with room for a taller chart too
            lower.append(pair(("not working · 24 h", self.problems_text(Text, pr, rows, half)),
                              ("coming up", self.next_text(Text, rows, half)), rows))
            spare -= rows + 2
        chart_h = chart_min + spare - 2  # the chart takes whatever is left (its panel is chart_h + 2 lines)
        out.append(card(self.chart(Text, chart_h, width - 4), "ari · last 24 h", height=chart_h + 2))
        return out + lower

    def chart(self, Text: Any, height: int, width: int) -> Any:
        act = self.activity or {}
        tot = act.get("total") or {}
        out = Text()
        out.append(f"✓ {tot.get('ok', 0)} answered   ", style=ACCENT)
        out.append(f"✗ {tot.get('failed', 0)} failed   ", style=BAD if tot.get("failed") else DIM)
        out.append(f"· {tot.get('ignored', 0)} ignored", style=MID)
        if tot.get("answer_ms") is not None:
            out.append(f"   ~{tot['answer_ms'] / 1000:.1f}s to answer", style=DIM)
        out.append(f"   jobs ✓ {tot.get('jobs_ok', 0)}", style=DIM)
        if tot.get("jobs_failed"):
            out.append(f" ✗ {tot['jobs_failed']}", style=BAD)
        out.append("\n")
        buckets = act.get("ari") or [{} for _ in range(24)]
        out.append_text(bars(Text, buckets, [("ok", ACCENT), ("failed", BAD), ("ignored", FAINT)], max(1, height - 2),
                             width, act.get("start")))
        out.rstrip()
        return out

    def now_text(self, Text: Any, rows: int, width: int) -> Any:
        out = Text()
        running = self.data.get("running") or []
        now = time.time()
        spin = SPINNER[int(getattr(self, "_t", 0) * 8) % len(SPINNER)]
        for j in running[:rows]:
            out.append(f"{spin} ", style=ACCENT)
            out.append(f"{j.get('plugin')}·{j.get('workflow')}"[:24].ljust(25), style=TEXT)
            out.append(f"{ago(max(0, now - float(j.get('created_at') or now)))}".rjust(6), style=MID)
            who = str(j.get("lease_owner") or "")
            if who:
                out.append(f"  {who[:max(0, width - 35)]}", style=DIM)
            out.append("\n")
        if not running:
            out.append("nothing running\n", style=DIM)
        jobs = ((self.data.get("status") or {}).get("jobs") or {})
        left = rows - min(len(running), rows) - 1
        if left >= 0:
            out.append(f"{jobs.get('queued', 0)} queued · {jobs.get('waiting', 0)} waiting for you", style=DIM)
        out.rstrip()
        return out

    def models_text(self, Text: Any, rows: int, width: int) -> Any:
        calls = ((self.data.get("calls") or {}).get("calls")) or []
        by: dict[str, list[dict]] = {}
        for c in calls:
            by.setdefault(str(c.get("model") or c.get("tier") or "?"), []).append(c)
        out = Text()
        if not by:
            out.append("no model calls yet", style=DIM)
            return out
        for name, cs in sorted(by.items(), key=lambda kv: -len(kv[1]))[:rows]:
            lat = [float(c["latency_ms"]) for c in cs if c.get("latency_ms") is not None]
            bad = sum(1 for c in cs if c.get("verdict") != "ok")
            out.append(f"{name[:18]:<19}", style=TEXT)
            out.append(f"{len(cs):>3}×", style=MID)
            if lat:
                out.append(f" {sum(lat) / len(lat) / 1000:4.1f}s ", style=DIM)
                n = max(4, width - 38)
                out.append_text(gspark(Text, lat[:n][::-1], max(lat)))
                out.append(" " * max(0, n - len(lat[:n])))
            if bad:
                out.append(f" ✗{bad}", style=WARN)
            out.append("\n")
        out.rstrip()
        return out

    def problems_text(self, Text: Any, pr: dict, rows: int, width: int) -> Any:
        out = Text()
        probs = pr.get("problems") or []
        if not probs:
            out.append("✓ nothing failed today", style=ACCENT)
            return out
        for g in probs[:rows]:
            out.append(f"×{g.get('count', 1):<3}", style=BAD)
            out.append(f"{str(g.get('area') or '')[:20]:<21}", style=TEXT)
            out.append(f"{str(g.get('what') or '')[:max(8, width - 26)]}\n", style=MID)
        out.rstrip()
        return out

    def next_text(self, Text: Any, rows: int, width: int) -> Any:
        out = Text()
        now = time.time()
        nxt = sorted([s for s in (self.data.get("schedules") or []) if s.get("enabled") and s.get("next_run_at")],
                     key=lambda s: s["next_run_at"])
        if not nxt:
            out.append("nothing scheduled", style=DIM)
            return out
        for s in nxt[:rows]:
            out.append(f"in {ago(max(0, s['next_run_at'] - now)):<8}", style=ACCENT)
            out.append(f"{s.get('plugin', '')}·{s.get('workflow', '')}"[:max(8, width - 12)] + "\n", style=TEXT)
        out.rstrip()
        return out

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
    """The conversation with Ari, live. On top, by what Ari is doing:
    - listening, speaking or idle: a waveform of the PC's microphone (Ari's own voice while it speaks), how English
      the last thing heard was, and your words typing in as they're heard;
    - thinking or working: the request as steps (heard → understood → the tools → answer) lighting up as they
      happen, and the model answering now with its reply arriving.
    Under it, the conversation."""

    def __init__(self, api: Api):
        self.api = api
        self.ari = AriState()
        self.turns: list[dict] = []
        self.earlier: list[dict] = []  # the conversations before this one, for when this one is short
        self.live: dict = {}  # GET /live: the voice levels and the model answering now
        self.trace: dict | None = None  # the request being worked on (its job's steps so far)
        feed = Feed(api, "ari.state,ari.listening,step.running", backlog=1)
        feed.subs.append(lambda evs: self.ari.apply(evs, time.monotonic()))
        feed.start()
        threading.Thread(target=self._poll, daemon=True, name="ari").start()
        threading.Thread(target=self._poll_live, daemon=True, name="ari-live").start()

    def _poll(self) -> None:
        while True:
            try:
                chats = self.api.get("/ari-chats")
                if chats:
                    self.turns = (self.api.get(f"/ari/{chats[0]['conv']}") or {}).get("turns") or []
                    if len(self.turns) < 12 and len(chats) > 1:
                        earlier: list[dict] = []
                        for c in chats[1:4]:
                            earlier = ((self.api.get(f"/ari/{c['conv']}") or {}).get("turns") or []) + earlier
                        self.earlier = earlier
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2)

    def busy(self) -> bool:
        return not self.ari.off and self.ari.phase not in ("idle", "done")

    def _poll_live(self) -> None:
        n = 0
        while True:
            n += 1
            try:
                self.live = self.api.get("/live") or {}
            except Exception:  # noqa: BLE001 - an older argusd
                self.live = {}
            job = self.ari.think_job
            if job and self.ari.phase in ("thinking", "working", "speaking") and n % 3 == 0:
                try:
                    self.trace = self.api.get(f"/ari/trace/{urllib.parse.quote(job)}")
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(0.2 if self.busy() else 1.0)

    # ---- the top: voice

    def wave(self, Text: Any, t: float, width: int, rows: int = 2) -> list[Any]:
        """`rows` rows of bars up from the middle line, and as many of their dimmer reflection under it."""
        v = (self.live or {}).get("voice") or {}
        fresh = time.time() - float(v.get("at") or 0) < 3
        speaking = fresh and v.get("speaking") and v.get("out")
        levels = list(v.get("out") if speaking else v.get("mic") or []) if fresh else []
        levels = ([0.0] * max(0, width - len(levels)) + levels)[-width:]
        colours = ramp("#1f6f66", "#ccfbf1") if speaking else TEAL_RAMP
        if not fresh or self.ari.off:
            colours = GREY_RAMP
        ups = [Text() for _ in range(rows)]
        downs = [Text() for _ in range(rows)]
        for x in levels:
            x = max(0.0, min(1.0, x))
            h = x * rows * 8  # in eighths of a cell
            col = colours[min(len(colours) - 1, 1 + int(x * (len(colours) - 1.01)))]
            dim = blend(col, "#0b0f14", 0.5)
            for r in range(rows):  # r = 0 next to the middle line
                a = max(0.0, min(8.0, h - r * 8))
                if r == 0 and a < 1:
                    ups[r].append("▁" if fresh else "·", style=LINE)
                else:
                    ups[r].append(EIGHTHS[int(a)] if a >= 1 else " ", style=col)
                downs[r].append("█" if a >= 6 else "▀" if a >= 3 else "▔" if a >= 1 else " ", style=dim)
        return [*reversed(ups), *downs]

    def gauges(self, Text: Any) -> Any:
        v = (self.live or {}).get("voice") or {}
        out = Text()
        eng = v.get("english")
        if isinstance(eng, (int, float)):
            out.append("   English ", style=DIM)
            out.append(f"{eng:.0%} ", style=TEXT if eng >= 0.5 else WARN)
            out.append_text(meter(Text, float(eng), 10))
        mic = v.get("mic") or []
        if mic and time.time() - float(v.get("at") or 0) < 3:
            out.append("   level ", style=DIM)
            out.append_text(meter(Text, max(mic[-6:]), 10, ramp("#134e4a", "#99f6e4")))
        return out

    # ---- the top: the request as it happens

    @staticmethod
    def stage_name(what: str, done: bool) -> str:
        w = what.strip()
        if w.startswith("decide"):
            return "understood" if done else "understanding"
        if w.startswith("tool ") and ": " in w:
            return w.split(": ", 1)[1].replace("_", " ")
        if w in ("reply", "answer", "say"):
            return "answer"
        return w.replace("_", " ")

    def stages(self) -> list[dict]:
        """heard ✓, the job's steps (done ✓, failed ✗, running ⠋), then speak (waiting until Ari speaks)."""
        tr = self.trace or {}
        out = [{"name": "heard", "state": "done", "sub": ""}]
        models: dict[str, str] = {}
        for s in tr.get("steps") or []:
            if s.get("kind") == "model":
                what = str(s.get("what") or "")
                if "(" in what:
                    models[what.rsplit("(", 1)[1].rstrip(")")] = what.split(" (", 1)[0]
        for s in tr.get("steps") or []:
            if s.get("kind") != "step":
                continue
            running = s.get("ms") is None and s.get("ok", True)
            state = "running" if running else "done" if s.get("ok", True) else "failed"
            what = str(s.get("what") or "")
            sub = " · ".join(x for x in (models.get(what, ""), f"{s['ms'] / 1000:.1f} s" if s.get("ms") else "") if x)
            out.append({"name": self.stage_name(what, state == "done"), "state": state, "sub": sub})
        phase = self.ari.phase
        if not any(st["state"] == "running" for st in out) and phase in ("thinking", "working"):
            out.append({"name": "…", "state": "running", "sub": ""})
        out.append({"name": "speak", "state": "running" if phase == "speaking" else
                    "done" if phase in ("done", "following") else "wait", "sub": ""})
        return out

    def pipeline(self, Text: Any, t: float, width: int) -> list[Any]:
        spin = SPINNER[int(t * 8) % len(SPINNER)]
        sts = self.stages()
        icon = {"done": "✓", "failed": "✗", "running": spin, "wait": "○"}
        colour = {"done": ACCENT, "failed": BAD, "running": TEXT, "wait": LINE}

        def w(st: dict) -> int:
            return len(f" {icon[st['state']]} {st['name']} ") + 2 + 2

        while len(sts) > 3 and sum(w(x) for x in sts) > width:  # the oldest finished steps go first
            k = next((i for i, x in enumerate(sts[1:-1], 1) if x["state"] == "done"), 1)
            sts.pop(k)
        rows = [Text(), Text(), Text(), Text()]
        for i, st in enumerate(sts):
            col, label = colour[st["state"]], f" {icon[st['state']]} {st['name'][:24]} "
            line = col if st["state"] != "wait" else LINE
            rows[0].append("╭" + "─" * len(label) + "╮", style=line)
            rows[1].append("│", style=line)
            rows[1].append(label, style=f"bold {col}" if st["state"] != "wait" else FAINT)
            rows[1].append("│", style=line)
            rows[2].append("╰" + "─" * len(label) + "╯", style=line)
            rows[3].append(st["sub"][:len(label) + 2].ljust(len(label) + 2), style=DIM)
            if i < len(sts) - 1:
                link = "━━" if st["state"] == "done" else "──"
                rows[0].append("  ")
                rows[1].append(link, style=TEAL_RAMP[5] if st["state"] == "done" else LINE)
                rows[2].append("  ")
                rows[3].append("  ")
        return rows

    def model_line(self, Text: Any, t: float, width: int) -> list[Any]:
        """The model answering now (N): which, for what step, how fast, and its reply arriving."""
        live = self.live or {}
        m = live.get("model") or {}
        if not m or time.time() - float(live.get("model_at") or 0) > 20:
            return []
        if self.ari.think_job and m.get("job_id") and m.get("job_id") != self.ari.think_job:
            return []
        head, body = Text(), Text()
        state = m.get("state")
        ms = m.get("ms") or 0
        if state == "end":
            ok = m.get("verdict") == "ok"
            head.append("✓ " if ok else "✗ ", style=ACCENT if ok else BAD)
        else:
            head.append(SPINNER[int(t * 8) % len(SPINNER)] + " ", style=ACCENT)
        head.append(str(m.get("model") or m.get("tier") or "model"), style=f"bold {TEXT}")
        head.append(f"  {m.get('step') or ''}", style=DIM)
        if ms:
            head.append(f"  {ms / 1000:.1f} s", style=MID)
        text = str(m.get("text") or "")
        toks = m.get("tokens_out") or (len(text) / 4 if text else 0)
        if ms and toks:
            head.append(f"  {toks / max(ms / 1000, 0.05):.0f} tok/s", style=ACCENT)
        if state == "end" and m.get("verdict") != "ok":
            head.append(f"  {m.get('verdict')}: {str(m.get('why') or '')[:60]}", style=BAD)
        if text:
            room = max(10, width - 4)
            shown = " ".join(text.split())
            shown = shown if len(shown) <= room else "…" + shown[-(room - 1):]
            body.append("  " + shown, style=MID if state == "end" else TEXT)
            if state != "end":
                body.append("▍", style=ACCENT if int(t * 2) % 2 else LINE)
        return [head, body] if text else [head]

    _typed, _typed_at = "", 0.0

    def typed(self, target: str, t: float, per_s: float = 45) -> str:
        """`target` typing in: what was shown stays, new words appear a few letters a frame."""
        keep = 0
        while keep < min(len(self._typed), len(target)) and self._typed[keep] == target[keep]:
            keep += 1
        n = min(len(target), keep + max(1, int((t - self._typed_at) * per_s)))
        self._typed, self._typed_at = target[:n], t
        return self._typed

    # ---- all of it

    def render(self, t: float, height: int, width: int = 100) -> Any:
        Console, Group, Live, Panel, Table, Text = _rich()
        phase = "off" if self.ari.off else self.ari.phase
        colour = PHASE_COLOUR.get(phase, ACCENT)
        head = Text()
        spin = SPINNER[int(t * 8) % len(SPINNER)] + " " if phase in ("thinking", "working") else ""
        head.append_text(chip(Text, f"{spin}{phase}", colour if phase not in ("idle", "off") else LINE,
                              fg="#0b0f14" if phase not in ("idle", "off") else MID))
        working = phase in ("thinking", "working")
        top: list[Any] = []
        if working:
            if self.ari.text:
                head.append(f"  \"{self.ari.text[:max(10, width - 24)]}\"", style=f"bold {TEXT}")
            if height >= 12:
                top = [*self.pipeline(Text, t, width), *self.model_line(Text, t, width)]
            else:
                top = self.model_line(Text, t, width)
        else:
            head.append_text(self.gauges(Text))
            if height >= 8:
                top = self.wave(Text, t, width, rows=2 if height >= 14 else 1)

        def turns_text(turns: list[dict], quiet: bool) -> Any:
            out = Text()
            for tr in turns[-40:]:
                who = tr.get("role")
                when = time.strftime("%H:%M", time.localtime(tr.get("created_at") or time.time()))
                text = tr.get("text") or "…"
                if who == "you":
                    out.append(f"\n {when}  you ", style=FAINT if quiet else DIM)
                    out.append(f"{text}\n", style=MID if quiet else f"bold {TEXT}")
                else:
                    out.append(f" {when}  ari ", style=FAINT if quiet else ACCENT2)
                    out.append(f"{text}\n", style=DIM if quiet else ACCENT)
            return out

        body = turns_text(self.turns, quiet=False)
        if phase in ("listening", "following") and self.ari.text:  # your words as they're heard
            shown = self.typed(self.ari.text, t)
            body.append(f"\n {time.strftime('%H:%M')}  you ", style=DIM)
            body.append(shown, style=f"bold {TEXT}")
            body.append("▍", style=ACCENT if int(t * 2) % 2 else LINE)
            body.append("\n")
        room = height - 1 - len(top) - (1 if top else 0)
        if self.earlier and len(body.plain.splitlines()) < room:  # room: the earlier talks, quieter
            older = turns_text(self.earlier, quiet=True)
            older.append("\n ── now " + "─" * 40 + "\n", style=FAINT)
            older.append_text(body)
            body = older
        lines = body.split("\n")
        keep = max(3, room)
        if len(lines) > keep:  # the newest lines that fit
            body = Text("\n").join(lines[-keep:])
        if not self.api.up and not self.turns:
            body = offline(Text, self.api)
        return Group(head, *top, *([Text("")] if top else []), body)


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
        self.activity: dict[str, Any] = {}
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
            n = getattr(self, "_n", 0)
            self._n = n + 1
            if n % 6 == 0:
                try:
                    self.activity = self.api.get("/timeline/activity?hours=24") or {}
                except Exception:  # noqa: BLE001
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
                    ch = (label[k - 1] if b - a >= len(label) + 2 and 1 <= k <= len(label) else
                          "━" if a == b else "╺" if x == a else "╸" if x == b else "━")
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
        out.append_text(chip(Text, f"last {self.minutes} min", LINE, fg=TEXT))
        out.append("  a lane per machine\n", style=FAINT)
        lanes = self.lane_text(Text, width)
        out.append_text(lanes)
        used = 1 + len(lanes.plain.splitlines())
        # the jobs of the last 24 hours, hour by hour; it takes the room the events don't need
        chart_h = max(3, min(10, height - used - len(self.rows) - 3))
        act = self.activity or {}
        tot = act.get("total") or {}
        out.append("\n")
        out.append_text(chip(Text, "jobs · 24 h", LINE, fg=TEXT))
        out.append(f"  ✓ {tot.get('jobs_ok', 0)}", style=DIM)
        out.append(f"  ✗ {tot.get('jobs_failed', 0)}\n", style=BAD if tot.get("jobs_failed") else DIM)
        out.append_text(bars(Text, act.get("jobs") or [{} for _ in range(24)], [("ok", MID), ("failed", BAD)],
                             chart_h, max(24, width - 2), act.get("start")))
        used += 2 + chart_h + 1
        out.append("\n")
        for e in list(self.rows)[-(max(3, height - used - 1)):]:
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
        line.append("▌" if selected else "▏", style=ACCENT if selected else col)
        line.append(time.strftime("%H:%M:%S ", time.localtime(r.get("at") or 0)), style=MID if selected else FAINT)
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
        out.append_text(chip(Text, self.mode))
        for flag in (["failures only"] if self.failures_only else []) + ([f"/{self.search}"] if self.search else []):
            out.append(" ")
            out.append_text(chip(Text, flag, LINE, fg=TEXT))
        out.append("   l switch · f failures · / search · ↑↓ ⏎ trace\n", style=FAINT)
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
            shown = rows[-max(3, room):]
            for i, r in enumerate(shown):
                idx_from_newest = len(shown) - 1 - i
                line = self.story_line(Text, r, idx_from_newest == sel)
                line.truncate(max(20, width - 1), overflow="ellipsis")  # one line each, never wrapped
                out.append_text(line)
                out.append("\n")
            spare = room + 1 - max(1, len(shown))  # the header, the requests, the divider, then this
            if not self.trace and spare >= 3:  # room left: the newest log lines under the requests
                out.append("╶─ latest log lines " + "─" * max(0, width - 21) + "\n", style=LINE)
                for line in self.log_texts(Text, spare):
                    line.truncate(max(20, width - 1), overflow="ellipsis")
                    out.append_text(line)
                    out.append("\n")
            if self.trace:
                out.append_text(tr)
            return out
        for line in self.log_texts(Text, room):
            out.append_text(line)
            out.append("\n")
        return out

    def log_texts(self, Text: Any, room: int) -> list[Any]:
        """The newest `room` log lines and model calls, one Text each (filters and search apply)."""
        lines: list[Any] = []
        n = self.search.lower()
        for at, item in self.log_lines()[-(room * 3):]:
            out = Text()
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
                lines.append(out)
                continue
            r = item["log"]
            col, lv = LEVEL.get(str(r.get("level") or "info").lower(), (TEXT, "INF"))
            if self.failures_only and lv not in ("WRN", "ERR", "CRT"):
                continue
            if n and n not in json.dumps(r, default=str).lower():
                continue
            out.append(time.strftime(" %H:%M:%S ", time.localtime(at)) if at else "          ", style=FAINT)
            out.append(f"{lv} ", style=f"bold {col if lv != 'INF' else DIM}")
            out.append(f"{str(r.get('source') or '')[:7]:<7} ", style=DIM)
            out.append(f"{str(r.get('logger') or '').replace('argus.', '')[:14]:<14} ", style=FAINT)
            out.append(str(r.get("msg") or "")[:110], style=col if lv != "INF" else TEXT)
            extra = r.get("extra") or {}
            keep = [k for k in ("heard", "problem", "error", "why", "reply", "tools", "text", "took_ms") if k in extra]
            keep += [k for k in extra if k not in keep][: max(0, 4 - len(keep))]
            if keep:
                out.append("  " + " ".join(f"{k}={extra[k]}" for k in keep)[:100], style=DIM)
            lines.append(out)
        return lines[-room:] if room > 0 else []


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


FRAME = {"deck": ("argus", "p problems"), "ari": ("ari", None), "events": ("timeline", "+/- zoom"),
         "logs": ("logs", "l  f  /  ↑↓  ⏎")}


def frame(name: str, view: Any, t: float, width: int, height: int) -> Any:
    """One pane: the view inside a rounded frame with its name on top and its keys at the bottom."""
    w, h = max(10, width - 4), max(3, height - 2)
    if name == "deck":
        r = view.render(t, w, h)
    else:
        r = view.render(t, h, w)
    title, hint = FRAME[name]
    return card(r, title, height=height, hint=hint, main=True)


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
                r = frame(name, view, t, console.size.width, console.size.height)
            except Exception as e:  # noqa: BLE001 - never die on one bad frame
                r = Text(f"  (view error: {type(e).__name__}: {e})", style=BAD)
            live.update(r, refresh=True)
            time.sleep(1 / fps)


TMUX_CONF = """set -g mouse on
set -g status-style "bg=default,fg=#6b7280"
set -g status-left "#[bg=#5eead4,fg=#0b0f14,bold] ◉ ARGUS #[default] "
set -g status-right "#[fg=#9ca3af]%H:%M #[fg=#6b7280]· #h "
set -g status-left-length 20
# each view draws its own rounded frame: tmux's lines between the panes stay almost invisible
set -g pane-border-style "fg=#161a20"
set -g pane-active-border-style "fg=#1f6f66"
set -g pane-border-status off
# bg=default, not a colour: the terminal's own background shows through (kitty's background_opacity)
set -g window-style "bg=default"
set -g window-active-style "bg=default"
set -g default-terminal "tmux-256color"
set -ga terminal-overrides ",*:Tc"
"""


def run_all(argv: list[str]) -> int:
    """All four views in one tmux window: the deck (logo, eye, status) top left, Ari top right (a short strip), events
    bottom left and logs bottom right (most of the right side)."""
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
    logs = tmux("split-window", "-v", "-t", ari, "-l", "70%", "-P", "-F", "#{pane_id}", cmd("logs"))
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
