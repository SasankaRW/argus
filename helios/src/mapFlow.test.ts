import { describe, expect, it } from "vitest";
import type { ArgusEvent, ArgusMap, MapEdge, MapNode } from "./api";
import { boxOf, buildLevels, L, layoutLevels } from "./mapGraph";
import { newsRoute, pickDots } from "./mapFlow";

const NOW = 1_000_000;
const node = (id: string, kind: MapNode["kind"], over: Partial<MapNode> = {}): MapNode =>
  ({ id, kind, label: id, group: null, meta: {}, first_seen: 0, ...over });
const edge = (src: string, dst: string, over: Partial<MapEdge> = {}): MapEdge =>
  ({ src, dst, count: 3, last_kind: "job.queued", first_seen: 0, last_seen: NOW - 3600, ...over });
const ev = (from: string, to: string, ago = 1, seq = 1): ArgusEvent =>
  ({ seq, id: `e${seq}`, kind: "job.queued", job_id: null, step: null, from, to, data: null, at: NOW - ago });

function sample(): ArgusMap {
  return {
    seq: 1,
    nodes: [
      node("argus", "core"), node("helios", "app"), node("ari", "app"), node("phone", "service", { meta: { online: true } }),
      node("lifehub", "app"),
      node("worker-laptop", "worker", { state: "online", group: "calypso" }), node("worker-pc", "worker", { state: "online", group: "pc" }),
      node("advisor", "plugin", { jobs: { active: 1, queued: 0, waiting: 0 } }), node("watchers", "plugin", { jobs: { active: 0, queued: 0, waiting: 0 } }),
      node("t1", "model", { state: "closed", meta: { tier: "T1", provider: "ollama", model: "qwen2.5-coder:7b" } }),
      node("island", "service"),
    ],
    edges: [edge("advisor", "t1")],
  };
}

const dims = (c: { big?: boolean; meter?: number; skin?: string }) => (c.big ? { w: 196, h: 76 } : { w: 176, h: 54 });
const opt = { orient: "h" as const, dims, xs: [100, 385, 615, 850, 1100], width: 1200, height: 600, locked: [L.models], compact: [L.models, L.plugins] };
const map = sample();
const b = buildLevels(map, { now: NOW, events: [] });
const routes = layoutLevels(b.cards, b.lines, opt).routes;
const of = boxOf(map);
const opts = () => ({ afterSeq: 0, routes, of, last: new Map<string, number>(), nowMs: 0 });

describe("pickDots", () => {
  it("one dot per event along the line between its boxes", () => {
    const { dots } = pickDots([ev("worker-laptop", "advisor", 1, 10)], opts());
    expect(dots).toEqual([expect.objectContaining({ route: "worker-laptop>plugins", back: false, tone: "work" })]);
  });
  it("runs against the line when the event goes the other way", () => {
    const { dots } = pickDots([ev("advisor", "worker-laptop", 1, 10)], opts());
    expect(dots[0]).toMatchObject({ route: "worker-laptop>plugins", back: true });
  });
  it("a burst on one route makes one dot", () => {
    const six = [1, 2, 3, 4, 5, 6].map((s) => ev("worker-laptop", "advisor", 1, s));
    expect(pickDots(six, opts()).dots).toHaveLength(1);
  });
  it("again after the gap", () => {
    const last = new Map([["worker-laptop>plugins", 0]]);
    expect(pickDots([ev("worker-laptop", "advisor", 1, 7)], { ...opts(), last, nowMs: 599 }).dots).toHaveLength(0);
    expect(pickDots([ev("worker-laptop", "advisor", 1, 8)], { ...opts(), last, nowMs: 600 }).dots).toHaveLength(1);
  });
  it("no line, no dot, no throw", () => {
    expect(pickDots([ev("worker-laptop", "island", 1, 9), { ...ev("x", "y", 1, 10), from: null }], opts()).dots).toEqual([]);
  });
  it("only events after afterSeq, and returns the newest seq", () => {
    const r = pickDots([ev("helios", "argus", 1, 3), ev("helios", "argus", 1, 9)], { ...opts(), afterSeq: 5 });
    expect(r.dots).toHaveLength(1);
    expect(r.seq).toBe(9);
  });
});

describe("newsRoute", () => {
  const alert = (tags: string[]) => ({ ...ev("watchers", "argus", 1, 11), kind: "notify.queued",
    data: { title: "Watcher: Wishlist: Dune book", priority: 4, tags } });
  it("a wishlist alert runs home to the phone, and to Life Hub", () => {
    const chains = newsRoute(alert(["eyes", "wishlist"]), routes, of);
    expect(chains[0]).toEqual(["<worker-laptop>plugins", "worker-laptop>argus", "<phone>argus"]);
    expect(chains[1]).toEqual(["<lifehub>argus"]);
  });
  it("other important news only goes to the phone", () => {
    expect(newsRoute(alert(["eyes"]), routes, of)).toHaveLength(1);
  });
  it("ordinary messages are not news", () => {
    expect(newsRoute({ ...ev("advisor", "argus", 1, 12), kind: "notify.queued", data: { priority: 3 } }, routes, of)).toEqual([]);
  });
});
