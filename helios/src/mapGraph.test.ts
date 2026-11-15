import { describe, expect, it } from "vitest";
import type { ArgusEvent, ArgusMap, MapEdge, MapNode } from "./api";
import { boxOf, buildLevels, crossings, layoutLevels, lineKind, orderColumns, statusLine } from "./mapGraph";

const NOW = 1_000_000;
const node = (id: string, kind: MapNode["kind"], over: Partial<MapNode> = {}): MapNode =>
  ({ id, kind, label: id, group: null, meta: {}, first_seen: 0, ...over });
const edge = (src: string, dst: string, over: Partial<MapEdge> = {}): MapEdge =>
  ({ src, dst, count: 3, last_kind: "job.queued", first_seen: 0, last_seen: NOW - 3600, ...over });
const ev = (from: string, to: string, ago = 5): ArgusEvent =>
  ({ seq: 1, id: "e", kind: "job.queued", job_id: null, step: null, from, to, data: null, at: NOW - ago });

function sample(over: { nodes?: MapNode[]; edges?: MapEdge[] } = {}): ArgusMap {
  return {
    seq: 1,
    nodes: [
      node("argus", "core"), node("scheduler", "service"), node("approvals", "service", { pending: 0 }), node("backup", "service"),
      node("helios", "app"), node("ari", "app"), node("phone", "service", { meta: { online: true } }),
      node("worker-laptop", "worker", { state: "online", group: "laptop" }), node("worker-pc", "worker", { state: "online", group: "pc" }),
      node("advisor", "plugin", { jobs: { active: 1, queued: 0, waiting: 0 } }), node("notes", "plugin", { jobs: { active: 0, queued: 0, waiting: 0 } }),
      node("t1", "model", { state: "closed", meta: { tier: "T1", provider: "ollama", model: "qwen2.5-coder:7b" } }),
      node("t2", "model", { state: "closed", meta: { tier: "T2", provider: "ollama", model: "qwen2.5-coder:14b" } }),
      node("t3", "model", { state: "closed", meta: { tier: "T3", provider: "claude" } }),
      node("v1", "model", { state: "closed", meta: { tier: "V1", provider: "ollama", model: "qwen2.5vl:7b" } }),
      node("island", "service"), node("outbox", "service"), node("watchdog", "service"), node("github", "service"), node("power", "service"),
      ...(over.nodes ?? []),
    ],
    edges: [edge("advisor", "t1"), edge("t1", "t2", { last_kind: "model.escalated" }), edge("t2", "t3", { last_kind: "model.escalated" }), ...(over.edges ?? [])],
  };
}
const ctx = (events: ArgusEvent[] = []) => ({ now: NOW, events });
const line = (b: ReturnType<typeof buildLevels>, a: string, c: string) => b.lines.find((l) => (l.a === a && l.b === c) || (l.a === c && l.b === a));

describe("which components get a box", () => {
  it("shows only the essentials and drops island, outbox, watchdog, GitHub and power", () => {
    const ids = buildLevels(sample(), ctx()).cards.map((c) => c.id).sort();
    expect(ids).toEqual(["approvals", "argus", "ari", "backup", "helios", "phone", "plugins", "scheduler", "t1", "t2", "t3", "v1", "worker-laptop", "worker-pc"].sort());
  });

  it("folds every plugin into one box and the phone app and its worker into the phone", () => {
    const m = sample({ nodes: [node("phone-app", "service", { unsent: 2 }), node("worker-phone", "worker", { state: "online", meta: { capabilities: ["phone"] } })] });
    const of = boxOf(m);
    expect(of("advisor")).toBe("plugins");
    expect(of("notes")).toBe("plugins");
    expect(of("phone-app")).toBe("phone");
    expect(of("worker-phone")).toBe("phone");
    expect(of("island")).toBeNull();
    const cards = buildLevels(m, ctx()).cards;
    expect(cards.filter((c) => c.id === "phone")).toHaveLength(1);
    expect(cards.some((c) => c.id === "worker-phone")).toBe(false);
  });

  it("lists every model tier, local ones before Claude, with its state", () => {
    const m = sample();
    m.nodes.find((n) => n.id === "t2")!.state = "open";
    const models = buildLevels(m, ctx()).cards.filter((c) => c.level === 3);
    expect(models.map((c) => c.label)).toEqual(["qwen2.5-coder:7b", "qwen2.5-coder:14b", "Claude", "qwen2.5vl:7b"]);
    expect(models[1].status).toBe("T2 · paused");
    expect(models[1].tone).toBe("bad");
  });

  it("counts the plugins that have a running job", () => {
    const plugins = buildLevels(sample(), ctx()).cards.find((c) => c.id === "plugins")!;
    expect(plugins.status).toBe("1 working");
  });
});

