// The plugin view's zoom-in, as data: what moves, when and how far. Pure, so it can be tested; MapView turns each entry
// into a Web Animations API animation (element.animate) and plays the whole set forward to open and backward to close.
//
//  1. camera: the map pushes in toward the Plugins box (scale ~1.6) while the other boxes and lines fade
//  2. the running plugins come out of the Plugins box and travel to their places, one after another
//     (inputs slide in from the left, outputs from the right, with a fade)
//  3. the lines fade in once the cards have settled
//  4. the idle panel rises from below
// Material's emphasized-decelerate easing throughout. With reduced motion: one short crossfade.

export const EASE = "cubic-bezier(.05, .7, .1, 1)";
export const PUSH = 1.6;

export type Pt = { id: string; x: number; y: number };
export type Spec = { target: string; keyframes: Keyframe[]; delay: number; duration: number };
export type PlanIn = { origin: { x: number; y: number }; running: Pt[]; inputs: Pt[]; outputs: Pt[]; reduced: boolean };
export type Plan = { specs: Spec[]; total: number };

const fade = (a = 0, b = 1): Keyframe[] => [{ opacity: a }, { opacity: b }];

export function zoomPlan(i: PlanIn): Plan {
  if (i.reduced) {
    return { specs: [
      { target: "cam", keyframes: fade(1, 0), delay: 0, duration: 160 },
      { target: "layer", keyframes: fade(0, 1), delay: 0, duration: 160 },
      { target: "bar", keyframes: fade(0, 1), delay: 0, duration: 160 },
    ], total: 160 };
  }
  const specs: Spec[] = [
    { target: "cam", keyframes: [{ transform: "scale(1)" }, { transform: `scale(${PUSH})` }], delay: 0, duration: 450 },
    { target: "fade", keyframes: fade(1, 0), delay: 0, duration: 450 },
    { target: "plugbox", keyframes: fade(1, 0), delay: 250, duration: 250 },
    { target: "layer", keyframes: fade(0, 1), delay: 0, duration: 120 },
    { target: "bar", keyframes: fade(0, 1), delay: 250, duration: 250 },
  ];
  const run = i.running.length > 1 ? Math.min(60, 250 / (i.running.length - 1)) : 0;
  i.running.forEach((c, k) => specs.push({
    target: `card:${c.id}`,
    keyframes: [
      { opacity: 0, transform: `translate(${i.origin.x - c.x}px, ${i.origin.y - c.y}px) scale(.6)` },
      { opacity: 1, transform: "translate(0px, 0px) scale(1)" },
    ],
    delay: 150 + k * run, duration: 450,
  }));
  const side = (list: Pt[], from: number, start: number) => {
    const step = list.length > 1 ? Math.min(40, 150 / (list.length - 1)) : 0;
    list.forEach((c, k) => specs.push({
      target: `card:${c.id}`,
      keyframes: [{ opacity: 0, transform: `translate(${from}px, 0px)` }, { opacity: 1, transform: "translate(0px, 0px)" }],
      delay: start + k * step, duration: 400,
    }));
  };
  side(i.inputs, -40, 250);
  side(i.outputs, 40, 300);
  specs.push({ target: "lines", keyframes: fade(0, 1), delay: 600, duration: 250 });
  specs.push({ target: "idle", keyframes: [{ opacity: 0, transform: "translateY(40px)" }, { opacity: 1, transform: "translateY(0px)" }], delay: 450, duration: 350 });
  const total = Math.max(...specs.map((s) => s.delay + s.duration));
  return { specs, total };
}
