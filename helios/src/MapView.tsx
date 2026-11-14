// The live map: boxes from the registry, lines from traffic, a dot along a line for every message.

import {
  Background, BackgroundVariant, BaseEdge, EdgeLabelRenderer, EdgeProps, getBezierPath, Handle, Node, NodeProps, Position, ReactFlow,
  useEdgesState, useInternalNode, useNodesState, useReactFlow, useViewport, ReactFlowProvider, Edge,
} from "@xyflow/react";
import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ArgusEvent, ArgusMap, MapNode } from "./api";
import { Ms } from "./m3";
import type { IconName } from "./icons";
import { ColumnLabel, layout, loadPins, NODE_H, NODE_W, Pos, Route, splinePath, savePins } from "./layout";
import type { Pulse } from "./live";
import { ago } from "./format";

export type Selection = { type: "node"; id: string } | { type: "edge"; src: string; dst: string } | { type: "job"; id: string } | null;

type BoxData = { n: MapNode; hot: boolean; fresh: boolean; selected: boolean; info: BoxInfo };
type LineType = "ok" | "ctl" | "mdl" | "bad";
type LineData = { route?: Route; count: number; rate: number; ltype: LineType; pulses: (Pulse & { back: boolean })[]; flow: boolean; selected: boolean; hot: boolean; tone: string };

const KIND_LABEL: Record<string, string> = { core: "core", worker: "worker", plugin: "plugin", model: "model", app: "app", service: "service" };

export function sub(n: MapNode): string {
  if (n.kind === "core") return `core${n.meta?.version ? ` · v${n.meta.version}` : ""}`;
  if (n.kind === "worker") return `${n.state ?? "unknown"}${n.group ? ` · ${n.group}` : ""}`;
  if (n.id === PLUGINS) {
    const j = n.jobs ?? { active: 0, queued: 0, waiting: 0 };
    const busy = [j.active ? `${j.active} running` : "", j.queued ? `${j.queued} queued` : "", j.waiting ? `${j.waiting} waiting` : ""].filter(Boolean);
    return busy.length ? `${n.meta.count} plugins · ${busy.join(" · ")}` : `${n.meta.count} plugins · idle`;
  }
  if (n.kind === "plugin") {
    const j = n.jobs ?? { active: 0, queued: 0, waiting: 0 };
    if (!j.active && !j.queued && !j.waiting) return "idle";
    return [j.active ? `${j.active} running` : "", j.queued ? `${j.queued} queued` : "",
      j.waiting ? `${j.waiting} waiting` : ""].filter(Boolean).join(" · ");
  }
  if (n.id === "phone") {
    const m = n.meta as { online?: boolean | null; since?: number | null; device?: string };
    if (m.online === true) return `online · ${m.device ?? "tailscale"}`;
    if (m.online === false) return `offline${m.since ? ` · ${ago(m.since)}` : ""}`;
    return "checking…";
  }
  if (n.id === "power") {
    const m = n.meta as { state?: string; mode?: string; idle_since?: number };
    const sim = m.mode === "simulated" ? " · sim" : "";
    if (m.state === "busy") return `PC busy${sim}`;
    if (m.state === "would_shutdown") return `would shut down${sim}`;
    if (m.state === "idle") return `idle${m.idle_since ? ` ${ago(m.idle_since).replace(/ ago$/, "")}` : ""}${sim}`;
    return `watching${sim}`;
  }
  if (n.id === "approvals") return n.pending ? `${n.pending} waiting for you` : "nothing to decide";
  if (n.id === "scheduler") return "cron · triggers";
  if (n.id === "phone-app") return n.failed ? `${n.failed} not delivered` : n.unsent ? `${n.unsent} waiting for the phone` : "all delivered";
  if (n.kind === "model") {
    const st = n.state === "open" ? "paused" : n.state === "half_open" ? "trying again" : "ready";
    const calls = (n as MapNode & { calls?: number }).calls ?? 0;
    return `${st} · ${calls} call${calls === 1 ? "" : "s"}`;
  }
  return n.implicit ? "seen in events" : KIND_LABEL[n.kind];
}

