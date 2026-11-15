// The live map: the essential parts in five columns (Apps | Argus | Machines | Plugins | Models) joined by lines that
// show health, with what needs attention above. The layout and the health come from mapGraph.ts and attention.ts (pure,
// tested); this file fits the drawing to its space, feeds it the live data and handles picking a box.

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api, type Approval, type ArgusEvent, type ArgusMap } from "./api";
import { attentionItems } from "./attention";
import { useClaude } from "./chips";
import { LevelCanvas } from "./LevelCanvas";
import { buildLevels, Card, Dim, L, layoutLevels, PLUGINS } from "./mapGraph";
import { MapHead } from "./MapHead";
import type { Selection } from "./mapInfo";

export type FitPad = { top: `${number}px`; right: `${number}px`; bottom: `${number}px`; left: `${number}px` };

type Props = {
  map: ArgusMap;
  selection: Selection;
  onSelect: (s: Selection) => void;
  flow: boolean;
  fitPad?: FitPad;  // room kept clear around the map (cards floating over it)
  headLeft?: number;  // where the attention row starts, when floating cards take the corner (the command layout)
  events?: ArgusEvent[];
  queue?: { running: number; queued: number } | null;  // the core's queue, from the status
  byWorker?: Record<string, number>;  // jobs running on each worker
  vertical?: boolean;  // the phone: levels top to bottom, scrolled
  onPlugins?: () => void;  // the Plugins page
  onReview?: () => void;  // the Approvals page
  onChanged?: () => void;  // a control was used: load the map again
};

// Desktop: five columns on a 1200 x 600 canvas (the positions of the approved preview). Phone: rows on a 346 wide canvas that scrolls.
export const DESK = { width: 1200, height: 600, xs: [100, 385, 615, 850, 1100] };
const deskDim = (c: Card): Dim => (c.big ? { w: 196, h: c.meter !== undefined ? 86 : 76 } : { w: c.skin === "model" ? 196 : c.skin === "plug" ? 210 : 176, h: c.meter !== undefined ? 68 : 54 });
const phoneDim = (c: Card): Dim => (c.big ? { w: 150, h: 70 } : { w: 102, h: 74 });
export const PHONE = { width: 346 };
export const HEAD_H = 64;  // the attention row

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

// When the oldest waiting approval was asked for (epoch seconds), read again when an approval event arrives.
function useOldestApproval(events: ArgusEvent[], waiting: number): number | null {
  const [oldest, setOldest] = useState<number | null>(null);
  const last = events.filter((e) => /^approval\./.test(e.kind)).map((e) => e.seq).pop() ?? 0;
  useEffect(() => {
    if (!waiting) { setOldest(null); return; }
    api<Approval[]>("/approvals?state=pending").then((l) => setOldest(l.length ? Math.min(...l.map((a) => a.created_at)) : null)).catch(() => {});
  }, [last, waiting]);
  return oldest;
}

export function MapView(p: Props) {
  const now = useNow();
  const events = p.events ?? [];
  const claude = useClaude();
  const built = useMemo(
    () => buildLevels(p.map, { now, events, queue: p.queue, byWorker: p.byWorker, claude }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [p.map, now, p.events, p.queue, p.byWorker, claude],
  );
  const vertical = !!p.vertical;
  const layout = useMemo(
    () => (vertical
      ? layoutLevels(built.cards, built.lines, { orient: "v", dims: phoneDim, width: PHONE.width, perRow: 3, rowGap: 14, levelGap: 40, locked: [L.models] })
      : layoutLevels(built.cards, built.lines, { orient: "h", dims: deskDim, xs: DESK.xs, width: DESK.width, height: DESK.height, locked: [L.models], compact: [L.models, L.plugins] })),
    [built, vertical],
  );
  const waiting = p.map.nodes.find((n) => n.id === "approvals")?.pending ?? 0;
  const oldest = useOldestApproval(events, waiting);
  const items = useMemo(
    () => attentionItems(built.cards, { now, approvals: { count: waiting, oldest }, nodeKind: (id) => p.map.nodes.find((n) => n.id === id)?.kind }),
    [built.cards, now, waiting, oldest, p.map],
  );
  const wrap = useRef<HTMLDivElement>(null);
  const size = useBox(wrap);
  const [anim, setAnim] = useState(false);  // fitting animates only after the first fit, not from nothing
  const measured = size.w > 0;
  useEffect(() => {
    if (!measured) return;
    const id = requestAnimationFrame(() => setAnim(true));
    return () => cancelAnimationFrame(id);
  }, [measured]);
  const headRef = useRef<HTMLDivElement>(null);
  const headSize = useBox(headRef);
  const pad = { t: px(p.fitPad?.top), r: px(p.fitPad?.right), b: px(p.fitPad?.bottom), l: px(p.fitPad?.left) };
  const headTop = Math.max(8, pad.t - HEAD_H - 6);
  if (!vertical) pad.t = Math.max(pad.t, headTop + headSize.h + 8);  // the map starts below the row, however tall it grew (a narrow window wraps it)

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
  const pick = (c: Card) => (c.id === PLUGINS ? p.onPlugins?.() : p.onSelect({ type: "node", id: c.select }));
  const head = (
    <MapHead ref={headRef} items={items} onReview={() => p.onReview?.()} onLogs={(id) => p.onSelect({ type: "node", id, logs: true })} onChanged={() => p.onChanged?.()}
      className={vertical ? "stack" : undefined}
      style={vertical ? undefined : { top: headTop, left: p.headLeft ?? Math.max(18, pad.l), right: Math.max(18, pad.r - 12) }} />
  );
  return (
    <div ref={wrap} className={`lm${vertical ? " scroll" : ""}`} onClick={(e) => { if (e.target === e.currentTarget) p.onSelect(null); }}>
      {vertical ? (
        <div style={{ paddingBottom: pad.b, boxSizing: "border-box" }}>
          <div style={{ marginTop: Math.max(8, pad.t - 50) }}>{head}</div>
          <div style={{ width: layout.width * scale, height: layout.height * scale, margin: "0 auto" }}>
            <div style={{ transform: `scale(${scale})`, transformOrigin: "0 0", width: layout.width, height: layout.height }}>
              <LevelCanvas layout={layout} flow={p.flow} selected={sel} onPick={pick} label="Live map" />
            </div>
          </div>
        </div>
      ) : (<>
        {head}
        <div className={`lm-stage${anim ? " anim" : ""}`} style={{ transform: `translate(${x}px, ${y}px) scale(${scale})`, width: layout.width, height: layout.height }}>
          <LevelCanvas layout={layout} flow={p.flow} selected={sel} onPick={pick} label="Live map" />
        </div>
      </>)}
    </div>
  );
}
