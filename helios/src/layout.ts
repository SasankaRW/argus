// Map layout: ELK arranges boxes left to right by who talks to whom. Boxes you drag stay where you put them.

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

export function loadPins(): Record<string, Pos> {
  try { return JSON.parse(localStorage.getItem(PINS_KEY) || "{}"); } catch { return {}; }
}
export function savePins(p: Record<string, Pos>) {
  try { localStorage.setItem(PINS_KEY, JSON.stringify(p)); } catch { /* ignore */ }
}

// Core first, then apps and workers, then plugins, then models and services.
const RANK: Record<string, number> = { core: 0, app: 1, worker: 1, plugin: 2, service: 3, model: 3 };

export async function layout(nodes: MapNode[], edges: MapEdge[]): Promise<Record<string, Pos>> {
  const ids = new Set(nodes.map((n) => n.id));
  const kind = new Map(nodes.map((n) => [n.id, n.kind]));
  const graph = {
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "RIGHT",
      "elk.layered.spacing.nodeNodeBetweenLayers": "80",
      "elk.spacing.nodeNode": "34",
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
        // core on the left, the model tiers stacked in one column on the right (T1 above T2 above T3)
        layoutOptions: (n.kind === "core" ? { "elk.layered.layering.layerConstraint": "FIRST" }
          : n.kind === "model" ? { "elk.layered.layering.layerConstraint": "LAST" } : {}) as Record<string, string>,
      })),
    // lines between two models (escalations) are drawn top to bottom, so they don't shape the columns
    edges: edges
      .filter((e) => ids.has(e.src) && ids.has(e.dst) && !(kind.get(e.src) === "model" && kind.get(e.dst) === "model"))
      .map((e, i) => ({ id: `e${i}`, sources: [e.src], targets: [e.dst] })),
  };
  const out = await (await getElk()).layout(graph);
  const pos: Record<string, Pos> = {};
  for (const c of out.children ?? []) pos[c.id] = { x: c.x ?? 0, y: c.y ?? 0 };
  return pos;
}
