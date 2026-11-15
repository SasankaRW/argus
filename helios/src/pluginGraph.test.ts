import { describe, expect, it } from "vitest";
import type { ArgusEvent, ArgusMap, MapEdge, MapNode } from "./api";
import { layoutLevels } from "./mapGraph";
import { buildPluginView, PL, pluginCard } from "./pluginGraph";

const NOW = 1_000_000;
const node = (id: string, kind: MapNode["kind"], over: Partial<MapNode> = {}): MapNode =>
  ({ id, kind, label: id, group: null, meta: {}, first_seen: 0, ...over });
const edge = (src: string, dst: string, over: Partial<MapEdge> = {}): MapEdge =>
  ({ src, dst, count: 3, last_kind: "job.queued", first_seen: 0, last_seen: NOW - 3600, ...over });
const ev = (from: string, to: string, ago = 5): ArgusEvent =>
  ({ seq: 1, id: "e", kind: "job.queued", job_id: null, step: null, from, to, data: null, at: NOW - ago });
const jobs = (active: number, queued = 0, waiting = 0) => ({ active, queued, waiting });

function sample(): ArgusMap {
  return {
    seq: 1,
    nodes: [
      node("argus", "core"), node("scheduler", "service"), node("helios", "app"), node("ari", "app"), node("approvals", "service", { pending: 1 }),
      node("phone", "service", { meta: { online: true } }), node("web", "service"),
      node("worker-pc", "worker", { state: "online", group: "pc" }),
      node("advisor", "plugin", { jobs: jobs(1) }), node("tracker", "plugin", { jobs: jobs(0, 2) }), node("workstation", "plugin", { jobs: jobs(0), paused: true, since: NOW - 600 }),
      node("notes", "plugin", { jobs: jobs(0) }), node("diary", "plugin", { jobs: jobs(0) }),
      node("t1", "model", { state: "closed", meta: { tier: "T1", provider: "ollama", model: "qwen" } }),
      node("t3", "model", { state: "closed", meta: { tier: "T3", provider: "claude" } }),
    ],
    edges: [
      edge("scheduler", "advisor"), edge("helios", "tracker"), edge("advisor", "t1"), edge("advisor", "phone"), edge("advisor", "web"),
      edge("tracker", "approvals"), edge("t1", "t3", { last_kind: "model.escalated" }), edge("worker-pc", "advisor"), edge("argus", "advisor"),
      edge("scheduler", "workstation"), edge("workstation", "phone"), edge("scheduler", "notes"),
    ],
  };
}
const view = (m = sample(), events: ArgusEvent[] = [], workflows: Record<string, string> = {}) => buildPluginView(m, { now: NOW, events, workflows });
const card = (v: ReturnType<typeof view>, id: string) => v.cards.find((c) => c.id === id);

describe("the plugins shown", () => {
  it("shows running plugins green, waiting ones amber and stopped ones red", () => {
    const v = view(sample(), [], { advisor: "price_check" });
    expect(card(v, "advisor")).toMatchObject({ skin: "run", tone: "ok", status: "running · price_check", level: PL.plugins });
    expect(card(v, "tracker")).toMatchObject({ skin: "wait", tone: "warn", status: "waiting for a worker" });
    expect(card(v, "workstation")).toMatchObject({ skin: "stop", tone: "bad", paused: true });
    expect(v.running).toBe(1);
    expect(v.stopped).toBe(1);
  });

  it("shows a stopped plugin even when nothing is queued for it, and says what waits when something is", () => {
    expect(card(view(), "workstation")).toBeDefined();
    expect(card(view(), "workstation")!.status).toMatch(/^stopped since \d\d:\d\d$/);
    const m = sample();
    m.nodes.find((n) => n.id === "workstation")!.jobs = jobs(0, 3);
    expect(card(view(m), "workstation")!.status).toBe("stopped · 3 waiting");
    expect(pluginCard(node("p", "plugin", { paused: true, jobs: jobs(2) })).skin).toBe("stop");  // a stopped plugin is never "running"
  });

  it("lists the idle plugins by name, dimmed, and leaves them out of the columns", () => {
    const v = view();
    expect(v.idle).toEqual(["diary", "notes"]);
    expect(card(v, "notes")).toBeUndefined();
  });

  it("says running with the number of jobs when there is no workflow name", () => {
    const m = sample();
    m.nodes.find((n) => n.id === "advisor")!.jobs = jobs(3);
    expect(card(view(m), "advisor")!.status).toBe("running · 3 jobs");
  });

  it("is empty when nothing runs, waits or is stopped", () => {
    const m = sample();
    for (const n of m.nodes) if (n.kind === "plugin") { n.jobs = jobs(0); n.paused = false; }
    const v = view(m);
    expect(v.cards).toEqual([]);
    expect(v.idle).toHaveLength(5);
    expect(v.running).toBe(0);
  });
});

