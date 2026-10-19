# ruff: noqa  - the eye engine, kept as it was written for the GIFs
"""Shared robot-eyes engine: shapes, springs, blinks, body language, compositing.
Story files provide: SEQ, starts/story_t, gaze_point, aux_glow/aux_flat draw hooks,
and optionally an extra_motion(p, t, s, side, wt, ue) hook. See spacestory.py etc.
"""
import math, random, bisect
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageChops

W, H = 400, 300
S = 2
FPS = 20
SUB = 8
CX = (125, 275)
CY = 150

BASE = dict(w=92, h=92, r=24, dy=0, dx=0, to=0, ti=0, bo=0, bi=0, asy=0,
            cheek=0, heart=0, spiral=0, glow=1.0, bright=1.0,
            gxL=0, gxR=0, gyL=0, gyR=0)

K = {
    "neutral":    {},
    "squash":     dict(h=74, w=106, dy=7, r=28),
    "happy":      dict(w=100, h=84, r=42, cheek=0.62, dy=-4, glow=1.1),
    "content":    dict(w=98, h=80, r=40, cheek=0.32, dy=2),
    "curious":    dict(w=96, h=104, r=30, dy=-3, asy=0.10, glow=1.1),
    "notice":     dict(w=98, h=112, r=32, dy=-3, glow=1.15),
    "wow":        dict(w=100, h=118, r=34, dy=-4, glow=1.2),
    "bliss":      dict(w=100, h=46, r=23, cheek=0.35, dy=4, glow=0.9),
    "focus":      dict(to=0.15, ti=0.4, bo=0.12, bi=0.12, h=96, r=18, dy=1, glow=1.15),
    "tired":      dict(to=0.35, ti=0.35, bo=0.12, bi=0.12, dy=8, glow=0.85, bright=0.92),
    "sad":        dict(to=0.5, dy=10, h=86, glow=0.75, bright=0.85),
    "annoyed":    dict(to=0.28, ti=0.5, bo=0.1, bi=0.1, dy=4, h=94, glow=0.9),
    "angry":      dict(ti=0.55, h=98, r=16, dy=4, glow=1.25),
    "suspicious": dict(to=0.42, ti=0.42, bo=0.25, bi=0.25, dy=4),
    "surprised":  dict(w=106, h=124, r=36, dy=-5, glow=1.2),
    "yawn":       dict(w=108, h=34, r=17, dy=-8, glow=0.9),
    "sleepy":     dict(dy=8, glow=0.65, bright=0.8),
    "love":       dict(heart=1, glow=1.2, dy=-8),
    "dizzy":      dict(spiral=1),
}
EMO = ["happy", "angry", "sad", "surprised", "suspicious", "love", "dizzy", "sleepy"]
NOBLINK = ("love", "dizzy", "happy", "sleepy", "squash", "content", "bliss", "yawn", "focus")

SPR = {
    "w": (260, 0.32), "h": (260, 0.32), "r": (200, 0.6),
    "dx": (520, 0.55), "dy": (300, 0.30),
    "to": (240, 0.42), "ti": (240, 0.42), "bo": (240, 0.42), "bi": (240, 0.42),
    "asy": (200, 0.6), "cheek": (240, 0.4), "heart": (110, 1.0), "spiral": (110, 1.0),
    "glow": (60, 1.0), "bright": (60, 1.0),
    "gxL": (420, 0.8), "gxR": (420, 0.8), "gyL": (420, 0.8), "gyR": (420, 0.8),
}
LAG_KEYS = ("w", "h", "r", "to", "ti", "bo", "bi", "cheek")


def sm(x):
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


def clamp(x, a, b):
    return min(max(x, a), b)


def ramp(t, a, b):
    return sm((t - a) / (b - a))


def lid_over(lid):
    lid = min(lid, 0.93)
    return dict(to=lid * 0.85, ti=lid * 0.85, bo=lid * 0.15, bi=lid * 0.15)