// One line per pair of components: a reply travels back along the request's line instead of adding a second one.
export type MergedEdge = { src: string; dst: string; count: number; last_kind: string; first_seen: number; last_seen: number };
export function mergeEdges(edges: ArgusMap["edges"]): MergedEdge[] {
  const byPair = new Map<string, MergedEdge>();
  for (const e of [...edges].sort((a, b) => a.first_seen - b.first_seen)) {
    const key = [e.src, e.dst].sort().join("|");
    const m = byPair.get(key);
    if (!m) byPair.set(key, { ...e });
    else {
      m.count += e.count;
      if (e.last_seen > m.last_seen) { m.last_seen = e.last_seen; m.last_kind = e.last_kind; }
    }
  }
  return [...byPair.values()];
}

function dotColor(n: MapNode, hot: boolean): string {
  if (n.kind === "worker") return n.state === "online" ? "var(--ok)" : "var(--bad)";
  if (n.kind === "model" && n.state === "open") return "var(--bad)";
  if (hot) return "var(--flow)";
  if (n.kind === "core") return "var(--amber)";
  if (n.kind === "model") return n.state === "half_open" ? "var(--amber)" : "var(--violet)";
  if (n.kind === "plugin") return (n.jobs?.active ?? 0) > 0 ? "var(--flow)" : (n.jobs?.waiting ?? 0) > 0 ? "var(--amber)" : "var(--ok)";
  if (n.id === "phone") return n.meta?.online === true ? "var(--ok)" : n.meta?.online === false ? "var(--bad)" : "var(--tx3)";
  if (n.id === "power") return n.meta?.state === "busy" ? "var(--flow)" : n.meta?.state === "would_shutdown" ? "var(--amber)" : "var(--ok)";
  if (n.id === "approvals") return n.pending ? "var(--amber)" : "var(--ok)";
  if (n.id === "phone-app") return n.failed ? "var(--bad)" : "var(--ok)";
  return "var(--tx3)";
}

// What a box shows beyond its name: an icon tile (colour by meaning), one key/value row and, where the numbers exist,
// a meter. Only live data: counts from the registry, the status and the jobs list. Nothing is made up.
export type BoxInfo = { icon: IconName; tone: "core" | "ok" | "sec" | "ter" | "err" | "plain"; row?: [string, string]; meter?: number };
export type Ctx = { queue: { running: number; queued: number } | null; byWorker: Record<string, number> };

export function boxInfo(n: MapNode, ctx: Ctx): BoxInfo {
  if (n.kind === "core") {
    const q = ctx.queue;
    return { icon: "dns", tone: "core", row: q ? ["Queue", `${q.running} running · ${q.queued} waiting`] : undefined,
      meter: q && q.running + q.queued > 0 ? q.running / (q.running + q.queued) : undefined };
  }
  if (n.kind === "worker") {
    const run = ctx.byWorker[n.id] ?? ctx.byWorker[n.id.replace(/-now$/, "")] ?? 0;
    return { icon: /pc|desktop|gpu/i.test(n.id + n.label) ? "desktop_windows" : "laptop_chromebook", tone: n.state === "online" ? "ok" : "err",
      row: ["Running", `${run} ${run === 1 ? "job" : "jobs"}`] };
  }
  if (n.kind === "plugin") {
    const j = n.jobs ?? { active: 0, queued: 0, waiting: 0 };
    const all = j.active + j.queued + (j.waiting ?? 0);
    return { icon: "extension", tone: "sec", row: ["Jobs", all ? `${j.active} running · ${j.queued + (j.waiting ?? 0)} waiting` : "none"], meter: all ? j.active / all : undefined };
  }
  if (n.kind === "model") {
    const calls = (n as MapNode & { calls?: number }).calls ?? 0;
    return { icon: "neurology", tone: n.state === "open" ? "err" : "ter", row: ["Calls", String(calls)] };
  }
  if (n.id === "phone" || n.id === "phone-app") return { icon: "smartphone", tone: n.id === "phone" && n.meta?.online === true ? "ok" : "plain", row: n.id === "phone-app" ? ["Waiting", String(n.unsent ?? 0)] : undefined };
  if (n.id === "power") return { icon: "power_settings_new", tone: "plain" };
  if (n.id === "approvals") return { icon: "pending_actions", tone: "ter", row: ["Waiting", String(n.pending ?? 0)] };
  if (n.id === "scheduler") return { icon: "schedule", tone: "sec" };
  return { icon: "hub", tone: "plain" };
}

