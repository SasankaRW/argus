// What is inside the Plugins box: three columns (what feeds the plugins that have jobs, those plugins, what they use or
// produce) with the same line health as the main map, and the idle plugins as a list. Pure; the lines come from the
// registry's edges between plugin ids and other components, so nothing is made up.

import type { ArgusEvent, ArgusMap, MapNode } from "./api";
import type { IconName } from "./icons";
import { buildLevels, Card, Line, lineKind, RECENT, Tone } from "./mapGraph";

export type PluginView = { cards: Card[]; lines: Line[]; idle: string[]; running: number; stopped: number };

// The three columns of the plugin view (Card.level)
export const PL = { inputs: 0, plugins: 1, outputs: 2 } as const;

// Boxes that are the way in (they start or feed work). Anything else is an input only when it sends to a plugin and
// never receives from it.
const INPUTS = new Set(["scheduler", "helios", "ari"]);
const HIDDEN = new Set(["argus", "outbox", "watchdog"]);
const NAMES: Record<string, string> = { web: "Web search", notes: "Notes", "vault-sync": "Vault sync", phone: "Phone", approvals: "Approvals", backup: "Backups" };
const ICONS: Record<string, IconName> = {
  scheduler: "schedule", helios: "dashboard", ari: "mic", "vault-sync": "refresh", phone: "smartphone", approvals: "pending_actions",
  web: "search", notes: "receipt_long", backup: "dns", lifehub: "home", tracker: "done_all",
};

const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1).replace(/[-_]/g, " ");
const pair = (a: string, b: string) => (a < b ? `${a}|${b}` : `${b}|${a}`);
const hhmm = (t: number) => new Date(t * 1000).toTimeString().slice(0, 5);

// A plugin card: running (green, with the workflow), waiting for a worker (amber) or stopped from Helios (red).
export function pluginCard(n: MapNode, workflow?: string): Card {
  const j = n.jobs ?? { active: 0, queued: 0, waiting: 0 };
  const label = n.label || n.id;
  const base = { id: n.id, select: n.id, label, icon: "extension" as IconName, level: PL.plugins };
  if (n.paused) {
    const q = j.queued + (j.waiting ?? 0);
    return { ...base, skin: "stop", tone: "bad", paused: true, status: q ? `stopped · ${q} waiting` : n.since ? `stopped since ${hhmm(n.since)}` : "stopped" };
  }
  if (j.active > 0) return { ...base, skin: "run", tone: "ok", status: workflow ? `running · ${workflow}` : j.active > 1 ? `running · ${j.active} jobs` : "running" };
  return { ...base, skin: "wait", tone: "warn", status: j.queued > 0 ? "waiting for a worker" : "waiting" };
}

export function buildPluginView(map: ArgusMap, ctx: { now: number; events: ArgusEvent[]; workflows?: Record<string, string> }): PluginView {
  const nodes = new Map(map.nodes.map((n) => [n.id, n]));
  const plugins = map.nodes.filter((n) => n.kind === "plugin");
  const busy = (n: MapNode) => (n.jobs?.active ?? 0) + (n.jobs?.queued ?? 0) + (n.jobs?.waiting ?? 0) > 0;
  const shown = (n: MapNode) => !!n.paused || busy(n);  // a stopped plugin is shown even with nothing queued: Start is in its panel
  const live = plugins.filter(shown);
  const idle = plugins.filter((n) => !shown(n)).map((n) => n.label || n.id).sort((a, b) => a.localeCompare(b));
  const liveIds = new Set(live.map((n) => n.id));
  // the main map's boxes give the same state (and wording) to a phone, a model or Approvals here as there
  const main = new Map(buildLevels(map, { now: ctx.now, events: ctx.events }).cards.map((c) => [c.id, c]));
  const mainOf = (id: string) => (id === "phone-app" ? "phone" : id);

  const traffic = new Map<string, number>();
  for (const e of ctx.events) {
    if (!e.from || !e.to || ctx.now - e.at > RECENT || e.at > ctx.now + 5) continue;
    traffic.set(pair(e.from, e.to), (traffic.get(pair(e.from, e.to)) ?? 0) + 1);
  }

  const cards: Card[] = live.map((p) => pluginCard(p, ctx.workflows?.[p.id]));
  const lines: Line[] = [];

  // the other end of every line to or from a plugin shown here, sorted into the way in and the way out
  const ends = new Map<string, { ins: number; outs: number; plugins: Set<string> }>();
  for (const e of map.edges) {
    const pIn = liveIds.has(e.dst), pOut = liveIds.has(e.src);
    if (!pIn && !pOut) continue;
    const other = pIn ? e.src : e.dst;
    const n = nodes.get(other);
    if (!n || n.kind === "plugin" || n.kind === "worker" || n.kind === "core" || HIDDEN.has(other)) continue;
    const id = mainOf(other);
    const x = ends.get(id) ?? { ins: 0, outs: 0, plugins: new Set<string>() };
    if (pIn) x.ins += e.count; else x.outs += e.count;
    x.plugins.add(pIn ? e.dst : e.src);
    ends.set(id, x);
  }
  for (const [id, x] of ends) {
    const n = nodes.get(id) ?? nodes.get("phone-app");
    const isModel = n?.kind === "model";
    const input = !isModel && (INPUTS.has(id) || n?.kind === "app" || (x.ins > 0 && x.outs === 0 && id !== "phone" && id !== "approvals"));
    const m = main.get(id);
    const tone: Tone = m?.tone ?? "ok";
    cards.push({
      id, select: m?.select ?? id, label: m?.label ?? NAMES[id] ?? cap(n?.label && n.label !== id ? n.label : id), icon: m?.icon ?? ICONS[id] ?? "hub",
      status: m?.status ?? (input ? "feeds plugins" : "used by plugins"), level: input ? PL.inputs : PL.outputs,
      skin: m?.skin === "model" ? "model" : "plain", tone, problem: m?.problem,
    });
    for (const pid of x.plugins) {
      const recent = (traffic.get(pair(pid, id)) ?? 0) + (id === "phone" ? (traffic.get(pair(pid, "phone-app")) ?? 0) : 0);
      lines.push({ a: input ? id : pid, b: input ? pid : id, kind: lineKind({ tone: input ? "ok" : tone, recent }) });
    }
  }
  // a model that escalates to a bigger one: a purple dotted line between the two when both are shown
  const here = new Set(cards.map((c) => c.id));
  for (const e of map.edges) {
    if (nodes.get(e.src)?.kind === "model" && nodes.get(e.dst)?.kind === "model" && here.has(e.src) && here.has(e.dst)) lines.push({ a: e.src, b: e.dst, kind: "esc" });
  }
  // keep the escalation order of models (local tiers first, Claude last)
  const order = new Map([...main.values()].map((c, i) => [c.id, i]));
  const kind = (c: Card) => (c.skin === "model" ? 1 : 0);
  cards.sort((a, b) => (a.level - b.level) || (kind(a) - kind(b)) || ((order.get(a.id) ?? 99) - (order.get(b.id) ?? 99)));
  return {
    cards, lines, idle,
    running: live.filter((p) => !p.paused && (p.jobs?.active ?? 0) > 0).length,
    stopped: live.filter((p) => p.paused).length,
  };
}
