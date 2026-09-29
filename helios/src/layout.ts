// Map layout: ELK arranges boxes left to right by who talks to whom. Devices (the PC's worker, the phone,
// apps) always get the first column of their own, the model tiers the last. Boxes you drag stay where you put them.

import type { ELK as ElkApi } from "elkjs/lib/elk-api";
import type { MapEdge, MapNode } from "./api";

export const NODE_W = 168;
export const NODE_H = 50;

// ELK is large, so it loads after the first paint, in its own file.
let elkPromise: Promise<ElkApi> | null = null;
function getElk(): Promise<ElkApi> {
  elkPromise ??= import("elkjs/lib/elk.bundled.js").then((m) => new m.default());
  return elkPromise;
}
const PINS_KEY = "helios.pins";

export type Pos = { x: number; y: number };
// ELK's route for a line: start, control points, end. Drawn as a curve that goes around boxes.
export type Route = Pos[];
// A plain heading above a column (no box around it).
export type ColumnLabel = { id: string; text: string; x: number; y: number };

export function loadPins(): Record<string, Pos> {
  try { return JSON.parse(localStorage.getItem(PINS_KEY) || "{}"); } catch { return {}; }
}
export function savePins(p: Record<string, Pos>) {
  try { localStorage.setItem(PINS_KEY, JSON.stringify(p)); } catch { /* ignore */ }
}

export function isDevice(n: MapNode): boolean {
  return n.kind === "worker" || n.kind === "app" || n.id === "phone";
}

// Column order: devices, then everything ELK places by traffic, then the models.
function partition(n: MapNode): number {
  if (isDevice(n)) return 0;
  if (n.kind === "model") return 2;
  return 1;
}

const RANK: Record<string, number> = { core: 0, app: 1, worker: 1, plugin: 2, service: 3, model: 3 };

export async function layout(nodes: MapNode[], edges: MapEdge[]):
  Promise<{ pos: Record<string, Pos>; labels: ColumnLabel[]; routes: Record<string, Route> }> {
  const ids = new Set(nodes.map((n) => n.id));
  const kind = new Map(nodes.map((n) => [n.id, n.kind]));
  const graph = {
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "RIGHT",
      "elk.partitioning.activate": "true",
      "elk.edgeRouting": "SPLINES",
      "elk.layered.edgeRouting.splines.mode": "CONSERVATIVE",
      "elk.layered.spacing.nodeNodeBetweenLayers": "56",
      "elk.layered.spacing.edgeNodeBetweenLayers": "14",
      "elk.layered.spacing.edgeEdgeBetweenLayers": "8",
      "elk.spacing.nodeNode": "20",
      "elk.spacing.edgeNode": "12",
      "elk.spacing.edgeEdge": "8",
      "elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX",
      "elk.layered.considerModelOrder.strategy": "PREFER_EDGES",
      "elk.separateConnectedComponents": "false",
    },
    children: [...nodes]
      .sort((a, b) => (RANK[a.kind] ?? 3) - (RANK[b.kind] ?? 3) || a.first_seen - b.first_seen || a.id.localeCompare(b.id))
      .map((n) => ({
        id: n.id,
        width: NODE_W,
        height: NODE_H,
        layoutOptions: { "elk.partitioning.partition": String(partition(n)), "elk.portConstraints": "FIXED_POS" } as Record<string, string>,
        // one port in the middle of each side, where the map draws its handles
        ports: [
          { id: `${n.id}:w`, x: 0, y: NODE_H / 2, width: 0, height: 0, layoutOptions: { "elk.port.side": "WEST" } },
          { id: `${n.id}:e`, x: NODE_W, y: NODE_H / 2, width: 0, height: 0, layoutOptions: { "elk.port.side": "EAST" } },
        ],
      })),
    // lines between two models (escalations) are drawn top to bottom, so they don't shape the columns
    edges: edges
      .filter((e) => ids.has(e.src) && ids.has(e.dst) && !(kind.get(e.src) === "model" && kind.get(e.dst) === "model"))
      .map((e) => ({ id: `${e.src}>${e.dst}`, sources: [`${e.src}:e`], targets: [`${e.dst}:w`] })),
  };
  const out = await (await getElk()).layout(graph);
  const pos: Record<string, Pos> = {};
  for (const c of out.children ?? []) pos[c.id] = { x: c.x ?? 0, y: c.y ?? 0 };
  const routes: Record<string, Route> = {};
  for (const e of (out.edges ?? []) as { id: string; sections?: { startPoint: Pos; endPoint: Pos; bendPoints?: Pos[] }[] }[]) {
    const sec = e.sections?.[0];
    if (sec) routes[e.id] = [sec.startPoint, ...(sec.bendPoints ?? []), sec.endPoint];
  }
  const devs = nodes.filter(isDevice).map((n) => pos[n.id]).filter(Boolean);
  const labels: ColumnLabel[] = devs.length
    ? [{ id: "devices", text: "Devices", x: Math.min(...devs.map((p) => p.x)), y: Math.min(...devs.map((p) => p.y)) - 26 }]
    : [];
  return { pos, labels, routes };
}

// ELK's spline route as an SVG path: its bend points are the control points of cubic Bezier pieces.
// If the count does not fit that shape, fall back to a smooth curve through the points.
export function splinePath(pts: Route): string {
  if (pts.length < 2) return "";
  const [s, ...rest] = pts;
  let d = `M ${s.x} ${s.y}`;
  if (rest.length % 3 === 0) {
    for (let i = 0; i < rest.length; i += 3) {
      const [a, b, c] = rest.slice(i, i + 3);
      d += ` C ${a.x} ${a.y} ${b.x} ${b.y} ${c.x} ${c.y}`;
    }
    return d;
  }
  // Catmull-Rom through the points, as cubic Beziers
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[Math.max(0, i - 1)], p1 = pts[i], p2 = pts[i + 1], p3 = pts[Math.min(pts.length - 1, i + 2)];
    const c1 = { x: p1.x + (p2.x - p0.x) / 6, y: p1.y + (p2.y - p0.y) / 6 };
    const c2 = { x: p2.x - (p3.x - p1.x) / 6, y: p2.y - (p3.y - p1.y) / 6 };
    d += ` C ${c1.x} ${c1.y} ${c2.x} ${c2.y} ${p2.x} ${p2.y}`;
  }
  return d;
}
