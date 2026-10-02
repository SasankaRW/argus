"""Ari's island on the PC: a black shape grown out of the top edge of the main screen, over every app.

    python -m argus.ari_popup     (dev.ps1 up starts it when ari.popup is on; needs: pip install -e .[popup])

Native Qt (no browser inside): a few MB of memory, and nothing runs while it sits idle. It follows Ari through
Argus's event stream (a light poll):
  idle     a slim lip under the edge; hover shows "ari"
  active   it widens: listening / thinking / working / speaking, with what Ari is on
  done     the answer, two lines, then it folds back
  click    it opens down: the time, Talk (the PC's "Hey Ari" listener starts listening), how Argus is doing, what
           waits for you, the next job, your shortcuts (set up in Helios > Ari > island). Clicking anywhere else
           folds it again.
It runs on the machine with your desktop session (the PC), also after Argus itself moves to the laptop: set
ARGUS_URL to the laptop's address.
"""

from __future__ import annotations

import argparse
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

FOLLOW_S = 20.0  # how long Ari listens for a follow-up after answering (ari.talk_idle_s)
WORDS = {"listening": "listening", "thinking": "thinking", "working": "on it", "speaking": "speaking"}
NICE = {"search_my_files": "searching your files", "find_file": "looking for the file", "open_app": "opening it",
        "set_volume": "setting the volume", "argus_status": "checking Argus", "add_note": "writing it down",
        "run_routine": "running the routine", "lab_status": "checking the lab", "repo_status": "checking your repos",
        "look_at_screen": "looking at your screen", "summarise_clipboard": "reading what you copied",
        "weather": "checking the weather", "web_search": "searching the web", "read_page": "reading the page",
        "search_everything": "finding the file"}
TONE = {"listening": "#39ff9c", "done": "#39ff9c", "working": "#4cc2ff", "thinking": "#f5a524",
        "speaking": "#f5a524", "idle": "#f5a524"}


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

    def apply(self, events: list[dict], now: float) -> bool:
        """Feed events (oldest first). True when what shows changed."""
        changed = False
        for e in events:
            data = e.get("data") or {}
            if e["kind"] == "ari.state":
                phase = str(data.get("phase") or "idle")
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


def target_size(mode: str, text: str, hover: bool, details_h: float) -> tuple[float, float]:
    if mode == "details":
        return DETAILS_W, min(MAX_H, details_h)
    if mode in ("active", "done"):  # one look for both: Ari's answer stays in the same pill it spoke from
        return max(220, min(MAX_W, (100 if mode == "active" else 50) + len(text) * 7.0)), 36
    return HOVER if hover else IDLE


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


@dataclass
class Info:
    """What the opened island shows (fetched when it opens, and every 15 s while open)."""
    status: dict | None = None
    waiting: int = 0
    next: dict | None = None
    last: str | None = None
    widgets: list[dict] = field(default_factory=lambda: [{"id": w, "on": True} for w in
                                                        ("clock", "status", "inbox", "next", "shortcuts")])
    shortcuts: list[dict] = field(default_factory=list)


