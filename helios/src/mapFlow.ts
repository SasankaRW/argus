// What travels the live map: each new event that joins two boxes becomes a dot along the line between them, and news
// for you (an important phone message, a wishlist alert) runs home from its plugin to the Phone box. Pure (no React,
// no clock: `nowMs` is passed in), so the tests can run it. FlowLayer.tsx moves the dots; useMapFlow.ts feeds it.

import type { ArgusEvent } from "./api";
import type { Routed } from "./mapGraph";
import { PLUGINS, CORE } from "./mapGraph";

// `next`: the steps still to go after this line ("a>b" or "<a>b"), for news travelling several lines in turn.
export type Dot = { id: string; route: string; back: boolean; tone: "work" | "news"; at: number; next?: string[] };

// One step ("a>b" along the line, "<a>b" against it) as a dot.
export function stepDot(id: string, s: string, tone: Dot["tone"], at: number, next: string[] = []): Dot {
  const back = s.startsWith("<");
  return { id, route: back ? s.slice(1) : s, back, tone, at, next };
}

export const MAX_DOTS = 12;  // on screen at once
export const GAP_MS = 600;  // per line: a burst of messages is one dot

export const FRESH_S = 15;  // seconds: an event older than this is history, not news, whatever its seq

// The events after `afterSeq` that happened in the last FRESH_S seconds. Helios draws the map first and merges the
// last 150 events in a moment later, and a reconnect can replay more: those have new seqs but are old news.
export function freshEvents(events: ArgusEvent[], afterSeq: number, nowSec: number): ArgusEvent[] {
  return events.filter((e) => e.seq > afterSeq && nowSec - e.at <= FRESH_S);
}

export const routeKey = (r: { a: string; b: string }) => `${r.a}>${r.b}`;

// The line joining boxes x and y; `back` when it is drawn from y to x (the dot runs against the drawing).
export function routeBetween(x: string, y: string, routes: Routed[]): { route: string; back: boolean } | null {
  for (const r of routes) {
    if (r.a === x && r.b === y) return { route: routeKey(r), back: false };
    if (r.a === y && r.b === x) return { route: routeKey(r), back: true };
  }
  return null;
}

// Dots for the events after `afterSeq`, oldest first. `last` (line -> ms of its last dot) is updated in place.
export function pickDots(events: ArgusEvent[], o: {
  afterSeq: number; routes: Routed[]; of: (id: string) => string | null; last: Map<string, number>; nowMs: number;
}): { dots: Dot[]; seq: number } {
  const dots: Dot[] = [];
  let seq = o.afterSeq;
  for (const e of events) {
    if (e.seq <= o.afterSeq) continue;
    seq = Math.max(seq, e.seq);
    const a = e.from ? o.of(e.from) : null, b = e.to ? o.of(e.to) : null;
    if (!a || !b || a === b) continue;
    const r = routeBetween(a, b, o.routes);
    if (!r) continue;
    const prev = o.last.get(r.route);
    if (prev !== undefined && o.nowMs - prev < GAP_MS) continue;
    o.last.set(r.route, o.nowMs);
    dots.push({ id: `${e.seq}`, route: r.route, back: r.back, tone: "work", at: o.nowMs });
  }
  return { dots, seq };
}

const tagsOf = (e: ArgusEvent): string[] => (Array.isArray(e.data?.tags) ? (e.data.tags as string[]) : []);

// News for you: a phone message of priority high or above, or any wishlist alert.
export function isNews(e: ArgusEvent): boolean {
  if (e.kind !== "notify.queued") return false;
  return Number(e.data?.priority ?? 0) >= 4 || tagsOf(e).includes("wishlist");
}

// A step from x to y written as the dot travels it: "a>b" along the drawn line, "<a>b" against it.
function step(x: string, y: string, routes: Routed[]): string | null {
  const r = routeBetween(x, y, routes);
  return r ? `${r.back ? "<" : ""}${r.route}` : null;
}

// The hops news takes home: plugin box -> a machine (Calypso first) -> Argus -> Phone, and for a wishlist alert a
// second chain from Argus to Life Hub. [] when it isn't news or the map has no way home.
export function newsRoute(e: ArgusEvent, routes: Routed[], of: (id: string) => string | null): string[][] {
  if (!isNews(e)) return [];
  const start = e.from ? of(e.from) : null;
  const toArgus = (box: string): string[] | null => {
    if (box === CORE) return [];
    const direct = step(box, CORE, routes);
    if (direct) return [direct];
    if (box !== PLUGINS) return null;
    const machines = routes.filter((r) => r.b === PLUGINS && routes.some((q) => q.a === r.a && q.b === CORE)).map((r) => r.a);
    const m = machines.find((id) => /laptop|calypso/i.test(id)) ?? machines[0];
    if (!m) return null;
    return [step(PLUGINS, m, routes)!, step(m, CORE, routes)!];
  };
  const head = start ? toArgus(start) : null;
  const home = step(CORE, "phone", routes);
  if (!head || !home) return [];
  const chains = [[...head, home]];
  const hub = ["lifehub", "life-hub"].map((id) => step(CORE, id, routes)).find(Boolean);
  if (tagsOf(e).includes("wishlist") && hub) chains.push([hub]);
  return chains;
}
