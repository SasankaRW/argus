import { describe, expect, it } from "vitest";
import { camera, cardPose, K, C, settled, smooth, Spring, springStep, zoomFrame } from "./zoomMath";

const canvas = { w: 1200, h: 600 };
const box = { cx: 850, cy: 300, w: 200, h: 54 };

// the point of the Plugins box's centre on the canvas under the camera, and its size
function seen(p: number) {
  const f = zoomFrame(p, box, canvas);
  return { x: f.cam.tx, y: f.cam.ty, w: box.w * f.cam.sc, h: box.h * f.cam.sc, f };
}

describe("zoomFrame", () => {
  it("is the plain map at p = 0", () => {
    const f = zoomFrame(0, box, canvas);
    expect(f.cam).toEqual({ tx: box.cx, ty: box.cy, sc: 1 });
    expect(camera(f, box)).toBe("translate(850px, 300px) scale(1) translate(-850px, -300px)");  // the identity
    expect([f.map, f.head, f.bar, f.view, f.lines, f.idle]).toEqual([1, 1, 0, 0, 0, 0]);
    expect(f.live).toEqual({ map: true, view: false, head: true, bar: false });
  });

  it("makes the Plugins box fill the canvas, centred, at p = 1", () => {
    const s = seen(1);
    expect(s.x).toBeCloseTo(600);
    expect(s.y).toBeCloseTo(300);
    expect(s.w).toBeGreaterThanOrEqual(canvas.w);  // the limiting side (the width) is filled, edge to edge
    expect(s.f.map).toBe(0);
    expect(s.f.view).toBe(1);
    expect(s.f.mix).toBe(0);
    expect([s.f.lines, s.f.idle, s.f.head]).toEqual([1, 1, 0]);
    expect(s.f.live).toEqual({ map: false, view: true, head: false, bar: true });
  });

  it("zooms by a steady factor and moves the box toward the middle all the way", () => {
    const xs = [0, 0.25, 0.5, 0.75, 1].map((p) => seen(p).x);
    for (let i = 1; i < xs.length; i++) expect(xs[i]).toBeLessThan(xs[i - 1]);
    const r1 = seen(0.5).w / seen(0.25).w, r2 = seen(0.75).w / seen(0.5).w;
    expect(r1).toBeCloseTo(r2);
  });

  it("hands over from the map to the view in order: map fades, view appears from the box's colour, lines and idle list last", () => {
    expect(zoomFrame(0.5, box, canvas).view).toBe(0);
    expect(zoomFrame(0.5, box, canvas).map).toBe(1);
    expect(zoomFrame(0.75, box, canvas).view).toBeGreaterThan(0);
    expect(zoomFrame(0.75, box, canvas).lines).toBe(0);
    expect(zoomFrame(0.9, box, canvas).lines).toBeGreaterThan(0);
    expect(zoomFrame(0.84, box, canvas).idle).toBeGreaterThan(0);
    expect(zoomFrame(0.7, box, canvas).mix).toBeGreaterThan(zoomFrame(0.9, box, canvas).mix);
  });

  it("only makes the map or the view pointable when it is (almost) all there", () => {
    expect(zoomFrame(0.5, box, canvas).live).toEqual({ map: false, view: false, head: false, bar: false });
    expect(zoomFrame(0.96, box, canvas).live.view).toBe(true);
    expect(zoomFrame(0.01, box, canvas).live.map).toBe(true);
  });

  it("clamps p to 0..1", () => {
    expect(zoomFrame(-3, box, canvas)).toEqual(zoomFrame(0, box, canvas));
    expect(zoomFrame(9, box, canvas)).toEqual(zoomFrame(1, box, canvas));
  });

  it("with reduced motion has no camera and no travelling: one crossfade", () => {
    const a = zoomFrame(0, box, canvas, true), b = zoomFrame(1, box, canvas, true), mid = zoomFrame(0.4, box, canvas, true);
    expect(camera(a, box)).toBe("translate(850px, 300px) scale(1) translate(-850px, -300px)");
    expect(camera(b, box)).toBe(camera(a, box));
    expect([a.map, a.view, b.map, b.view, mid.view]).toEqual([1, 0, 0, 1, 0]);
    expect(cardPose("mid", 0, 3, 1, { x: 1, y: 2 }, canvas, true)).toEqual({ tx: 0, ty: 0, scale: 1, opacity: 1 });
    expect(cardPose("mid", 0, 3, 0, { x: 1, y: 2 }, canvas, true).opacity).toBe(0);
  });
});

