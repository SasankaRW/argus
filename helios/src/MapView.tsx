// The live map: the essential parts in five columns (Apps | Argus | Machines | Plugins | Models) joined by lines that
// show health, with what needs attention above. Selecting the Plugins box zooms the camera into it until the plugin
// view (what is working inside) takes its place. The layout and the health come from mapGraph.ts, attention.ts and
// pluginGraph.ts (pure, tested), the zoom from zoomMath.ts and usePluginZoom.ts; this file fits the drawing to its
// space, feeds it the live data and handles picking a box.

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api, type Approval, type ArgusEvent, type ArgusMap } from "./api";
import { attentionItems } from "./attention";
import { useClaude } from "./chips";
import { useJobs } from "./jobsFeed";
import { LevelCanvas } from "./LevelCanvas";
import { Ms } from "./m3";
import { boxOf, buildLevels, Card, Dim, L, layoutLevels, PLUGINS } from "./mapGraph";
import { MapHead } from "./MapHead";
import type { Selection } from "./mapInfo";
import { buildPluginView, layoutPluginView, PL } from "./pluginGraph";
import { useMapNav } from "./useMapNav";
import { usePluginZoom } from "./usePluginZoom";
import { reducedMotion, useMapFlow } from "./useMapFlow";
import type { SendLiveItem } from "./sendLive";

export type FitPad = { top: `${number}px`; right: `${number}px`; bottom: `${number}px`; left: `${number}px` };

type Props = {
  map: ArgusMap;
  selection: Selection;
  onSelect: (s: Selection) => void;
  flow: boolean;
  stale?: boolean;  // Argus out of reach: the last map, dimmed, lines still
  transfers?: SendLiveItem[];  // Argus Send's live transfers: shown in the plugin view
  sendName?: (id: string) => string;
  onTransfers?: () => void;  // the Send boxes open the Transfers page
  fitPad?: FitPad;  // room kept clear around the map (cards floating over it)
  headLeft?: number;  // where the attention row starts, when floating cards take the corner (the command layout)
  events?: ArgusEvent[];
  queue?: { running: number; queued: number } | null;  // the core's queue, from the status
  byWorker?: Record<string, number>;  // jobs running on each worker
  vertical?: boolean;  // the phone held upright: levels top to bottom, scrolled
  compact?: boolean;  // the phone held sideways: the desktop's five columns, drawn smaller and sized for reading
  onPlugins?: () => void;  // the Plugins page
  onReview?: () => void;  // the Approvals page
  onChanged?: () => void;  // a control was used: load the map again
};

// Desktop: five columns on a 1200 x 600 canvas (the positions of the approved preview). Phone: rows on a 346 wide canvas that scrolls.
export const DESK = { width: 1200, height: 600, xs: [100, 385, 615, 850, 1100] };
const deskDim = (c: Card): Dim => (c.big ? { w: 196, h: c.meter !== undefined ? 86 : 76 } : { w: c.skin === "model" ? 196 : c.skin === "plug" ? 210 : 176, h: c.meter !== undefined ? 68 : 54 });
const phoneDim = (c: Card): Dim => (c.big ? { w: 150, h: 70 } : { w: 102, h: 74 });
export const PHONE = { width: 346 };
// Phone held sideways (about 844 x 390): the same five columns on an 836 x 330 canvas, boxes sized so the text stays 12 px and up.
export const COMPACT = { width: 836, height: 330, xs: [64, 250, 450, 612, 764] };
const compactDim = (c: Card): Dim => (c.big ? { w: 132, h: c.meter !== undefined ? 60 : 58 } : { w: c.skin === "model" ? 134 : 120, h: c.meter !== undefined ? 50 : 42 });
export const HEAD_H = 64;  // the attention row
const PHONE_HEAD_TOP = 68;  // the phone: the attention strip starts under the Live chip

