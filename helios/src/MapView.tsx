// The live map: boxes from the registry, lines from traffic, a dot along a line for every message.

import {
  Background, BaseEdge, Controls, EdgeProps, getBezierPath, Handle, Node, NodeProps, Position, ReactFlow,
  useEdgesState, useNodesState, useReactFlow, ReactFlowProvider, Edge,
} from "@xyflow/react";
import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ArgusMap, MapNode } from "./api";
import { layout, loadPins, NODE_H, NODE_W, Pos, savePins } from "./layout";
import type { Pulse } from "./live";

export type Selection = { type: "node"; id: string } | { type: "edge"; src: string; dst: string } | { type: "job"; id: string } | null;

type BoxData = { n: MapNode; hot: boolean; fresh: boolean; selected: boolean };
type LineData = { count: number; pulses: (Pulse & { back: boolean })[]; flow: boolean; selected: boolean; hot: boolean; tone: string };

const KIND_LABEL: Record<string, string> = { core: "core", worker: "worker", plugin: "plugin", model: "model", app: "app", service: "service" };

function sub(n: MapNode): string {
  if (n.kind === "core") return `core${n.meta?.version ? ` · v${n.meta.version}` : ""}`;
  if (n.kind === "worker") return `${n.state ?? "unknown"}${n.group ? ` · ${n.group}` : ""}`;
  if (n.kind === "plugin") {
    const j = n.jobs ?? { active: 0, queued: 0 };
    if (!j.active && !j.queued) return "idle";
    return [j.active ? `${j.active} running` : "", j.queued ? `${j.queued} queued` : ""].filter(Boolean).join(" · ");
  }
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
  if (n.kind === "plugin") return (n.jobs?.active ?? 0) > 0 ? "var(--flow)" : "var(--ok)";
  return "var(--tx3)";
}

const Box = memo(function Box({ data }: NodeProps<Node<BoxData>>) {
  const { n, hot, fresh, selected } = data;
  return (
    <div className={`box${hot ? " on" : ""}${selected ? " sel" : ""}${n.implicit ? " ghost" : ""}${fresh ? " born" : ""}`}>
      <Handle type="target" position={Position.Left} className="port" isConnectable={false} />
      <Handle id="t" type="target" position={Position.Top} className="port" isConnectable={false} />
      <Handle id="b-in" type="target" position={Position.Bottom} className="port" isConnectable={false} />
      <div className="nn">
        <span className="dot" style={{ background: dotColor(n, hot) }} />
        <span className="name">{n.label}</span>
        {fresh && <span className="new">new</span>}
      </div>
      <div className="ns">{sub(n)}</div>
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

const Line = memo(function Line(p: EdgeProps<Edge<LineData>>) {
  const [path] = getBezierPath({ sourceX: p.sourceX, sourceY: p.sourceY, targetX: p.targetX, targetY: p.targetY, sourcePosition: p.sourcePosition, targetPosition: p.targetPosition });
  const d = p.data!;
  const width = Math.min(1.2 + Math.log2(1 + d.count) * 0.35, 3.6);
  const cls = `line${d.hot ? (d.tone === "bad" ? " bad" : d.tone === "warn" ? " warn" : " on") : ""}${d.selected ? " sel" : ""}`;
  return (
    <>
      <BaseEdge id={p.id} path={path} className={cls} style={{ strokeWidth: d.hot || d.selected ? width + 0.4 : width }} interactionWidth={16} />
      {d.flow && d.pulses.map((x) => <Dot key={x.id} path={path} tone={x.tone} back={x.back} />)}
    </>
  );
});

const nodeTypes = { box: Box };
const edgeTypes = { line: Line };

type Props = {
  map: ArgusMap;
  pulses: Pulse[];
  active: Record<string, number>;
  selection: Selection;
  onSelect: (s: Selection) => void;
  flow: boolean;
  relayoutSignal: number;
};

function Inner({ map, pulses, active, selection, onSelect, flow, relayoutSignal }: Props) {
  const [positions, setPositions] = useState<Record<string, Pos>>({});
  const [pins, setPins] = useState<Record<string, Pos>>(() => loadPins());
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<BoxData>>([]);
  const [edges, setEdges] = useEdgesState<Edge<LineData>>([]);
  const [now, setNow] = useState(Date.now());
  const rf = useReactFlow();
  const userMoved = useRef(false); // once you pan or zoom, the map stops re-fitting itself
  const wrap = useRef<HTMLDivElement>(null);
  const fit = (duration = 300) => rf.fitView({ padding: 0.12, maxZoom: 1.1, duration });

  // Re-run layout when the set of boxes or lines changes (not on every count update).
  const lines = useMemo(() => mergeEdges(map.edges), [map.edges]);
  const shapeKey = useMemo(
    () => map.nodes.map((n) => n.id).sort().join("|") + "#" + lines.map((e) => `${e.src}>${e.dst}`).sort().join("|"),
    [map.nodes, lines],
  );
  useEffect(() => {
    let cancelled = false;
    layout(map.nodes, lines).then((p) => { if (!cancelled) setPositions(p); });
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

  // "hot" fades 1.6 s after the last message
  useEffect(() => {
    const iv = window.setInterval(() => setNow(Date.now()), 400);
    return () => window.clearInterval(iv);
  }, []);

  const oldest = useMemo(() => Math.min(...map.nodes.map((n) => n.first_seen)), [map.nodes]);

  useEffect(() => {
    if (!Object.keys(positions).length) return;
    setNodes((prev) => {
      const dragging = new Set(prev.filter((x) => x.dragging).map((x) => x.id));
      return map.nodes.map((n) => {
        const pos = pins[n.id] ?? positions[n.id] ?? { x: 0, y: 0 };
        const hot = now - (active[n.id] ?? 0) < 1600;
        const fresh = n.first_seen - oldest > 3600 && Date.now() / 1000 - n.first_seen < 7 * 86400;
        const old = prev.find((x) => x.id === n.id);
        return {
          id: n.id,
          type: "box",
          position: dragging.has(n.id) && old ? old.position : pos,
          data: { n, hot, fresh, selected: selection?.type === "node" && selection.id === n.id },
          width: NODE_W,
          height: NODE_H,
          draggable: true,
          selectable: false,
        };
      });
    });
  }, [map.nodes, positions, pins, active, now, selection, oldest, setNodes]);

  // Boxes in the same column (the model tiers) connect top to bottom instead of side to side.
  const stacked = (a: string, b: string) => {
    const pa = pins[a] ?? positions[a], pb = pins[b] ?? positions[b];
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
            count: e.count,
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
  }, [lines, pulses, flow, selection, now, setEdges, positions, pins]);

  return (
    <div ref={wrap} style={{ width: "100%", height: "100%" }}>
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
      onNodeClick={(_, node) => onSelect({ type: "node", id: node.id })}
      onEdgeClick={(_, edge) => onSelect({ type: "edge", src: edge.source, dst: edge.target })}
      onPaneClick={() => onSelect(null)}
      onMoveStart={(e) => { if (e) userMoved.current = true; }}
      minZoom={0.3}
      maxZoom={2}
      proOptions={{ hideAttribution: true }}
      nodesConnectable={false}
      elementsSelectable={false}
      colorMode="dark"
    >
      <Background gap={22} size={1} color="#1a1f2a" />
      <Controls showInteractive={false} position="bottom-right" onFitView={() => { userMoved.current = false; }} />
    </ReactFlow>
    </div>
  );
}

export function MapView(p: Props) {
  return (
    <ReactFlowProvider>
      <Inner {...p} />
    </ReactFlowProvider>
  );
}