describe("inputs and outputs", () => {
  it("puts what feeds the plugins on the left and what they use or produce on the right", () => {
    const v = view();
    const level = (id: string) => card(v, id)?.level;
    expect([level("scheduler"), level("helios")]).toEqual([PL.inputs, PL.inputs]);
    expect([level("t1"), level("phone"), level("approvals"), level("web")]).toEqual([PL.outputs, PL.outputs, PL.outputs, PL.outputs]);
  });

  it("leaves out the core, workers and other plugins", () => {
    const ids = view().cards.map((c) => c.id);
    for (const hidden of ["argus", "worker-pc", "notes"]) expect(ids).not.toContain(hidden);
  });

  it("names things the way the main map does and falls back to a readable name", () => {
    const v = view();
    expect(card(v, "t1")!.label).toBe("qwen");
    expect(card(v, "web")!.label).toBe("Web search");
    expect(card(v, "approvals")!.status).toBe("1 waiting for you");
  });

  it("links an input to the plugins it feeds and a plugin to what it uses", () => {
    const v = view();
    const has = (a: string, b: string) => v.lines.some((l) => l.a === a && l.b === b);
    expect(has("scheduler", "advisor")).toBe(true);
    expect(has("scheduler", "workstation")).toBe(true);
    expect(has("advisor", "t1")).toBe(true);
    expect(has("advisor", "phone")).toBe(true);
    expect(has("workstation", "phone")).toBe(true);
    expect(has("scheduler", "notes")).toBe(false);  // an idle plugin has no card to join
  });

  it("draws an escalation between two models that are both shown", () => {
    const m = sample();
    m.edges.push(edge("advisor", "t3"));
    expect(view(m).lines.filter((l) => l.kind === "esc").map((l) => `${l.a}>${l.b}`)).toEqual(["t1>t3"]);
    expect(view().lines.some((l) => l.kind === "esc")).toBe(false);  // Claude is not shown, so no escalation line
  });

  it("keeps the models together, in escalation order", () => {
    const m = sample();
    m.edges.push(edge("advisor", "t3"));
    expect(view(m).cards.filter((c) => c.level === PL.outputs).map((c) => c.id).slice(-2)).toEqual(["t1", "t3"]);
  });
});

describe("line health", () => {
  const kind = (v: ReturnType<typeof view>, a: string, b: string) => v.lines.find((l) => l.a === a && l.b === b)?.kind;

  it("lights a line only for traffic in the last minute", () => {
    expect(kind(view(sample(), [ev("advisor", "t1", 10)]), "advisor", "t1")).toBe("busy");
    expect(kind(view(sample(), [ev("advisor", "t1", 90)]), "advisor", "t1")).toBe("quiet");
    expect(kind(view(sample(), [ev("scheduler", "advisor", 3)]), "scheduler", "advisor")).toBe("busy");
    expect(kind(view(sample(), [ev("scheduler", "advisor", 3)]), "scheduler", "workstation")).toBe("quiet");
  });

  it("is broken toward an offline phone and slow toward a half-open model", () => {
    const m = sample();
    m.nodes.find((n) => n.id === "phone")!.meta = { online: false };
    m.nodes.find((n) => n.id === "t1")!.state = "half_open";
    const v = view(m);
    expect(kind(v, "advisor", "phone")).toBe("broken");
    expect(kind(v, "workstation", "phone")).toBe("broken");
    expect(kind(v, "advisor", "t1")).toBe("slow");
  });

  it("counts the phone app's traffic as the phone's", () => {
    expect(kind(view(sample(), [ev("advisor", "phone-app", 5)]), "advisor", "phone")).toBe("busy");
  });
});

describe("layout", () => {
  it("lays out three columns, left to right, with the plugins in the middle", () => {
    const v = view();
    const dims = (c: { skin: string }) => (["run", "wait", "stop"].includes(c.skin) ? { w: 210, h: 60 } : { w: 176, h: 54 });
    const l = layoutLevels(v.cards, v.lines, { orient: "h", dims, xs: [170, 600, 1030], top: 60, bottom: 430, width: 1200, height: 600, locked: [PL.outputs] });
    const xs = (lv: number) => Object.values(l.placed).filter((c) => c.level === lv).map((c) => c.x);
    expect(new Set(xs(PL.inputs))).toEqual(new Set([170]));
    expect(new Set(xs(PL.plugins))).toEqual(new Set([600]));
    expect(new Set(xs(PL.outputs))).toEqual(new Set([1030]));
    for (const c of Object.values(l.placed)) { expect(c.y - c.h / 2).toBeGreaterThanOrEqual(0); expect(c.y + c.h / 2).toBeLessThanOrEqual(600 - 120); }
  });
});