def bump(d, c=0.12, w=0.09):
    return math.exp(-((d - c) / w) ** 2)


def cr(pts, u):
    n = len(pts) - 1
    s = min(max(u, 0.0), 1.0) * n
    i = min(int(s), n - 1)
    f = s - i
    p0, p1, p2, p3 = pts[max(i - 1, 0)], pts[i], pts[i + 1], pts[min(i + 2, n)]

    def c(a0, a1, a2, a3):
        return 0.5 * ((2 * a1) + (-a0 + a2) * f + (2 * a0 - 5 * a1 + 4 * a2 - a3) * f * f
                      + (-a0 + 3 * a1 - 3 * a2 + a3) * f ** 3)
    return (c(p0[0], p1[0], p2[0], p3[0]), c(p0[1], p1[1], p2[1], p3[1]))


def lerp(a, b, u):
    return (a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u)


def sleepy_twitch(u):
    return lid_over(0.93 - 0.14 * math.exp(-((u - 3.3) / 0.12) ** 2))


def sleepy_stir(u):
    return lid_over(0.93 - 0.43 * sm((u - 0.1) / 0.9))


def sleepy_settle(u):
    lid = 0.2 + 0.4 * sm(u / 1.2) - 0.3 * sm((u - 1.4) / 0.1) + 0.73 * sm((u - 1.6) / 1.2)
    return lid_over(lid)


def sag_over(u):
    return dict(dy=16, h=82)


def yawn_over(u):
    a = sm(u / 0.7)
    rel = sm((u - 1.5) / 0.6)
    return dict(h=46 - 20 * a + 10 * rel, w=100 + 10 * a - 6 * rel, dy=-2 - 8 * a + 9 * rel)


