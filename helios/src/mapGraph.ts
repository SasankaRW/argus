// The live map as levels: which boxes are shown (a short whitelist), what state each one is in, how healthy each line
// is, what the top status line says, and where everything goes. Pure (no React, no clock of its own: `now` is passed
// in, in seconds like the events' `at`), so the unit tests can run it. MapView.tsx draws the result.

import type { ArgusEvent, ArgusMap, MapNode } from "./api";
import type { IconName } from "./icons";

// ---------------------------------------------------------------- types

export type Tone = "ok" | "warn" | "bad" | "idle";
export type Skin = "plain" | "core" | "plug" | "model" | "run" | "wait";
export type Slot = "top" | "mid" | "bl" | "br";
export type Card = {
  id: string;
  select: string;  // the node the inspector opens when the box is picked
  label: string;
  icon: IconName;
  status: string;  // one line
  level: number;  // the column (the row on the phone)
  skin: Skin;
  tone: Tone;
  problem?: string;  // "Phone is offline": what the status line says when this box is not fine
  slot?: Slot;  // a fixed place inside the Argus level
  big?: boolean;
};
// busy: working with traffic in the last minute; ok: working; quiet: nothing lately; slow; broken; esc: asks a bigger model
export type LineKind = "busy" | "ok" | "quiet" | "slow" | "broken" | "esc";
export type Line = { a: string; b: string; kind: LineKind };
export type Problem = { tone: "bad" | "warn"; text: string };

export type LevelCtx = {
  now: number;  // seconds
  events: ArgusEvent[];
  queue?: { running: number; queued: number } | null;
  byWorker?: Record<string, number>;
};

export const RECENT = 60;  // seconds of traffic that make a line "busy"
export const CORE = "argus";
export const PLUGINS = "plugins";
export const LEVEL_HEADS = ["Apps", "Argus", "Machines and work", "Models"];

// ---------------------------------------------------------------- small helpers

const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
const pairKey = (a: string, b: string) => (a < b ? `${a}|${b}` : `${b}|${a}`);
const caps = (n: MapNode): string[] => (Array.isArray(n.meta?.capabilities) ? (n.meta.capabilities as string[]) : []);

export function ago(sec: number): string {
  if (sec < 90) return "just now";
  if (sec < 3600) return `${Math.round(sec / 60)} min ago`;
  if (sec < 86400) return `${Math.round(sec / 3600)} h ago`;
  return `${Math.round(sec / 86400)} d ago`;
}

export function workerLabel(n: MapNode): string {
  const raw = `${n.id} ${n.group ?? ""}`.toLowerCase();
  if (/laptop/.test(raw)) return "Laptop";
  if (/\bpc\b|desktop|gpu|-pc/.test(raw)) return "PC";
  return cap(n.id.replace(/^(worker|desktop)-/, ""));
}

export function modelLabel(n: MapNode): string {
  const m = n.meta as { provider?: string; model?: string };
  if (m.provider === "claude") return "Claude";
  return m.model ?? n.label;
}

function modelIcon(n: MapNode): IconName {
  const m = n.meta as { provider?: string; model?: string; tier?: string };
  if (m.provider === "claude") return "token";
  if (/vision|llava|vl\b|moondream|minicpm/i.test(`${m.model} ${m.tier} ${n.id}`)) return "visibility";
  if (/embed|nomic|minilm|bge/i.test(`${m.model} ${m.tier} ${n.id}`)) return "memory";
  return "neurology";
}

// ---------------------------------------------------------------- which raw component is which box

