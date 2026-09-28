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
type LineData = { count: number; pulses: Pulse[]; flow: boolean; selected: boolean; hot: boolean; bad: boolean };

const KIND_LABEL: Record<string, string> = { core: "core", worker: "worker", plugin: "plugin", model: "model", app: "app", service: "service" };

function sub(n: MapNode): string {
  if (n.kind === "core") return `core${n.meta?.version ? ` · v${n.meta.version}` : ""}`;
  if (n.kind === "worker") return `${n.state ?? "unknown"}${n.group ? ` · ${n.group}` : ""}`;
  if (n.kind === "plugin") {
    const j = n.jobs ?? { active: 0, queued: 0 };
    if (!j.active && !j.queued) return "idle";
    return [j.active ? `${j.active} running` : "", j.queued ? `${j.queued} queued` : ""].filter(Boolean).join(" · ");
  }
  return n.implicit ? "seen in events" : KIND_LABEL[n.kind];
}

function dotColor(n: MapNode, hot: boolean): string {
  if (n.kind === "worker") return n.state === "online" ? "var(--ok)" : "var(--bad)";
  if (hot) return "var(--flow)";
  if (n.kind === "core") return "var(--amber)";
  if (n.kind === "model") return "var(--violet)";
  if (n.kind === "plugin") return (n.jobs?.active ?? 0) > 0 ? "var(--flow)" : "var(--ok)";
  return "var(--tx3)";
}

const Box = memo(function Box({ data }: NodeProps<Node<BoxData>>) {
  const { n, hot, fresh, selected } = data;
  return (
    <div className={`box${hot ? " on" : ""}${selected ? " sel" : ""}${n.implicit ? " ghost" : ""}${fresh ? " born" : ""}`}>
      <Handle type="target" position={Position.Left} className="port" isConnectable={false} />
      <div className="nn">
        <span className="dot" style={{ background: dotColor(n, hot) }} />
        <span className="name">{n.label}</span>
        {fresh && <span className="new">new</span>}
      </div>
      <div className="ns">{sub(n)}</div>
      <Handle type="source" position={Position.Right} className="port" isConnectable={false} />
    </div>
  );
});

function Dot({ path, bad }: { path: string; bad: boolean }) {
  const ref = useRef<SVGAnimateMotionElement>(null);
  useLayoutEffect(() => { ref.current?.beginElement(); }, []);
  return (
    <circle r="3.2" className={`packet${bad ? " bad" : ""}`}>
      <animateMotion ref={ref} dur="1.1s" begin="indefinite" fill="freeze" path={path} keyPoints="0;1" keyTimes="0;1" calcMode="linear" />
    </circle>
  );
}

const Line = memo(function Line(p: EdgeProps<Edge<LineData>>) {
  const [path] = getBezierPath({ sourceX: p.sourceX, sourceY: p.sourceY, targetX: p.targetX, targetY: p.targetY, sourcePosition: p.sourcePosition, targetPosition: p.targetPosition });
  const d = p.data!;
  const width = Math.min(1.2 + Math.log2(1 + d.count) * 0.35, 3.6);
  const cls = `line${d.hot ? (d.bad ? " bad" : " on") : ""}${d.selected ? " sel" : ""}`;
  return (
    <>
      <BaseEdge id={p.id} path={path} className={cls} style={{ strokeWidth: d.hot || d.selected ? width + 0.4 : width }} interactionWidth={16} />
      {d.flow && d.pulses.map((x) => <Dot key={x.id} path={path} bad={x.bad} />)}
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
  const fit = (duration = 300) => rf.fitView({ padding: 0.3, maxZoom: 1.1, duration });

  // Re-run layout when the set of boxes or lines changes (not on every count update).
  const shapeKey = useMemo(
    () => map.nodes.map((n) => n.id).sort().join("|") + "#" + map.edges.map((e) => `${e.src}>${e.dst}`).sort().join("|"),
    [map],
  );
  useEffect(() => {
    let cancelled = false;
    layout(map.nodes, map.edges).then((p) => { if (!cancelled) setPositions(p); });
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

  useEffect(() => {
    const t = Date.now();
    setEdges(
      map.edges.map((e) => {
        const mine = pulses.filter((p) => p.src === e.src && p.dst === e.dst);
        const live = mine.filter((p) => t - p.at < 1400);
        return {
          id: `${e.src}>${e.dst}`,
          source: e.src,
          target: e.dst,
          type: "line",
          selectable: false,
          data: {
            count: e.count,
            pulses: live,
            flow,
            hot: live.length > 0,
            bad: live.some((p) => p.bad),
            selected: selection?.type === "edge" && selection.src === e.src && selection.dst === e.dst,
          },
        };
      }),
    );
  }, [map.edges, pulses, flow, selection, now, setEdges]);

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
