import { describe, expect, it } from "vitest";
import type { ArgusEvent, ArgusMap, MapEdge, MapNode } from "./api";
import { layoutLevels } from "./mapGraph";
import { buildPluginView, jobText } from "./pluginGraph";

const NOW = 1_000_000;
const node = (id: string, kind: MapNode["kind"], over: Partial<MapNode> = {}): MapNode => ({ id, kind, label: id, group: null, meta: {}, first_seen: 0, ...over });
const plugin = (id: string, active = 0, queued = 0, waiting = 0) => node(id, "plugin", { jobs: { active, queued, waiting } });
const edge = (src: string, dst: string): MapEdge => ({ src, dst, count: 4, last_kind: "job.queued", first_seen: 0, last_seen: NOW - 3000 });
const ev = (from: string, to: string, ago = 5): ArgusEvent => ({ seq: 1, id: "e", kind: "x", job_id: null, step: null, from, to, data: null, at: NOW - ago });

function map(): ArgusMap {
  return {
    seq: 1,
    nodes: [
      node("argus", "core"), node("scheduler", "service"), node("helios", "app"), node("ari", "app"), node("vault-sync", "service"), node("approvals", "service"),
      node("phone", "service", { meta: { online: false } }), node("web-search", "service"), node("worker-pc", "worker", { state: "online" }), node("outbox", "service"),
      node("t1", "model", { state: "closed", meta: { tier: "T1", provider: "ollama", model: "qwen2.5-coder:7b" } }),
      node("t2", "model", { state: "closed", meta: { tier: "T2", provider: "ollama", model: "qwen2.5-coder:14b" } }),
      plugin("advisor", 1), plugin("watchers", 2), plugin("tracker", 0, 1), plugin("vault", 0, 0, 1), plugin("zeta"), plugin("diary"),
    ],
    edges: [
      edge("scheduler", "advisor"), edge("scheduler", "watchers"), edge("helios", "tracker"), edge("vault-sync", "vault"),
      edge("advisor", "t1"), edge("t1", "t2"), edge("watchers", "web-search"), edge("watchers", "phone"), edge("tracker", "approvals"),
      edge("worker-pc", "advisor"), edge("argus", "advisor"), edge("outbox", "watchers"), edge("scheduler", "zeta"),
    ].map((e, i) => (i === 5 ? { ...e, last_kind: "model.escalated" } : e)),
  };
}
const view = (m = map(), events: ArgusEvent[] = []) => buildPluginView(m, { now: NOW, events });
const byId = (v: ReturnType<typeof view>, id: string) => v.cards.find((c) => c.id === id)!;

describe("plugin view", () => {
  it("shows only plugins that have jobs; the rest are listed as idle, by name", () => {
    const v = view();
    expect(v.cards.filter((c) => c.level === 1).map((c) => c.id).sort()).toEqual(["advisor", "tracker", "vault", "watchers"]);
    expect(v.idle).toEqual(["diary", "zeta"]);
    expect(v.running).toBe(2);
  });

  it("colours running plugins green and waiting ones amber, with the job text", () => {
    const v = view();
    expect(byId(v, "advisor")).toMatchObject({ skin: "run", status: "running" });
    expect(byId(v, "tracker")).toMatchObject({ skin: "wait", status: "waiting for a worker" });
    expect(byId(v, "vault")).toMatchObject({ skin: "wait", status: "waiting" });
    expect(jobText(plugin("p", 2))).toEqual({ text: "running · 2 jobs", skin: "run" });
    expect(jobText(plugin("p", 1), "price check").text).toBe("running · price check");
  });

  it("puts what feeds plugins on the left and what they use or produce on the right", () => {
    const v = view();
    const ids = (lv: number) => v.cards.filter((c) => c.level === lv).map((c) => c.id).sort();
    expect(ids(0)).toEqual(["helios", "scheduler", "vault-sync"]);
    expect(ids(2)).toEqual(["approvals", "phone", "t1", "web-search"]);
  });

  it("leaves out the core, workers, the outbox and plugins that are idle", () => {
    const ids = view().cards.map((c) => c.id);
    for (const hidden of ["argus", "worker-pc", "outbox", "zeta", "ari"]) expect(ids).not.toContain(hidden);
  });

  it("names unregistered components from their id", () => {
    expect(byId(view(), "web-search").label).toBe("Web search");
    expect(byId(view(), "vault-sync").label).toBe("Vault sync");
  });

  it("gives the same line health as the main map: offline phone broken, recent traffic busy, history ok", () => {
    const v = view(map(), [ev("advisor", "t1", 8)]);
    const line = (a: string, b: string) => v.lines.find((l) => l.a === a && l.b === b)?.kind;
    expect(line("watchers", "phone")).toBe("broken");
    expect(line("advisor", "t1")).toBe("busy");
    expect(line("scheduler", "advisor")).toBe("ok");
    expect(line("advisor", "t1")).not.toBe("ok");
    expect(byId(v, "phone").tone).toBe("bad");
  });

  it("is quiet about traffic older than a minute", () => {
    const v = view(map(), [ev("advisor", "t1", 90)]);
    expect(v.lines.find((l) => l.a === "advisor" && l.b === "t1")?.kind).toBe("ok");
  });

  it("draws a model's escalation to a bigger one as a purple line when both are shown", () => {
    const m = map();
    m.edges.push(edge("advisor", "t2"));
    const v = view(m);
    expect(v.lines.filter((l) => l.kind === "esc")).toEqual([{ a: "t1", b: "t2", kind: "esc" }]);
  });

  it("keeps the models together in escalation order", () => {
    const m = map();
    m.edges.push(edge("advisor", "t2"));
    const right = view(m).cards.filter((c) => c.level === 2).map((c) => c.id);
    expect(right.indexOf("t2") - right.indexOf("t1")).toBe(1);
  });

  it("lays out in three columns without heading text", () => {
    const v = view();
    const l = layoutLevels(v.cards, v.lines, { orient: "h", dims: () => ({ w: 176, h: 54 }), xs: [180, 595, 1010], top: 70, bottom: 400, width: 1190, height: 600 });
    expect(l.heads).toEqual([]);
    expect(l.placed.scheduler.x).toBe(180);
    expect(l.placed.advisor.x).toBe(595);
    expect(l.placed.t1.x).toBe(1010);
  });

  it("copes with nothing running", () => {
    const m = map();
    m.nodes = m.nodes.map((n) => (n.kind === "plugin" ? { ...n, jobs: { active: 0, queued: 0, waiting: 0 } } : n));
    const v = view(m);
    expect(v.cards).toEqual([]);
    expect(v.idle).toHaveLength(6);
  });
});