class Story:
    """Bundle of per-story timeline + gaze + draw hooks; drives the shared engine."""

    def __init__(self, seq, total, gaze_point, aux_glow, aux_flat,
                 extra_motion=None, glitch_amp=None, story_total=None, blink_gap=(2.2, 3.6), aux_dark=None,
                 aux_color=None):
        self.SEQ = seq
        self.TOTAL = total
        self.STORY_TOTAL = story_total if story_total is not None else total
        self.NF = int(total * FPS)
        self.gaze_point = gaze_point
        self.aux_glow = aux_glow
        self.aux_flat = aux_flat
        self.aux_dark = aux_dark
        self.aux_color = aux_color
        self.extra_motion = extra_motion or (lambda p, t, s, side, wt, ue: None)
        self.glitch_amp = glitch_amp or (lambda t: 0.0)
        self.blink_gap = blink_gap
        starts, sstarts, t, sc = [], [], 0.0, 0.0
        for _, d, o in seq:
            starts.append(t)
            sstarts.append(sc)
            t += d
            if not o.get("hold"):
                sc += d
        assert abs(t - total) < 1e-6, t
        self.starts = starts
        self.sstarts = sstarts
        self.BLINKS = self._build_blinks()

    def entry_at(self, t):
        return max(0, bisect.bisect_right(self.starts, t) - 1)

    def story_t(self, t):
        i = self.entry_at(t)
        if self.SEQ[i][2].get("hold"):
            return self.sstarts[i]
        return self.sstarts[i] + (t - self.starts[i])

    def target_at(self, t):
        i = self.entry_at(t)
        name, dur, opts = self.SEQ[i]
        u = t - self.starts[i]
        p = dict(BASE)
        p.update(K[name])
        if "over" in opts:
            p.update(opts["over"](u))
        tr = opts.get("track")
        if tr:
            x, y = self.gaze_point(tr, self.story_t(t), t)
            gs = 0.10 * opts.get("gaze_scale", 1.0)
            p["gxL"] = clamp((x - CX[0]) * gs, -22, 22)
            p["gxR"] = clamp((x - CX[1]) * gs, -22, 22)
            p["gyL"] = p["gyR"] = clamp((y - CY) * 0.13, -17, 17)
        return p, i

    def simulate(self):
        tgt0, _ = self.target_at(0)
        x = dict(tgt0)
        v = {k: 0.0 for k in x}
        wts = {e: 0.0 for e in EMO}
        wts[self.SEQ[0][0] if self.SEQ[0][0] in EMO else "sleepy"] = 1.0
        traj, wtraj = [], []
        dt = 1.0 / (FPS * SUB)
        t = 0.0
        for f in range(self.NF):
            traj.append(dict(x))
            wtraj.append(dict(wts))
            for _ in range(SUB):
                tg, i = self.target_at(t)
                cur = self.SEQ[i][0]
                for k, (kk, z) in SPR.items():
                    c = 2 * z * math.sqrt(kk)
                    a = kk * (tg[k] - x[k]) - c * v[k]
                    v[k] += a * dt
                    x[k] += v[k] * dt
                for e in EMO:
                    goal = 1.0 if e == cur else 0.0
                    wts[e] += (goal - wts[e]) * (1 - math.exp(-dt / 0.16))
                t += dt
        return traj, wtraj

    def _build_blinks(self):
        ev = []
        for i, (name, dur, opts) in enumerate(self.SEQ):
            for b in opts.get("blink", []):
                ev.append(self.starts[i] + b)
        rng = random.Random(5)
        t = 1.5
        while t < self.TOTAL - 0.8:
            t += rng.uniform(*self.blink_gap)
            nm = self.SEQ[self.entry_at(t)][0]
            if nm in NOBLINK:
                continue
            if any(abs(t - e) < 0.8 for e in ev):
                continue
            ev.append(t)
            if rng.random() < 0.2:
                ev.append(t + 0.3)
        return sorted(ev)

    def blink_at(self, t):
        f = 1.0
        for b in self.BLINKS:
            d = t - b
            if 0 <= d < 0.16:
                v = 1 - 0.92 * sm(d / 0.06) if d < 0.06 else 0.08 + 0.92 * sm((d - 0.06) / 0.10)
                f = min(f, v)
        return f

    def ue_of(self, t):
        ue = {}
        for e in EMO:
            last = 0.0
            for i, (nm, _, _) in enumerate(self.SEQ):
                if nm == e and self.starts[i] <= t and not (i > 0 and self.SEQ[i - 1][0] == e):
                    last = self.starts[i]
            ue[e] = t - last
        return ue

    def eye_params(self, f, t, traj, wtraj, side):
        s = self.story_t(t)
        p = dict(traj[f])
        if side > 0:
            pf = traj[max(0, f - 1)]
            for k in LAG_KEYS:
                p[k] = pf[k]
        wt = wtraj[f]
        ue = self.ue_of(t)
        calm = 1 - min(1, wt["sleepy"] + wt["dizzy"])
        breath = 1 + 0.018 * math.sin(2 * math.pi * t / 2.8) * (1 - wt["sleepy"])
        p["h"] *= breath
        p["w"] /= math.sqrt(breath)
        p["dx"] += calm * (2.5 * math.sin(0.8 * t + 0.3) + 1.5 * math.sin(2.3 * t))
        p["dy"] += calm * 1.2 * math.sin(1.1 * t + 1)
        p["dx"] += p["gxL"] if side < 0 else p["gxR"]
        p["dy"] += p["gyL"] if side < 0 else p["gyR"]
        p["h"] *= 1 - side * p["asy"]
        u = ue["happy"]
        bph = abs(math.sin(math.pi * 2.4 * u))
        p["dy"] += -wt["happy"] * 6 * bph
        p["h"] *= 1 + wt["happy"] * 0.07 * (bph - 0.5)
        p["w"] *= 1 - wt["happy"] * 0.05 * (bph - 0.5)
        p["dx"] += wt["happy"] * 3 * math.sin(2 * math.pi * 0.9 * u)
        u = ue["angry"]
        p["dx"] += wt["angry"] * (1.3 * math.sin(2 * math.pi * 6.3 * u) + 0.9 * math.sin(2 * math.pi * 8.1 * u + 1))
        p["dy"] += wt["angry"] * 0.8 * math.sin(2 * math.pi * 7.1 * u + 2)
        p["h"] *= 1 + wt["angry"] * 0.035 * math.sin(2 * math.pi * 3 * u)
        p["dy"] += wt["sad"] * 2.0 * math.sin(2 * math.pi * 0.35 * ue["sad"])
        u = ue["surprised"]
        p["h"] *= 1 + wt["surprised"] * 0.03 * math.sin(2 * math.pi * 2.4 * u)
        p["dx"] += wt["surprised"] * 0.8 * math.sin(2 * math.pi * 6 * u)
        u = ue["love"]
        ph = (u * 1.25) % 1
        beat = math.exp(-((ph - 0.07) / 0.07) ** 2) + 0.55 * math.exp(-((ph - 0.28) / 0.08) ** 2)
        p["pulse"] = 1 + wt["love"] * 0.16 * beat
        p["glow"] += wt["love"] * 0.25 * beat
        u = ue["dizzy"]
        p["dx"] += wt["dizzy"] * 4 * math.cos(3.2 * u)
        p["dy"] += wt["dizzy"] * 3 * math.sin(3.2 * u)
        p["phi"] = 8 * u + 2.5 * math.sin(1.3 * u)
        p.setdefault("pulse", 1.0)
        self.extra_motion(p, t, s, side, wt, ue)
        b = self.blink_at(t)
        sup = min(1, wt["love"] + wt["dizzy"] + wt["happy"] + wt["sleepy"])
        b = 1 - (1 - b) * (1 - sup)
        p["blink"] = b
        p["w"] *= 1 + 0.08 * (1 - b)
        clampp(p)
        return p

    def frame(self, f, traj, wtraj, scale=1.0):
        t = f / FPS
        s = self.story_t(t)
        eyes = np.zeros((H * S, W * S), np.float32)
        glow = bright = 1.0
        for cx, side in zip(CX, (-1, 1)):
            p = self.eye_params(f, t, traj, wtraj, side)
            glow, bright = p["glow"], p["bright"]
            ah, asp = p["heart"], p["spiral"]
            out = np.asarray(eye_mask(cx, CY, p, side), np.float32) / 255 * (1 - min(1, ah + asp))
            if ah > 0.01:
                out += np.asarray(heart_mask(cx, CY, p), np.float32) / 255 * ah
            if asp > 0.01:
                out += np.asarray(spiral_mask(cx, CY, p, side), np.float32) / 255 * asp
            eyes = np.maximum(eyes, np.clip(out, 0, 1))
        m = Image.fromarray((eyes * 255).astype(np.uint8)).resize((W, H), Image.LANCZOS)
        core = np.asarray(m, np.float32) / 255
        g1 = np.asarray(m.filter(ImageFilter.GaussianBlur(7)), np.float32) / 255
        g2 = np.asarray(m.filter(ImageFilter.GaussianBlur(22)), np.float32) / 255
        img = np.clip((core + glow * (0.55 * g1 + 0.45 * g2)) * bright, 0, 1)
        if self.aux_dark is not None:
            dk_im = self.aux_dark(t, s).filter(ImageFilter.MaxFilter(9)).resize((W, H), Image.LANCZOS)
            dk = np.asarray(dk_im, np.float32) / 255
            img = np.clip(img - dk * 0.95, 0, 1)
        a = self.aux_glow(t, s).resize((W, H), Image.LANCZOS)
        ac = np.asarray(a, np.float32) / 255
        a1 = np.asarray(a.filter(ImageFilter.GaussianBlur(3.5)), np.float32) / 255
        a2 = np.asarray(a.filter(ImageFilter.GaussianBlur(11)), np.float32) / 255
        fl = np.asarray(self.aux_flat(s).resize((W, H), Image.LANCZOS), np.float32) / 255
        img = np.clip(img + ac + 1.2 * a1 + 0.6 * a2 + fl, 0, 1)
        img = apply_glitch(img, f, self.glitch_amp(s))
        gray = np.clip(img, 0, 1)

        if self.aux_color is not None:
            # a colored prop layer, composited on top of the white eyes by alpha -
            # it never adds into the white glow channel, so it can't melt into it
            col = self.aux_color(t, s).resize((W, H), Image.LANCZOS)
            rgb = np.asarray(col.convert("RGB"), np.float32) / 255
            alpha = (np.asarray(col.split()[-1], np.float32) / 255) if col.mode == "RGBA" else np.zeros((H, W), np.float32)
            base = np.stack([gray, gray, gray], axis=-1)
            out_arr = np.clip(base * (1 - alpha[..., None]) + rgb * alpha[..., None], 0, 1)
            out = Image.fromarray((out_arr * 255).astype(np.uint8), "RGB")
            if scale != 1.0:
                out = out.resize((int(W * scale), int(H * scale)), Image.LANCZOS)
            return out

        out = Image.fromarray((gray * 255).astype(np.uint8), "L")
        if scale != 1.0:
            out = out.resize((int(W * scale), int(H * scale)), Image.LANCZOS)
        pm = Image.frombuffer("P", out.size, out.tobytes(), "raw", "P", 0, 1)
        pm.putpalette(PAL)
        return pm

    def render(self, path, scale=1.0):
        traj, wtraj = self.simulate()
        frames = [self.frame(f, traj, wtraj, scale) for f in range(self.NF)]
        kwargs = dict(save_all=True, append_images=frames[1:], duration=int(1000 / FPS),
                      loop=0, optimize=True, disposal=2 if self.aux_color is not None else 1)
        frames[0].save(path, **kwargs)
        print("frames", self.NF, "seconds", self.TOTAL, "blinks", len(self.BLINKS))
        return frames