const Box = memo(function Box({ data }: NodeProps<Node<BoxData>>) {
  const { n, hot, fresh, selected, info } = data;
  const live = n.kind === "worker" && n.state === "online";
  return (
    <div className={`box t-${info.tone}${hot ? " on" : ""}${selected ? " sel" : ""}${n.implicit ? " ghost" : ""}${fresh ? " born" : ""}`}>
      <Handle type="target" position={Position.Left} className="port" isConnectable={false} />
      <Handle id="t" type="target" position={Position.Top} className="port" isConnectable={false} />
      <Handle id="b-in" type="target" position={Position.Bottom} className="port" isConnectable={false} />
      <div className="nh">
        <span className="ni"><Ms n={info.icon} fill /></span>
        <div className="nx">
          <div className="nn"><span className="name">{n.label}</span>{fresh && <span className="new">new</span>}</div>
          <div className="ns">{sub(n)}</div>
        </div>
        <span className={`dot${live ? " live" : ""}`} style={{ background: dotColor(n, hot) }} />
      </div>
      {info.row && <div className="brow"><span>{info.row[0]}</span><span className="num">{info.row[1]}</span></div>}
      {info.meter !== undefined && <div className="bar"><span style={{ width: `${Math.round(Math.max(0.04, info.meter) * 100)}%` }} /></div>}
      <Handle type="source" position={Position.Right} className="port" isConnectable={false} />
      <Handle id="b" type="source" position={Position.Bottom} className="port" isConnectable={false} />
      <Handle id="t-out" type="source" position={Position.Top} className="port" isConnectable={false} />
    </div>
  );
});

function Dot({ path, tone, back }: { path: string; tone: string; back: boolean }) {
  const ref = useRef<SVGAnimateMotionElement>(null);
  useLayoutEffect(() => { ref.current?.beginElement(); }, []);
  return (
    <circle r="3.2" className={`packet ${tone}`}>
      <animateMotion ref={ref} dur="1.1s" begin="indefinite" fill="freeze" path={path} keyPoints={back ? "1;0" : "0;1"} keyTimes="0;1" calcMode="linear" />
    </circle>
  );
}

// Where each handle sits on a box. Boxes have a fixed size, so line ends are worked out here instead of measured
// on screen: measuring breaks when the map is shown turned sideways (the phone's full-screen map).
const HANDLE: Record<string, [number, number, Position]> = {
  "": [NODE_W, NODE_H / 2, Position.Right], in: [0, NODE_H / 2, Position.Left],
  b: [NODE_W / 2, NODE_H, Position.Bottom], "t-out": [NODE_W / 2, 0, Position.Top],
  t: [NODE_W / 2, 0, Position.Top], "b-in": [NODE_W / 2, NODE_H, Position.Bottom],
};
function useEnds(p: EdgeProps<Edge<LineData>>) {
  const a = useInternalNode(p.source), b = useInternalNode(p.target);
  if (!a || !b) return p;
  const [sx, sy, sp] = HANDLE[p.sourceHandleId ?? ""] ?? HANDLE[""];
  const [tx, ty, tp] = HANDLE[p.targetHandleId ?? "in"] ?? HANDLE.in;
  const pa = a.internals.positionAbsolute, pb = b.internals.positionAbsolute;
  return { ...p, sourceX: pa.x + sx, sourceY: pa.y + sy, targetX: pb.x + tx, targetY: pb.y + ty, sourcePosition: sp, targetPosition: tp };
}

const Line = memo(function Line(props: EdgeProps<Edge<LineData>>) {
  const p = useEnds(props);
  const d = p.data!;
  // ELK's route when the boxes are where the layout put them; a plain curve once you drag one away
  const r = d.route;
  const fits = r && r.length >= 2 && Math.abs(r[0].x - p.sourceX) < 3 && Math.abs(r[0].y - p.sourceY) < 3
    && Math.abs(r[r.length - 1].x - p.targetX) < 3 && Math.abs(r[r.length - 1].y - p.targetY) < 3;
  const bez = getBezierPath({ sourceX: p.sourceX, sourceY: p.sourceY, targetX: p.targetX, targetY: p.targetY, sourcePosition: p.sourcePosition, targetPosition: p.targetPosition });
  const path = fits ? splinePath(r!) : bez[0];
  const mid = fits ? r![Math.floor(r!.length / 2)] : { x: bez[1], y: bez[2] };
  const width = 2.5;
  const flowing = d.hot || d.rate > 0;
  const type = d.hot && d.tone === "bad" ? "bad" : d.ltype;
  return (
    <>
      <BaseEdge id={p.id} path={path} className={`line${d.selected ? " sel" : ""}`} style={{ strokeWidth: width }} interactionWidth={16} />
      {flowing && <path d={path} className={`line flow ${type}`} style={{ strokeWidth: width }} fill="none" pointerEvents="none" />}
      {d.rate > 0 && (
        <EdgeLabelRenderer>
          <span className={`rate ${type}`} style={{ transform: `translate(-50%, -50%) translate(${mid.x}px, ${mid.y}px)` }}>{d.rate}/min</span>
        </EdgeLabelRenderer>
      )}
      {d.flow && d.pulses.map((x) => <Dot key={x.id} path={path} tone={x.tone} back={x.back} />)}
    </>
  );
});

