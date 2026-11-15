// The live map: the essential parts in four levels (Apps, Argus, Machines and work, Models) joined by lines that show
// health, with a status line on top. The layout and the health come from mapGraph.ts (pure, tested); this file fits
// the drawing to its space, feeds it the live data and handles picking a box.

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ArgusEvent, ArgusMap } from "./api";
import { LevelCanvas } from "./LevelCanvas";
import { Ms } from "./m3";
import { buildLevels, Card, Dim, layoutLevels, LEVEL_HEADS, PLUGINS, statusLine } from "./mapGraph";
import type { Selection } from "./mapInfo";

export type FitPad = { top: `${number}px`; right: `${number}px`; bottom: `${number}px`; left: `${number}px` };

type Props = {
  map: ArgusMap;
  selection: Selection;
  onSelect: (s: Selection) => void;
  flow: boolean;
  fitPad?: FitPad;  // room kept clear around the map (cards floating over it)
  events?: ArgusEvent[];
  queue?: { running: number; queued: number } | null;  // the core's queue, from the status
  byWorker?: Record<string, number>;  // jobs running on each worker
  vertical?: boolean;  // the phone: levels top to bottom, scrolled
  onPlugins?: () => void;
};

// Desktop: four columns on a 1190 x 600 canvas. Phone: rows on a 346 wide canvas that scrolls.
export const DESK = { width: 1190, height: 600, xs: [100, 460, 790, 1055], top: 100, bottom: 540 };
const deskDim = (c: Card): Dim => (c.big ? { w: 196, h: 76 } : { w: c.skin === "model" ? 210 : 176, h: 54 });
const phoneDim = (c: Card): Dim => (c.big ? { w: 150, h: 70 } : { w: 102, h: 74 });
export const PHONE = { width: 346 };

const px = (s?: string) => (s ? parseFloat(s) || 0 : 0);

// The time as the map sees it, in seconds; moves on every few seconds so a line stops being "busy" a minute after the last message.
function useNow(): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now() / 1000), 5000);
    return () => window.clearInterval(t);
  }, []);
  return now;
}

export function useBox(ref: React.RefObject<HTMLElement | null>): { w: number; h: number } {
  const [s, setS] = useState({ w: 0, h: 0 });
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const read = () => setS((o) => (o.w === el.clientWidth && o.h === el.clientHeight ? o : { w: el.clientWidth, h: el.clientHeight }));
    read();
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return s;
}

export function MapView(p: Props) {
  const now = useNow();
  const events = p.events ?? [];
  const built = useMemo(
    () => buildLevels(p.map, { now, events, queue: p.queue, byWorker: p.byWorker }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [p.map, now, p.events, p.queue, p.byWorker],
  );
  const vertical = !!p.vertical;
  const layout = useMemo(
    () => (vertical
      ? layoutLevels(built.cards, built.lines, { orient: "v", dims: phoneDim, heads: LEVEL_HEADS, width: PHONE.width, perRow: 3, rowGap: 14, levelGap: 40, locked: [3] })
      : layoutLevels(built.cards, built.lines, { orient: "h", dims: deskDim, heads: LEVEL_HEADS, xs: DESK.xs, top: DESK.top, bottom: DESK.bottom, width: DESK.width, height: DESK.height, locked: [3] })),
    [built, vertical],
  );
  const status = useMemo(() => statusLine(built.problems), [built.problems]);
  const wrap = useRef<HTMLDivElement>(null);
  const size = useBox(wrap);
  const [anim, setAnim] = useState(false);  // fitting animates only after the first fit, not from nothing
  const measured = size.w > 0;
  useEffect(() => {
    if (!measured) return;
    const id = requestAnimationFrame(() => setAnim(true));
    return () => cancelAnimationFrame(id);
  }, [measured]);
  const pad = { t: px(p.fitPad?.top), r: px(p.fitPad?.right), b: px(p.fitPad?.bottom), l: px(p.fitPad?.left) };

  // fit: desktop contains the whole canvas in the space left by the floating cards; the phone fits the width and scrolls
  let scale = 1, x = 0, y = 0;
  if (size.w && size.h) {
    if (vertical) scale = Math.min(1.5, size.w / layout.width);
    else {
      const aw = Math.max(200, size.w - pad.l - pad.r), ah = Math.max(160, size.h - pad.t - pad.b);
      scale = Math.min(aw / layout.width, ah / layout.height, 1.45);
      x = pad.l + (aw - layout.width * scale) / 2;
      y = pad.t + (ah - layout.height * scale) / 2;
    }
  }

  const sel = p.selection?.type === "node" ? p.selection.id : null;
  const pick = (c: Card) => (c.id === PLUGINS && p.onPlugins ? p.onPlugins() : p.onSelect({ type: "node", id: c.select }));
  const statusTop = Math.max(8, vertical ? pad.t - 50 : pad.t - 46);
  const pills = status.items.slice(0, 3);
  const statusPills = (<>
    {status.items.length === 0 && <span className="lm-pill ok"><Ms n="check" />All fine</span>}
    {pills.map((it) => <span key={it.text} className={`lm-pill ${it.tone}`}><Ms n={it.tone === "bad" ? "cloud_off" : "hourglass_top"} />{it.text}</span>)}
    {status.items.length > pills.length && <span className="lm-pill">+{status.items.length - pills.length} more</span>}
  </>);
  return (
    <div ref={wrap} className={`lm${vertical ? " scroll" : ""}`} onClick={(e) => { if (e.target === e.currentTarget) p.onSelect(null); }}>
      {vertical ? (
        <div style={{ paddingBottom: pad.b, boxSizing: "border-box" }}>
          <div className="lm-status flow" style={{ marginTop: statusTop }}>{statusPills}</div>
          <div style={{ width: layout.width * scale, height: layout.height * scale, margin: "0 auto" }}>
            <div style={{ transform: `scale(${scale})`, transformOrigin: "0 0", width: layout.width, height: layout.height }}>
              <LevelCanvas layout={layout} flow={p.flow} selected={sel} onPick={pick} label="Live map" />
            </div>
          </div>
        </div>
      ) : (<>
        <div className="lm-status" style={{ top: statusTop, left: Math.max(18, pad.l) }} role="status">{statusPills}</div>
        <div className={`lm-stage${anim ? " anim" : ""}`} style={{ transform: `translate(${x}px, ${y}px) scale(${scale})`, width: layout.width, height: layout.height }}>
          <LevelCanvas layout={layout} flow={p.flow} selected={sel} onPick={pick} label="Live map" />
        </div>
      </>)}
    </div>
  );
}