// A raw component id -> the id of the box it is drawn in, or null when it is not on the map. The map shows a short
// list; everything else (island, outbox, watchdog, GitHub ...) keeps its data but gets no box.
export function boxOf(map: ArgusMap): (id: string) => string | null {
  const byId = new Map(map.nodes.map((n) => [n.id, n]));
  const phoneWorker = map.nodes.find((n) => n.kind === "worker" && caps(n).includes("phone"))?.id;
  const keep = new Set(["helios", "ari", "scheduler", "approvals", "backup"]);
  return (id) => {
    const n = byId.get(id);
    if (id === "phone" || id === "phone-app" || id === phoneWorker) return "phone";
    if (n?.kind === "plugin") return PLUGINS;
    if (n?.kind === "core") return CORE;
    if (n?.kind === "worker") return id;
    if (n?.kind === "model") return id;
    if (keep.has(id) && n) return id;
    return null;
  };
}

// Messages per box and per pair of boxes in the last minute, from the events already streamed.
export function recentTraffic(events: ArgusEvent[], now: number, of: (id: string) => string | null) {
  const byBox = new Map<string, number>(), byPair = new Map<string, number>();
  for (const e of events) {
    if (now - e.at > RECENT || e.at > now + 5) continue;
    const a = e.from ? of(e.from) : null, b = e.to ? of(e.to) : null;
    for (const x of new Set([a, b])) if (x) byBox.set(x, (byBox.get(x) ?? 0) + 1);
    if (a && b && a !== b) byPair.set(pairKey(a, b), (byPair.get(pairKey(a, b)) ?? 0) + 1);
  }
  return { byBox, byPair };
}

// ---------------------------------------------------------------- line health

// A line is as bad as the box at its far end: broken beats slow beats traffic beats history.
export function lineKind(o: { tone: Tone; recent: number; seen: boolean }): LineKind {
  if (o.tone === "bad") return "broken";
  if (o.tone === "warn") return "slow";
  if (o.recent > 0) return "busy";
  return o.seen ? "ok" : "quiet";
}

// ---------------------------------------------------------------- the boxes

type Built = { cards: Card[]; lines: Line[]; problems: Problem[] };