// A plain heading above a column; never takes clicks.
const Heading = memo(function Heading({ data }: NodeProps<Node<{ label: ColumnLabel }>>) {
  return <span className="col-h">{data.label.text}</span>;
});

const nodeTypes = { box: Box, heading: Heading };
const edgeTypes = { line: Line };

type Props = {
  map: ArgusMap;
  pulses: Pulse[];
  active: Record<string, number>;
  selection: Selection;
  onSelect: (s: Selection) => void;
  flow: boolean;
  relayoutSignal: number;
  fitPad?: FitPad;  // room kept clear around the map when it fits itself (cards floating over it)
  events?: ArgusEvent[];  // for the rate on each line (messages in the last minute)
  queue?: Ctx["queue"];  // the core's queue, from the status
  byWorker?: Ctx["byWorker"];  // jobs running on each worker
  vertical?: boolean;  // lay the boxes out top to bottom (the phone)
};

// Zoom in, out and fit, as floating round buttons (the board's zoom column on the phone, the zoom row on the desktop).
function ZoomPad({ onFit }: { onFit: () => void }) {
  const rf = useReactFlow();
  const { zoom } = useViewport();
  return (
    <div className="zoompad" role="group" aria-label="Zoom">
      <button type="button" className="zb zout" aria-label="Zoom out" onClick={() => rf.zoomOut({ duration: 200 })}><Ms n="remove" /></button>
      <span className="zpct mono">{Math.round(zoom * 100)}%</span>
      <button type="button" className="zb zin" aria-label="Zoom in" onClick={() => rf.zoomIn({ duration: 200 })}><Ms n="add" /></button>
      <button type="button" className="zb zfit" aria-label="Fit to screen" onClick={onFit}><Ms n="fit_screen" /></button>
    </div>
  );
}

export type FitPad = { top: `${number}px`; right: `${number}px`; bottom: `${number}px`; left: `${number}px` };

