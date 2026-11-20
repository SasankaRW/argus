// One drawing of boxes and lines at a fixed logical size (MapView scales it to fit). Pointing at (or tabbing to) a box dims everything it is not connected to.

import { memo, useLayoutEffect, useRef, useState } from "react";
import { FlowLayer } from "./FlowLayer";
import { COOKIE, Ms } from "./m3";
import type { Dot } from "./mapFlow";
import type { Layout, Placed } from "./mapGraph";

type Props = {
  layout: Layout;
  flow: boolean;
  selected?: string | null;
  onPick: (c: Placed) => void;
  label: string;
  className?: string;
  dots?: { incoming: Dot[]; onArrive?: (box: string, tone: Dot["tone"], last: boolean) => void };  // the flow layer (MapView)
  opening?: boolean;  // play the opening: boxes settle in column by column, solid lines draw themselves (MapView, once)
};

// The numbers in a status line, each re-mounted when it changes so it plays a small tick (livemap.css .lm-tk).
function Ticking({ text }: { text: string }) {
  return <>{text.split(/(\d+)/).map((part, i) => (/^\d+$/.test(part) ? <span key={`${i}:${part}`} className="lm-tk">{part}</span> : part))}</>;
}

// The opening's lines: a solid line is drawn from its start (dash offset from its length to 0), then the dash is cleared.
function useDrawLines(svg: React.RefObject<SVGSVGElement | null>, on: boolean) {
  useLayoutEffect(() => {
    const el = svg.current;
    if (!on || !el) return;
    const ps = [...el.querySelectorAll<SVGPathElement>("path.quiet, path.busy")];
    for (const p of ps) {
      const len = p.getTotalLength(), col = Number(p.dataset.col ?? 0);
      p.style.transition = "none"; p.style.strokeDasharray = `${len}`; p.style.strokeDashoffset = `${len}`;
      void p.getBoundingClientRect();
      p.style.transition = `stroke-dashoffset 700ms cubic-bezier(.05, .7, .1, 1) ${col * 90 + 260}ms`;
      p.style.strokeDashoffset = "0";
    }
    const t = window.setTimeout(() => { for (const p of ps) { p.style.transition = ""; p.style.strokeDasharray = ""; p.style.strokeDashoffset = ""; } }, 1500);
    return () => window.clearTimeout(t);
  }, [svg, on]);
}

const BADGE: Record<string, "close" | "info"> = { broken: "close", slow: "info" };

export const LevelCanvas = memo(function LevelCanvas({ layout, flow, selected, onPick, label, className, dots, opening }: Props) {
  const [focus, setFocus] = useState<string | null>(null);
  const lines = useRef<SVGSVGElement>(null);
  useDrawLines(lines, !!opening);
  const near = new Set<string>();
  if (focus) {
    near.add(focus);
    for (const r of layout.routes) { if (r.a === focus) near.add(r.b); if (r.b === focus) near.add(r.a); }
  }
  const off = (r: { a: string; b: string }) => (focus && !(r.a === focus || r.b === focus) ? " dim" : "");
  return (
    <div className={`lm-cv${focus ? " focus" : ""}${flow ? "" : " still"}${dots ? " dots" : ""}${opening ? " opening" : ""}${className ? ` ${className}` : ""}`} style={{ width: layout.width, height: layout.height }} role="group" aria-label={label}>
      <svg ref={lines} className="lm-l" width={layout.width} height={layout.height} aria-hidden="true">
        {layout.routes.map((r) => (
          <path key={`${r.a}>${r.b}`} d={r.d} data-a={r.a} data-b={r.b} data-col={Math.min(layout.placed[r.a]?.level ?? 0, layout.placed[r.b]?.level ?? 0)} className={`lm-e ${r.kind}${off(r)}`} />
        ))}
      </svg>
      {dots && <FlowLayer layout={layout} incoming={dots.incoming} near={focus ? near : null} onArrive={dots.onArrive} />}
      {Object.values(layout.placed).map((c) => (
        <button
          key={c.id}
          type="button"
          data-card={c.id}
          className={`lm-n ${c.skin} t-${c.tone}${c.big ? " big" : ""}${c.h < 44 ? " tight" : ""}${selected === c.select ? " sel" : ""}${focus && !near.has(c.id) ? " dim" : ""}`}
          style={{ left: c.x - c.w / 2, top: c.y - c.h / 2, width: c.w, height: c.h, ["--col" as string]: c.level }}
          onClick={() => onPick(c)}
          onMouseEnter={() => setFocus(c.id)}
          onMouseLeave={() => setFocus(null)}
          onFocus={() => setFocus(c.id)}
          onBlur={() => setFocus(null)}
        >
          <span className="lm-i">{c.skin === "core" && <svg className="lm-cookie" aria-hidden="true" viewBox="-36 -36 72 72"><path d={COOKIE} /></svg>}<Ms n={c.icon} fill /></span>
          <span className="lm-t">
            <span className="lm-nm">{c.label}</span><span className="lm-st">{c.skin === "core" ? <Ticking text={c.status} /> : c.status}</span>
            {c.meter !== undefined && <span className="lm-mb" role="meter" aria-label={`${c.label} load`} aria-valuemin={0} aria-valuemax={1} aria-valuenow={c.meter}><i className={c.meter > 0.85 ? "hi" : undefined} style={{ width: `${Math.round(c.meter * 100)}%` }} /></span>}
          </span>
        </button>
      ))}
      {layout.routes.filter((r) => BADGE[r.kind]).map((r) => (
        <span key={`b${r.a}>${r.b}`} className={`lm-badge ${r.kind}${off(r)}`} style={{ left: r.mx, top: r.my }} data-a={r.a} data-b={r.b}>
          <Ms n={BADGE[r.kind]} />
        </span>
      ))}
    </div>
  );
});
