// Map layout: one column (lane) per kind of part, left to right, so devices, Argus, plugins, services and models
// are clearly apart. Boxes you drag stay where you put them; Auto layout puts them back.

import type { MapEdge, MapNode } from "./api";

export const NODE_W = 168;
export const NODE_H = 50;
const COL_GAP = 44; // between lanes
const ROW_GAP = 18; // between boxes in a lane
export const LANE_PAD = 10;
export const LANE_HEAD = 30; // room for the lane title above the first box

const PINS_KEY = "helios.pins";

export type Pos = { x: number; y: number };
export type Lane = { id: string; label: string; x: number; y: number; w: number; h: number; count: number };

export function loadPins(): Record<string, Pos> {
  try { return JSON.parse(localStorage.getItem(PINS_KEY) || "{}"); } catch { return {}; }
}
export function savePins(p: Record<string, Pos>) {
  try { localStorage.setItem(PINS_KEY, JSON.stringify(p)); } catch { /* ignore */ }
}

export const LANES: { id: string; label: string }[] = [
  { id: "devices", label: "Devices & apps" },
  { id: "argus", label: "Argus" },
  { id: "plugins", label: "Plugins" },
  { id: "services", label: "Services" },
  { id: "models", label: "Models" },
];

export function laneOf(n: MapNode): string {
  if (n.kind === "worker" || n.kind === "app" || n.id === "phone") return "devices";
  if (n.kind === "core") return "argus";
  if (n.kind === "plugin") return "plugins";
  if (n.kind === "model") return "models";
  return "services";
}

// Within a lane: models in tier order (T1 above T2), the rest oldest first.
function order(a: MapNode, b: MapNode): number {
  if (a.kind === "model" && b.kind === "model") return a.id.localeCompare(b.id, undefined, { numeric: true });
  if (a.implicit !== b.implicit) return a.implicit ? 1 : -1;
  return a.first_seen - b.first_seen || a.id.localeCompare(b.id);
}

export async function layout(nodes: MapNode[], _edges: MapEdge[]): Promise<{ pos: Record<string, Pos>; lanes: Lane[] }> {
  const groups = LANES.map((l) => ({ ...l, items: nodes.filter((n) => laneOf(n) === l.id).sort(order) }))
    .filter((g) => g.items.length);
  const step = NODE_H + ROW_GAP;
  const tallest = Math.max(1, ...groups.map((g) => g.items.length));
  const pos: Record<string, Pos> = {};
  const lanes: Lane[] = [];
  groups.forEach((g, col) => {
    const x = col * (NODE_W + COL_GAP);
    const top = ((tallest - g.items.length) * step) / 2; // centre short lanes against the tallest
    g.items.forEach((n, i) => { pos[n.id] = { x, y: top + i * step }; });
    lanes.push({ id: g.id, label: g.label, count: g.items.length, x: x - LANE_PAD, y: -LANE_HEAD - LANE_PAD,
      w: NODE_W + LANE_PAD * 2, h: tallest * step - ROW_GAP + LANE_HEAD + LANE_PAD * 2 });
  });
  return { pos, lanes };
}
