// One drawing of boxes and lines at a fixed logical size (MapView scales it to fit). Used for the whole system and,
// in the plugin view, for what is inside Plugins. Pointing at (or tabbing to) a box dims everything it is not connected to.

import { memo, useState } from "react";
import { Ms } from "./m3";
import type { Layout, Placed } from "./mapGraph";

type Props = {
  layout: Layout;
  flow: boolean;
  selected?: string | null;
  onPick: (c: Placed) => void;
  label: string;
  className?: string;
};

const BADGE: Record<string, "close" | "info"> = { broken: "close", slow: "info" };

export const LevelCanvas = memo(function LevelCanvas({ layout, flow, selected, onPick, label, className }: Props) {
  const [focus, setFocus] = useState<string | null>(null);
  const near = new Set<string>();
  if (focus) {
    near.add(focus);
    for (const r of layout.routes) { if (r.a === focus) near.add(r.b); if (r.b === focus) near.add(r.a); }
  }
  const off = (r: { a: string; b: string }) => (focus && !(r.a === focus || r.b === focus) ? " dim" : "");
  return (
    <div className={`lm-cv${focus ? " focus" : ""}${flow ? "" : " still"}${className ? ` ${className}` : ""}`} style={{ width: layout.width, height: layout.height }} role="group" aria-label={label}>
      <svg className="lm-l" width={layout.width} height={layout.height} aria-hidden="true">
        {layout.routes.map((r) => (
          <path key={`${r.a}>${r.b}`} d={r.d} data-a={r.a} data-b={r.b} className={`lm-e ${r.kind}${off(r)}`} />
        ))}
      </svg>
      {layout.heads.map((h) => <div key={h.text} className="lm-ch" style={{ left: h.x, top: h.y }}>{h.text}</div>)}
      {Object.values(layout.placed).map((c) => (
        <button
          key={c.id}
          type="button"
          data-card={c.id}
          className={`lm-n ${c.skin} t-${c.tone}${c.big ? " big" : ""}${selected === c.select ? " sel" : ""}${focus && !near.has(c.id) ? " dim" : ""}`}
          style={{ left: c.x - c.w / 2, top: c.y - c.h / 2, width: c.w, height: c.h }}
          onClick={() => onPick(c)}
          onMouseEnter={() => setFocus(c.id)}
          onMouseLeave={() => setFocus(null)}
          onFocus={() => setFocus(c.id)}
          onBlur={() => setFocus(null)}
        >
          <span className="lm-i"><Ms n={c.icon} fill /></span>
          <span className="lm-t"><span className="lm-nm">{c.label}</span><span className="lm-st">{c.status}</span></span>
          {c.skin === "plug" && <span className="lm-go"><Ms n="fit_screen" /></span>}
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
