import { describe, expect, it } from "vitest";
import { keepInView, pinch, S_MAX, S_MIN, STEP, View, wheelFactor, zoomAbout } from "./navMath";

const v: View = { x: 100, y: 50, s: 1 };

describe("zoom", () => {
  it("keeps the point under the finger where it is", () => {
    const z = zoomAbout(v, 300, 200, 2);
    // canvas point under (300, 200) before: ((300-100)/1, (200-50)/1); after: same point at the same screen spot
    expect((300 - z.x) / z.s).toBeCloseTo(200);
    expect((200 - z.y) / z.s).toBeCloseTo(150);
    expect(z.s).toBe(2);
  });
  it("stays between the smallest and largest scale", () => {
    expect(zoomAbout(v, 0, 0, 100).s).toBe(S_MAX);
    expect(zoomAbout(v, 0, 0, 0.001).s).toBe(S_MIN);
  });
  it("zooming in and out by the same step comes back", () => {
    const z = zoomAbout(zoomAbout(v, 250, 120, STEP), 250, 120, 1 / STEP);
    expect(z.x).toBeCloseTo(v.x); expect(z.y).toBeCloseTo(v.y); expect(z.s).toBeCloseTo(v.s);
  });
});

describe("keep the canvas reachable", () => {
  it("never lets it leave the screen entirely", () => {
    const far = keepInView({ x: 5000, y: -5000, s: 1 }, 400, 600, 390, 700);
    expect(far.x).toBeLessThanOrEqual(390 - 90);
    expect(far.y + 600).toBeGreaterThanOrEqual(90);
  });
  it("leaves a view that is already fine alone", () => {
    expect(keepInView(v, 400, 600, 390, 700)).toEqual(v);
  });
  it("copes with a canvas smaller than the margin", () => {
    const t = keepInView({ x: 9999, y: 9999, s: 0.25 }, 100, 100, 390, 700);
    expect(t.x).toBeLessThanOrEqual(390);
    expect(t.x + 25).toBeGreaterThan(0);
  });
});

describe("pinch", () => {
  it("fingers moving apart zoom in, and the middle drags the canvas", () => {
    const z = pinch(v, { cx: 200, cy: 200, d: 100 }, { cx: 210, cy: 190, d: 200 });
    expect(z.s).toBeCloseTo(2);
    expect(((200 - z.x + 10) / z.s)).toBeCloseTo((200 - v.x) / v.s);  // the canvas point under the old middle is under the new one
  });
  it("does nothing odd when the fingers start on top of each other", () => {
    expect(pinch(v, { cx: 1, cy: 1, d: 0 }, { cx: 1, cy: 1, d: 50 })).toEqual(v);
  });
});

describe("wheel", () => {
  it("scrolling up zooms in, down zooms out, a trackpad pinch faster", () => {
    expect(wheelFactor(-100, false)).toBeGreaterThan(1);
    expect(wheelFactor(100, false)).toBeLessThan(1);
    expect(wheelFactor(-10, true)).toBeGreaterThan(wheelFactor(-10, false));
  });
});