// The plugin view: three columns on the same canvas; the idle plugins sit in a panel along the bottom.
const MAX_IDLE = 30;  // names shown in the idle panel

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
  // The element behind the ref can change (turning the phone swaps the upright map for the sideways one), so it is
  // looked at after every render and the observer follows it; watching only the first one left the sizes at 0.
  const [el, setEl] = useState<HTMLElement | null>(null);
  useLayoutEffect(() => { if (ref.current !== el) setEl(ref.current); });
  useLayoutEffect(() => {
    if (!el) { setS((o) => (o.w || o.h ? { w: 0, h: 0 } : o)); return; }
    const read = () => setS((o) => (o.w === el.clientWidth && o.h === el.clientHeight ? o : { w: el.clientWidth, h: el.clientHeight }));
    read();
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, [el]);
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
  const compact = !!p.compact && !vertical;
  const layout = useMemo(
    () => (vertical
      ? layoutLevels(built.cards, built.lines, { orient: "v", dims: phoneDim, width: PHONE.width, perRow: 3, rowGap: 14, levelGap: 40, locked: [L.models] })
      : compact
      ? layoutLevels(built.cards, built.lines, { orient: "h", dims: compactDim, xs: COMPACT.xs, top: 36, bottom: 300, rowPitch: 54, rowPad: 70, width: COMPACT.width, height: COMPACT.height, locked: [L.models], compact: [L.models, L.plugins], slotDx: [-60, 68] })
      : layoutLevels(built.cards, built.lines, { orient: "h", dims: deskDim, xs: DESK.xs, width: DESK.width, height: DESK.height, locked: [L.models], compact: [L.models, L.plugins] })),
    [built, vertical, compact],
  );
  // the flow layer: dots for what happens from now on (off with the Flow switch, on a stale map, under reduced motion)
  const of = useMemo(() => boxOf(p.map), [p.map]);
  const flowOn = p.flow && !p.stale && !reducedMotion();
  const fl = useMapFlow(events, layout.routes, of, flowOn);
  const dots = useMemo(() => (flowOn ? { incoming: fl.incoming } : undefined), [flowOn, fl.incoming]);
  const waiting = p.map.nodes.find((n) => n.id === "approvals")?.pending ?? 0;
  const oldest = useOldestApproval(events, waiting);
  const items = useMemo(
    () => attentionItems(built.cards, { now, approvals: { count: waiting, oldest }, nodeKind: (id) => p.map.nodes.find((n) => n.id === id)?.kind }),
    [built.cards, now, waiting, oldest, p.map],
  );
  const wrap = useRef<HTMLDivElement>(null);
  const size = useBox(wrap);
  const jobs = useJobs(events);
  const pv = useMemo(() => {
    const workflows: Record<string, string> = {};
    for (const j of jobs) if ((j.state === "running" || j.state === "leased") && !workflows[j.plugin]) workflows[j.plugin] = j.workflow;
    return buildPluginView(p.map, { now, events, workflows, transfers: p.transfers, name: p.sendName });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.map, now, p.events, jobs, p.transfers, p.sendName]);
  const plugLayout = useMemo(
    () => layoutPluginView(pv),
    [pv],
  );
  const pb = layout.placed[PLUGINS];
  const column = (lv: number) => Object.values(plugLayout.placed).filter((c) => c.level === lv).sort((a, b) => a.y - b.y).map((c) => c.id);
  const zoom = usePluginZoom({
    enabled: !vertical && !!pb, box: pb ? { cx: pb.x, cy: pb.y, w: pb.w, h: pb.h } : null, canvas: { w: layout.width, h: layout.height }, plug: { w: plugLayout.width, h: plugLayout.height },
    groups: { src: column(PL.inputs), mid: column(PL.plugins), snk: column(PL.outputs) },
    at: Object.fromEntries(Object.values(plugLayout.placed).map((c) => [c.id, { x: c.x, y: c.y }])),
    fitScreen: compact, root: wrap, hasSelection: !!p.selection, clearSelection: () => p.onSelect(null),
  });
  const inPlugins = zoom.phase === "view";
  const [anim, setAnim] = useState(false);  // fitting animates only after the first fit, not from nothing
  const measured = size.w > 0;
  useEffect(() => {
    if (!measured) return;
    const id = requestAnimationFrame(() => setAnim(true));
    return () => cancelAnimationFrame(id);
  }, [measured]);
  const headRef = zoom.refs.head as React.RefObject<HTMLDivElement | null>;
  const headSize = useBox(headRef);
  const pad = { t: px(p.fitPad?.top), r: px(p.fitPad?.right), b: px(p.fitPad?.bottom), l: px(p.fitPad?.left) };
  const headTop = vertical ? PHONE_HEAD_TOP : compact ? 8 : Math.max(8, pad.t - HEAD_H - 6);
  if (!vertical) pad.t = Math.max(pad.t, headTop + headSize.h + 4);  // the map starts below the row, however tall it grew (a narrow window wraps it)

  // fit: desktop contains the whole canvas in the space left by the floating cards; the phone fits the width and scrolls
  let scale = 1, x = 0, y = 0;
  if (size.w && size.h) {
    if (vertical) {  // the phone fits the width, starts under the attention strip, and is moved with a finger from there
      scale = Math.min(1.3, (size.w - 24) / layout.width);
      x = (size.w - layout.width * scale) / 2;
      y = headTop + headSize.h + 14;
    } else {
      const aw = Math.max(200, size.w - pad.l - pad.r), ah = Math.max(160, size.h - pad.t - pad.b);
      scale = Math.min(aw / layout.width, ah / layout.height, 1.45);
      x = pad.l + (aw - layout.width * scale) / 2;
      y = pad.t + (ah - layout.height * scale) / 2;
    }
  }

  // Sideways phone: the plugin view (laid out on 1200 x 600) is fitted to what is drawn (the cards and the idle panel) and
  // to the whole screen under the bar, so its text stays as large as the screen allows.
  const barSize = useBox(zoom.refs.bar), idleSize = useBox(zoom.refs.idle);
  let plugFit: React.CSSProperties | undefined, idleFit: React.CSSProperties | undefined;
  if (compact && size.w && size.h) {
    const cs = Object.values(plugLayout.placed);
    const x0 = Math.min(...cs.map((c) => c.x - c.w / 2)), x1 = Math.max(...cs.map((c) => c.x + c.w / 2)), y1 = Math.max(...cs.map((c) => c.y + c.h / 2));
    const y0 = Math.min(...cs.map((c) => c.y - c.h / 2));
    const iy = y1 + 14, M = 16;
    const bx0 = x0 - M, bx1 = x1 + M, by0 = y0 - M, by1 = iy + idleSize.h + M;
    const aw = size.w - 24, top = headTop + barSize.h + 8, ah = size.h - 12 - top;
    const k = Math.min(aw / (bx1 - bx0), ah / (by1 - by0), 1.2);
    const left = 12 + (aw - (bx1 - bx0) * k) / 2 - bx0 * k, topW = top + (ah - (by1 - by0) * k) / 2 - by0 * k;
    plugFit = { inset: "auto", left: (left - x) / scale, top: (topW - y) / scale, width: plugLayout.width, height: plugLayout.height, transformOrigin: "0 0", scale: String(k / scale) };
    idleFit = { left: x0, right: plugLayout.width - x1, bottom: "auto", top: iy };
  }

  const nav = useMapNav({ ref: wrap, fit: { x, y, s: scale }, content: { w: layout.width, h: layout.height }, enabled: !inPlugins && !!size.w, ignore: ".lm-head, .lm-nav" });
  const V = nav.view;
  const stageStyle = { transform: `translate(${V.x}px, ${V.y}px) scale(${V.s})`, width: layout.width, height: layout.height };
  const stageCls = `lm-stage${anim && !nav.dragging && (nav.smooth || !nav.moved) ? " anim" : ""}`;
  const navBtns = compact ? null : (
    <div className="lm-nav" role="group" aria-label="Move the map" style={vertical || compact ? undefined : { bottom: pad.b + 14 }}>
      <button type="button" aria-label="Zoom in" onClick={nav.zoomIn}><Ms n="add" /></button>
      <button type="button" aria-label="Zoom out" onClick={nav.zoomOut}><Ms n="remove" /></button>
      <button type="button" aria-label="Fit the whole map" className={nav.moved ? "on" : undefined} onClick={nav.reset}><Ms n="fit_screen" /></button>
    </div>
  );
  const sel = p.selection?.type === "node" ? p.selection.id : null;
  const pick = (c: Card) => (c.id === PLUGINS ? (vertical ? p.onPlugins?.() : (nav.reset(), zoom.open())) : p.onSelect({ type: "node", id: c.select }));
  const headStyle = vertical ? { top: headTop, left: 12, right: 12 } : compact ? { top: 8, left: 12, right: 64 } : { top: headTop, left: p.headLeft ?? Math.max(18, pad.l), right: Math.max(18, pad.r - 12) };
  const head = (
    <MapHead ref={headRef} items={items} anyPaused={p.map.nodes.some((n) => !!n.paused)} onReview={() => p.onReview?.()} onLogs={(id) => p.onSelect({ type: "node", id, logs: true })} onChanged={() => p.onChanged?.()}
      className={vertical ? "stack" : compact ? "compact" : undefined}
      style={headStyle} />
  );
  return (
    <div ref={wrap} className={`lm lm-pan${compact ? " lm-compact" : ""}${p.stale ? " lm-stale" : ""}${nav.dragging ? " drag" : ""}`} onClick={(e) => { if (e.target === e.currentTarget) p.onSelect(null); }}>
      {vertical ? (<>
        {!p.stale && head}
        <div className={stageCls} style={stageStyle}>
          <LevelCanvas layout={layout} flow={p.flow} selected={sel} onPick={pick} label="Live map" dots={dots} />
        </div>
        {navBtns}
      </>) : (<>
        {!p.stale && head}
        <div ref={zoom.refs.bar} className={`lm-head lm-bar${compact ? " compact" : ""}`} style={{ ...headStyle, opacity: 0, pointerEvents: "none" }} aria-hidden={!inPlugins}>
          <button ref={zoom.refs.back} type="button" className="lm-go lm-back" onClick={zoom.back}><Ms n="arrow_back" />Back to map</button>
          <span className="lm-note">{compact
            ? `${pv.running ? `${pv.running} working` : "Nothing running"}${pv.stopped ? ` · ${pv.stopped} stopped` : ""}`
            : `${pv.running ? `${pv.running} working now.` : "Nothing is running right now."}${pv.stopped ? ` ${pv.stopped} stopped.` : ""} Idle plugins are listed below.`}</span>
          <button type="button" className="lm-go lm-back" onClick={() => p.onPlugins?.()}><Ms n="extension" />All plugins</button>
        </div>
        <div className={stageCls} style={stageStyle}>
          <div ref={zoom.refs.clip} className="lm-clip">
            <div ref={zoom.refs.cam} className="lm-cam">
              <LevelCanvas layout={layout} flow={p.flow} selected={sel} onPick={pick} label="Live map" dots={dots} />
            </div>
            <div ref={zoom.refs.view} className="lm-plug" aria-label="Inside the plugins" aria-hidden={!inPlugins} style={{ opacity: 0, pointerEvents: "none", ...plugFit }}>
              <div ref={zoom.refs.pbg} className="lm-pbg" />
              <LevelCanvas layout={plugLayout} flow={p.flow} selected={sel} onPick={(c) => (c.select === "transfers" ? p.onTransfers?.() : p.onSelect({ type: "node", id: c.select }))} label="Inside the plugins" />
              <div ref={zoom.refs.idle} className="lm-idle" style={idleFit}>
                <b>{pv.idle.length} idle plugin{pv.idle.length === 1 ? "" : "s"}</b>
                <div>{pv.idle.slice(0, MAX_IDLE).map((n) => <span key={n}>{n}</span>)}{pv.idle.length > MAX_IDLE && <span>+{pv.idle.length - MAX_IDLE} more</span>}</div>
              </div>
            </div>
          </div>
        </div>
        {navBtns}
      </>)}
    </div>
  );
}