describe("line health", () => {
  it("ranks broken over slow over busy over ok over quiet", () => {
    expect(lineKind({ tone: "bad", recent: 9, seen: true })).toBe("broken");
    expect(lineKind({ tone: "warn", recent: 9, seen: true })).toBe("slow");
    expect(lineKind({ tone: "ok", recent: 1, seen: true })).toBe("busy");
    expect(lineKind({ tone: "ok", recent: 0, seen: true })).toBe("ok");
    expect(lineKind({ tone: "idle", recent: 0, seen: false })).toBe("quiet");
  });

  it("is busy only when there was traffic in the last minute", () => {
    const fresh = buildLevels(sample({ edges: [edge("helios", "argus")] }), ctx([ev("helios", "argus", 10)]));
    expect(line(fresh, "helios", "argus")?.kind).toBe("busy");
    const stale = buildLevels(sample({ edges: [edge("helios", "argus")] }), ctx([ev("helios", "argus", 120)]));
    expect(line(stale, "helios", "argus")?.kind).toBe("ok");
  });

  it("counts traffic to a plugin as traffic on the Plugins box", () => {
    const b = buildLevels(sample(), ctx([ev("scheduler", "advisor", 3)]));
    expect(line(b, "plugins", "argus")?.kind).toBe("busy");
    expect(line(b, "scheduler", "argus")?.kind).toBe("busy");
  });

  it("is broken when the phone is offline and the line carries nothing", () => {
    const m = sample();
    m.nodes.find((n) => n.id === "phone")!.meta = { online: false, since: NOW - 600 };
    const b = buildLevels(m, ctx());
    expect(line(b, "phone", "argus")?.kind).toBe("broken");
    expect(b.cards.find((c) => c.id === "phone")?.tone).toBe("bad");
  });

  it("is slow while messages wait for the phone and broken when they failed", () => {
    expect(line(buildLevels(sample({ nodes: [node("phone-app", "service", { unsent: 2 })] }), ctx()), "phone", "argus")?.kind).toBe("slow");
    expect(line(buildLevels(sample({ nodes: [node("phone-app", "service", { failed: 1 })] }), ctx()), "phone", "argus")?.kind).toBe("broken");
  });

  it("is broken for an offline worker and for a model whose circuit is open, slow for a half-open one", () => {
    const m = sample();
    m.nodes.find((n) => n.id === "worker-pc")!.state = "offline";
    m.nodes.find((n) => n.id === "t1")!.state = "open";
    m.nodes.find((n) => n.id === "t2")!.state = "half_open";
    const b = buildLevels(m, ctx());
    expect(line(b, "worker-pc", "argus")?.kind).toBe("broken");
    expect(line(b, "plugins", "t1")?.kind).toBe("broken");
    expect(line(b, "plugins", "t2")?.kind).toBe("slow");
  });

  it("is broken when the last backup failed", () => {
    const b = buildLevels(sample({ edges: [edge("argus", "backup", { last_kind: "backup.failed" })] }), ctx());
    expect(line(b, "backup", "argus")?.kind).toBe("broken");
  });

  it("draws an escalation as a purple line between two models only where the map has one", () => {
    const b = buildLevels(sample(), ctx());
    expect(b.lines.filter((l) => l.kind === "esc").map((l) => `${l.a}>${l.b}`)).toEqual(["t1>t2", "t2>t3"]);
  });
});

describe("status line", () => {
  it("says All fine when nothing is wrong", () => {
    expect(statusLine(buildLevels(sample(), ctx()).problems)).toEqual({ tone: "ok", text: "All fine", items: [] });
  });

  it("names what is wrong, broken first", () => {
    const m = sample();
    m.nodes.find((n) => n.id === "t2")!.state = "half_open";
    m.nodes.find((n) => n.id === "phone")!.meta = { online: false };
    const s = statusLine(buildLevels(m, ctx()).problems);
    expect(s.tone).toBe("bad");
    expect(s.text).toBe("Phone is offline · qwen2.5-coder:14b is slow to recover");
    expect(s.items).toHaveLength(2);
  });

  it("says plugins wait for a worker when jobs are queued and none runs", () => {
    const m = sample();
    m.nodes.find((n) => n.id === "advisor")!.jobs = { active: 0, queued: 2, waiting: 0 };
    expect(statusLine(buildLevels(m, ctx()).problems).text).toBe("Plugins are waiting for a worker");
  });
});

