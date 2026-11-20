"""The island's timing, kept apart from the drawing so it can be tested: page flips, rows rising in, the page dots, the
next job's ring, Ari's orb, load bars easing and ripples. Every function takes the time elapsed and returns where
things are; with `still` (Windows "animation effects" off) it returns the end state at once.

The island (ari_popup.py) repaints every frame only while it is open; folded, nothing here runs."""

from __future__ import annotations

import math

FLIP_S = 0.46  # a page flip
FLIP_DX = 36.0  # how far a page slides
FLIP_SCALE = 0.04  # the page that leaves sinks back this much
RISE_S = 0.46  # a row rising in
RISE_STEP_S = 0.03  # one row after another
RISE_DY = 8.0
DOT, DOT_ON = 5.0, 14.0
RIPPLE_S = 0.6
WAVE_S = 1.6

Pose = tuple[float, float, float]  # dx, alpha, scale


def _bezier(p1: float, p2: float, u: float) -> float:
    return 3 * p1 * u * (1 - u) ** 2 + 3 * p2 * u * u * (1 - u) + u ** 3


def ease(t: float) -> float:
    """Emphasized decelerate, cubic-bezier(.05, .7, .1, 1): quick out of the gate, a long gentle landing."""
    if t <= 0:
        return 0.0
    if t >= 1:
        return 1.0
    lo, hi = 0.0, 1.0  # find the curve's u for this x, then its y
    for _ in range(30):
        mid = (lo + hi) / 2
        if _bezier(0.05, 0.1, mid) < t:
            lo = mid
        else:
            hi = mid
    return _bezier(0.7, 1.0, (lo + hi) / 2)


def flip_frame(t: float, direction: int, still: bool = False) -> tuple[Pose, Pose]:
    """Where the old and the new page are, `t` of the way through a flip (`direction` 1 = to the next page)."""
    e = 1.0 if still else ease(t)
    d = 1 if direction >= 0 else -1
    old = (-d * FLIP_DX * e + 0.0, 1 - e, 1 - FLIP_SCALE * e)
    new = (d * FLIP_DX * (1 - e) + 0.0, e, 1.0)
    return old, new


def rise(elapsed: float, index: int, still: bool = False) -> tuple[float, float]:
    """Row `index` (0 = the top one) `elapsed` s after its page appeared: (how far below its place, how visible)."""
    if still:
        return 0, 1
    e = ease((elapsed - RISE_STEP_S * index) / RISE_S)
    return (0, 1) if e >= 1 else (RISE_DY * (1 - e), e)


def dot_widths(n: int, page: int, prev: int, t: float, still: bool = False) -> list[float]:
    """The page dots' widths: the current one a pill, growing as the one it took over from shrinks."""
    e = 1.0 if still else ease(t)
    out = [DOT] * n
    if 0 <= prev < n and prev != page:
        out[prev] = DOT + (DOT_ON - DOT) * (1 - e)
    if 0 <= page < n:
        out[page] = DOT + (DOT_ON - DOT) * (e if prev != page else 1)
    return out


def countdown(now: float, due: float, window: float = 3600) -> tuple[float, str]:
    """The next job's ring (how much of it is still open: 1 an hour or more away, 0 when it's due) and its words."""
    left = due - now
    if left <= 0:
        return 0, "now"
    frac = min(1.0, left / window)
    mins = round(left / 60)
    return frac, (f"{max(1, mins)} min" if mins < 60 else f"{int(left // 3600)} h")


def wave(t: float, n: int = 5) -> list[float]:
    """Ari's orb: bar heights (0.25 to 0.85 of the waveform's height), each a little behind the one before."""
    return [0.55 + 0.30 * math.sin(2 * math.pi * t / WAVE_S - i * 0.6) for i in range(n)]


def approach(cur: float, target: float, dt: float, half: float = 0.15) -> float:
    """Move `cur` toward `target`: halfway every `half` seconds (a load bar easing to its new value)."""
    if cur == target:
        return cur
    return target + (cur - target) * 0.5 ** (max(0.0, dt) / half)


def ripple(elapsed: float) -> tuple[float, float] | None:
    """A press ripple: (how far it has spread 0..1, how strong), or None once it has faded (600 ms)."""
    if elapsed >= RIPPLE_S:
        return None
    return ease(elapsed / RIPPLE_S), 0.18 * (1 - elapsed / RIPPLE_S)