# ======================= eyes (shape) =======================
def eye_mask(cx, cy, p, side):
    w, h = p["w"] * S, p["h"] * p["blink"] * S
    cx, cy = (cx + p["dx"]) * S, (cy + p["dy"]) * S
    x0, x1, y0, y1 = cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2
    im = Image.new("L", (W * S, H * S), 0)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([x0, y0, x1, y1], radius=min(p["r"] * S, h / 2), fill=255)
    to, ti, bo, bi = p["to"], p["ti"], p["bo"], p["bi"]
    tL, tR = (to, ti) if side < 0 else (ti, to)
    bL, bR = (bo, bi) if side < 0 else (bi, bo)
    hh = p["h"] * S
    m = 6
    if tL > 0.005 or tR > 0.005:
        d.polygon([(x0 - m, y0 - m), (x1 + m, y0 - m),
                   (x1 + m, y0 + hh * tR), (x0 - m, y0 + hh * tL)], fill=0)
    if bL > 0.005 or bR > 0.005:
        d.polygon([(x0 - m, y1 + m), (x1 + m, y1 + m),
                   (x1 + m, y1 - hh * bR), (x0 - m, y1 - hh * bL)], fill=0)
    if p["cheek"] > 0.01:
        top = y0 + h * (1 - p["cheek"])
        d.ellipse([cx - w * 0.5, top, cx + w * 0.5, top + h * 1.6], fill=0)
    return im.filter(ImageFilter.GaussianBlur(5)).point(lambda v: 255 if v > 128 else 0)


