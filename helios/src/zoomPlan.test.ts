import { describe, expect, it } from "vitest";
import { EASE, PUSH, zoomPlan } from "./zoomPlan";

const pt = (id: string, x: number, y: number) => ({ id, x, y });
const input = (over = {}) => ({
  origin: { x: 800, y: 300 },
  running: [pt("a", 600, 120), pt("b", 600, 220), pt("c", 600, 320)],
  inputs: [pt("sched", 180, 100), pt("helios", 180, 200)],
  outputs: [pt("t1", 1010, 100), pt("t2", 1010, 200), pt("phone", 1010, 300)],
  reduced: false, ...over,
});
const spec = (plan: ReturnType<typeof zoomPlan>, target: string) => plan.specs.find((s) => s.target === target)!;

describe("zoomPlan", () => {
  it("takes about 700 to 900 ms and every animation ends within it", () => {
    const p = zoomPlan(input());
    expect(p.total).toBeGreaterThanOrEqual(700);
    expect(p.total).toBeLessThanOrEqual(900);
    for (const s of p.specs) expect(s.delay + s.duration).toBeLessThanOrEqual(p.total);
  });

  it("pushes the camera in to about 1.6 while the rest of the map fades in about 450 ms", () => {
    const p = zoomPlan(input());
    expect(PUSH).toBeCloseTo(1.6);
    expect(spec(p, "cam").keyframes[1]).toEqual({ transform: "scale(1.6)" });
    expect(spec(p, "cam").duration).toBe(450);
    expect(spec(p, "fade").duration).toBe(450);
    expect(spec(p, "fade").keyframes).toEqual([{ opacity: 1 }, { opacity: 0 }]);
  });

  it("starts each running plugin at the Plugins box, small and invisible, staggered by about 60 ms", () => {
    const p = zoomPlan(input());
    const [a, b, c] = ["a", "b", "c"].map((id) => spec(p, `card:${id}`));
    expect(a.keyframes[0]).toEqual({ opacity: 0, transform: "translate(200px, 180px) scale(.6)" });  // from (800,300) to (600,120)
    expect(a.keyframes[1]).toEqual({ opacity: 1, transform: "translate(0px, 0px) scale(1)" });
    expect(b.delay - a.delay).toBe(60);
    expect(c.delay - b.delay).toBe(60);
  });

  it("slides inputs in from the left and outputs from the right by 40 px", () => {
    const p = zoomPlan(input());
    expect(spec(p, "card:sched").keyframes[0]).toEqual({ opacity: 0, transform: "translate(-40px, 0px)" });
    expect(spec(p, "card:t1").keyframes[0]).toEqual({ opacity: 0, transform: "translate(40px, 0px)" });
  });

  it("fades the lines in after the cards have settled and raises the idle panel from below", () => {
    const p = zoomPlan(input());
    const settled = Math.min(...["a", "b", "c", "sched", "t1"].map((id) => spec(p, `card:${id}`)).map((s) => s.delay + s.duration * 0.5));
    expect(spec(p, "lines").delay).toBeGreaterThan(settled);
    expect(spec(p, "idle").keyframes[0]).toEqual({ opacity: 0, transform: "translateY(40px)" });
  });

  it("keeps a long list of plugins inside the time by tightening the stagger", () => {
    const many = Array.from({ length: 12 }, (_, i) => pt(`p${i}`, 600, 100 + i * 30));
    const p = zoomPlan(input({ running: many }));
    expect(p.total).toBeLessThanOrEqual(900);
  });

  it("is one short crossfade with reduced motion, and no movement", () => {
    const p = zoomPlan(input({ reduced: true }));
    expect(p.total).toBeLessThanOrEqual(200);
    expect(p.specs.every((s) => s.keyframes.every((k) => !("transform" in k)))).toBe(true);
  });

  it("uses Material's emphasized-decelerate easing", () => {
    expect(EASE).toBe("cubic-bezier(.05, .7, .1, 1)");
  });
});
