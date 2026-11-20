// The dots travelling the map's lines. One requestAnimationFrame loop moves every dot by hand (no React render per
// frame): each dot is a bright head and three fading trail dots, placed along the line with getPointAtLength. When a
// dot reaches a box, that box flashes once and the next step of a news chain starts. Pointing at a box (LevelCanvas's
// focus) dims dots on lines it isn't on. Never drawn under reduced motion: LevelCanvas leaves the layer out.

import { memo, useEffect, useRef } from "react";
import { stepDot, type Dot } from "./mapFlow";
import type { Layout } from "./mapGraph";
import { nextDots } from "./useMapFlow";

const NS = "http://www.w3.org/2000/svg";
const SPEED = { work: 0.3, news: 0.42 };  // canvas px per ms
const HEAD = { work: "#e8fff0", news: "#fff4d0" };
const TINT = { work: "var(--lm-busy)", news: "var(--lm-slow)" };
const TRAIL = [{ r: 2.6, o: 0.38 }, { r: 2.0, o: 0.26 }, { r: 1.4, o: 0.14 }];
const LAG = 0.035;
const START_MS = 1500;  // a dot handed over longer ago than this is not started (the layer was off or remounted)  // each trail dot sits this much further back along the line

type Run = { dot: Dot; path: SVGPathElement; len: number; t0: number; dur: number; g: SVGGElement; parts: SVGCircleElement[] };

const ease = (u: number) => 0.5 - Math.cos(Math.PI * u) / 2;

function draw(svg: SVGSVGElement, tone: Dot["tone"]): { g: SVGGElement; parts: SVGCircleElement[] } {
  const g = document.createElementNS(NS, "g");
  const parts: SVGCircleElement[] = [];
  for (const t of [...TRAIL].reverse()) {
    const c = document.createElementNS(NS, "circle");
    c.setAttribute("r", String(t.r)); c.setAttribute("fill", TINT[tone]); c.setAttribute("opacity", String(t.o));
    g.appendChild(c); parts.unshift(c);
  }
  const head = document.createElementNS(NS, "circle");
  head.setAttribute("r", "4.2"); head.setAttribute("fill", HEAD[tone]);
  head.style.filter = `drop-shadow(0 0 5px ${TINT[tone]})`;
  g.appendChild(head); parts.unshift(head);  // parts[0] is the head, then the trail, oldest last
  svg.appendChild(g);
  return { g, parts };
}

export function flash(root: Element | null, box: string, tone: Dot["tone"]) {
  const el = root?.querySelector<HTMLElement>(`.lm-n[data-card="${CSS.escape(box)}"]`);
  if (!el) return;
  const cls = tone === "news" ? "hit-warn" : "hit";
  el.classList.remove("hit", "hit-warn");
  void el.offsetWidth;  // restart the animation
  el.classList.add(cls);
}

export const FlowLayer = memo(function FlowLayer({ layout, incoming, near, onArrive }: {
  layout: Layout;
  incoming: Dot[];
  near: Set<string> | null;  // the pointed-at box and its neighbours, or null
  onArrive?: (box: string, tone: Dot["tone"], last: boolean) => void;
}) {
  const svg = useRef<SVGSVGElement>(null);
  const paths = useRef(new Map<string, SVGPathElement>());
  const runs = useRef<Run[]>([]);
  const seen = useRef(new Set<string>());
  const frame = useRef(0);
  const nearRef = useRef(near);
  nearRef.current = near;
  const arrive = useRef(onArrive);
  arrive.current = onArrive;

  const start = (d: Dot, now: number) => {
    const path = paths.current.get(d.route), s = svg.current;
    if (!path || !s) return;
    const len = path.getTotalLength();
    if (!len) return;
    const { g, parts } = draw(s, d.tone);
    runs.current.push({ dot: d, path, len, t0: now, dur: len / SPEED[d.tone], g, parts });
  };

  const tick = (now: number) => {
    const done: Run[] = [];
    const nr = nearRef.current;
    for (const r of runs.current) {
      const u = Math.min(1, (now - r.t0) / r.dur);
      r.parts.forEach((c, i) => {
        const f = ease(Math.max(0, u - i * LAG));
        const pt = r.path.getPointAtLength((r.dot.back ? 1 - f : f) * r.len);
        c.setAttribute("cx", String(pt.x)); c.setAttribute("cy", String(pt.y));
      });
      const [a, b] = r.dot.route.split(">");
      r.g.classList.toggle("dim", !!nr && !(nr.has(a) && nr.has(b)));
      if (u >= 1) done.push(r);
    }
    for (const r of done) {
      r.g.remove();
      runs.current = runs.current.filter((x) => x !== r);
      const [a, b] = r.dot.route.split(">");
      const to = r.dot.back ? a : b, rest = r.dot.next ?? [];
      flash(svg.current?.parentElement ?? null, to, r.dot.tone);
      arrive.current?.(to, r.dot.tone, rest.length === 0);
      if (rest.length) start(stepDot(`${r.dot.id}+`, rest[0], r.dot.tone, now, rest.slice(1)), now);
    }
    frame.current = runs.current.length ? requestAnimationFrame(tick) : 0;
  };

  useEffect(() => {
    const live = new Set(layout.routes.map((r) => `${r.a}>${r.b}`));
    const now = performance.now();
    const fresh = incoming.filter((d) => !seen.current.has(d.id));
    for (const d of fresh) seen.current.add(d.id);
    if (seen.current.size > 500) seen.current = new Set(incoming.map((d) => d.id));
    for (const d of fresh) if (now - d.at <= START_MS) start(d, now);
    // keep at most MAX_DOTS: the oldest go first, and none on a line that is gone (checked on every layout change)
    const keep = new Set(nextDots(runs.current.map((r) => r.dot), [], live).map((d) => d.id));
    for (const r of runs.current) if (!keep.has(r.dot.id)) r.g.remove();
    runs.current = runs.current.filter((r) => keep.has(r.dot.id));
    if (!frame.current && runs.current.length) frame.current = requestAnimationFrame(tick);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [incoming, layout]);

  useEffect(() => () => { cancelAnimationFrame(frame.current); frame.current = 0; runs.current = []; }, []);

  return (
    <svg ref={svg} className="lm-dots" width={layout.width} height={layout.height} aria-hidden="true">
      {layout.routes.map((r) => (
        <path key={`${r.a}>${r.b}`} d={r.d} fill="none" stroke="none"
          ref={(el) => { const k = `${r.a}>${r.b}`; if (el) paths.current.set(k, el); else paths.current.delete(k); }} />
      ))}
    </svg>
  );
});