def heart_mask(cx, cy, p):
    sc = 3.0 * p["pulse"] * S
    pts = []
    for i in range(120):
        t = 2 * math.pi * i / 120
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        pts.append(((cx + p["dx"]) * S + x * sc, (cy + p["dy"]) * S - (y + 2.5) * sc))
    im = Image.new("L", (W * S, H * S), 0)
    ImageDraw.Draw(im).polygon(pts, fill=255)
    return im


def spiral_mask(cx, cy, p, side):
    phi = p["phi"] * side
    R = 44 * S
    n = 160
    pts = []
    for i in range(n + 1):
        th = 5 * math.pi * i / n
        r = R * i / n
        pts.append(((cx + p["dx"]) * S + r * math.cos(th + phi),
                    (cy + p["dy"]) * S + r * math.sin(th + phi)))
    im = Image.new("L", (W * S, H * S), 0)
    ImageDraw.Draw(im).line(pts, fill=255, width=int(8 * S), joint="curve")
    return im


def clampp(p):
    for k in ("to", "ti", "bo", "bi"):
        p[k] = min(max(p[k], 0.0), 0.95)
    p["cheek"] = min(max(p["cheek"], 0.0), 0.85)
    p["w"] = max(p["w"], 6)
    p["h"] = max(p["h"], 6)
    p["r"] = max(p["r"], 4)
    p["heart"] = min(max(p["heart"], 0.0), 1.0)
    p["spiral"] = min(max(p["spiral"], 0.0), 1.0)


