// The maths of the zoom into the Plugins box. One progress value p (0 = the map, 1 = the plugin view) drives everything:
// the camera, what fades, and where each plugin card is. Pure, so it can be tested; usePluginZoom.ts writes the numbers
// to the DOM on every frame. p comes from a spring, so it can stop and turn round at any moment.

export type Box = { cx: number; cy: number; w: number; h: number };  // the Plugins box, in canvas coordinates
export type Canvas = { w: number; h: number };

const cl = (x: number) => Math.max(0, Math.min(1, x));
export const smooth = (x: number) => { const t = cl(x); return t * t * (3 - 2 * t); };

export type Frame = {
  cam: { tx: number; ty: number; sc: number };  // the map: translate(tx, ty) scale(sc) translate(-cx, -cy)
  map: number;  // opacity of the map
  head: number;  // opacity of the attention row
  bar: number;  // opacity of the Back bar
  view: number;  // opacity of the plugin view
  mix: number;  // 0..100: how much of the Plugins box's colour is still in the view's background
  lines: number;  // opacity of the plugin view's lines and badges
  idle: number;  // opacity (and rise) of the idle plugin list
  live: { map: boolean; view: boolean; head: boolean; bar: boolean };  // who may be pointed at
};

// reduced: no camera and no travelling, one short crossfade (the layers carry a CSS transition) between 0 and 1.
export function zoomFrame(p: number, box: Box, canvas: Canvas, reduced = false): Frame {
  if (reduced) {
    const on = p >= 0.5;
    return { cam: { tx: box.cx, ty: box.cy, sc: 1 }, map: on ? 0 : 1, head: on ? 0 : 1, bar: on ? 1 : 0, view: on ? 1 : 0, mix: 0, lines: on ? 1 : 0, idle: on ? 1 : 0,
      live: { map: !on, view: on, head: !on, bar: on } };
  }
  p = cl(p);
  // the box grows until it fills the canvas (a little more, so no edge shows)
  const S = Math.min(canvas.w / box.w, canvas.h / box.h) * 1.03;
  return {
    cam: { tx: box.cx + (canvas.w / 2 - box.cx) * p, ty: box.cy + (canvas.h / 2 - box.cy) * p, sc: Math.pow(S, p) },
    map: 1 - smooth((p - 0.62) / 0.3),
    head: 1 - smooth(p / 0.35),
    bar: smooth((p - 0.5) / 0.2),
    view: smooth((p - 0.6) / 0.25),
    mix: Math.round((1 - smooth((p - 0.62) / 0.35)) * 100),
    lines: smooth((p - 0.86) / 0.14),
    idle: smooth((p - 0.82) / 0.18),
    live: { map: p < 0.02, view: p > 0.95, head: p < 0.3, bar: p > 0.55 },
  };
}

export const camera = (f: Frame, box: Box) => `translate(${f.cam.tx}px, ${f.cam.ty}px) scale(${f.cam.sc}) translate(${-box.cx}px, ${-box.cy}px)`;

export type Kind = "mid" | "src" | "snk";  // the running plugins, what feeds them, what they use or produce

// Where one card of the plugin view is at p: the running plugins come out of the middle of the canvas, inputs and
// outputs drift in from the sides. `i` and `n`: its place among the n cards of its column; the stagger always ends by
// p = 0.70, so every card has arrived by p = 1 however many there are.
export function cardPose(kind: Kind, i: number, n: number, p: number, at: { x: number; y: number }, canvas: Canvas, reduced = false) {
  if (reduced) return { tx: 0, ty: 0, scale: 1, opacity: p >= 0.5 ? 1 : 0 };
  const step = Math.min(0.035, 0.08 / Math.max(1, n));
  const q = smooth((cl(p) - (0.62 + step * i)) / 0.3);
  if (kind === "mid") return { tx: (canvas.w / 2 - at.x) * (1 - q) * 0.9, ty: (canvas.h / 2 - at.y) * (1 - q) * 0.9, scale: 0.6 + 0.4 * q, opacity: q };
  return { tx: (kind === "src" ? -60 : 60) * (1 - q), ty: 0, scale: 0.94 + 0.06 * q, opacity: q };
}

// ---------------------------------------------------------------- the spring

export type Spring = { x: number; v: number };
export const K = 150, C = 26;  // stiff, and a little over-damped: quick, no wobble

// One step of a spring pulled toward `to` (dt in seconds, never more than 32 ms so a slow frame cannot throw it).
export function springStep(s: Spring, to: number, dt: number): Spring {
  const d = Math.min(0.032, Math.max(0, dt));
  const v = s.v + (-K * (s.x - to) - C * s.v) * d;
  return { x: s.x + v * d, v };
}
export const settled = (s: Spring, to: number) => Math.abs(s.x - to) < 0.0007 && Math.abs(s.v) < 0.002;
