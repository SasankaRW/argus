"""Build one eyes clip from a list of beats. A clip file only says what happens; this does the rest.

    from clipkit import make
    ABOUT = "coding: reads along, focuses, thinks, pleased when it works"
    POINTS = {"code": (200, 175)}                      # extra places to look at (the screen is 400 x 300)
    BEATS = [
        ("home", "neutral", 1.8, {}),                  # every clip starts and ends on a home beat
        ("read", "curious", 3.2, {"track": "code"}),
        ...
        ("home", "neutral", 1.8, {}),
    ]
    STORY = make(BEATS, POINTS)

A beat is (tag, shape, seconds, options). Tags that start with "home" are the shared resting pose (the neutral
shape, eyes centred, perfectly still, no blink): the player may switch to another app's clip only there, so every
clip comes back home every few beats. Options: track (a place to look at), over (a lid/shape tweak by name:
think, tilt, sag, yawn, sleepy_stir, sleepy_settle, sleepy_twitch), blink (seconds into the beat to blink at).
"""

from __future__ import annotations

import math

import eyecore as C
from eyecore import sm
from PIL import Image

# softer, slower springs than the stories: calm movement on a small screen
for _k in ("w", "h", "r", "to", "ti", "bo", "bi", "asy", "cheek", "dy"):
    _kk, _z = C.SPR[_k]
    C.SPR[_k] = (_kk * 0.5, max(_z, 0.45))
for _k in ("gxL", "gxR", "gyL", "gyR"):
    _kk, _z = C.SPR[_k]
    C.SPR[_k] = (_kk * 0.4, 0.85)

SHAPES = {
    "neutral": "the resting pose (use in home beats, or as a calm beat with a track)",
    "content": "softly pleased, slightly squinted (no blinks)",
    "happy": "big happy arcs (no blinks)",
    "bliss": "eyes nearly closed with joy (no blinks)",
    "curious": "a little taller, one eye slightly bigger",
    "notice": "taller: something caught its eye",
    "wow": "tall and bright: impressed",
    "surprised": "biggest: startled (use rarely)",
    "focus": "inner lids pressed down: looks cross on the screen (prefer suspicious for reading)",
    "suspicious": "even squint: studying something closely",
    "tired": "heavy lids",
    "sleepy": "almost closed (no blinks)",
    "yawn": "a yawn (pair with over='yawn')",
    "sad": "drooping outer lids",
    "annoyed": "flat, half-lidded",
    "angry": "slanted inner lids (avoid: the eyes are friendly)",
    "squash": "squashed wide: a bump or a laugh",
    "love": "hearts (use rarely)",
}
POINTS = {"centre": (200, 150), "farL": (20, 150), "farR": (380, 150), "nearL": (150, 150), "nearR": (250, 150),
          "up": (200, 55), "upL": (70, 60), "upR": (330, 60), "down": (210, 235), "downL": (90, 235),
          "downR": (310, 235)}


def think(u):
    return dict(to=0.14, ti=0.14, bo=0.1, bi=0.1, asy=0.12)


def tilt(u):
    return dict(asy=0.14)


OVERS = {"think": think, "tilt": tilt, "sag": C.sag_over, "yawn": C.yawn_over, "sleepy_stir": C.sleepy_stir,
         "sleepy_settle": C.sleepy_settle, "sleepy_twitch": C.sleepy_twitch}
LOOP = 2.4  # the clip's length is rounded up to a multiple of this, so the home sway joins up at the loop


def _env(s, a, b, fi, fo):
    if s <= a or s >= b:
        return 0.0
    return sm((s - a) / fi) * sm((b - s) / fo)


def make(beats, points=None, blink_gap=(2.8, 4.6)):
    """The Story for these beats (checked: known shapes and options, home at both ends, 20-90 s)."""
    pts = dict(POINTS, **(points or {}))
    seq, tags = [], []
    for i, beat in enumerate(beats):
        tag, shape, secs, opts = beat
        if shape not in C.K:
            raise ValueError(f"beat {i} ({tag}): unknown shape {shape!r}; use one of {sorted(SHAPES)}")
        if not 0.5 <= float(secs) <= 8:
            raise ValueError(f"beat {i} ({tag}): {secs} s; a beat lasts 0.5 to 8 s")
        o = dict(opts or {})
        bad = set(o) - {"track", "over", "blink", "gaze_scale"}
        if bad:
            raise ValueError(f"beat {i} ({tag}): unknown options {sorted(bad)}")
        if "track" in o and o["track"] not in pts:
            raise ValueError(f"beat {i} ({tag}): unknown place {o['track']!r}; add it to POINTS")
        if isinstance(o.get("over"), str):
            if o["over"] not in OVERS:
                raise ValueError(f"beat {i} ({tag}): unknown over {o['over']!r}; use one of {sorted(OVERS)}")
            o["over"] = OVERS[o["over"]]
        if tag.startswith("home") and (shape != "neutral" or set(o) - {"blink"} or o.get("blink")):
            raise ValueError(f"beat {i} ({tag}): a home beat is plain ('neutral', no options)")
        seq.append([shape, float(secs), o])
        tags.append(tag)
    if not tags or not tags[0].startswith("home") or not tags[-1].startswith("home"):
        raise ValueError("the first and the last beat must be home beats")
    raw = sum(d for _, d, _ in seq)
    total = math.ceil(raw / LOOP - 1e-9) * LOOP
    if not 20 <= total <= 90:
        raise ValueError(f"the clip is {total:.1f} s; make it 30 to 60 s")
    seq[-1][1] += total - raw
    seq = [tuple(b) for b in seq]
    starts, t = [], 0.0
    for _, d, _ in seq:
        starts.append(t)
        t += d
    home = [(starts[i], starts[i] + seq[i][1]) for i, g in enumerate(tags) if g.startswith("home")]

    def home_w(s):
        if s <= 0 or s >= total:
            return 1.0
        w = 0.0
        for a, b in home:
            w = max(w, _env(s, a, b, 0.0001 if a == 0 else 0.6, 0.0001 if b >= total - 1e-6 else 0.6))
        return w

    def gaze_point(kind, s, t):
        return pts.get(kind, (200, 150))

    def extra_motion(p, t, s, side, wt, ue):  # home beats: no idle sway, no breathing (they match frame 0)
        w = home_w(s)
        if w <= 0:
            return
        calm = 1 - min(1, wt["sleepy"] + wt["dizzy"])
        p["dx"] -= w * calm * (2.5 * math.sin(0.8 * t + 0.3) + 1.5 * math.sin(2.3 * t))
        p["dy"] -= w * calm * 1.2 * math.sin(1.1 * t + 1)
        breath = 1 + 0.018 * math.sin(2 * math.pi * t / 2.8) * (1 - wt["sleepy"])
        k = 1 + w * (breath - 1)
        p["h"] /= k
        p["w"] *= math.sqrt(k)

    def blank(*_):
        return Image.new("L", (C.W * C.S, C.H * C.S), 0)

    story = C.Story(seq, total, gaze_point, lambda t, s: blank(), lambda s: blank(), extra_motion=extra_motion,
                    blink_gap=blink_gap)
    story.BLINKS = [b for b in story.BLINKS if not any(a - 0.2 <= b <= e + 0.2 for a, e in home)]
    story.TAGS, story.STARTS, story.HOME = tags, starts, home
    return story