describe("cardPose", () => {
  const at = { x: 600, y: 120 };
  it("brings running plugins out of the middle of the canvas, small, and settles them in place", () => {
    const start = cardPose("mid", 0, 4, 0.62, at, canvas);
    expect(start.opacity).toBe(0);
    expect(start.scale).toBeCloseTo(0.6);
    expect(start.tx).toBeCloseTo((600 - 600) * 0.9);
    expect(start.ty).toBeCloseTo((300 - 120) * 0.9);
    expect(cardPose("mid", 0, 4, 1, at, canvas)).toEqual({ tx: 0, ty: 0, scale: 1, opacity: 1 });
  });
  it("slides inputs in from the left and outputs from the right by 60 px", () => {
    expect(cardPose("src", 0, 4, 0.62, at, canvas).tx).toBe(-60);
    expect(cardPose("snk", 0, 4, 0.62, at, canvas).tx).toBe(60);
    expect(cardPose("src", 0, 4, 1, at, canvas).tx).toBeCloseTo(0);
  });
  it("staggers the cards, and every one has arrived by p = 1 however many there are", () => {
    expect(cardPose("mid", 3, 4, 0.7, at, canvas).opacity).toBeLessThan(cardPose("mid", 0, 4, 0.7, at, canvas).opacity);
    for (const n of [1, 4, 8, 30]) for (let i = 0; i < n; i++) expect(cardPose("mid", i, n, 1, at, canvas).opacity).toBe(1);
  });
});

describe("spring", () => {
  const run = (s: Spring, to: number, frames: number, dt = 1 / 60) => { for (let i = 0; i < frames; i++) s = springStep(s, to, dt); return s; };

  it("is over-damped (no wobble) and arrives in well under a second", () => {
    expect(C * C).toBeGreaterThan(4 * K);
    let s: Spring = { x: 0, v: 0 };
    let max = 0, n = 0;
    while (!settled(s, 1) && n < 600) { s = springStep(s, 1, 1 / 60); max = Math.max(max, s.x); n++; }
    expect(max).toBeLessThanOrEqual(1);
    expect(n / 60).toBeLessThan(1.2);
  });

  it("turns round when the target changes half way, from where it is, without leaving 0..1", () => {
    let s = run({ x: 0, v: 0 }, 1, 12);  // 0.2 s in: moving up
    expect(s.x).toBeGreaterThan(0.1);
    expect(s.x).toBeLessThan(0.95);
    const turned = s.x;
    let lo = 1, hi = 0;
    for (let i = 0; i < 400; i++) { s = springStep(s, 0, 1 / 60); lo = Math.min(lo, s.x); hi = Math.max(hi, s.x); }
    expect(hi).toBeLessThan(Math.min(1, turned + 0.2));  // it may coast a little further, never to the end
    expect(lo).toBeGreaterThanOrEqual(-1e-6);
    expect(settled(s, 0) || Math.abs(s.x) < 0.001).toBe(true);
  });

  it("can be reversed again and again", () => {
    let s: Spring = { x: 0, v: 0 };
    for (let k = 0; k < 6; k++) { s = run(s, k % 2 ? 0 : 1, 7); expect(s.x).toBeGreaterThanOrEqual(-1e-6); expect(s.x).toBeLessThanOrEqual(1 + 1e-6); }
  });

  it("does not jump after a slow frame", () => {
    const a = springStep({ x: 0, v: 0 }, 1, 5), b = springStep({ x: 0, v: 0 }, 1, 0.032);
    expect(a).toEqual(b);
    expect(springStep({ x: 0.3, v: 0 }, 1, -1)).toEqual({ x: 0.3, v: 0 });
  });

  it("smooth is 0 at 0, 1 at 1 and clamps", () => {
    expect([smooth(-1), smooth(0), smooth(0.5), smooth(1), smooth(2)]).toEqual([0, 0, 0.5, 1, 1]);
  });
});
