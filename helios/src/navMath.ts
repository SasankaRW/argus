// Moving around the live map: where the canvas sits on screen, and the pan / zoom rules. Pure, so the rules are tested.
// A view is the canvas's top-left corner in the map area's pixels and its scale.

export type View = { x: number; y: number; s: number };

export const S_MIN = 0.25;
export const S_MAX = 3;
const EDGE = 90;  // at least this much of the canvas stays on screen, so it can never be lost off the side

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

// Zoom by factor f keeping the point (cx, cy) of the screen where it is: the map grows or shrinks around the finger or cursor.
export function zoomAbout(v: View, cx: number, cy: number, f: number): View {
  const s = clamp(v.s * f, S_MIN, S_MAX), r = s / v.s;
  return { x: cx - (cx - v.x) * r, y: cy - (cy - v.y) * r, s };
}

// Keep part of the canvas (cw x ch at scale s) inside the area (vw x vh).
export function keepInView(v: View, cw: number, ch: number, vw: number, vh: number): View {
  const w = cw * v.s, h = ch * v.s;
  const ex = Math.min(EDGE, w), ey = Math.min(EDGE, h);
  return { s: v.s, x: clamp(v.x, ex - w, vw - ex), y: clamp(v.y, ey - h, vh - ey) };
}

// Two fingers: the scale follows how far apart they are, the canvas follows their middle.
export function pinch(v: View, prev: { cx: number; cy: number; d: number }, now: { cx: number; cy: number; d: number }): View {
  const z = zoomAbout(v, prev.cx, prev.cy, prev.d > 0 ? now.d / prev.d : 1);
  return { ...z, x: z.x + (now.cx - prev.cx), y: z.y + (now.cy - prev.cy) };
}

// The mouse wheel (or a trackpad's pinch, which arrives as a wheel with ctrl held, in bigger steps).
export const wheelFactor = (deltaY: number, ctrl: boolean, mode = 0) => Math.exp(-deltaY * (mode === 1 ? 0.05 : ctrl ? 0.01 : 0.0016));

export const STEP = 1.35;  // the + and - buttons
