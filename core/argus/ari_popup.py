"""Ari's island on the PC: a black shape grown out of the top edge of the main screen, over every app.

    python -m argus.ari_popup     (dev.ps1 up starts it when ari.popup is on; needs: pip install -e .[popup])

Native Qt (no browser inside): a few MB of memory, and nothing runs while it sits idle. It follows Ari through
Argus's event stream (a light poll):
  idle     a slim lip under the edge; hover shows "ari"
  active   it widens: listening / thinking / working / speaking, with what Ari is on
  done     the answer, two lines, then it folds back
  click    it opens down: the time, Talk (the PC's "Hey Ari" listener starts listening), how Argus is doing, what
           waits for you, the next job, your shortcuts (set up in Helios > Ari > island). Clicking anywhere else
           folds it again. Scroll on it to flip pages: what's playing (play / pause / next), the PC's load (CPU,
           memory, GPU, disk, network), this month with today's weather.
  dots     an orange dot on the lip while an app uses the mic, a green one for the camera (Argus's own listener
           doesn't count); hover says which app, or the song that's playing
  keys     Ctrl+Alt+Space: a box to type to Ari; Ctrl+Alt+D: hide the island / bring it back (Helios > Ari >
           island to change them)
It runs on the machine with your desktop session (the PC), also after Argus itself moves to the laptop: set
ARGUS_URL to the laptop's address.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .island_live import (
    Hardware,
    Media,
    clock,
    level,
    listen_hotkeys,
    month_grid,
    position_now,
    privacy,
    rate,
    weekday_letters,
)

# ------------------------------------------------------------------ sizes (logical px)

SHOULDER_MAX = 13     # the concave curve into the screen's edge, each side
MARGIN = 28           # room round the shape for its shadow (the window is this much bigger on 3 sides)
SH_SCALE = 8          # the shadow is drawn at 1/8 size and scaled up: about 8 px of soft fade
SH_DROP = 3           # how far below the island it sits
SH_ALPHA = 120        # how dark it is at its darkest
IDLE = (84, 10)
HOVER = (138, 22)
DETAILS_W = 336
MAX_W = 430
MAX_H = 300
W, H = MAX_W + 2 * 30 + 2 * MARGIN, MAX_H + MARGIN  # the window (room for the widest shoulders)

FOLLOW_S = 10.0  # how long Ari listens for a follow-up after answering (ari.talk_idle_s)
WORDS = {"listening": "listening", "thinking": "thinking", "working": "on it", "speaking": "speaking",
         "starting": "starting"}
NICE = {"search_my_files": "searching your files", "find_file": "looking for the file", "open_app": "opening it",
        "set_volume": "setting the volume", "argus_status": "checking Argus", "add_note": "writing it down",
        "run_routine": "running the routine", "lab_status": "checking the lab", "repo_status": "checking your repos",
        "look_at_screen": "looking at your screen", "summarise_clipboard": "reading what you copied",
        "weather": "checking the weather", "web_search": "searching the web", "read_page": "reading the page",
        "search_everything": "finding the file"}
TONE = {"listening": "#39ff9c", "done": "#39ff9c", "working": "#4cc2ff", "thinking": "#f5a524",
        "speaking": "#f5a524", "idle": "#f5a524", "starting": "#6f7680"}


def placement(x: int, y: int, width: int) -> tuple[int, int, int, int]:
    """The window: top middle of the screen, touching its top edge."""
    return x + (width - W) // 2, y, W, H


def shoulder_of(h: float) -> float:
    return min(SHOULDER_MAX, h * 0.55)


def radius_of(h: float, sh: float, big: bool) -> float:
    return max(2.0, min(24.0 if big else 19.0, h - sh, h / 2))


def stretch_of(h: float) -> float:
    """How much wider than tall the curves are: small shapes get long, soft curves (a tapered notch rather than
    tiny round corners); from 24 px tall they are plain round."""
    return 1.0 + 1.6 * max(0.0, min(1.0, (24 - h) / 16))


def shoulder_w(h: float) -> float:
    return shoulder_of(h) * stretch_of(h)


def outline(w: float, h: float, big: bool) -> list[tuple]:
    """The shape as path commands (origin: its top-left, shoulders included): ("M", x, y), ("L", ...),
    ("C", c1x, c1y, c2x, c2y, x, y). Concave shoulders into the edge, round bottom corners."""
    sh = shoulder_of(h)
    r = radius_of(h, sh, big)
    st = stretch_of(h)
    sw, rx = sh * st, r * st  # horizontal extents of the shoulder and the bottom corner
    x0, x1, k = sw, sw + w, 0.55
    return [("M", 0, 0), ("L", x1 + sw, 0),
            ("C", x1 + sw * (1 - k), 0, x1, sh * k, x1, sh),
            ("L", x1, h - r),
            ("C", x1, h - r * (1 - k), x1 - rx * (1 - k), h, x1 - rx, h),
            ("L", x0 + rx, h),
            ("C", x0 + rx * (1 - k), h, x0, h - r * (1 - k), x0, h - r),
            ("L", x0, sh),
            ("C", x0, sh * k, sw * (1 - k), 0, 0, 0)]


@dataclass
class Spring:
    """A spring towards a target size: a little overshoot, then it settles."""
    w: float
    h: float
    vw: float = 0.0
    vh: float = 0.0
    k: float = 260.0
    c: float = 26.0

    def step(self, tw: float, th: float, dt: float) -> bool:
        """Advance dt seconds. True while it still moves."""
        dt = min(dt, 0.032)
        for _ in range(2):
            d = dt / 2
            self.vw += (self.k * (tw - self.w) - self.c * self.vw) * d
            self.w += self.vw * d
            self.vh += (self.k * (th - self.h) - self.c * self.vh) * d
            self.h += self.vh * d
        if abs(tw - self.w) < 0.3 and abs(th - self.h) < 0.3 and abs(self.vw) + abs(self.vh) < 2:
            self.w, self.h, self.vw, self.vh = tw, th, 0.0, 0.0
            return False
        return True


@dataclass
class AriState:
    """What Ari is doing, from the event stream (like Helios's pill)."""
    phase: str = "idle"
    text: str = ""
    since: float = 0.0
    think_job: str | None = None
    off: bool = False  # Ari turned off (or paused): not listening anywhere until turned back on
    game: bool = False  # game mode: the GPU is the game's (Ari off, models unloaded)
    game_auto: bool = True  # game mode turns itself on while a game is open

    def apply(self, events: list[dict], now: float) -> bool:
        """Feed events (oldest first). True when what shows changed."""
        changed = False
        for e in events:
            data = e.get("data") or {}
            if e["kind"] == "ari.game":
                g, auto = bool(data.get("on")), bool(data.get("auto", True))
                if (g, auto) != (self.game, self.game_auto):
                    self.game, self.game_auto, changed = g, auto, True
                continue
            if e["kind"] == "ari.listening":
                off = not data.get("listening", True)
                if off != self.off:
                    self.off, changed = off, True
                if off:
                    self.phase, self.text, self.since = "idle", "", now
                continue
            if e["kind"] == "ari.state":
                phase = str(data.get("phase") or "idle")
                if phase == "off":  # the island shows "off" itself (see draw): fold back
                    self.off, self.phase, self.text, self.since = True, "idle", "", now
                    changed = True
                    continue
                if phase == "thinking":
                    self.think_job = e.get("job_id")
                text = str(data.get("text") or "")
                if phase == "following" and not text:
                    text = self.text  # the answer stays on show while Ari listens for a follow-up
                self.phase, self.text, self.since = phase, text, now
                changed = True
            elif e["kind"] == "step.running" and e.get("job_id") and e.get("job_id") == self.think_job:
                step = str(e.get("step") or "")
                if step.startswith("tool ") and ": " in step:
                    name = step.split(": ", 1)[1]
                    self.phase, self.text, self.since = "working", NICE.get(name, name.replace("_", " ")), now
                    changed = True
                elif step == "web":
                    self.phase, self.text, self.since = "working", "searching the web", now
                    changed = True
        return changed

    def fold_due(self, now: float) -> bool:
        """An answer shows for a while (longer for longer ones), then the island folds. After a spoken answer
        ("following": Ari still listens for a follow-up, no "Hey Ari" needed) it stays while that lasts."""
        if self.phase == "following":
            return now - self.since > FOLLOW_S
        return self.phase == "done" and now - self.since > min(9.0, 3.5 + len(self.text) * 0.04)


STOPPABLE = ("thinking", "working", "speaking")  # while Ari is doing any of these, the island has a Stop key


def target_size(mode: str, text: str, hover: bool, details_h: float, hover_text: str = "") -> tuple[float, float]:
    if mode == "details":
        return DETAILS_W, min(MAX_H, details_h)
    if mode in ("active", "done"):  # one look for both: Ari's answer stays in the same pill it spoke from
        return max(220, min(MAX_W, (100 if mode == "active" else 50) + len(text) * 7.0)), 36
    if hover and hover_text:  # the song that's playing, or which app has the mic: wide enough to read
        return max(HOVER[0], min(330, 44 + len(hover_text) * 6.4)), HOVER[1]
    return HOVER if hover else IDLE


PAGE_IDS = ("media", "system", "calendar")  # pages of the opened island after Ari's own (island.PAGES_IDS)


def pages_of(widgets: list[dict]) -> list[str]:
    """The opened island's pages, in the order set in Helios: Ari's first, then the ones turned on."""
    return ["ari"] + [w["id"] for w in widgets if w.get("on") and w.get("id") in PAGE_IDS]


def hover_label(game: bool, off: bool, priv: dict, media: dict | None) -> str:
    """What the island says on hover: who has the mic or camera first, then the song, else just "ari"."""
    if game:
        return "a r i · game mode"
    if off:
        return "a r i · off"
    used = [("mic", a) for a in priv.get("mic") or []] + [("camera", a) for a in priv.get("camera") or []]
    if used:
        return " · ".join(f"{k}: {a}" for k, a in used[:2])
    if media and media.get("playing") and media.get("title"):
        return "♪ " + media["title"] + (f" — {media['artist']}" if media.get("artist") else "")
    return "a r i"


def icon_for(action: str) -> str:
    """The little picture on a shortcut chip, from what it does."""
    kind, _, rest = action.partition(":")
    if kind == "show":
        return "inbox" if rest == "inbox" else "grid" if rest in ("", "map") else "chat" if rest == "ari" else "page"
    if kind == "power":
        return "moon" if rest in ("sleep", "hibernate") else "power"
    return {"routine": "spark", "run": "bolt", "url": "globe", "phone": "bell", "helios": "page"}.get(kind, "dot")


def next_schedule(schedules: list[dict]) -> dict | None:
    live = [s for s in schedules if s.get("enabled") and s.get("next_run_at")]
    return min(live, key=lambda s: s["next_run_at"]) if live else None


def open_target(base: str, action: str) -> str | None:
    """Where a shortcut or link goes in the browser; None when Argus runs it instead."""
    if action.startswith("url:") and action[4:].startswith(("http://", "https://")):
        return action[4:]
    if action.startswith("show:"):
        page = action[5:]
        return f"{base.rstrip('/')}/helios/" + ("" if page in ("", "map") else f"#{page}")
    if action.startswith("helios:"):
        return f"{base.rstrip('/')}/helios/{action[7:]}"
    return None


# ------------------------------------------------------------------ talking to Argus (a background thread)

class Api:
    def __init__(self, url: str, token: str | None):
        self.url, self.token = url.rstrip("/"), token

    def call(self, method: str, path: str, body: Any = None, timeout: float = 6) -> Any:
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {self.token}"} if self.token else {})})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else None


def ask_parts(d: dict) -> dict[str, Any]:
    """An Ari task's question (event ari.ask) as the island shows it: the lines, the choices as buttons, a text box
    when it wants an answer, a remember box when it may keep it, or Yes / No for a confirmation."""
    lines = [str(x) for x in d.get("summary") or []]
    choices: list[str] = []
    for ln in list(lines):
        if ln.startswith("Choices: "):
            choices = [c.strip() for c in ln[len("Choices: "):].split(",") if c.strip()][:6]
            lines.remove(ln)
    fields = list(d.get("fields") or [])
    return {"title": str(d.get("title") or "Ari needs you"), "lines": [ln for ln in lines if ln][:4],
            "choices": choices, "answer": "answer" in fields, "remember": "remember" in fields,
            "confirm": "answer" not in fields, "picture": bool(d.get("picture")), "approval": d.get("approval")}


def decision(parts: dict, text: str | None, remember: bool = False, yes: bool = True) -> dict:
    """The body for POST /approvals/{id}/decide from the island."""
    if parts["confirm"]:
        return {"answer": "approve" if yes else "reject", "by": "island"}
    if not yes or not (text or "").strip():
        return {"answer": "reject", "by": "island"}
    fields: dict[str, Any] = {"answer": text.strip()}
    if parts["remember"]:
        fields["remember"] = bool(remember)
    return {"answer": "approve", "fields": fields, "by": "island"}


@dataclass
class Info:
    """What the opened island shows (fetched when it opens, and every 15 s while open)."""
    status: dict | None = None
    waiting: int = 0
    next: dict | None = None
    last: str | None = None
    widgets: list[dict] = field(default_factory=lambda: [{"id": w, "on": True} for w in
                                                        ("clock", "status", "inbox", "next", "shortcuts",
                                                         "media", "system", "calendar", "privacy")])
    shortcuts: list[dict] = field(default_factory=list)
    keys: dict[str, str] = field(default_factory=lambda: {"ask": "ctrl+alt+space", "hide": "ctrl+alt+d"})


def fetch_info(api: Api) -> Info:
    info = Info()
    for path, fn in (
        ("/island", lambda r: (setattr(info, "widgets", r.get("widgets") or info.widgets),
                               setattr(info, "shortcuts", r.get("shortcuts") or []),
                               setattr(info, "keys", r.get("keys") or info.keys))),
        ("/status", lambda r: setattr(info, "status", r)),
        ("/inbox", lambda r: setattr(info, "waiting", len(r.get("items") or []))),
        ("/schedules", lambda r: setattr(info, "next", next_schedule(r or []))),
    ):
        try:
            fn(api.call("GET", path))
        except Exception:  # one part missing is fine
            pass
    if any(w.get("id") == "last" and w.get("on") for w in info.widgets):
        try:
            chats = api.call("GET", "/ari-chats")
            if chats:
                turns = api.call("GET", f"/ari/{chats[0]['conv']}")["turns"]
                last = next((t["text"] for t in reversed(turns) if t["role"] == "ari" and t.get("text")), None)
                info.last = last.strip() if last else None
        except Exception:
            pass
    return info


# ------------------------------------------------------------------ the window

def main(argv: list[str] | None = None) -> int:  # noqa: C901 - one window, drawn by hand
    from .config import parse_env_file

    p = argparse.ArgumentParser(prog="ari-popup", description="Ari's island at the top of the screen")
    p.add_argument("--url", default=os.environ.get("ARGUS_URL", "http://127.0.0.1:8600"))
    p.add_argument("--preview", help=argparse.SUPPRESS)  # render one state to a PNG and exit (for checking the look)
    p.add_argument("--phase", default="idle", help=argparse.SUPPRESS)
    p.add_argument("--text", default="", help=argparse.SUPPRESS)
    p.add_argument("--details", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--hover", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--page", default="ari", help=argparse.SUPPRESS)  # with --details: media, system, calendar
    p.add_argument("--ask", action="store_true", help=argparse.SUPPRESS)  # the type-to-Ari box
    args = p.parse_args(argv)
    token = os.environ.get("ARGUS_WORKER_TOKEN") or parse_env_file(Path(".env")).get("ARGUS_WORKER_TOKEN")
    try:
        from PySide6.QtCore import QObject, QPointF, QRect, QRectF, Qt, QTimer, Signal  # type: ignore
        from PySide6.QtGui import (  # type: ignore[import-not-found]
            QBrush,
            QColor,
            QCursor,
            QFont,
            QFontMetrics,
            QImage,
            QLinearGradient,
            QPainter,
            QPainterPath,
            QPen,
            QRadialGradient,
            QRegion,
        )
        from PySide6.QtWidgets import (  # type: ignore[import-not-found]
            QApplication,
            QCheckBox,
            QHBoxLayout,
            QLabel,
            QLineEdit,
            QPushButton,
            QVBoxLayout,
            QWidget,
        )
    except ImportError as e:
        print(f"ari-popup needs its extras: pip install -e .[popup]  ({e})", file=sys.stderr)
        return 2

    api = Api(args.url, token)

    class Bus(QObject):
        events = Signal(list)
        info = Signal(object)
        flash = Signal(str)
        ask = Signal(dict)
        answered = Signal(str)
        live = Signal(str, object)  # what the PC itself says: media, hw, priv, weather
        hotkey = Signal(str)
        keys = Signal(dict)

    bus = Bus()

    class Live:
        """Reads the PC for the island on its own thread, only what is on show (Island.want): the mic/camera dots
        every 2 s, what's playing every second, the load every 1.5 s, the weather every half hour."""

        EVERY = {"priv": 2.0, "media": 1.0, "hw": 1.5, "weather": 1800.0}

        def __init__(self) -> None:
            self.media = Media()
            self.hw = Hardware()
            self.last: dict[str, float] = {}
            self.wake = threading.Event()

        def poke(self, kind: str) -> None:
            """Read this now (after a click on play, say)."""
            self.last.pop(kind, None)
            self.wake.set()

        def read(self, kind: str) -> Any:
            if kind == "priv":
                return privacy() if sys.platform == "win32" else {"mic": [], "camera": []}
            if kind == "media":
                return self.media.read()
            if kind == "hw":
                return self.hw.read()
            return api.call("GET", "/island/weather")

        def run(self) -> None:
            while True:
                want = set(getattr(island, "want", set()))
                now = time.monotonic()
                for kind in want:
                    if now - self.last.get(kind, -1e9) < self.EVERY[kind]:
                        continue
                    self.last[kind] = now
                    try:
                        data = self.read(kind)
                    except Exception:  # noqa: BLE001 - try again in a minute (Argus restarting, say)
                        self.last[kind] = now - self.EVERY[kind] + 60
                        continue
                    bus.live.emit(kind, data)
                self.wake.wait(0.5)
                self.wake.clear()

    live = Live()

    def font(families: list[str], px: float, weight: int = 400) -> QFont:
        f = QFont()
        f.setFamilies(families)
        f.setPixelSize(max(1, round(px)))
        f.setWeight(QFont.Weight(weight))
        f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        return f

    SANS = ["Segoe UI Variable Text", "Segoe UI Variable Display", "Segoe UI", "Inter", "Helvetica Neue", "Arial"]
    DISPLAY = ["Segoe UI Variable Display", "Segoe UI Variable Text", "Segoe UI", "Inter", "Arial"]
    MONO = ["Cascadia Mono", "Consolas", "Menlo", "monospace"]
    F_TEXT = font(SANS, 13)
    F_SMALL = font(SANS, 12)
    F_LABEL = font(MONO, 11)
    F_CLOCK = font(DISPLAY, 21, 400)
    F_CHIP = font(SANS, 11.5, 500)
    F_TITLE = font(SANS, 14, 600)
    F_TINY = font(MONO, 10)
    F_DAY = font(SANS, 11.5)
    BAR = {"ok": "#34d399", "warm": "#f5a524", "hot": "#ef6a6a"}
    RIGHT_MID = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

    class Island(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("Ari")
            self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                                | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            self._shadow_key: Any = None
            self._shadow: Any = None
            self.setMouseTracking(True)
            self.ari = AriState()
            self.info = Info()
            self.details = False
            self.hover = False
            self.hot: str | None = None  # the button under the mouse
            self.flash_text: str | None = None
            self.spring = Spring(*IDLE)
            self.buttons: list[tuple[QRectF, str]] = []
            self.details_h = 120.0
            self.anim = QTimer(self, interval=16)
            self.anim.timeout.connect(self.frame)
            self.last_frame = time.monotonic()
            self.pulse = QTimer(self, interval=33)  # the indicators move while Ari is busy (30 fps)
            self.pulse.timeout.connect(self.update)
            self.fold = QTimer(self, singleShot=True)
            self.fold.timeout.connect(self.check_fold)
            self.away = QTimer(self, interval=60)  # while open: a click anywhere else folds it
            self.away.timeout.connect(self.click_away)
            self.was_down = False
            self.leave = QTimer(self, singleShot=True, interval=4000)
            self.leave.timeout.connect(self.close_details)
            self.refresh = QTimer(self, interval=15000)
            self.refresh.timeout.connect(self.load_info)
            self.clock = QTimer(self, interval=1000)
            self.clock.timeout.connect(self.update)
            bus.events.connect(self.on_events)
            bus.info.connect(self.on_info)
            bus.flash.connect(self.on_flash)
            bus.live.connect(self.on_live)
            self.page = 0
            self.wheel_acc, self.wheel_at = 0.0, 0.0
            self.media: dict | None = None
            self.art: Any = None  # the album art (QImage) and its colour
            self.art_tone = QColor("#4cc2ff")
            self.art_key: Any = None
            self.hw: dict = {}
            self.priv: dict = {"mic": [], "camera": []}
            self.weather: dict = {}
            self.weather_at = 0.0
            self.tick = QTimer(self, interval=500)  # the media page's position moves on its own
            self.tick.timeout.connect(self.update)
            self.place()
            self.set_mask()

        # -------------------------------------------------------------- state

        def mode(self) -> str:
            if self.details:
                return "details"
            if self.ari.phase == "idle":
                return "idle"
            return "done" if self.ari.phase in ("done", "following") else "active"

        def shown_text(self) -> str:
            return self.ari.text or ("say what you need" if self.ari.phase == "listening" else "")

        def hover_text(self) -> str:
            label = hover_label(self.ari.game, self.ari.off, self.priv, self.media)
            return "" if label == "a r i" else label

        def pages(self) -> list[str]:
            return pages_of(self.info.widgets)

        def page_id(self) -> str:
            ps = self.pages()
            self.page = max(0, min(self.page, len(ps) - 1))
            return ps[self.page]

        def wants(self) -> set[str]:
            """What the sampler thread should read now (nothing while the island is hidden)."""
            if not self.isVisible():
                return set()
            out = set()
            if any(w.get("id") == "privacy" and w.get("on") for w in self.info.widgets):
                out.add("priv")
            if self.details:
                pid = self.page_id()
                out |= {"media": {"media"}, "system": {"hw"}, "calendar": {"weather"}}.get(pid, set())
            elif self.hover and self.mode() == "idle":
                out.add("media")
            return out

        def target(self) -> tuple[float, float]:
            label = WORDS.get(self.ari.phase, "")
            return target_size(self.mode(), f"{label}  {self.shown_text()}", self.hover, self.details_h,
                               self.hover_text())

        def kick(self) -> None:
            """Something changed: animate to the new size (the timer stops by itself once it settles)."""
            self.want = self.wants()
            self.set_mask()
            if not self.anim.isActive():
                self.last_frame = time.monotonic()
                self.anim.start()
            busy = self.mode() == "active" or self.ari.phase == "following"  # the follow-up ring moves
            if busy and not self.pulse.isActive():
                self.pulse.start()
            elif not busy and self.pulse.isActive():
                self.pulse.stop()
            self.update()

        def frame(self) -> None:
            now = time.monotonic()
            moving = self.spring.step(*self.target(), now - self.last_frame)
            self.last_frame = now
            self.set_mask()
            self.update()
            if not moving:
                self.anim.stop()

        def on_events(self, evs: list) -> None:
            if self.ari.apply(evs, time.monotonic()):
                if self.ari.phase == "done":
                    self.fold.start(int(min(9.0, 3.5 + len(self.ari.text) * 0.04) * 1000) + 50)
                elif self.ari.phase == "following":
                    self.fold.start(int(FOLLOW_S * 1000) + 50)
                self.kick()

        def check_fold(self) -> None:
            if self.ari.fold_due(time.monotonic()):
                self.ari.phase, self.ari.text = "idle", ""
                self.kick()

        def on_info(self, info: Info) -> None:
            self.info = info
            self.kick()

        def on_flash(self, text: str) -> None:
            self.flash_text = text or None
            if text:
                QTimer.singleShot(2600, lambda: self.on_flash(""))
            self.update()

        def on_live(self, kind: str, data: Any) -> None:
            if kind == "media":
                self.media = data
                key = (data or {}).get("art") and hash(data["art"])
                if key != self.art_key:
                    self.art_key = key
                    self.art = None
                    if data and data.get("art"):
                        img = QImage.fromData(data["art"])
                        if not img.isNull():
                            self.art = img
                            c = img.scaled(1, 1, Qt.AspectRatioMode.IgnoreAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation).pixelColor(0, 0)
                            h, sat, v, _a = c.getHsvF()
                            self.art_tone = QColor.fromHsvF(max(0.0, h), max(0.45, sat), max(0.75, v))
                        else:
                            self.art_tone = QColor("#4cc2ff")
                    else:
                        self.art_tone = QColor("#4cc2ff")
            elif kind == "hw":
                self.hw = data or {}
            elif kind == "priv":
                if data == self.priv:
                    return
                self.priv = data
            elif kind == "weather":
                self.weather = data or {}
            self.kick()

        def load_info(self) -> None:
            threading.Thread(target=lambda: bus.info.emit(fetch_info(api)), daemon=True).start()

        def open_details(self) -> None:
            self.details = True
            self.load_info()
            self.refresh.start()
            self.clock.start()
            self.away.start()
            self.tick.start()
            self.kick()

        def close_details(self) -> None:
            if not self.details:
                return
            self.details = False
            for t in (self.refresh, self.clock, self.away, self.leave, self.tick):
                t.stop()
            self.kick()

        def click_away(self) -> None:
            """Windows: a mouse press outside the island folds it (it never takes the keyboard, so no focus)."""
            down = mouse_down()
            if down and not self.was_down:
                pos = self.mapFromGlobal(QCursor.pos())
                if not self.shape_rect().adjusted(-2, -2, 2, 2).contains(QPointF(pos)):
                    self.close_details()
            self.was_down = down

        # -------------------------------------------------------------- geometry

        def place(self) -> None:
            g = QApplication.primaryScreen().geometry()  # the whole screen: it touches its very top edge
            self.setGeometry(*placement(g.x(), g.y(), g.width()))

        def shape_rect(self) -> QRectF:
            w, h = self.spring.w, self.spring.h
            sw = shoulder_w(h)
            return QRectF((W - w) / 2 - sw, 0, w + 2 * sw, h)

        def set_mask(self) -> None:
            """Only the island (and room for its shadow) is part of the window; clicks elsewhere go to the app
            below. While it grows, the whole new size at once, so nothing is cut off."""
            tw, th = self.target()
            w, h = max(self.spring.w, tw), max(self.spring.h, th)
            sh = SHOULDER_MAX * 2.6
            x = int((W - w) / 2 - sh - MARGIN)
            self.setMask(QRegion(QRect(max(0, x), 0, min(W, int(w + 2 * sh + 2 * MARGIN) + 2), int(h + MARGIN))))

        # -------------------------------------------------------------- drawing

        def path(self, open_top: bool = False) -> Any:
            """The shape; open_top: only the part below the screen's edge (for the shadow)."""
            r = self.shape_rect()
            p = QPainterPath()
            cmds = outline(self.spring.w, self.spring.h, self.mode() == "details")
            if open_top:  # start at the right shoulder's tip, go round the bottom, end at the left tip
                cmds = [("M", cmds[1][1], cmds[1][2])] + cmds[2:]
            for cmd in cmds:
                pts = [QPointF(r.x() + cmd[i], r.y() + cmd[i + 1]) for i in range(1, len(cmd), 2)]
                if cmd[0] == "M":
                    p.moveTo(pts[0])
                elif cmd[0] == "L":
                    p.lineTo(pts[0])
                else:
                    p.cubicTo(pts[0], pts[1], pts[2])
            if not open_top:
                p.closeSubpath()
            return p

        def shadow(self, mode: str, tone: Any) -> Any:
            """The shadow under the island, blurred by drawing it at 1/SH_SCALE size and scaling it back up
            (twice, so the edge fades smoothly). Kept while the size doesn't change."""
            key = (round(self.spring.w), round(self.spring.h), mode, tone.name())
            if self._shadow_key == key:
                return self._shadow
            k = SH_SCALE
            small = QImage(max(1, W // k), max(1, H // k), QImage.Format.Format_ARGB32_Premultiplied)
            small.fill(Qt.GlobalColor.transparent)
            sp = QPainter(small)
            sp.setRenderHint(QPainter.RenderHint.Antialiasing)
            sp.scale(1 / k, 1 / k)
            sp.translate(0, SH_DROP)
            sp.setPen(Qt.PenStyle.NoPen)
            sp.setBrush(QColor(0, 0, 0, SH_ALPHA))
            sp.drawPath(self.path())
            if mode in ("active", "done"):
                c = QColor(tone)
                c.setAlpha(40)
                sp.setBrush(c)
                sp.drawPath(self.path())
            sp.end()
            smooth = Qt.TransformationMode.SmoothTransformation
            ratio = Qt.AspectRatioMode.IgnoreAspectRatio
            half = small.scaled(max(1, W * 2 // k), max(1, H * 2 // k), ratio, smooth)
            self._shadow = half.scaled(W, H, ratio, smooth)
            self._shadow_key = key
            return self._shadow

        def paintEvent(self, _e: Any) -> None:  # noqa: N802 - Qt's name
            qp = QPainter(self)
            qp.setRenderHint(QPainter.RenderHint.Antialiasing)
            qp.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            path = self.path()
            mode = self.mode()
            tone = QColor(TONE.get(self.ari.phase, "#f5a524"))
            # soft shadow: the shape drawn small and scaled up smoothly (a cheap blur that fades to nothing, no
            # hard ends); in Ari's colour too while it talks
            qp.drawImage(0, 0, self.shadow(mode, tone))
            # the shape itself
            r = self.shape_rect()
            g = QLinearGradient(0, 0, 0, r.height())
            g.setColorAt(0, QColor("#000000"))
            g.setColorAt(0.6, QColor("#030303"))
            g.setColorAt(1, QColor("#0b0b0c"))
            qp.setPen(Qt.PenStyle.NoPen)
            qp.setBrush(QBrush(g))
            qp.drawPath(path)
            qp.setClipPath(path)
            body = QRectF(r.x() + shoulder_w(self.spring.h), 0, self.spring.w, self.spring.h)
            self.buttons = []
            if mode == "idle":
                mic, cam = bool(self.priv.get("mic")), bool(self.priv.get("camera"))
                if self.hover and self.spring.h > 16:
                    label = hover_label(self.ari.game, self.ari.off, self.priv, self.media)
                    used = label.startswith(("mic:", "camera:"))
                    qp.setPen(QColor("#f5a524" if self.ari.game else "#ef6a6a" if self.ari.off else
                                     "#fb923c" if used and mic else "#4ade80" if used else
                                     "#c4c8ce" if label.startswith("♪") else "#5d6670"))
                    qp.setFont(F_LABEL if label.startswith("a r i") or used else F_SMALL)
                    fm = QFontMetrics(qp.font())
                    qp.drawText(body.adjusted(12, 0, -12, 0), Qt.AlignmentFlag.AlignCenter,
                                fm.elidedText(label, Qt.TextElideMode.ElideRight, int(body.width() - 24)))
                else:
                    if self.ari.off:  # a small dot on the lip: Ari is off (amber: game mode)
                        qp.setPen(Qt.PenStyle.NoPen)
                        qp.setBrush(QColor("#f5a524" if self.ari.game else "#ef6a6a"))
                        qp.drawEllipse(QPointF(body.center().x(), body.center().y()), 2.2, 2.2)
                    # privacy: orange while an app has the mic, green while one has the camera (still dots: no
                    # redraws while you sit in a call)
                    dots = [c for c, on in (("#fb923c", mic), ("#4ade80", cam)) if on]
                    for i, col in enumerate(dots):
                        qp.setPen(Qt.PenStyle.NoPen)
                        qp.setBrush(QColor(col))
                        qp.drawEllipse(QPointF(body.right() - 10 - i * 8, body.center().y()), 2.3, 2.3)
            elif mode in ("active", "done") and self.spring.h > 24:
                self.draw_active(qp, body, tone, mode)
            elif mode == "details":
                self.draw_details(qp, body)
            qp.end()

        def draw_indicator(self, qp: Any, cx: float, cy: float, tone: Any) -> None:
            t = time.monotonic()
            ph = self.ari.phase
            if ph in ("listening", "speaking"):
                speed = 1.6 if ph == "listening" else 1.1
                for i in range(5):
                    s = 0.22 + 0.78 * (0.5 + 0.5 * math.sin((t / speed) * 2 * math.pi + i * 1.3))
                    h = 16 * s
                    qp.setPen(Qt.PenStyle.NoPen)
                    qp.setBrush(tone)
                    qp.drawRoundedRect(QRectF(cx - 9 + i * 4.4, cy - h / 2, 2.4, h), 1.2, 1.2)
            elif ph in ("thinking", "working"):
                speed = 0.8 if ph == "working" else 1.1
                start = -((t / speed) % 1.0) * 360
                grad = QPen(tone, 2.2)
                grad.setCapStyle(Qt.PenCapStyle.RoundCap)
                qp.setPen(grad)
                qp.setBrush(Qt.BrushStyle.NoBrush)
                qp.drawArc(QRectF(cx - 7, cy - 7, 14, 14), int(start * 16), int(-250 * 16))
            else:
                if ph == "following":  # still listening for a follow-up: a ring that runs down with the time left
                    left = max(0.0, 1.0 - (t - self.ari.since) / FOLLOW_S)
                    ring = QPen(tone, 1.6)
                    ring.setCapStyle(Qt.PenCapStyle.RoundCap)
                    qp.setPen(ring)
                    qp.setBrush(Qt.BrushStyle.NoBrush)
                    qp.setOpacity(0.35 + 0.65 * left)
                    qp.drawArc(QRectF(cx - 8.5, cy - 8.5, 17, 17), 90 * 16, int(left * 360 * 16))
                    qp.setOpacity(1)
                glow = QRadialGradient(QPointF(cx, cy), 9)
                c = QColor(tone)
                glow.setColorAt(0, c)
                c2 = QColor(tone)
                c2.setAlpha(0)
                glow.setColorAt(1, c2)
                qp.setPen(Qt.PenStyle.NoPen)
                qp.setBrush(QBrush(glow))
                qp.drawEllipse(QPointF(cx, cy), 9, 9)
                qp.setBrush(tone)
                qp.drawEllipse(QPointF(cx, cy), 3.8, 3.8)

        def draw_active(self, qp: Any, body: Any, tone: Any, mode: str) -> None:
            fade = max(0.0, min(1.0, (self.spring.h - 24) / 10))
            qp.setOpacity(fade)
            x = body.x() + 16
            cy = body.y() + 18
            self.draw_indicator(qp, x + 9, cy, tone)
            x += 28
            right = body.right() - 16
            if mode == "active" and self.ari.phase in STOPPABLE:  # a round Stop key at the right end
                stop = QRectF(right - 22, body.y() + 7, 22, 22)
                self.stop_key(qp, stop)
                right -= 30
            if mode == "active":
                label = WORDS.get(self.ari.phase, "")
                qp.setFont(F_LABEL)
                qp.setPen(tone)
                lw = QFontMetrics(F_LABEL).horizontalAdvance(label)
                qp.drawText(QRectF(x, body.y(), lw + 2, 36), Qt.AlignmentFlag.AlignVCenter, label)
                x += lw + 10
                qp.setFont(F_TEXT)
                qp.setPen(QColor("#e4e6e8"))
                text = self.shown_text()
                text = QFontMetrics(F_TEXT).elidedText(text, Qt.TextElideMode.ElideRight, int(right - x))
                qp.drawText(QRectF(x, body.y(), right - x, 36), Qt.AlignmentFlag.AlignVCenter, text)
            else:  # done: the same line, the answer instead of the label (the whole answer is in Helios)
                qp.setFont(F_TEXT)
                qp.setPen(QColor("#e4e6e8"))
                text = QFontMetrics(F_TEXT).elidedText(" ".join(self.ari.text.split()), Qt.TextElideMode.ElideRight,
                                                       int(right - x))
                qp.drawText(QRectF(x, body.y(), right - x, 36), Qt.AlignmentFlag.AlignVCenter, text)
            qp.setOpacity(1)

        def button(self, qp: Any, rect: Any, key: str, text: str, *, fill: str = "#141618", color: str = "#e4e6e8",
                   f: Any = None, radius: float = 13) -> None:
            hot = self.hot == key
            qp.setPen(Qt.PenStyle.NoPen)
            qp.setBrush(QColor("#1f2225" if hot and fill == "#141618" else fill))
            qp.drawRoundedRect(rect, radius, radius)
            qp.setPen(QColor(color))
            qp.setFont(f or F_CHIP)
            qp.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
            self.buttons.append((rect, key))

        def stop_key(self, qp: Any, rect: Any) -> None:
            """A round Stop key: a small square in a ring (Ari stops talking and drops what it is working on)."""
            hot = self.hot == "stop"
            qp.setPen(QPen(QColor(248, 113, 113, 150 if hot else 90), 1))
            qp.setBrush(QColor("#2a1416" if hot else "#1c1213"))
            qp.drawEllipse(rect.adjusted(0.5, 0.5, -0.5, -0.5))
            c = rect.center()
            qp.setPen(Qt.PenStyle.NoPen)
            qp.setBrush(QColor("#f87171" if hot else "#ef6a6a"))
            qp.drawRoundedRect(QRectF(c.x() - 4, c.y() - 4, 8, 8), 1.6, 1.6)
            self.buttons.append((rect.adjusted(-4, -4, 4, 4), "stop"))  # a bit larger to hit

        def draw_details(self, qp: Any, body: Any) -> None:
            fade = max(0.0, min(1.0, (self.spring.h - 70) / 40))
            qp.setOpacity(fade)
            x0, x1 = body.x() + 16, body.right() - 16
            y = body.y() + 12
            pid = self.page_id()
            if pid == "media":
                y = self.draw_media(qp, x0, x1, y)
            elif pid == "system":
                y = self.draw_system(qp, x0, x1, y)
            elif pid == "calendar":
                y = self.draw_calendar(qp, x0, x1, y)
            else:
                y = self.draw_ari(qp, x0, x1, y)
            if self.flash_text:
                qp.setFont(F_SMALL)
                qp.setPen(QColor("#39ff9c"))
                qp.drawText(QRectF(x0, y + 2, x1 - x0, 18), Qt.AlignmentFlag.AlignLeft, self.flash_text)
                y += 22
            y = self.draw_dots(qp, body, y)
            qp.setOpacity(1)
            h = y - body.y() + 6
            if abs(h - self.details_h) > 1:
                self.details_h = h
                QTimer.singleShot(0, self.kick)

        def draw_dots(self, qp: Any, body: Any, y: float) -> float:
            """Which page is on show, as dots under it (click one, or scroll, to flip)."""
            n = len(self.pages())
            if n < 2:
                return y
            gap = 12
            cx0 = body.center().x() - gap * (n - 1) / 2
            for i in range(n):
                c = QPointF(cx0 + i * gap, y + 6)
                on = i == self.page
                qp.setPen(Qt.PenStyle.NoPen)
                qp.setBrush(QColor("#e4e6e8" if on else "#3a3f46" if self.hot != f"page:{i}" else "#6f7680"))
                if on:
                    qp.drawRoundedRect(QRectF(c.x() - 5, c.y() - 2.5, 10, 5), 2.5, 2.5)
                else:
                    qp.drawEllipse(c, 2.5, 2.5)
                self.buttons.append((QRectF(c.x() - 6, c.y() - 7, 12, 14), f"page:{i}"))
            return y + 14

        def page_head(self, qp: Any, x0: float, x1: float, y: float, title: str) -> float:
            """A quiet header for the extra pages: the page's name on the left, the time on the right."""
            qp.setFont(F_LABEL)
            qp.setPen(QColor("#6f7680"))
            qp.drawText(QRectF(x0, y, 200, 18), Qt.AlignmentFlag.AlignVCenter, title)
            qp.drawText(QRectF(x1 - 80, y, 80, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                        time.strftime("%H:%M"))
            return y + 24

        def draw_ari(self, qp: Any, x0: float, x1: float, y: float) -> float:
            info = self.info
            on = {w["id"] for w in info.widgets if w.get("on")}
            order = [w["id"] for w in info.widgets if w.get("on")]
            # header: the time and date on the left; Talk (a round mic key) and "···" (Helios) on the right
            talk = QRectF(x1 - 30, y, 30, 30)
            more = QRectF(talk.x() - 36, y, 30, 30)
            power = QRectF(more.x() - 36, y, 30, 30)
            gamek = QRectF(power.x() - 36, y, 30, 30)
            if "clock" in on:
                now = time.localtime()
                qp.setFont(F_CLOCK)
                qp.setPen(QColor("#f4f5f6"))
                tstr = time.strftime("%H:%M", now)
                tw = QFontMetrics(F_CLOCK).horizontalAdvance(tstr)
                qp.drawText(QRectF(x0, y, tw + 4, 30), Qt.AlignmentFlag.AlignVCenter, tstr)
                qp.setFont(F_SMALL)
                qp.setPen(QColor("#6f7680"))
                qp.drawText(QRectF(x0 + tw + 9, y + 1, gamek.x() - x0 - tw - 12, 30), Qt.AlignmentFlag.AlignVCenter,
                            time.strftime("%a %d %b", now))
            else:
                qp.setFont(F_TEXT)
                qp.setPen(QColor("#e4e6e8"))
                qp.drawText(QRectF(x0, y, 120, 30), Qt.AlignmentFlag.AlignVCenter, "Ari")
            self.round_key(qp, gamek, "game", "game-on" if self.ari.game else "game")
            self.round_key(qp, power, "power", "start" if self.ari.off else "power")
            self.round_key(qp, more, "helios:#ari", "more")
            self.round_key(qp, talk, "talk", "mic")
            y += 42
            # status rows in one quiet card
            st = info.status or {}
            jobs = st.get("jobs") or {}
            rows: list[tuple[str, str, str, str | None]] = []  # dot, text, right, click key
            rows.append(("#f5a524" if self.ari.game else "#4b5058",
                         "Game mode on" if self.ari.game else "Game mode off",
                         "auto ✓" if self.ari.game_auto else "auto off", "gameauto"))
            for wid in order:
                if wid == "status":
                    running = (jobs.get("running") or 0) + (jobs.get("leased") or 0)
                    queued = (jobs.get("queued") or 0) + (jobs.get("retry") or 0)
                    ok = st.get("status") == "ok"
                    right = "idle" if not running and not queued else \
                        " · ".join(x for x in (f"{running} running" if running else "",
                                               f"{queued} queued" if queued else "") if x)
                    rows.append(("#34d399" if ok else "#f5a524", "All good" if ok else "Needs a look", right, None))
                elif wid == "inbox" and info.waiting:
                    rows.append(("#f5a524", f"{info.waiting} waiting for you", "›", "show:inbox"))
                elif wid == "next" and info.next and info.next.get("next_run_at"):
                    nxt = info.next
                    at = time.localtime(nxt["next_run_at"])
                    same_day = time.strftime("%Y%m%d", at) == time.strftime("%Y%m%d")
                    rows.append(("#60a5fa", nxt.get("label") or f"{nxt['plugin']} · {nxt['workflow']}",
                                 time.strftime("%H:%M" if same_day else "%a %H:%M", at), None))
            if rows:
                ch = 30 * len(rows)
                card = QRectF(x0 - 4, y, x1 - x0 + 8, ch)
                qp.setPen(QPen(QColor(255, 255, 255, 14), 1))
                qp.setBrush(QColor("#0f1012"))
                qp.drawRoundedRect(card, 12, 12)
                for i, (dot, text, right, key) in enumerate(rows):
                    ry = y + 30 * i
                    if i:
                        qp.setPen(QPen(QColor(255, 255, 255, 10), 1))
                        qp.drawLine(QPointF(x0 + 8, ry), QPointF(x1 - 4, ry))
                    if key:
                        r = QRectF(card.x(), ry, card.width(), 30)
                        self.buttons.append((r, key))
                        if self.hot == key:
                            qp.setPen(Qt.PenStyle.NoPen)
                            qp.setBrush(QColor(255, 255, 255, 8))
                            qp.drawRoundedRect(r.adjusted(2, 2, -2, -2), 9, 9)
                    self.line(qp, x0 + 6, x1 - 4, ry + 4, dot, text, right, hot=self.hot == key and key is not None)
                y += ch + 10
            if "last" in on and info.last:
                qp.setFont(F_SMALL)
                qp.setPen(QColor("#a9afb6"))
                qp.drawText(QRectF(x0, y, x1 - x0, 34), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                            two_lines("Ari: " + info.last, QFontMetrics(F_SMALL), int(x1 - x0)))
                y += 40
            if "shortcuts" in on and info.shortcuts:
                fm = QFontMetrics(F_CHIP)
                cx = x0 - 4
                for sc in info.shortcuts:
                    label = fm.elidedText(sc["label"], Qt.TextElideMode.ElideRight, 110)
                    cw = fm.horizontalAdvance(label) + 40
                    if cx + cw > x1 + 4 and cx > x0:
                        cx, y = x0 - 4, y + 32
                    self.chip(qp, QRectF(cx, y, cw, 26), "sc:" + sc["action"], label)
                    cx += cw + 6
                y += 32
            return y

        def draw_media(self, qp: Any, x0: float, x1: float, y: float) -> float:
            """What's playing: the cover, title and artist, where it is in the song, and back / play / next."""
            m = self.media or {}
            y = self.page_head(qp, x0, x1, y, "now playing" + (f" · {m['app'].lower()}" if m.get("app") else ""))
            art = QRectF(x0, y, 64, 64)
            clip = QPainterPath()
            clip.addRoundedRect(art, 10, 10)
            qp.save()
            qp.setClipPath(clip, Qt.ClipOperation.IntersectClip)
            if self.art is not None:
                qp.drawImage(art, self.art)
            else:
                qp.fillRect(art, QColor("#15171a"))
                qp.setPen(QColor("#3a3f46"))
                qp.setFont(font(SANS, 26))
                qp.drawText(art, Qt.AlignmentFlag.AlignCenter, "♪")
            qp.restore()
            tx = x0 + 78
            if m.get("title"):
                qp.setFont(F_TITLE)
                qp.setPen(QColor("#f4f5f6"))
                qp.drawText(QRectF(tx, y + 6, x1 - tx, 20), Qt.AlignmentFlag.AlignVCenter,
                            QFontMetrics(F_TITLE).elidedText(m["title"], Qt.TextElideMode.ElideRight, int(x1 - tx)))
                qp.setFont(F_SMALL)
                qp.setPen(QColor("#9aa1aa"))
                who = " · ".join(x for x in (m.get("artist"), m.get("album")) if x)
                qp.drawText(QRectF(tx, y + 28, x1 - tx, 18), Qt.AlignmentFlag.AlignVCenter,
                            QFontMetrics(F_SMALL).elidedText(who, Qt.TextElideMode.ElideRight, int(x1 - tx)))
                qp.setFont(F_TINY)
                qp.setPen(QColor(self.art_tone) if m.get("playing") else QColor("#6f7680"))
                qp.drawText(QRectF(tx, y + 46, x1 - tx, 16), Qt.AlignmentFlag.AlignVCenter,
                            "playing" if m.get("playing") else "paused" if m.get("paused") else "")
            else:
                qp.setFont(F_TEXT)
                qp.setPen(QColor("#9aa1aa"))
                qp.drawText(QRectF(tx, y, x1 - tx, 64), Qt.AlignmentFlag.AlignVCenter,
                            "Nothing playing" if self.media is not None or sys.platform == "win32" else "No media here")
            y += 76
            pos = position_now(m.get("pos"), m.get("end"), m.get("updated"), bool(m.get("playing")))
            end = m.get("end")
            if pos is not None and end:  # the song's progress, in the cover's colour
                bar = QRectF(x0, y + 4, x1 - x0, 4)
                qp.setPen(Qt.PenStyle.NoPen)
                qp.setBrush(QColor(255, 255, 255, 22))
                qp.drawRoundedRect(bar, 2, 2)
                qp.setBrush(self.art_tone)
                qp.drawRoundedRect(QRectF(bar.x(), bar.y(), bar.width() * max(0.0, min(1.0, pos / end)), 4), 2, 2)
                qp.setFont(F_TINY)
                qp.setPen(QColor("#6f7680"))
                qp.drawText(QRectF(x0, y + 10, 80, 14), Qt.AlignmentFlag.AlignLeft, clock(pos))
                qp.drawText(QRectF(x1 - 80, y + 10, 80, 14), Qt.AlignmentFlag.AlignRight, clock(end))
                y += 28
            # back / play-pause / next
            cx = (x0 + x1) / 2
            for key, dx, big in (("media:previous", -52, False), ("media:play_pause", 0, True),
                                 ("media:next", 52, False)):
                r = 19 if big else 15
                rect = QRectF(cx + dx - r, y + 2 + (19 - r), 2 * r, 2 * r)
                self.media_key(qp, rect, key, playing=bool(m.get("playing")))
            return y + 46

        def media_key(self, qp: Any, rect: Any, key: str, playing: bool) -> None:
            hot = self.hot == key
            big = key == "media:play_pause"
            qp.setPen(QPen(QColor(255, 255, 255, 26 if hot else 14), 1))
            qp.setBrush(QColor("#f4f5f6" if big and hot else "#e4e6e8" if big else "#1c1e22" if hot else "#141518"))
            qp.drawEllipse(rect.adjusted(0.5, 0.5, -0.5, -0.5))
            c = rect.center()
            ink = QColor("#0b0b0c" if big else "#e4e6e8" if hot else "#c4c8ce")
            qp.setPen(Qt.PenStyle.NoPen)
            qp.setBrush(ink)
            act = key.split(":", 1)[1]
            if act == "play_pause" and playing:  # pause: two bars
                qp.drawRoundedRect(QRectF(c.x() - 5, c.y() - 6, 3.6, 12), 1, 1)
                qp.drawRoundedRect(QRectF(c.x() + 1.4, c.y() - 6, 3.6, 12), 1, 1)
            elif act == "play_pause":  # play: a triangle
                tri = QPainterPath()
                tri.moveTo(c.x() - 4, c.y() - 6.5)
                tri.lineTo(c.x() + 6.5, c.y())
                tri.lineTo(c.x() - 4, c.y() + 6.5)
                tri.closeSubpath()
                qp.drawPath(tri)
            else:  # previous / next: a triangle and a bar
                d = 1 if act == "next" else -1
                tri = QPainterPath()
                tri.moveTo(c.x() - 4 * d, c.y() - 5)
                tri.lineTo(c.x() + 3 * d, c.y())
                tri.lineTo(c.x() - 4 * d, c.y() + 5)
                tri.closeSubpath()
                qp.drawPath(tri)
                qp.drawRect(QRectF(c.x() + 3 * d - (1.6 if d > 0 else 0), c.y() - 5, 1.6, 10))
            self.buttons.append((rect.adjusted(-3, -3, 3, 3), key))

        def draw_system(self, qp: Any, x0: float, x1: float, y: float) -> float:
            """The PC's load: a bar per part, amber past 75 %, red past 90 %."""
            hw = self.hw
            y = self.page_head(qp, x0, x1, y, "system")
            rows: list[tuple[str, str, float | None]] = []
            if "cpu" in hw:
                rows.append(("CPU", f"{hw['cpu']:.0f}%", hw["cpu"]))
            if "ram" in hw:
                rows.append(("Memory", f"{hw['ram_used']:.1f} / {hw['ram_total']:.0f} GB", hw["ram"]))
            g = hw.get("gpu")
            if g:
                temp = f" · {g['temp']:.0f}°" if g.get("temp") is not None else ""
                rows.append(("GPU", f"{g['util']:.0f}%{temp}", g["util"]))
                rows.append(("Video memory", f"{g['mem_used']:.1f} / {g['mem_total']:.0f} GB", g.get("mem")))
            if "disk" in hw:
                rows.append(("Disk", f"{hw['disk_free']:.0f} GB free", hw["disk"]))
            if not rows:
                qp.setFont(F_TEXT)
                qp.setPen(QColor("#9aa1aa"))
                qp.drawText(QRectF(x0, y, x1 - x0, 30), Qt.AlignmentFlag.AlignVCenter, "Reading…")
                return y + 34
            for label, value, pct in rows:
                qp.setFont(F_SMALL)
                qp.setPen(QColor("#c4c8ce"))
                qp.drawText(QRectF(x0, y, 140, 16), Qt.AlignmentFlag.AlignVCenter, label)
                qp.setFont(F_TINY)
                qp.setPen(QColor("#9aa1aa"))
                qp.drawText(QRectF(x1 - 160, y, 160, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                            value)
                bar = QRectF(x0, y + 19, x1 - x0, 4)
                qp.setPen(Qt.PenStyle.NoPen)
                qp.setBrush(QColor(255, 255, 255, 18))
                qp.drawRoundedRect(bar, 2, 2)
                if pct is not None:
                    qp.setBrush(QColor(BAR[level(pct)]))
                    qp.drawRoundedRect(QRectF(bar.x(), bar.y(), bar.width() * max(0.02, min(1.0, pct / 100)), 4),
                                       2, 2)
                y += 31
            if "down" in hw:
                qp.setFont(F_SMALL)
                qp.setPen(QColor("#c4c8ce"))
                qp.drawText(QRectF(x0, y, 100, 18), Qt.AlignmentFlag.AlignVCenter, "Network")
                qp.setFont(F_TINY)
                qp.setPen(QColor("#9aa1aa"))
                qp.drawText(QRectF(x1 - 200, y, 200, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                            f"↓ {rate(hw['down'])}   ↑ {rate(hw['up'])}")
                y += 24
            return y + 2

        def draw_calendar(self, qp: Any, x0: float, x1: float, y: float) -> float:
            """This month, today marked; today's weather (Helios > Settings > weather town) above it."""
            now = time.localtime()
            qp.setFont(F_TITLE)
            qp.setPen(QColor("#f4f5f6"))
            qp.drawText(QRectF(x0, y, 200, 24), Qt.AlignmentFlag.AlignVCenter, time.strftime("%B %Y", now))
            w = self.weather
            if w.get("place"):
                temp = f"{round(w['now'])}°" if w.get("now") is not None else ""
                qp.setFont(F_TITLE)
                tw = QFontMetrics(F_TITLE).horizontalAdvance(temp)
                qp.drawText(QRectF(x1 - tw - 2, y, tw + 2, 24), Qt.AlignmentFlag.AlignVCenter, temp)
                qp.setFont(F_SMALL)
                qp.setPen(QColor("#9aa1aa"))
                line = ", ".join(x for x in (
                    w.get("sky"), f"{round(w['low'])}–{round(w['high'])}°" if w.get("low") is not None else "",
                    f"{round(w['rain'])}% rain" if (w.get("rain") or 0) >= 30 else "") if x)
                qp.drawText(QRectF(x0, y + 24, x1 - x0, 18), RIGHT_MID,
                            QFontMetrics(F_SMALL).elidedText(f"{w['place']} · {line}", Qt.TextElideMode.ElideRight,
                                                              int(x1 - x0)))
            else:
                qp.setFont(F_SMALL)
                qp.setPen(QColor("#5d6670"))
                qp.drawText(QRectF(x0, y + 24, x1 - x0, 18), RIGHT_MID,
                            "weather: set your town in Helios > Settings")
            y += 50
            first = 0  # Monday, as Sri Lanka's calendars do
            cw = (x1 - x0) / 7
            qp.setFont(F_TINY)
            qp.setPen(QColor("#5d6670"))
            for i, d in enumerate(weekday_letters(first)):
                qp.drawText(QRectF(x0 + i * cw, y, cw, 14), Qt.AlignmentFlag.AlignCenter, d)
            y += 18
            for week in month_grid(now.tm_year, now.tm_mon, first):
                for i, d in enumerate(week):
                    if not d:
                        continue
                    cell = QRectF(x0 + i * cw, y, cw, 22)
                    today = d == now.tm_mday
                    if today:
                        qp.setPen(Qt.PenStyle.NoPen)
                        qp.setBrush(QColor("#4cc2ff"))
                        qp.drawEllipse(cell.center(), 10.5, 10.5)
                    qp.setFont(F_DAY)
                    qp.setPen(QColor("#0b0b0c" if today else "#6f7680" if i >= 5 else "#d4d7dc"))
                    qp.drawText(cell, Qt.AlignmentFlag.AlignCenter, str(d))
                y += 24
            return y + 4

        def line(self, qp: Any, x0: float, x1: float, y: float, dot: str, text: str, right: str,
                 hot: bool = False) -> float:
            qp.setPen(Qt.PenStyle.NoPen)
            qp.setBrush(QColor(dot))
            qp.drawEllipse(QPointF(x0 + 3, y + 11), 3, 3)
            qp.setFont(F_SMALL)
            fs = QFontMetrics(F_SMALL)
            rw = fs.horizontalAdvance(right) + 4
            qp.setPen(QColor("#e4e6e8" if hot else "#6f7680"))
            qp.drawText(QRectF(x1 - rw - 4, y, rw, 22), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                        right)
            qp.setFont(F_TEXT)
            qp.setPen(QColor("#e8eaec"))
            left = QFontMetrics(F_TEXT).elidedText(text, Qt.TextElideMode.ElideRight, int(x1 - x0 - 16 - rw - 10))
            qp.drawText(QRectF(x0 + 14, y, x1 - x0 - 14 - rw, 22), Qt.AlignmentFlag.AlignVCenter, left)
            return y + 26

        def chip(self, qp: Any, rect: Any, key: str, text: str) -> None:
            hot = self.hot == key
            qp.setPen(QPen(QColor(255, 255, 255, 26 if hot else 16), 1))
            qp.setBrush(QColor("#1c1e22" if hot else "#141518"))
            qp.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), rect.height() / 2, rect.height() / 2)
            ink = QColor("#f1f2f3" if hot else "#c4c8ce")
            self.glyph(qp, QPointF(rect.x() + 15, rect.center().y()), icon_for(key[3:]), QColor("#9aa1aa") if not hot
                       else ink)
            qp.setPen(ink)
            qp.setFont(F_CHIP)
            qp.drawText(rect.adjusted(28, 0, -10, 0), Qt.AlignmentFlag.AlignVCenter, text)
            self.buttons.append((rect, key))

        def glyph(self, qp: Any, c: Any, kind: str, color: Any) -> None:
            """A 12 px line icon centred on c."""
            qp.save()
            qp.translate(c)
            pen = QPen(color, 1.3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            qp.setPen(pen)
            qp.setBrush(Qt.BrushStyle.NoBrush)
            path = QPainterPath()
            if kind == "inbox":
                path.moveTo(-5.5, 0.5)
                path.lineTo(-3.5, -4.5)
                path.lineTo(3.5, -4.5)
                path.lineTo(5.5, 0.5)
                path.lineTo(5.5, 4.5)
                path.lineTo(-5.5, 4.5)
                path.closeSubpath()
                path.moveTo(-5.5, 0.5)
                path.lineTo(-2, 0.5)
                path.lineTo(-1, 2)
                path.lineTo(1, 2)
                path.lineTo(2, 0.5)
                path.lineTo(5.5, 0.5)
            elif kind == "grid":
                for dx, dy in ((-5, -5), (1, -5), (-5, 1), (1, 1)):
                    path.addRoundedRect(QRectF(dx, dy, 4, 4), 1, 1)
            elif kind == "chat":
                path.addRoundedRect(QRectF(-5.5, -4.5, 11, 8), 3, 3)
                path.moveTo(-2.5, 3.5)
                path.lineTo(-3.5, 6)
                path.lineTo(0, 3.5)
            elif kind == "page":
                path.addRoundedRect(QRectF(-4.5, -5.5, 9, 11), 1.5, 1.5)
                path.moveTo(-2, -2)
                path.lineTo(2, -2)
                path.moveTo(-2, 1)
                path.lineTo(2, 1)
            elif kind == "moon":
                path.moveTo(2.5, -5)
                path.cubicTo(-5, -5.5, -6, 5.5, 0.5, 5.5)
                path.cubicTo(3, 5.5, 5, 3.5, 5.5, 2)
                path.cubicTo(1, 3, -1.5, -2, 2.5, -5)
            elif kind == "power":
                path.moveTo(0, -5.5)
                path.lineTo(0, 0)
                path.arcMoveTo(QRectF(-5, -4.5, 10, 10), 60)
                path.arcTo(QRectF(-5, -4.5, 10, 10), 60, -300)
            elif kind == "spark":
                path.moveTo(0, -5.5)
                path.cubicTo(0.5, -1, 1, -0.5, 5.5, 0)
                path.cubicTo(1, 0.5, 0.5, 1, 0, 5.5)
                path.cubicTo(-0.5, 1, -1, 0.5, -5.5, 0)
                path.cubicTo(-1, -0.5, -0.5, -1, 0, -5.5)
            elif kind == "bolt":
                path.moveTo(1, -5.5)
                path.lineTo(-3.5, 1)
                path.lineTo(0, 1)
                path.lineTo(-1, 5.5)
                path.lineTo(3.5, -1)
                path.lineTo(0, -1)
                path.closeSubpath()
            elif kind == "globe":
                path.addEllipse(QPointF(0, 0), 5.5, 5.5)
                path.addEllipse(QPointF(0, 0), 2.3, 5.5)
                path.moveTo(-5.5, 0)
                path.lineTo(5.5, 0)
            elif kind == "bell":
                path.moveTo(-4.5, 3)
                path.cubicTo(-3.5, 2, -3.5, 0, -3.5, -1)
                path.cubicTo(-3.5, -6, 3.5, -6, 3.5, -1)
                path.cubicTo(3.5, 0, 3.5, 2, 4.5, 3)
                path.closeSubpath()
                path.moveTo(-1.2, 5)
                path.lineTo(1.2, 5)
            else:
                path.addEllipse(QPointF(0, 0), 2, 2)
            qp.drawPath(path)
            qp.restore()

        def round_key(self, qp: Any, rect: Any, key: str, icon: str) -> None:
            """A round key: Talk (a green mic) or "···" (Helios)."""
            hot = self.hot == key
            talk = icon == "mic"
            edge = QColor(52, 211, 153, 110 if hot else 70) if talk else QColor(255, 255, 255, 26 if hot else 16)
            qp.setPen(QPen(edge, 1))
            qp.setBrush(QColor("#11261c" if talk and hot else "#0e1d16" if talk else "#1c1e22" if hot else "#141518"))
            qp.drawEllipse(rect.adjusted(0.5, 0.5, -0.5, -0.5))
            c = rect.center()
            if talk:
                pen = QPen(QColor("#4ade80" if hot else "#34d399"), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
                qp.setPen(pen)
                qp.setBrush(Qt.BrushStyle.NoBrush)
                qp.drawRoundedRect(QRectF(c.x() - 3, c.y() - 7.5, 6, 10), 3, 3)
                arc = QPainterPath()
                arc.moveTo(c.x() - 5.5, c.y() - 0.5)
                arc.cubicTo(c.x() - 5.5, c.y() + 5.5, c.x() + 5.5, c.y() + 5.5, c.x() + 5.5, c.y() - 0.5)
                qp.drawPath(arc)
                qp.drawLine(QPointF(c.x(), c.y() + 4), QPointF(c.x(), c.y() + 7))
            elif icon in ("game", "game-on"):  # a game pad: game mode (amber while on)
                col = QColor("#f5a524" if icon == "game-on" else ("#d4d7dc" if hot else "#8b929b"))
                qp.setPen(QPen(col, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                qp.setBrush(Qt.BrushStyle.NoBrush)
                qp.drawRoundedRect(QRectF(c.x() - 8, c.y() - 4.5, 16, 9), 4.5, 4.5)
                qp.drawLine(QPointF(c.x() - 5.5, c.y()), QPointF(c.x() - 2.5, c.y()))
                qp.drawLine(QPointF(c.x() - 4, c.y() - 1.5), QPointF(c.x() - 4, c.y() + 1.5))
                qp.setPen(Qt.PenStyle.NoPen)
                qp.setBrush(col)
                qp.drawEllipse(QPointF(c.x() + 3.2, c.y() - 0.8), 1.1, 1.1)
                qp.drawEllipse(QPointF(c.x() + 5.4, c.y() + 1.2), 1.1, 1.1)
            elif icon in ("power", "start"):  # power: turn Ari off; start (green): turn it back on
                col = QColor("#34d399" if icon == "start" else ("#f87171" if hot else "#8b929b"))
                pen = QPen(col, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
                qp.setPen(pen)
                qp.setBrush(Qt.BrushStyle.NoBrush)
                qp.drawArc(QRectF(c.x() - 6, c.y() - 5.5, 12, 12), 120 * 16, 300 * 16)
                qp.drawLine(QPointF(c.x(), c.y() - 7), QPointF(c.x(), c.y() - 1))
            else:
                qp.setPen(Qt.PenStyle.NoPen)
                qp.setBrush(QColor("#d4d7dc" if hot else "#8b929b"))
                for dx in (-5, 0, 5):
                    qp.drawEllipse(QPointF(c.x() + dx, c.y()), 1.4, 1.4)
            self.buttons.append((rect, key))

        # -------------------------------------------------------------- mouse

        def key_at(self, pos: Any) -> str | None:
            for rect, key in self.buttons:
                if rect.contains(QPointF(pos)):
                    return key
            return None

        def enterEvent(self, _e: Any) -> None:  # noqa: N802
            self.leave.stop()
            if not self.hover:
                self.hover = True
                self.kick()

        def leaveEvent(self, _e: Any) -> None:  # noqa: N802
            self.hover, self.hot = False, None
            if self.details:
                self.leave.start()
            self.kick()

        def mouseMoveEvent(self, e: Any) -> None:  # noqa: N802
            inside = self.shape_rect().contains(QPointF(e.position()))
            key = self.key_at(e.position()) if inside else None
            if key != self.hot:
                self.hot = key
                self.setCursor(Qt.CursorShape.PointingHandCursor if (key or inside) else Qt.CursorShape.ArrowCursor)
                self.update()
            if inside != self.hover:
                self.hover = inside
                self.kick()

        def mousePressEvent(self, e: Any) -> None:  # noqa: N802
            if not self.shape_rect().contains(QPointF(e.position())):
                return
            key = self.key_at(e.position()) if (self.details or self.mode() == "active") else None
            if key == "stop":
                self.stop_ari()
                return
            if key is None:
                (self.close_details if self.details else self.open_details)()
                return
            if key == "power":
                self.toggle_power()
            elif key == "game":
                self.toggle_game()
            elif key == "gameauto":
                self.toggle_game_auto()
            elif key == "talk":
                self.talk()
            elif key.startswith(("helios:", "show:")):
                target = open_target(args.url, key)
                if target:
                    webbrowser.open(target)
                self.close_details()
            elif key.startswith("sc:"):
                self.shortcut(key[3:])
            elif key.startswith("page:"):
                self.flip(int(key[5:]) - self.page)
            elif key.startswith("media:"):
                self.media_do(key[6:])

        def wheelEvent(self, e: Any) -> None:  # noqa: N802
            """Scroll on the opened island to flip its pages (a mouse notch or a touchpad swipe, one page at a
            time)."""
            if not self.details:
                return
            now = time.monotonic()
            if now - self.wheel_at > 0.4:
                self.wheel_acc = 0.0
            self.wheel_acc += e.angleDelta().y() or -e.angleDelta().x()
            if abs(self.wheel_acc) >= 100 and now - self.wheel_at > 0.22:
                self.flip(-1 if self.wheel_acc > 0 else 1)
                self.wheel_acc, self.wheel_at = 0.0, now
            e.accept()

        def flip(self, step: int) -> None:
            n = len(self.pages())
            new = max(0, min(n - 1, self.page + step))
            if new != self.page:
                self.page = new
                self.leave.stop()
                self.kick()

        def media_do(self, action: str) -> None:
            if self.media and action == "play_pause":  # show it at once; the next read confirms
                self.media = {**self.media, "playing": not self.media.get("playing"),
                              "paused": bool(self.media.get("playing")),
                              "pos": position_now(self.media.get("pos"), self.media.get("end"),
                                                  self.media.get("updated"), bool(self.media.get("playing"))),
                              "updated": time.time()}
                self.update()

            def go() -> None:
                live.media.control(action)
                time.sleep(0.35)
                live.poke("media")
            threading.Thread(target=go, daemon=True).start()

        def toggle_hidden(self) -> None:
            """Ctrl+Alt+D: the island goes away (nothing runs while it's hidden) or comes back."""
            if self.isVisible():
                self.close_details()
                self.hide()
            else:
                self.show()
                self.place()
            self.kick()

        def stop_ari(self) -> None:
            """Stop: Ari goes quiet at once and the answer it was working on is dropped."""
            job = self.ari.think_job if self.ari.phase in ("thinking", "working") else None
            self.ari.phase, self.ari.text = "idle", ""
            self.kick()

            def go() -> None:
                try:
                    api.call("POST", "/ari/stop", {"job": job or ""})
                except Exception:  # noqa: BLE001 - Argus out of reach: say so
                    bus.flash.emit("Stop didn't reach Argus")
            threading.Thread(target=go, daemon=True).start()

        def toggle_game(self) -> None:
            """Game mode on (Ari off, the GPU freed for the game) or off (Ari back)."""
            on = not self.ari.game
            self.ari.game = on
            self.kick()

            def go() -> None:
                try:
                    api.call("POST", "/ari/game", {"on": on, "by": "you"})
                except Exception:  # noqa: BLE001
                    bus.flash.emit("Couldn't reach Argus")
            threading.Thread(target=go, daemon=True).start()

        def toggle_game_auto(self) -> None:
            auto = not self.ari.game_auto
            self.ari.game_auto = auto
            self.kick()

            def go() -> None:
                try:
                    api.call("POST", "/ari/game", {"auto": auto})
                except Exception:  # noqa: BLE001
                    bus.flash.emit("Couldn't reach Argus")
            threading.Thread(target=go, daemon=True).start()

        def toggle_power(self) -> None:
            """Ari off (no listening, quiet, answers on their way dropped) or back on."""
            on = self.ari.off
            self.ari.off = not on
            self.kick()

            def go() -> None:
                try:
                    api.call("POST", "/ari/power", {"on": on})
                except Exception:  # noqa: BLE001 - Argus out of reach: say so
                    bus.flash.emit("Couldn't reach Argus")
            threading.Thread(target=go, daemon=True).start()

        def talk(self) -> None:
            def go() -> None:
                try:
                    r = api.call("POST", "/ari/wake")
                except Exception:
                    r = {"listener": False}
                if not r.get("listener"):  # no "Hey Ari" listener on this PC: Helios's mic instead
                    webbrowser.open(f"{args.url.rstrip('/')}/helios/?app=1&mic=1#ari")
            threading.Thread(target=go, daemon=True).start()
            self.close_details()

        def shortcut(self, action: str) -> None:
            target = open_target(args.url, action)
            if target:
                webbrowser.open(target)
                self.close_details()
                return
            label = next((s["label"] for s in self.info.shortcuts if s["action"] == action), action)
            bus.flash.emit(f"{label}…")

            def go() -> None:
                try:
                    api.call("POST", "/island/run", {"action": action}, timeout=15)
                    bus.flash.emit(f"{label} ✓")
                except Exception as e:  # noqa: BLE001 - shown on the island
                    bus.flash.emit(f"{label}: {str(e)[:60]}")
            threading.Thread(target=go, daemon=True).start()

    def two_lines(text: str, fm: Any, width: int) -> str:
        """The text in at most two lines; the second is cut with … when there is more."""
        words = text.split()
        first = ""
        while words and (not first or fm.horizontalAdvance(f"{first} {words[0]}") <= width):
            first = f"{first} {words.pop(0)}".strip()
        if not words:
            return first
        return first + "\n" + fm.elidedText(" ".join(words), Qt.TextElideMode.ElideRight, width)

    def mouse_down() -> bool:
        if sys.platform != "win32":
            return bool(QApplication.mouseButtons() & Qt.MouseButton.LeftButton)
        import ctypes

        return bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)

    class AskBox(QWidget):
        """Ari's task needs you: the question under the island, with a text box, choices, or Yes / No. Answering
        here is the same as in Helios or on the phone; answered elsewhere, it goes away."""

        STYLE = ("QWidget#ask{background:#0e1012;border:1px solid #2a2f36;border-radius:14px}"
                 "QLabel{color:#e4e6e8;font-size:13px} QLabel#t{font-weight:600}"
                 "QLineEdit{background:#16191d;color:#e4e6e8;border:1px solid #2a2f36;border-radius:8px;padding:6px}"
                 "QPushButton{background:#1c2026;color:#e4e6e8;border:1px solid #2a2f36;border-radius:8px;"
                 "padding:5px 12px} QPushButton#go{background:#2f6fed;border-color:#2f6fed}"
                 "QCheckBox{color:#9aa3b5}")

        def __init__(self) -> None:
            super().__init__()
            self.setObjectName("ask")
            self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                                | Qt.WindowType.Tool)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
            self.setStyleSheet(self.STYLE)
            self.parts: dict = {}
            bus.ask.connect(self.show_ask)
            bus.answered.connect(self.answered)

        def show_ask(self, d: dict) -> None:
            for c in self.findChildren(QWidget):
                c.deleteLater()
            if self.layout() is not None:
                QWidget().setLayout(self.layout())  # drop the old layout
            self.parts = p = ask_parts(d)
            box = QVBoxLayout(self)
            box.setContentsMargins(14, 12, 14, 12)
            t = QLabel(p["title"])
            t.setObjectName("t")
            box.addWidget(t)
            for ln in p["lines"]:
                lab = QLabel(ln)
                lab.setWordWrap(True)
                box.addWidget(lab)
            if p["picture"]:
                pic = QLabel()
                box.addWidget(pic)
                threading.Thread(target=self.load_picture, args=(pic, p["approval"]), daemon=True).start()
            self.text = self.keep = None
            if p["choices"]:
                row = QHBoxLayout()
                for c in p["choices"]:
                    b = QPushButton(c)
                    b.clicked.connect(lambda _=False, c=c: self.send(c))
                    row.addWidget(b)
                box.addLayout(row)
            if p["answer"]:
                self.text = QLineEdit()
                self.text.setPlaceholderText("Type your answer")
                self.text.returnPressed.connect(lambda: self.send(self.text.text()))
                box.addWidget(self.text)
            if p["remember"]:
                self.keep = QCheckBox("Remember this for next time")
                box.addWidget(self.keep)
            row = QHBoxLayout()
            no = QPushButton("No" if p["confirm"] else "Skip")
            no.clicked.connect(lambda: self.send(None, yes=False))
            go = QPushButton("Yes" if p["confirm"] else "Send")
            go.setObjectName("go")
            go.clicked.connect(lambda: self.send(self.text.text() if self.text is not None else None))
            row.addStretch(1)
            row.addWidget(no)
            row.addWidget(go)
            box.addLayout(row)
            self.setFixedWidth(420)
            self.adjustSize()
            g = QApplication.primaryScreen().geometry()
            self.move(g.x() + (g.width() - self.width()) // 2, g.y() + 64)
            self.show()

        def load_picture(self, label: Any, aid: str | None) -> None:
            try:
                img = str(api.call("GET", f"/approvals/{aid}")["payload"].get("image") or "")
                raw = base64.b64decode(img.split(",", 1)[1]) if "," in img else b""
            except Exception:  # noqa: BLE001
                return
            q = QImage.fromData(raw)
            if not q.isNull():
                from PySide6.QtGui import QPixmap  # type: ignore[import-not-found]

                QTimer.singleShot(0, lambda: (label.setPixmap(QPixmap.fromImage(q).scaledToWidth(390)),
                                              self.adjustSize()))

        def send(self, text: str | None, yes: bool = True) -> None:
            p = self.parts
            body = decision(p, text, bool(self.keep and self.keep.isChecked()), yes)
            if not p["confirm"] and yes and not (text or "").strip():
                return  # Send with nothing typed: wait for an answer
            self.hide()
            threading.Thread(target=lambda: api.call("POST", f"/approvals/{p['approval']}/decide", body),
                             daemon=True).start()

        def answered(self, aid: str) -> None:
            if self.parts.get("approval") == aid:
                self.hide()

    class QuickAsk(QWidget):
        """Ctrl+Alt+Space: a box under the island to type to Ari. Enter sends it (the island shows Ari thinking and
        the answer, as when you speak), Esc or a click elsewhere closes it. What you copied is one click away."""

        STYLE = ("QWidget#quick{background:#0b0c0e;border:1px solid #2a2f36;border-radius:16px}"
                 "QLineEdit{background:transparent;color:#f1f2f3;border:none;font-size:15px;padding:4px 2px}"
                 "QPushButton{background:#15181c;color:#9aa1aa;border:1px solid #23272d;border-radius:10px;"
                 "padding:4px 10px;text-align:left;font-size:12px} QPushButton:hover{color:#e4e6e8}"
                 "QLabel{color:#5d6670;font-size:11px}")

        def __init__(self) -> None:
            super().__init__()
            self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                                | Qt.WindowType.Tool)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)  # round corners, nothing behind them
            self.setStyleSheet(self.STYLE)
            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            frame = QWidget()
            frame.setObjectName("quick")
            frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
            outer.addWidget(frame)
            box = QVBoxLayout(frame)
            box.setContentsMargins(16, 10, 16, 10)
            box.setSpacing(6)
            self.edit = QLineEdit()
            self.edit.setPlaceholderText("Ask Ari…")
            self.edit.returnPressed.connect(self.send)
            box.addWidget(self.edit)
            self.copied = QPushButton()
            self.copied.clicked.connect(self.use_copied)
            box.addWidget(self.copied)
            self.hint = QLabel("Enter to send · Esc to close")
            box.addWidget(self.hint)
            self.setFixedWidth(460)

        def open(self) -> None:
            text = (QApplication.clipboard().text() or "").strip()
            self.clip = " ".join(text.split())[:900] if 0 < len(text) < 2000 else ""
            self.copied.setVisible(bool(self.clip))
            if self.clip:
                fm = QFontMetrics(self.copied.font())
                self.copied.setText(fm.elidedText(f"What you copied: “{self.clip}”", Qt.TextElideMode.ElideRight, 400))
            self.edit.clear()
            self.adjustSize()
            g = QApplication.primaryScreen().geometry()
            self.move(g.x() + (g.width() - self.width()) // 2, g.y() + 46)
            self.show()
            self.raise_()
            self.activateWindow()
            if sys.platform == "win32":  # the key press lets this window take the keyboard
                import ctypes

                ctypes.windll.user32.SetForegroundWindow(int(self.winId()))
            self.edit.setFocus()

        def use_copied(self) -> None:
            cur = self.edit.text().strip()
            self.edit.setText(f"{cur} {self.clip}".strip() if cur else self.clip)
            self.edit.setFocus()

        def send(self) -> None:
            text = self.edit.text().strip()[:1000]
            self.hide()
            if not text:
                return

            def go() -> None:
                try:
                    api.call("POST", "/ari", {"text": text, "source": "island"}, timeout=60)
                except Exception:  # noqa: BLE001
                    bus.flash.emit("Couldn't reach Argus")
            threading.Thread(target=go, daemon=True).start()

        def keyPressEvent(self, e: Any) -> None:  # noqa: N802
            if e.key() == Qt.Key.Key_Escape:
                self.hide()
            else:
                super().keyPressEvent(e)

        def changeEvent(self, e: Any) -> None:  # noqa: N802 - a click elsewhere closes it
            from PySide6.QtCore import QEvent  # type: ignore[import-not-found]

            if e.type() == QEvent.Type.ActivationChange and not self.isActiveWindow() and self.isVisible():
                QTimer.singleShot(150, lambda: None if self.isActiveWindow() else self.hide())
            super().changeEvent(e)

    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    island = Island()
    island.asker = AskBox()  # kept alive with the island
    quick = QuickAsk()
    if args.preview and args.ask:
        QApplication.clipboard().setText("the router keeps dropping the 5 GHz band every evening")
        quick.open()
        quick.grab().save(args.preview)
        return 0
    if args.preview:
        island.ari.phase, island.ari.text = args.phase, args.text
        island.hover = args.hover
        if args.hover and args.text.startswith("♪"):
            island.media = {"title": args.text[2:], "artist": "M83", "playing": True}
        elif args.hover and args.text == "mic":
            island.priv = {"mic": ["Discord"], "camera": []}
        elif args.text == "dots":
            island.priv = {"mic": ["Discord"], "camera": ["WindowsCamera"]}
        if args.details:
            island.details = True
            island.page = max(0, pages_of(island.info.widgets).index(args.page)
                              if args.page in pages_of(island.info.widgets) else 0)
            island.media = {"title": "Midnight City", "artist": "M83", "album": "Hurry Up, We're Dreaming",
                            "app": "Spotify", "playing": True, "pos": 97.0, "end": 243.0, "updated": None}
            island.hw = {"cpu": 23.0, "ram": 78.0, "ram_used": 24.9, "ram_total": 32, "disk": 93.0,
                         "disk_free": 61, "down": 2_400_000, "up": 180_000,
                         "gpu": {"util": 41.0, "mem_used": 7.4, "mem_total": 12, "mem": 62.0, "temp": 58.0}}
            island.weather = {"place": "Colombo", "now": 29.4, "low": 25.1, "high": 31.2, "rain": 80,
                              "sky": "thunderstorms"}
            island.info = Info(status={"status": "ok", "jobs": {"running": 1, "queued": 2}}, waiting=3,
                               next={"label": "homelab · check", "plugin": "homelab", "workflow": "check",
                                     "next_run_at": time.time() + 1800},
                               shortcuts=[{"label": "Sort downloads", "action": "run:x"},
                                          {"label": "Work mode", "action": "routine:work mode"},
                                          {"label": "Inbox", "action": "show:inbox"},
                                          {"label": "Helios", "action": "show:map"}])
        for _ in range(3):  # lay out (the details measure themselves), then settle the size
            island.spring.w, island.spring.h = island.target()
            island.grab()
        island.grab().save(args.preview)
        return 0
    island.show()
    app.primaryScreen().geometryChanged.connect(lambda _g: island.place())

    def on_hotkey(name: str) -> None:
        if name == "hide":
            island.toggle_hidden()
        elif name == "ask":
            quick.hide() if quick.isVisible() else quick.open()

    bus.hotkey.connect(on_hotkey)
    bus.keys.connect(lambda keys: listen_hotkeys({k: v for k, v in keys.items() if v}, bus.hotkey.emit,
                                                 lambda n: print(f"island: the {n} key is taken by another app",
                                                                 file=sys.stderr, flush=True)))

    def first_info() -> None:
        """The island's settings once at the start (its keys); Argus may still be starting."""
        while True:
            try:
                info = fetch_info(api)
                if info.status is not None:
                    bus.info.emit(info)
                    bus.keys.emit(info.keys)
                    return
            except Exception:  # noqa: BLE001
                pass
            time.sleep(5)

    threading.Thread(target=first_info, daemon=True, name="island-info").start()
    threading.Thread(target=live.run, daemon=True, name="island-live").start()

    def feed() -> None:
        """Follow Ari: a long poll of the event stream (Argus answers the moment something happens, else after 20 s,
        so nothing is asked every second all day), and "I'm here" every 30 s."""
        seq, last_ping = -1, 0.0
        while True:
            t0 = time.monotonic()
            try:
                if time.monotonic() - last_ping > 30:
                    api.call("POST", "/ari/popup")
                    last_ping = time.monotonic()
                if seq < 0:
                    seq = int(api.call("GET", "/events?kinds=ari.state&limit=1&newest=true").get("seq") or 0)
                    ears = api.call("GET", "/ari/listening")  # off already? (the red dot from the start)
                    bus.events.emit([{"kind": "ari.listening", "data": ears}])
                    try:
                        bus.events.emit([{"kind": "ari.game", "data": api.call("GET", "/ari/game")}])
                    except Exception:  # noqa: BLE001 - an older Argus without game mode
                        pass
                r = api.call("GET", "/events?kinds=ari.state,ari.listening,ari.game,step.running,ari.ask,"
                             f"approval.approved,approval.rejected,approval.expired&after={seq}&limit=200&wait=20",
                             timeout=30)
                evs = r.get("events") or []
                if evs:
                    seq = max(int(e["seq"]) for e in evs)
                    for e in evs:  # Ari's questions: the box under the island (gone once answered anywhere)
                        if e.get("kind") == "ari.ask":
                            bus.ask.emit(e.get("data") or {})
                        elif str(e.get("kind")).startswith("approval."):
                            bus.answered.emit(str((e.get("data") or {}).get("approval") or ""))
                    bus.events.emit([e for e in evs if e.get("kind") in ("ari.state", "ari.listening", "ari.game",
                                                                         "step.running")])
                elif time.monotonic() - t0 < 1:  # an older Argus that doesn't wait: don't ask in a tight loop
                    time.sleep(1.2)
            except Exception:  # Argus restarting or out of reach: look again later
                time.sleep(4)

    threading.Thread(target=feed, daemon=True, name="ari-feed").start()
    print("Ari's island is on (top of the screen).", flush=True)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