export function buildLevels(map: ArgusMap, ctx: LevelCtx): Built {
  const of = boxOf(map);
  const nodes = new Map(map.nodes.map((n) => [n.id, n]));
  const { byBox, byPair } = recentTraffic(ctx.events, ctx.now, of);
  const seenBox = new Set<string>();  // boxes with any line in the registry, ever
  for (const e of map.edges) for (const x of [of(e.src), of(e.dst)]) if (x) seenBox.add(x);
  const edgeOf = (a: string, b: string) => map.edges.find((e) => e.src === a && e.dst === b);
  const cards: Card[] = [];
  const lines: Line[] = [];
  const problems: Problem[] = [];
  const add = (c: Card) => { cards.push(c); if (c.problem && (c.tone === "bad" || c.tone === "warn")) problems.push({ tone: c.tone, text: c.problem }); return c; };
  const core = map.nodes.find((n) => n.kind === "core");

  // --- apps
  const phone = nodes.get("phone"), phoneApp = nodes.get("phone-app");
  const phoneW = map.nodes.find((n) => n.kind === "worker" && caps(n).includes("phone"));
  if (phone || phoneApp || phoneW) {
    const online = phoneW ? phoneW.state === "online" : (phone?.meta as { online?: boolean | null } | undefined)?.online;
    const since = (phone?.meta as { since?: number | null } | undefined)?.since;
    const failed = phoneApp?.failed ?? 0, unsent = phoneApp?.unsent ?? 0;
    let tone: Tone = "ok", status = "online", problem: string | undefined;
    if (online === false) { tone = "bad"; status = since ? `offline since ${new Date(since * 1000).toTimeString().slice(0, 5)}` : "offline"; problem = "Phone is offline"; }
    else if (failed > 0) { tone = "bad"; status = `${failed} not delivered`; problem = "Phone is not getting its messages"; }
    else if (unsent > 0) { tone = "warn"; status = `${unsent} waiting for it`; problem = "Phone is slow to receive"; }
    else if (online == null) { tone = "idle"; status = "checking"; }
    add({ id: "phone", select: phoneW?.id ?? "phone", label: "Phone", icon: "smartphone", status, level: 0, skin: "plain", tone, problem });
  }
  if (nodes.has("helios")) {
    const n = byBox.get("helios") ?? 0;
    add({ id: "helios", select: "helios", label: "Helios", icon: "dashboard", status: n ? "in use" : "dashboard", level: 0, skin: "plain", tone: "ok" });
  }
  if (nodes.has("ari")) {
    const last = Math.max(0, ...ctx.events.filter((e) => /^ari\./.test(e.kind)).map((e) => e.at));
    add({ id: "ari", select: "ari", label: "Ari", icon: "mic", status: last ? `heard ${ago(ctx.now - last)}` : "assistant", level: 0, skin: "plain", tone: "ok" });
  }

  // --- Argus
  if (nodes.has("scheduler")) add({ id: "scheduler", select: "scheduler", label: "Scheduler", icon: "schedule", status: "cron and triggers", level: 1, skin: "plain", tone: "ok", slot: "top" });
  if (core) {
    const q = ctx.queue;
    add({ id: CORE, select: core.id, label: "Argus", icon: "hub", level: 1, skin: "core", tone: "ok", slot: "mid", big: true,
      status: q ? (q.running + q.queued ? `${q.running} running · ${q.queued} waiting` : "idle") : "core" });
  }
  const appr = nodes.get("approvals");
  if (appr) add({ id: "approvals", select: "approvals", label: "Approvals", icon: "pending_actions", level: 1, skin: "plain", tone: "ok", slot: "bl",
    status: appr.pending ? `${appr.pending} waiting for you` : "nothing to decide" });
  if (nodes.has("backup")) {
    const e = edgeOf("argus", "backup");
    const failed = e?.last_kind === "backup.failed";
    add({ id: "backup", select: "backup", label: "Backups", icon: "dns", level: 1, skin: "plain", slot: "br",
      tone: failed ? "bad" : e ? "ok" : "idle", problem: failed ? "Backups are failing" : undefined,
      status: failed ? "last one failed" : e ? `last one ${ago(ctx.now - e.last_seen)}` : "none yet" });
  }

  // --- machines and work
  for (const w of map.nodes.filter((n) => n.kind === "worker" && !caps(n).includes("phone"))) {
    const run = ctx.byWorker?.[w.id] ?? 0;
    const state = w.state ?? "unknown";
    const label = workerLabel(w);
    const tone: Tone = state === "online" ? "ok" : state === "offline" ? "bad" : "warn";
    add({ id: w.id, select: w.id, label, icon: label === "Laptop" ? "laptop_chromebook" : "desktop_windows", level: 2, skin: "plain", tone,
      problem: tone === "bad" ? `${label} is offline` : tone === "warn" ? `${label} is slow to answer` : undefined,
      status: state !== "online" ? state : run ? `${run} running` : "ready" });
  }
  const plugins = map.nodes.filter((n) => n.kind === "plugin");
  if (plugins.length) {
    const working = plugins.filter((p) => (p.jobs?.active ?? 0) > 0).length;
    const queued = plugins.reduce((a, p) => a + (p.jobs?.queued ?? 0), 0);
    const stuck = !working && queued > 0;
    add({ id: PLUGINS, select: PLUGINS, label: "Plugins", icon: "extension", level: 2, skin: "plug", tone: stuck ? "warn" : "ok",
      problem: stuck ? "Plugins are waiting for a worker" : undefined,
      status: working ? `${working} working` : queued ? `${queued} waiting` : "idle" });
  }

  // --- models
  const models = map.nodes.filter((n) => n.kind === "model").sort((a, b) => modelRank(a).localeCompare(modelRank(b), "en", { numeric: true }));
  for (const m of models) {
    const label = modelLabel(m);
    const tone: Tone = m.state === "open" ? "bad" : m.state === "half_open" ? "warn" : "ok";
    const tier = String((m.meta as { tier?: string }).tier ?? "").toUpperCase();
    const st = m.state === "open" ? "paused" : m.state === "half_open" ? "trying again" : "ready";
    add({ id: m.id, select: m.id, label, icon: modelIcon(m), level: 3, skin: "model", tone,
      problem: tone === "bad" ? `${label} is paused` : tone === "warn" ? `${label} is slow to recover` : undefined,
      status: tier ? `${tier} · ${st}` : st });
  }

  // --- lines: every box joins Argus; the plugins join each model; a model joins the next one when it escalates
  const have = new Map(cards.map((c) => [c.id, c]));
  const tone = (id: string) => have.get(id)?.tone ?? "idle";
  if (have.has(CORE)) {
    for (const c of cards) {
      if (c.id === CORE || c.level === 3) continue;
      lines.push({ a: c.id, b: CORE, kind: lineKind({ tone: tone(c.id), recent: byBox.get(c.id) ?? 0, seen: seenBox.has(c.id) || (c.id === PLUGINS && (c.status !== "idle")) }) });
    }
  }
  if (have.has(PLUGINS)) {
    for (const m of models) {
      lines.push({ a: PLUGINS, b: m.id, kind: lineKind({ tone: tone(m.id), recent: byPair.get(pairKey(PLUGINS, m.id)) ?? 0, seen: seenBox.has(m.id) }) });
    }
  }
  const esc = new Set<string>();
  for (const e of map.edges) {
    if (nodes.get(e.src)?.kind !== "model" || nodes.get(e.dst)?.kind !== "model") continue;
    const k = pairKey(e.src, e.dst);
    if (esc.has(k)) continue;
    esc.add(k);
    lines.push({ a: e.src, b: e.dst, kind: "esc" });
  }
  // broken boxes first in the status line
  problems.sort((x, y) => (x.tone === y.tone ? 0 : x.tone === "bad" ? -1 : 1));
  return { cards, lines, problems };
}

