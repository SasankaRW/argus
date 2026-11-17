"""Ari's mark on the island: five pieces that flow into a different shape for each job (approved 2026-11-17).

Pure geometry, no Qt, so it can be tested: `Mark.step()` returns what to draw this frame in local units (the mark's
centre at 0, 0; about 24 wide, 12 tall), and the island scales and paints it. Every piece moves on a small spring,
so one move flows into the next with a little overshoot.

  ready      a soft mound of bars that breathes; now and then a shimmer passes through
  listening  tall bars, spread apart, moving like a voice
  thinking   Material 3 Expressive's loading indicator: one shape turning and morphing through the M3 shapes
  searching  a thin line with a light that reads along it: hop, rest, hop, then back to the next line
  working    Material 3's wavy progress: a wave flowing along a track
  speaking   bars rippling from the middle out, syllable by syllable
  done       Helios's scalloped badge pops in and turns slowly, a tick drawn inside it
  failed     a "!" that wobbles on a spring and settles
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

WHITE, GREEN, AMBER, BLUE, CORAL = "#c8c8cc", "#8fe3ad", "#ffc58f", "#a6c8ff", "#ffb4ab"
TONES = {"ready": WHITE, "listening": GREEN, "thinking": AMBER, "searching": BLUE, "working": BLUE,
         "speaking": AMBER, "done": GREEN, "failed": CORAL}
SEARCHY = ("search", "finding", "looking", "reading", "checking")  # a tool that looks things up reads; others act
FAILED = re.compile(r"^\s*(sorry|i (couldn't|could not|can't|cannot)|that didn't|something went wrong)", re.I)


def clamp(x: float, a: float = 0.0, b: float = 1.0) -> float:
    return max(a, min(b, x))


def smooth(x: float) -> float:
    return x * x * (3 - 2 * x)


def ease_out(x: float) -> float:
    return 1 - (1 - x) ** 3


def _hash(n: float) -> float:
    x = math.sin(n * 127.1 + 311.7) * 43758.5453
    return x - math.floor(x)


def breath(t: float, period: float = 4.8, phase: float = 0.0) -> float:
    """Like a person breathing: in for 40 % (eased), out for 45 %, a short rest. 0 empty, 1 full."""
    p = ((t + phase) % period) / period
    if p < 0.4:
        return smooth(p / 0.4)
    if p < 0.85:
        return 1 - smooth((p - 0.4) / 0.45)
    return 0.0


class Speech:
    """A made-up voice: syllables of different length and loudness, pauses between words. Deterministic."""

    def __init__(self, seed: int) -> None:
        self.syl: list[tuple[float, float, float]] = []
        t = 0.0
        for i in range(400):
            t += 0.18 + 0.3 * _hash(seed + i * 7.7) if _hash(seed + i * 3.1) < 0.2 else 0.02
            dur = 0.11 + 0.17 * _hash(seed + i * 1.3)
            self.syl.append((t, dur, 0.45 + 0.55 * _hash(seed + i * 5.9)))
            t += dur
        self.length = t

    def level(self, t: float, band: int = 0) -> float:
        tt = t % self.length
        lo, hi = 0, len(self.syl) - 1
        while lo < hi:
            m = (lo + hi + 1) // 2
            if self.syl[m][0] <= tt:
                lo = m
            else:
                hi = m - 1
        s0, d, amp = self.syl[lo]
        u = (tt - s0) / d
        if u < 0 or u > 1.6:
            return 0.0
        env = u / 0.18 if u < 0.18 else math.exp(-(u - 0.18) * 2.6)  # a quick rise, a softer fall
        return clamp(env * amp * (0.75 + 0.25 * math.sin(t * (17 + band * 5) + band * 1.7)))


VOICES = {"ari": Speech(3), "you": Speech(11)}
KEYS = ("x1", "y1", "x2", "y2", "w", "o")


@dataclass
class Seg:
    """A round-capped stroke: a bar, a dot (zero length) or a line."""
    x1: float
    y1: float
    x2: float
    y2: float
    w: float = 1.8
    o: float = 1.0


def _bar(x: float, h: float, w: float = 1.8, o: float = 1.0) -> Seg:
    return Seg(x, -h / 2, x, h / 2, w, o)


def _gone(x: float = 0.0, y: float = 0.0) -> Seg:
    return Seg(x, y, x, y, 0.1, 0.0)


def _bx(i: int, gap: float = 3.2) -> float:
    return (i - 2) * gap


def _star(n: int, depth: float, stretch: float, radius: float, rot: float) -> list[tuple[float, float]]:
    """A round shape with n soft bumps (or none), optionally stretched into a pill: the M3 shapes, by numbers."""
    pts = []
    for j in range(97):
        th = j / 96 * 2 * math.pi
        r = radius * (1 + depth * math.cos(n * (th - rot))) * (1 + stretch * math.cos(2 * (th - rot)))
        pts.append((r * math.cos(th), r * math.sin(th)))
    return pts


M3_SHAPES = ((10, 0.10, 0.0), (9, 0.18, 0.0), (5, 0.12, 0.0), (2, 0.0, 0.32), (8, 0.20, 0.0), (0, 0.0, 0.0))


def shape(state: str, t: float, clock: float) -> tuple[list[Seg], list[tuple]]:
    """The five pieces and any extras (("fill", points, colour), ("stroke", points, colour, width, alpha)) for a
    state, t seconds after it began, at `clock` seconds."""
    out: list[Seg] = []
    ex: list[tuple] = []
    tone = TONES.get(state, WHITE)
    if state == "listening":
        for i in range(5):
            lv = VOICES["you"].level(clock - abs(i - 2) * 0.02, i)
            out.append(_bar(_bx(i, 3.7), 2.6 + 9.5 * lv * (1 - abs(i - 2) * 0.16), 2.0))
    elif state == "speaking":
        for i in range(5):
            d = abs(i - 2)
            out.append(_bar(_bx(i, 3.3), 2.2 + 9.5 * VOICES["ari"].level(clock - d * 0.07, d) * (1 - d * 0.2)))
    elif state == "thinking":
        hold = 0.55
        step = clock / hold
        i, f = int(step) % len(M3_SHAPES), step - math.floor(step)
        k = 0.0 if f < 0.55 else ease_out((f - 0.55) / 0.45)  # rest on a shape, then morph (emphasized decelerate)
        a, b = M3_SHAPES[i], M3_SHAPES[(i + 1) % len(M3_SHAPES)]
        rot = clock * 2.4 + (i + k) * 0.9
        grow = clamp(t / 0.35)
        radius = 4.4 * 0.88 * (1 + 2.2 * (grow - 1) ** 3 + 1.2 * (grow - 1) ** 2 if grow > 0 else 0.0001)
        pa, pb = _star(a[0], a[1], a[2], radius, rot), _star(b[0], b[1], b[2], radius, rot)
        blend = [(x1 + (x2 - x1) * k, y1 + (y2 - y1) * k) for (x1, y1), (x2, y2) in zip(pa, pb, strict=True)]
        ex.append(("fill", blend, tone))
        out = [_gone() for _ in range(5)]
    elif state == "searching":
        span, line, hops = 9.0, 2.6, 5
        p = (clock % line) / line
        if p < 0.82:
            k = p / 0.82 * hops
            n, f = math.floor(k), k - math.floor(k)
            pos = -span + 2 + (n + smooth(clamp(f / 0.3))) * ((2 * span - 4) / hops)
        else:
            pos = (span - 2) - smooth((p - 0.82) / 0.18) * (2 * span - 4)
        out = [Seg(-span + k * span / 2, 0, -span + (k + 1) * span / 2, 0, 1.0, 0.34) for k in range(4)]
        out.append(Seg(pos - 1.5, 0, pos + 1.5, 0, 2.1, 1.0))
    elif state == "working":
        span, cyc = 10.0, 1.9
        p = (clock % cyc) / cyc

        def e(x: float) -> float:
            return 4 * x ** 3 if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2
        head = -span + 2 * span * e(clamp(p / 0.75))
        tail = -span + 2 * span * e(clamp((p - 0.22) / 0.78))
        amp, wl, ph = 1.3 * clamp(t / 0.35), 4.2, clock * 9
        wave, x = [], tail
        while x <= head:
            wave.append((x, amp * math.sin(x / wl * 2 * math.pi - ph)))
            x += 0.25
        if len(wave) > 1:
            ex.append(("stroke", wave, tone, 1.7, 1.0))
        if head + 1.6 < span:
            ex.append(("stroke", [(head + 1.6, 0), (span, 0)], tone, 1.7, 0.3))
        if tail - 1.6 > -span:
            ex.append(("stroke", [(-span, 0), (tail - 1.6, 0)], tone, 1.7, 0.3))
        out = [_gone(-span + j * 5, 0) for j in range(5)]
    elif state == "done":
        s = clamp(t / 0.5)
        pop = 0.0 if s == 0 else 1 + math.exp(-6 * s) * math.sin(s * 9.5) * 0.35
        scale = (ease_out(s) if t < 0.5 else 1.0) * pop
        rot, radius = t * 0.9, 5.2 * scale
        badge = []
        for j in range(121):
            th = j / 120 * 2 * math.pi
            r = radius * (1 + 0.1 * math.cos(9 * (th - rot)))
            badge.append((r * math.cos(th), r * math.sin(th)))
        ex.append(("fill", badge, tone))
        q = ease_out(clamp((t - 0.25) / 0.35))
        if q > 0:  # the tick, cut out in black, drawn from its foot up
            a, v, b = (-2.3, 0.1), (-0.7, 1.7), (2.5, -1.7)
            l1, l2 = math.dist(a, v), math.dist(v, b)
            d = (l1 + l2) * q
            pts = [a, v if d >= l1 else (a[0] + (v[0] - a[0]) * d / l1, a[1] + (v[1] - a[1]) * d / l1)]
            if d > l1:
                k = (d - l1) / l2
                pts.append((v[0] + (b[0] - v[0]) * k, v[1] + (b[1] - v[1]) * k))
            ex.append(("stroke", pts, "#000000", 1.25, 1.0))
        out = [_gone() for _ in range(5)]
    elif state == "failed":
        tilt = math.radians(16 * math.sin(t * 15) * math.exp(-t * 3.2))
        fx, fy = 0.0, 4.6  # it pivots at its foot

        def rot(x: float, y: float) -> tuple[float, float]:
            dx, dy = x - fx, y - fy
            return fx + dx * math.cos(tilt) - dy * math.sin(tilt), fy + dx * math.sin(tilt) + dy * math.cos(tilt)
        top, mid, dot = rot(0, -4.2), rot(0, 0.9), rot(0, 3.7)
        out = [Seg(*top, *mid, 2.1), Seg(*dot, *dot, 2.3), _gone(0, 1), _gone(0, 1), _gone(0, 1)]
    else:  # ready: the resting wave
        mound, b = (2.4, 3.7, 4.8, 3.7, 2.4), breath(clock, 4.8)
        sp = (clock % 5.5) / 1.1  # the shimmer crosses in about a second, then rests
        for i in range(5):
            glow = math.exp(-((sp * 5 - i - 0.5) ** 2) / 0.9) if sp < 1.3 else 0.0
            out.append(_bar(_bx(i), mound[i] * (0.82 + 0.18 * b) + glow * 1.4, 1.8,
                            0.62 + 0.38 * max(glow, 0.35 + 0.3 * b)))
    return out, ex


@dataclass
class Mark:
    """The five pieces with a spring each, so shapes flow into each other (a little overshoot, then they settle)."""
    state: str = "ready"
    since: float = 0.0
    pieces: list[Seg] = field(default_factory=list)
    vel: list[dict] = field(default_factory=list)

    def set(self, state: str, now: float) -> None:
        if state != self.state:
            self.state, self.since = state, now

    def step(self, now: float, dt: float, still: bool = False) -> tuple[list[Seg], list[tuple], str]:
        """Advance to `now` (seconds). still: reduced motion, so shapes appear at once and hold still."""
        clock = 0.0 if still else now
        target, extras = shape(self.state, 1.0 if still else now - self.since, clock)
        if not self.pieces or still:
            self.pieces = [Seg(**vars(s)) for s in target]
            self.vel = [dict.fromkeys(KEYS, 0.0) for _ in target]
        else:
            dt = min(dt, 0.033)
            for p, v, tg in zip(self.pieces, self.vel, target, strict=True):
                for key in KEYS:
                    k, c = (120.0, 22.0) if key == "o" else (260.0, 21.0)  # fades are softer than moves
                    v[key] += (k * (getattr(tg, key) - getattr(p, key)) - c * v[key]) * dt
                    setattr(p, key, getattr(p, key) + v[key] * dt)
                p.o, p.w = clamp(p.o), max(0.05, p.w)
        return self.pieces, extras, TONES.get(self.state, WHITE)


def mark_state(phase: str, text: str) -> str:
    """Which move fits what Ari is doing (an AriState phase and its text)."""
    if phase == "listening":
        return "listening"
    if phase in ("thinking", "starting"):
        return "thinking"
    if phase == "working":
        return "searching" if any(w in text.lower() for w in SEARCHY) else "working"
    if phase == "speaking":
        return "speaking"
    if phase == "done":
        return "failed" if FAILED.search(text or "") else "done"
    return "ready"