def fetch_info(api: Api) -> Info:
    info = Info()
    for path, fn in (
        ("/island", lambda r: (setattr(info, "widgets", r.get("widgets") or info.widgets),
                               setattr(info, "shortcuts", r.get("shortcuts") or []))),
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
        from PySide6.QtWidgets import QApplication, QWidget  # type: ignore[import-not-found]
    except ImportError as e:
        print(f"ari-popup needs its extras: pip install -e .[popup]  ({e})", file=sys.stderr)
        return 2

    api = Api(args.url, token)

    class Bus(QObject):
        events = Signal(list)
        info = Signal(object)
        flash = Signal(str)

    bus = Bus()

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

        def target(self) -> tuple[float, float]:
            label = WORDS.get(self.ari.phase, "")
            return target_size(self.mode(), f"{label}  {self.shown_text()}", self.hover, self.details_h)

        def kick(self) -> None:
            """Something changed: animate to the new size (the timer stops by itself once it settles)."""
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

        def load_info(self) -> None:
            threading.Thread(target=lambda: bus.info.emit(fetch_info(api)), daemon=True).start()

        def open_details(self) -> None:
            self.details = True
            self.load_info()
            self.refresh.start()
            self.clock.start()
            self.away.start()
            self.kick()

        def close_details(self) -> None:
            if not self.details:
                return
            self.details = False
            for t in (self.refresh, self.clock, self.away, self.leave):
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
                if self.hover and self.spring.h > 16:
                    qp.setPen(QColor("#5d6670"))
                    qp.setFont(F_LABEL)
                    qp.drawText(body, Qt.AlignmentFlag.AlignCenter, "a r i")
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
            info = self.info
            on = {w["id"] for w in info.widgets if w.get("on")}
            order = [w["id"] for w in info.widgets if w.get("on")]
            fade = max(0.0, min(1.0, (self.spring.h - 70) / 40))
            qp.setOpacity(fade)
            x0, x1 = body.x() + 16, body.right() - 16
            y = body.y() + 12
            # header: the time and date on the left; Talk (a round mic key) and "···" (Helios) on the right
            talk = QRectF(x1 - 30, y, 30, 30)
            more = QRectF(talk.x() - 36, y, 30, 30)
            if "clock" in on:
                now = time.localtime()
                qp.setFont(F_CLOCK)
                qp.setPen(QColor("#f4f5f6"))
                tstr = time.strftime("%H:%M", now)
                tw = QFontMetrics(F_CLOCK).horizontalAdvance(tstr)
                qp.drawText(QRectF(x0, y, tw + 4, 30), Qt.AlignmentFlag.AlignVCenter, tstr)
                qp.setFont(F_SMALL)
                qp.setPen(QColor("#6f7680"))
                qp.drawText(QRectF(x0 + tw + 9, y + 1, more.x() - x0 - tw - 12, 30), Qt.AlignmentFlag.AlignVCenter,
                            time.strftime("%a %d %b", now))
            else:
                qp.setFont(F_TEXT)
                qp.setPen(QColor("#e4e6e8"))
                qp.drawText(QRectF(x0, y, 120, 30), Qt.AlignmentFlag.AlignVCenter, "Ari")
            self.round_key(qp, more, "helios:#ari", "more")
            self.round_key(qp, talk, "talk", "mic")
            y += 42
            # status rows in one quiet card
            st = info.status or {}
            jobs = st.get("jobs") or {}
            rows: list[tuple[str, str, str, str | None]] = []  # dot, text, right, click key
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
            if self.flash_text:
                qp.setFont(F_SMALL)
                qp.setPen(QColor("#39ff9c"))
                qp.drawText(QRectF(x0, y + 2, x1 - x0, 18), Qt.AlignmentFlag.AlignLeft, self.flash_text)
                y += 22
            qp.setOpacity(1)
            h = y - body.y() + 6
            if abs(h - self.details_h) > 1:
                self.details_h = h
                QTimer.singleShot(0, self.kick)

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
            if key == "talk":
                self.talk()
            elif key.startswith(("helios:", "show:")):
                target = open_target(args.url, key)
                if target:
                    webbrowser.open(target)
                self.close_details()
            elif key.startswith("sc:"):
                self.shortcut(key[3:])

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

    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    island = Island()
    if args.preview:
        island.ari.phase, island.ari.text = args.phase, args.text
        island.hover = args.hover
        if args.details:
            island.details = True
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
                r = api.call("GET", f"/events?kinds=ari.state,step.running&after={seq}&limit=200&wait=20",
                             timeout=30)
                evs = r.get("events") or []
                if evs:
                    seq = max(int(e["seq"]) for e in evs)
                    bus.events.emit(evs)
                elif time.monotonic() - t0 < 1:  # an older Argus that doesn't wait: don't ask in a tight loop
                    time.sleep(1.2)
            except Exception:  # Argus restarting or out of reach: look again later
                time.sleep(4)

    threading.Thread(target=feed, daemon=True, name="ari-feed").start()
    print("Ari's island is on (top of the screen).", flush=True)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