// Escalation order: the general local tiers, then Claude, then the single-purpose ones (vision, embeddings) that are not in the chain.
function modelRank(n: MapNode): string {
  const m = n.meta as { provider?: string; tier?: string };
  const special = /vision|embed|vl|nomic|llava/i.test(`${(n.meta as { model?: string }).model} ${m.tier} ${n.id}`) || /^[ve]\d/i.test(m.tier ?? "");
  return `${special ? "y" : m.provider === "claude" ? "x" : "a"}${m.tier ?? n.id}`;
}

// ---------------------------------------------------------------- the status line

export type Status = { tone: "ok" | "bad" | "warn"; text: string; items: Problem[] };
export function statusLine(problems: Problem[]): Status {
  if (!problems.length) return { tone: "ok", text: "All fine", items: [] };
  const ps = [...problems].sort((x, y) => (x.tone === y.tone ? 0 : x.tone === "bad" ? -1 : 1));
  return { tone: ps[0].tone, text: ps.map((p) => p.text).join(" · "), items: ps };
}

// ---------------------------------------------------------------- crossings and ordering

// Lines between neighbouring columns cross when their ends are in opposite order. `ys`: the height of each box.
export function crossings(cols: string[][], edges: [string, string][], ys: Record<string, number>): number {
  const col = new Map<string, number>();
  cols.forEach((c, i) => c.forEach((id) => col.set(id, i)));
  const L: [string, string][] = [];
  for (const [a, b] of edges) {
    const ca = col.get(a), cb = col.get(b);
    if (ca === undefined || cb === undefined || Math.abs(ca - cb) !== 1) continue;
    L.push(ca < cb ? [a, b] : [b, a]);
  }
  let n = 0;
  for (let i = 0; i < L.length; i++) {
    for (let j = i + 1; j < L.length; j++) {
      const [a, b] = L[i], [c, d] = L[j];
      if (a === c || b === d || col.get(a) !== col.get(c)) continue;
      if (a === d || b === c) continue;
      if ((ys[a] - ys[c]) * (ys[b] - ys[d]) < 0) n++;
    }
  }
  return n;
}