describe("crossings and ordering", () => {
  const ys = (c: string[][]) => Object.fromEntries(c.flatMap((col) => col.map((id, i) => [id, 100 + i * 100])));

  it("counts lines whose ends are in opposite order", () => {
    const cols = [["a1", "a2"], ["b1", "b2"]];
    expect(crossings(cols, [["a1", "b2"], ["a2", "b1"]], ys(cols))).toBe(1);
    expect(crossings(cols, [["a1", "b1"], ["a2", "b2"]], ys(cols))).toBe(0);
    expect(crossings(cols, [["a1", "b1"], ["a1", "b2"]], ys(cols))).toBe(0);  // lines from one box never cross each other
  });

  it("reorders a column to remove crossings", () => {
    const cols = [["a1", "a2", "a3"], ["b3", "b1", "b2"]];
    const edges: [string, string][] = [["a1", "b1"], ["a2", "b2"], ["a3", "b3"]];
    expect(crossings(cols, edges, ys(cols))).toBeGreaterThan(0);
    const r = orderColumns(cols, edges, { top: 0, bottom: 300 });
    expect(r.crossings).toBe(0);
    for (const i of ["1", "2", "3"]) expect(r.cols[0].indexOf(`a${i}`)).toBe(r.cols[1].indexOf(`b${i}`));  // facing boxes end up side by side
  });

  it("leaves boxes with a fixed height and locked columns where they are", () => {
    const cols = [["a1", "a2"], ["b2", "b1"]];
    const edges: [string, string][] = [["a1", "b1"], ["a2", "b2"]];
    expect(orderColumns(cols, edges, { top: 0, bottom: 200, locked: [1] }).cols[1]).toEqual(["b2", "b1"]);
    const r = orderColumns(cols, edges, { top: 0, bottom: 200, fixed: { a1: 50 } });
    expect(r.ys.a1).toBe(50);
  });
});

describe("layout", () => {
  const opt = { orient: "h" as const, dims: (c: { big?: boolean }) => (c.big ? { w: 196, h: 76 } : { w: 176, h: 54 }), xs: [100, 460, 790, 1055], top: 100, bottom: 540, width: 1190, height: 600, locked: [3] };

  it("puts Argus in the middle of its level, Scheduler above and Approvals and Backups below", () => {
    const b = buildLevels(sample(), ctx());
    const l = layoutLevels(b.cards, b.lines, opt);
    const p = l.placed;
    expect(p.argus.y).toBe(320);
    expect(p.scheduler.y).toBeLessThan(p.argus.y);
    expect(p.scheduler.x).toBe(p.argus.x);
    expect(p.approvals.y).toBeGreaterThan(p.argus.y);
    expect(p.backup.y).toBeGreaterThan(p.argus.y);
    expect(p.approvals.x).toBeLessThan(p.argus.x);
    expect(p.backup.x).toBeGreaterThan(p.argus.x);
  });

  it("keeps the columns in order: apps, Argus, machines, models", () => {
    const b = buildLevels(sample(), ctx());
    const p = layoutLevels(b.cards, b.lines, opt).placed;
    expect(p.phone.x).toBeLessThan(p.argus.x);
    expect(p.argus.x).toBeLessThan(p["worker-pc"].x);
    expect(p["worker-pc"].x).toBeLessThan(p.t1.x);
  });

  it("draws every line edge to edge with a cubic curve and spreads lines that meet one side", () => {
    const b = buildLevels(sample(), ctx());
    const l = layoutLevels(b.cards, b.lines, opt);
    expect(l.routes).toHaveLength(b.lines.length);
    for (const r of l.routes) expect(r.d).toMatch(/^M[\d. -]+ C [\d. ,-]+$/);
    const left = l.routes.filter((r) => r.b === "argus" && ["phone", "helios", "ari"].includes(r.a)).map((r) => r.d.match(/ ([\d.-]+)$/)![1]);
    expect(new Set(left).size).toBe(3);  // three lines end at three different heights on Argus's left side
    const first = l.routes.find((r) => r.a === "helios")!;
    const x0 = Number(first.d.slice(1).split(" ")[0]);
    expect(x0).toBeCloseTo(l.placed.helios.x + 88, 0);  // starts on the box's right edge
  });

  it("has no line crossings for the standard graph", () => {
    const b = buildLevels(sample(), ctx());
    expect(layoutLevels(b.cards, b.lines, opt).crossings).toBe(0);
  });

  it("lays the phone out in rows that fit the width", () => {
    const b = buildLevels(sample(), ctx());
    const l = layoutLevels(b.cards, b.lines, { orient: "v", dims: () => ({ w: 102, h: 74 }), width: 346, perRow: 3, locked: [3] });
    for (const c of Object.values(l.placed)) {
      expect(c.x - c.w / 2).toBeGreaterThanOrEqual(0);
      expect(c.x + c.w / 2).toBeLessThanOrEqual(346);
    }
    expect(l.placed.phone.y).toBeLessThan(l.placed.argus.y);
    expect(l.placed.argus.y).toBeLessThan(l.placed["worker-pc"].y);
    expect(l.placed["worker-pc"].y).toBeLessThan(l.placed.t1.y);
    expect(l.height).toBeGreaterThan(l.placed.v1.y);
  });
});
