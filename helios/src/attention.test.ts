import { describe, expect, it } from "vitest";
import type { ArgusMap, MapNode } from "./api";
import { attentionItems, logsOf, MAX_ITEMS, waited } from "./attention";
import { buildLevels } from "./mapGraph";

const NOW = 1_000_000;
const node = (id: string, kind: MapNode["kind"], over: Partial<MapNode> = {}): MapNode =>
  ({ id, kind, label: id, group: null, meta: {}, first_seen: 0, ...over });
const map = (over: Partial<Record<string, Partial<MapNode>>> = {}): ArgusMap => {
  const nodes = [
    node("argus", "core"), node("scheduler", "service"), node("approvals", "service", { pending: 0 }), node("helios", "app"),
    node("phone", "service", { meta: { online: true } }),
    node("worker-laptop", "worker", { state: "online", group: "calypso" }), node("worker-pc", "worker", { state: "online", group: "pc" }),
    node("advisor", "plugin", { jobs: { active: 1, queued: 0, waiting: 0 } }),
    node("t1", "model", { state: "closed", meta: { tier: "T1", provider: "ollama", model: "qwen" } }),
  ].map((n) => ({ ...n, ...(over[n.id] ?? {}) }));
  return { seq: 1, nodes, edges: [] };
};
const items = (m: ArgusMap, approvals = { count: 0, oldest: null as number | null }) => {
  const cards = buildLevels(m, { now: NOW, events: [] }).cards;
  return attentionItems(cards, { now: NOW, approvals, nodeKind: (id) => m.nodes.find((n) => n.id === id)?.kind });
};

describe("attention items", () => {
  it("is empty when all is fine", () => {
    expect(items(map())).toEqual([]);
  });

  it("offers Ring phone for an offline phone, with since and the messages waiting", () => {
    const m = map({ phone: { meta: { online: false, since: NOW - 3600 } } });
    m.nodes.push(node("phone-app", "service", { unsent: 3 }));
    const [it] = items(m);
    expect(it).toMatchObject({ id: "phone", tone: "bad", title: "Phone is offline", act: { kind: "ring", label: "Ring phone" } });
    expect(it.detail).toMatch(/^since \d\d:\d\d · 3 messages waiting$/);
  });

  it("offers Review for waiting approvals, with the age of the oldest", () => {
    const [it] = items(map(), { count: 2, oldest: NOW - 4800 });
    expect(it).toMatchObject({ title: "2 approvals waiting", detail: "oldest 1 h 20 m", act: { kind: "review", label: "Review" } });
    expect(items(map(), { count: 1, oldest: null })[0]).toMatchObject({ title: "1 approval waiting", detail: undefined });
  });

  it("offers Open logs for a slow or failed component, aimed at that component's own log", () => {
    const m = map({ "worker-pc": { state: "offline" }, t1: { state: "half_open" } });
    const list = items(m);
    expect(list.map((i) => [i.title, i.tone])).toEqual([["PC is offline", "bad"], ["qwen is slow to recover", "warn"]]);
    expect(list[0].act).toEqual({ kind: "logs", label: "Open logs", target: "worker-pc" });
    expect(list[1].act).toEqual({ kind: "logs", label: "Open logs", target: "argus" });  // a model has no log of its own: the core's
  });

  it("puts broken first, then approvals, then slow", () => {
    const m = map({ phone: { meta: { online: false } }, t1: { state: "half_open" } });
    expect(items(m, { count: 1, oldest: NOW - 60 }).map((i) => i.id)).toEqual(["phone", "approvals", "t1"]);
  });

  it("does not raise a paused machine or a stopped plugin", () => {
    expect(items(map({ "worker-pc": { paused: true }, advisor: { paused: true, jobs: { active: 0, queued: 3, waiting: 0 } } }))).toEqual([]);
  });

  it("formats waits and finds a log", () => {
    expect([waited(30), waited(600), waited(4800), waited(7200), waited(40000)]).toEqual(["1 min", "10 min", "1 h 20 m", "2 h", "11 h"]);
    const c = buildLevels(map(), { now: NOW, events: [] }).cards;
    expect(logsOf(c.find((x) => x.id === "worker-pc")!, () => "worker")).toBe("worker-pc");
    expect(MAX_ITEMS).toBeGreaterThanOrEqual(3);
  });
});