// Spread the free boxes of each column evenly between `top` and `bottom`; boxes with a fixed height keep it.
function spread(cols: string[][], fixed: Record<string, number>, top: number, bottom: number): Record<string, number> {
  const ys: Record<string, number> = { ...fixed };
  for (const c of cols) {
    const free = c.filter((id) => fixed[id] === undefined);
    free.forEach((id, k) => { ys[id] = top + ((bottom - top) * (k + 0.5)) / free.length; });
  }
  return ys;
}

// Order the boxes inside each column to cross as few lines as possible: each box moves toward the average height of its
// neighbours (barycentre), then neighbouring boxes swap while a swap removes a crossing. Boxes in `fixed` keep their
// height and are not moved; columns in `locked` keep their order (the models, whose order is the escalation chain).
export function orderColumns(cols: string[][], edges: [string, string][], opt: { fixed?: Record<string, number>; top: number; bottom: number; locked?: number[] }) {
  const fixed = opt.fixed ?? {};
  const locked = new Set(opt.locked ?? []);
  const free = (c: string[]) => c.filter((id) => fixed[id] === undefined);
  const start = cols.map((c) => [...c]);
  const ysOf = (o: string[][]) => spread(o, fixed, opt.top, opt.bottom);
  const nb = new Map<string, string[]>();
  for (const [a, b] of edges) { (nb.get(a) ?? nb.set(a, []).get(a)!).push(b); (nb.get(b) ?? nb.set(b, []).get(b)!).push(a); }
  const colOf = (o: string[][]) => { const m = new Map<string, number>(); o.forEach((c, i) => c.forEach((id) => m.set(id, i))); return m; };

  // barycentre sweeps
  let cur = start.map((c) => [...c]);
  for (let it = 0; it < 14; it++) {
    const ys = ysOf(cur), cm = colOf(cur);
    cur = cur.map((c, ci) => {
      const f = free(c);
      if (locked.has(ci) || f.length < 2) return c;
      const t = (id: string) => {
        const near = (nb.get(id) ?? []).filter((o) => Math.abs((cm.get(o) ?? -9) - ci) === 1);
        return near.length ? near.reduce((a, o) => a + ys[o], 0) / near.length : ys[id];
      };
      const sorted = [...f].sort((a, b) => t(a) - t(b));
      let k = 0;
      return c.map((id) => (fixed[id] !== undefined ? id : sorted[k++]));
    });
  }
  let best = crossings(start, edges, ysOf(start)) <= crossings(cur, edges, ysOf(cur)) ? start : cur;
  let n = crossings(best, edges, ysOf(best));

  // swap neighbours while that removes a crossing
  for (let pass = 0; pass < 30 && n > 0; pass++) {
    let moved = false;
    for (let ci = 0; ci < best.length; ci++) {
      if (locked.has(ci)) continue;
      const idx = best[ci].map((id, i) => (fixed[id] === undefined ? i : -1)).filter((i) => i >= 0);
      for (let k = 0; k < idx.length - 1; k++) {
        const trial = best.map((c) => [...c]);
        [trial[ci][idx[k]], trial[ci][idx[k + 1]]] = [trial[ci][idx[k + 1]], trial[ci][idx[k]]];
        const m = crossings(trial, edges, ysOf(trial));
        if (m < n) { best = trial; n = m; moved = true; }
      }
    }
    if (!moved) break;
  }
  return { cols: best, ys: ysOf(best), crossings: n };
}

// ---------------------------------------------------------------- where everything goes

export type Dim = { w: number; h: number };
export type Placed = Card & Dim & { x: number; y: number };  // x, y: the centre
export type Routed = Line & { d: string; mx: number; my: number };
export type Layout = {
  width: number; height: number;
  placed: Record<string, Placed>;
  routes: Routed[];
  heads: { text: string; x: number; y: number }[];
  crossings: number;
};
export type LayoutOpt = {
  orient: "h" | "v";
  dims: (c: Card) => Dim;
  heads?: string[];
  // h: one x per level number, boxes spread between top and bottom. v: boxes in rows of `perRow`
  xs?: number[]; top?: number; bottom?: number; width: number; height?: number;
  perRow?: number; rowGap?: number; levelGap?: number; padTop?: number; slotDx?: number;
  locked?: number[];
};