function Inner({ map, pulses, active, selection, onSelect, flow, relayoutSignal, fitPad, rates, queue, byWorker, vertical }: Props & { rates: Record<string, number> }) {
  const [positions, setPositions] = useState<Record<string, Pos>>({});
  const [labels, setLabels] = useState<ColumnLabel[]>([]);
  const [routes, setRoutes] = useState<Record<string, Route>>({});
  const [pins, setPins] = useState<Record<string, Pos>>(() => loadPins());
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges] = useEdgesState<Edge<LineData>>([]);
  const [now, setNow] = useState(Date.now());
  const rf = useReactFlow();
  const coarse = useMemo(() => typeof window !== "undefined" && window.matchMedia("(pointer: coarse)").matches, []);
  const userMoved = useRef(false); // once you pan or zoom, the map stops re-fitting itself
  const wrap = useRef<HTMLDivElement>(null);
  const pad = useRef(fitPad);
  pad.current = fitPad;
  const fit = (duration = 300) => rf.fitView({ padding: pad.current ?? 0.1, maxZoom: 1.45, duration });
  const padKey = fitPad ? Object.values(fitPad).join(",") : "";
  useEffect(() => {  // the layout changed: fit again (unless you moved the map yourself)
    if (!userMoved.current) window.setTimeout(() => fit(300), 60);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [padKey]);

  // Re-run layout when the set of boxes or lines changes (not on every count update).
  const lines = useMemo(() => mergeEdges(map.edges), [map.edges]);
  const shapeKey = useMemo(
    () => (vertical ? "v" : "h") + map.nodes.map((n) => n.id).sort().join("|") + "#" + lines.map((e) => `${e.src}>${e.dst}`).sort().join("|"),
    [map.nodes, lines],
  );
  useEffect(() => {
    let cancelled = false;
    layout(map.nodes, lines, vertical).then((r) => { if (!cancelled) { setPositions(r.pos); setLabels(r.labels); setRoutes(r.routes); } });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shapeKey]);

  useEffect(() => {
    if (relayoutSignal === 0) return;
    setPins({});
    savePins({});
    userMoved.current = false;
    window.setTimeout(() => fit(400), 80);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [relayoutSignal]);

  // Keep everything in view as the map grows or the window changes size, until you take over.
  useEffect(() => {
    if (!userMoved.current && Object.keys(positions).length) window.setTimeout(() => fit(), 60);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [positions]);
  useEffect(() => {
    if (!wrap.current) return;
    const ro = new ResizeObserver(() => { if (!userMoved.current) fit(0); });
    ro.observe(wrap.current);
    return () => ro.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // "hot" fades 1.6 s after the last message: re-render once when the newest glow is due to fade, instead of
  // ticking all day (an idle map then costs nothing, which matters on the phone)
  useEffect(() => {
    const last = Math.max(0, ...Object.values(active));
    const left = last + 1600 - Date.now();
    if (left <= 0) return;
    const t = window.setTimeout(() => setNow(Date.now()), left + 30);
    return () => window.clearTimeout(t);
  }, [active, now]);

  const oldest = useMemo(() => Math.min(...map.nodes.map((n) => n.first_seen)), [map.nodes]);

  useEffect(() => {
    if (!Object.keys(positions).length) return;
    setNodes((prev) => {
      const dragging = new Set(prev.filter((x) => x.dragging).map((x) => x.id));
      const bg: Node[] = labels.map((l) => ({
        id: `h:${l.id}`, type: "heading", position: { x: l.x, y: l.y }, data: { label: l },
        draggable: false, selectable: false, focusable: false, className: "heading-node",
      }));
      return [...bg, ...map.nodes.map((n): Node => {
        const pos = pins[n.id] ?? positions[n.id] ?? { x: 0, y: 0 };
        const hot = now - (active[n.id] ?? 0) < 1600;
        const fresh = n.first_seen - oldest > 3600 && Date.now() / 1000 - n.first_seen < 86400;  // added today, not at setup
        const old = prev.find((x) => x.id === n.id);
        return {
          id: n.id,
          type: "box",
          position: dragging.has(n.id) && old ? old.position : pos,
          data: { n, hot, fresh, selected: selection?.type === "node" && selection.id === n.id, info: boxInfo(n, { queue: queue ?? null, byWorker: byWorker ?? {} }) },
          width: NODE_W,
          height: NODE_H,
          draggable: true,
          selectable: false,
        };
      })];
    });
  }, [map.nodes, positions, labels, pins, active, now, selection, oldest, setNodes, queue, byWorker]);

  // Boxes in the same column (the model tiers) connect top to bottom instead of side to side.
  const stacked = (a: string, b: string) => {
    const pa = pins[a] ?? positions[a], pb = pins[b] ?? positions[b];
    if (vertical && pa && pb) return pa.y <= pb.y ? { sourceHandle: "b", targetHandle: "t" } : { sourceHandle: "t-out", targetHandle: "b-in" };
    if (!pa || !pb || Math.abs(pa.x - pb.x) > NODE_W / 2) return {};
    return pa.y < pb.y ? { sourceHandle: "b", targetHandle: "t" } : { sourceHandle: "t-out", targetHandle: "b-in" };
  };

  useEffect(() => {
    const t = Date.now();
    setEdges(
      lines.map((e) => {
        const live = pulses
          .filter((p) => t - p.at < 1400 && ((p.src === e.src && p.dst === e.dst) || (p.src === e.dst && p.dst === e.src)))
          .map((p) => ({ ...p, back: p.src === e.dst }));
        const tones = live.map((p) => p.tone);
        return {
          id: `${e.src}>${e.dst}`,
          source: e.src,
          target: e.dst,
          ...stacked(e.src, e.dst),
          type: "line",
          selectable: false,
          data: {
            route: routes[`${e.src}>${e.dst}`],
            count: e.count,
            rate: rates[[e.src, e.dst].sort().join("|")] ?? 0,
            ltype: /fail|dead|error|offline|opened/.test(e.last_kind) ? "bad" : /^(model|check)\./.test(e.last_kind) || map.nodes.find((x) => x.id === e.dst || x.id === e.src)?.kind === "model" ? "mdl"
              : /^(job|step|approval|outbox|notify)\./.test(e.last_kind) ? "ok" : "ctl",
            pulses: live,
            flow,
            hot: live.length > 0,
            tone: tones.includes("bad") ? "bad" : tones.includes("warn") ? "warn" : "flow",
            selected: selection?.type === "edge" && selection.src === e.src && selection.dst === e.dst,
          },
        };
      }),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lines, pulses, flow, selection, now, setEdges, positions, pins, routes, rates]);

  return (
    <div ref={wrap} style={{ width: "100%", height: "100%", position: "relative" }}>
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      edgeTypes={edgeTypes}
      onNodesChange={onNodesChange}
      onNodeDragStop={(_, node) => {
        const next = { ...pins, [node.id]: node.position };
        setPins(next);
        savePins(next);
      }}
      onNodeClick={(_, node) => { if (node.type === "box") onSelect({ type: "node", id: node.id }); }}
      onEdgeClick={(_, edge) => onSelect({ type: "edge", src: edge.source, dst: edge.target })}
      onPaneClick={() => onSelect(null)}
      onMoveStart={(e) => { if (e) userMoved.current = true; }}
      // on a touch screen one finger scrolls the page and a tap only selects; pinch still zooms the map
      panOnDrag={!coarse}
      nodesDraggable={!coarse}
      preventScrolling={!coarse}
      minZoom={0.3}
      maxZoom={2}
      proOptions={{ hideAttribution: true }}
      nodesConnectable={false}
      elementsSelectable={false}
      colorMode="dark"
    >
      <Background variant={BackgroundVariant.Dots} gap={24} size={1.1} color="#232329" />
    </ReactFlow>
    <ZoomPad onFit={() => { userMoved.current = false; fit(300); }} />
    </div>
  );
}

// The map shows the main parts only: all plugins are one "Plugins" box (their own page has the details). Lines
// and message dots to or from any plugin go to that box.
export const PLUGINS = "plugins";
export function mainParts(map: ArgusMap, pulses: Pulse[], active: Record<string, number>) {
  const plug = new Set(map.nodes.filter((n) => n.kind === "plugin").map((n) => n.id));
  if (!plug.size) return { map, pulses, active };
  const to = (id: string) => (plug.has(id) ? PLUGINS : id);
  const ps = map.nodes.filter((n) => plug.has(n.id));
  const sum = (k: "active" | "queued" | "waiting") => ps.reduce((a, n) => a + (n.jobs?.[k] ?? 0), 0);
  const group: MapNode = {
    id: PLUGINS, kind: "plugin", label: "Plugins", group: null, first_seen: Math.min(...ps.map((n) => n.first_seen)),
    meta: { count: ps.length }, jobs: { active: sum("active"), queued: sum("queued"), waiting: sum("waiting") },
  };
  const nodes = [...map.nodes.filter((n) => !plug.has(n.id)), group];
  const edges = map.edges.map((e) => ({ ...e, src: to(e.src), dst: to(e.dst) })).filter((e) => e.src !== e.dst);
  const act: Record<string, number> = {};
  for (const [k, v] of Object.entries(active)) act[to(k)] = Math.max(act[to(k)] ?? 0, v);
  const pl = pulses.map((x) => ({ ...x, src: to(x.src), dst: to(x.dst) })).filter((x) => x.src !== x.dst);
  return { map: { ...map, nodes, edges }, pulses: pl, active: act };
}

export function MapView(p: Props & { onPlugins?: () => void }) {
  const m = useMemo(() => mainParts(p.map, p.pulses, p.active), [p.map, p.pulses, p.active]);
  const rates = useMemo(() => {  // messages per line in the last minute, counted from the live events (plugins fold into one box)
    const plug = new Set(p.map.nodes.filter((n) => n.kind === "plugin").map((n) => n.id));
    const to = (id: string) => (plug.has(id) ? PLUGINS : id);
    const since = Date.now() / 1000 - 60;
    const out: Record<string, number> = {};
    for (const e of p.events ?? []) {
      if (!e.from || !e.to || e.at < since) continue;
      const a = to(e.from), b = to(e.to);
      if (a === b) continue;
      const k = [a, b].sort().join("|");
      out[k] = (out[k] ?? 0) + 1;
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.events, p.map.nodes]);
  const sel = p.selection?.type === "node" && p.selection.id === PLUGINS ? null : p.selection;
  const onSelect = (s: Selection) => (s?.type === "node" && s.id === PLUGINS && p.onPlugins ? p.onPlugins() : p.onSelect(s));
  return (
    <ReactFlowProvider>
      <Inner {...p} {...m} selection={sel} onSelect={onSelect} rates={rates} />
    </ReactFlowProvider>
  );
}