# ======================= generic drawing helpers =======================
def circle(d, x, y, r, v):
    d.ellipse([(x - r) * S, (y - r) * S, (x + r) * S, (y + r) * S], fill=int(255 * clamp(v, 0, 1)))


def rot_ellipse(d, x, y, rx, ry, ang, v):
    pts = []
    for i in range(14):
        a = 2 * math.pi * i / 14
        ex, ey = rx * math.cos(a), ry * math.sin(a)
        pts.append(((x + ex * math.cos(ang) - ey * math.sin(ang)) * S,
                    (y + ex * math.sin(ang) + ey * math.cos(ang)) * S))
    d.polygon(pts, fill=int(255 * clamp(v, 0, 1)))


def diamond(d, x, y, r, ang, v, outline=False):
    pts = [((x + r * math.cos(ang + k * math.pi / 2)) * S, (y + r * math.sin(ang + k * math.pi / 2)) * S)
           for k in range(4)]
    if outline:
        d.line(pts + [pts[0]], fill=int(255 * clamp(v, 0, 1)), width=int(2 * S))
    else:
        d.polygon(pts, fill=int(255 * clamp(v, 0, 1)))


def particles(d, x, y, t, t0, n, vmin, vmax, life, size, seed, v=0.95):
    dt = t - t0
    if not (0 <= dt < life):
        return
    rng = random.Random(seed)
    for _ in range(n):
        a = rng.uniform(0, 2 * math.pi)
        sp = rng.uniform(vmin, vmax)
        r = sp * dt
        circle(d, x + r * math.cos(a), y + r * math.sin(a), size * (1 - dt / life) + 0.4, v * (1 - dt / life))


def ring(d, x, y, r, v, width=1.6):
    d.ellipse([(x - r) * S, (y - r) * S, (x + r) * S, (y + r) * S],
              outline=int(255 * clamp(v, 0, 1)), width=int(width * S))


PAL = []
for _v in range(256):
    PAL += [_v, _v, _v]


def apply_glitch(img, f, amp):
    if amp <= 0:
        return img
    rng = random.Random((f // 2) * 7919 + 13)
    for _ in range(5):
        y0 = rng.randint(70, 230)
        h = rng.randint(4, 16)
        dx = int(rng.choice([-1, 1]) * rng.uniform(6, 18) * amp)
        img[y0:y0 + h] = np.roll(img[y0:y0 + h], dx, axis=1)
    if rng.random() < 0.25 * min(1.0, amp / 0.5):
        img = img * rng.uniform(0.55, 0.85)
    return img