type Side = "l" | "r" | "t" | "b";

export function layoutLevels(cards: Card[], lines: Line[], opt: LayoutOpt): Layout {
  const levels = [...new Set(cards.map((c) => c.level))].sort((a, b) => a - b);
  const byId = new Map(cards.map((c) => [c.id, c]));
  const placed: Record<string, Placed> = {};
  const heads: Layout["heads"] = [];
  let order: string[][] = levels.map((lv) => cards.filter((c) => c.level === lv).map((c) => c.id));
  const edges: [string, string][] = lines.map((l) => [l.a, l.b]);
  const lvIndex = new Map(levels.map((lv, i) => [lv, i]));
  const dimOf = (id: string) => opt.dims(byId.get(id)!);
  let height = opt.height ?? 600;
  let cross = 0;

  if (opt.orient === "h") {
    const xs = opt.xs!, top = opt.top ?? 92, bottom = opt.bottom ?? height - 60, mid = (top + bottom) / 2;
    const dx = opt.slotDx ?? 96;
    const fixed: Record<string, number> = {}, fixedX: Record<string, number> = {};
    for (const c of cards) {
      if (!c.slot) continue;
      const x = xs[c.level];
      fixed[c.id] = c.slot === "top" ? top + 20 : c.slot === "mid" ? mid : bottom - 50;
      fixedX[c.id] = c.slot === "bl" ? x - dx : c.slot === "br" ? x + dx : x;
    }
    const r = orderColumns(order, edges.filter(([a, b]) => lvIndex.get(byId.get(a)!.level)! !== lvIndex.get(byId.get(b)!.level)!), { fixed, top, bottom, locked: opt.locked });
    order = r.cols;
    cross = r.crossings;
    for (const [i, lv] of levels.entries()) {
      for (const id of order[i]) {
        const c = byId.get(id)!, d = dimOf(id);
        placed[id] = { ...c, ...d, x: fixedX[id] ?? xs[lv], y: r.ys[id] };
      }
      const head = opt.heads?.[lv];
      if (head) heads.push({ text: head, x: xs[lv], y: 36 });
    }
  } else {
    // phone: one level per row, a few boxes in a row; a second row of the same level sits under the gaps of the first
    const per = opt.perRow ?? 3, W = opt.width, gap = opt.rowGap ?? 22, lg = opt.levelGap ?? 44;
    let y = opt.padTop ?? 20;
    const r = orderColumns(order, edges.filter(([a, b]) => lvIndex.get(byId.get(a)!.level)! !== lvIndex.get(byId.get(b)!.level)!), { top: 0, bottom: 1, locked: [...(opt.locked ?? []), ...levels.map((lv, i) => (cards.some((c) => c.level === lv && c.slot) ? i : -1)).filter((i) => i >= 0)] });
    order = r.cols;
    cross = r.crossings;
    for (const [i, lv] of levels.entries()) {
      const head = opt.heads?.[lv];
      if (head) { heads.push({ text: head, x: 4, y: y + 2 }); y += 22; }
      const ids = order[i];
      const slotted = ids.some((id) => byId.get(id)!.slot);
      const rowsOf: string[][] = [];
      if (slotted) {  // Scheduler, Argus, Approvals and Backups: one above, one in the middle, two below
        const pick = (s: Slot) => ids.filter((id) => byId.get(id)!.slot === s);
        rowsOf.push(pick("top"), pick("mid"), [...pick("bl"), ...pick("br")]);
      } else {
        for (let k = 0; k < ids.length; k += per) rowsOf.push(ids.slice(k, k + per));
      }
      let first = true;
      for (const row of rowsOf.filter((x) => x.length)) {
        const rh = Math.max(...row.map((id) => dimOf(id).h));
        const w = dimOf(row[0]).w;
        const slotW = (W - 8) / (slotted ? row.length : per);
        const gapX = (W - 8 - per * w) / Math.max(1, per - 1);
        row.forEach((id, k) => {
          const d = dimOf(id);
          let x: number;
          if (slotted) x = 4 + slotW * (k + 0.5);
          else if (first) x = 4 + w / 2 + k * (w + gapX) + ((per - row.length) * (w + gapX)) / 2;
          else x = 4 + w + gapX / 2 + k * (w + gapX);  // under the gaps between the boxes above
          placed[id] = { ...byId.get(id)!, ...d, x, y: y + rh / 2 };
        });
        y += rh + gap;
        first = false;
      }
      y += lg - gap;
    }
    height = y;
  }

  // --- lines: sides, then spread the lines that meet one side of a box, then edge-to-edge cubic curves
  const ends = lines.filter((l) => placed[l.a] && placed[l.b]).map((l) => {
    const A = placed[l.a], B = placed[l.b];
    const sameLevel = A.level === B.level;
    let sa: Side, sb: Side;
    const horizontal = opt.orient === "h" ? !sameLevel : sameLevel && Math.abs(A.y - B.y) < 8;
    if (horizontal) { sa = A.x <= B.x ? "r" : "l"; sb = A.x <= B.x ? "l" : "r"; }
    else { sa = A.y <= B.y ? "b" : "t"; sb = A.y <= B.y ? "t" : "b"; }
    return { l, sa, sb, oa: 0, ob: 0 };
  });
  const slots = new Map<string, { e: (typeof ends)[number]; end: "a" | "b"; other: Placed }[]>();
  for (const e of ends) {
    for (const [id, side, o, end] of [[e.l.a, e.sa, e.l.b, "a"], [e.l.b, e.sb, e.l.a, "b"]] as const) {
      const k = `${id}:${side}`;
      (slots.get(k) ?? slots.set(k, []).get(k)!).push({ e, end, other: placed[o] });
    }
  }
  for (const [k, L] of slots) {
    const horiz = /[lr]$/.test(k);
    const box = placed[k.slice(0, k.lastIndexOf(":"))];
    L.sort((p, q) => (horiz ? p.other.y - q.other.y : p.other.x - q.other.x));
    const room = (horiz ? box.h : box.w) - (horiz ? 16 : 40);
    const step = L.length > 1 ? Math.min(horiz ? 11 : 30, room / (L.length - 1)) : 0;
    L.forEach((it, i) => { const off = (i - (L.length - 1) / 2) * step; if (it.end === "a") it.e.oa = off; else it.e.ob = off; });
  }
  const pt = (p: Placed, side: Side, off: number): [number, number] =>
    side === "r" ? [p.x + p.w / 2, p.y + off] : side === "l" ? [p.x - p.w / 2, p.y + off] : side === "b" ? [p.x + off, p.y + p.h / 2] : [p.x + off, p.y - p.h / 2];
  const routes: Routed[] = ends.map((e) => {
    const [px, py] = pt(placed[e.l.a], e.sa, e.oa), [qx, qy] = pt(placed[e.l.b], e.sb, e.ob);
    const r = (n: number) => Math.round(n * 10) / 10;
    if (e.sa === "l" || e.sa === "r") {
      const cx = (px + qx) / 2;
      return { ...e.l, d: `M${r(px)} ${r(py)} C ${r(cx)} ${r(py)}, ${r(cx)} ${r(qy)}, ${r(qx)} ${r(qy)}`, mx: r(cx), my: r((py + qy) / 2) };
    }
    const cy = (py + qy) / 2;
    return { ...e.l, d: `M${r(px)} ${r(py)} C ${r(px)} ${r(cy)}, ${r(qx)} ${r(cy)}, ${r(qx)} ${r(qy)}`, mx: r((px + qx) / 2), my: r(cy) };
  });
  return { width: opt.width, height, placed, routes, heads, crossings: cross };
}
