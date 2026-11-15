import { describe, expect, it } from "vitest";
import type { ArgusEvent, ArgusMap, MapEdge, MapNode } from "./api";
import { boxOf, buildLevels, crossings, L, labelOf, Layout, layoutLevels, lineKind, orderColumns, workerLabel } from "./mapGraph";

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
      node("worker-laptop", "worker", { state: "online", group: "calypso" }), node("worker-pc", "worker", { state: "online", group: "pc" }),
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
    const models = buildLevels(m, ctx()).cards.filter((c) => c.level === L.models);
    expect(models.map((c) => c.label)).toEqual(["qwen2.5-coder:7b", "qwen2.5-coder:14b", "Claude", "qwen2.5vl:7b"]);
    expect(models[1].status).toBe("T2 · paused");
    expect(models[1].tone).toBe("bad");
  });

  it("counts the plugins that have a running job, and says how many are stopped", () => {
    expect(buildLevels(sample(), ctx()).cards.find((c) => c.id === "plugins")!.status).toBe("1 working");
    const m = sample();
    m.nodes.find((n) => n.id === "notes")!.paused = true;
    expect(buildLevels(m, ctx()).cards.find((c) => c.id === "plugins")!.status).toBe("1 working · 1 stopped");
  });

  it("does not call a stopped plugin's queue a stuck one", () => {
    const m = sample();
    m.nodes.find((n) => n.id === "advisor")!.jobs = { active: 0, queued: 2, waiting: 0 };
    m.nodes.find((n) => n.id === "advisor")!.paused = true;
    expect(buildLevels(m, ctx()).problems).toEqual([]);
  });
});

describe("machines, apps and meters", () => {
  it("shows the laptop as Calypso and any other machine by its host name", () => {
    const m = sample({ nodes: [node("worker-nas", "worker", { state: "online", group: "nas" }), node("worker-x", "worker", { state: "online", group: "Studio-Mac" })] });
    const labels = buildLevels(m, ctx()).cards.filter((c) => c.level === L.machines).map((c) => c.label);
    expect(labels).toEqual(["Calypso", "NAS", "PC", "Studio-Mac"]);
    expect(workerLabel(node("worker-pc", "worker", { group: "pc" }))).toBe("PC");
    expect(workerLabel(node("worker-studio", "worker", {}))).toBe("Studio");
    expect(labelOf(node("worker-phone", "worker", { meta: { capabilities: ["phone"] } }))).toBe("Phone");
  });

  it("draws a machine's meter only when its heartbeat reports a load", () => {
    expect(buildLevels(sample(), ctx()).cards.filter((c) => c.level === L.machines).map((c) => c.meter)).toEqual([undefined, undefined]);
    const m = sample();
    m.nodes.find((n) => n.id === "worker-pc")!.meta = { load: 71 };
    expect(buildLevels(m, ctx()).cards.find((c) => c.id === "worker-pc")!.meter).toBeCloseTo(0.71);
  });

  it("meters the Argus queue and Claude's budget, and nothing else", () => {
    const b = buildLevels(sample(), { now: NOW, events: [], queue: { running: 2, queued: 1 }, claude: { calls_today: 3, calls_per_day: 30 } });
    expect(b.cards.find((c) => c.id === "argus")!.meter).toBeCloseTo(2 / 3);
    const claude = b.cards.find((c) => c.id === "t3")!;
    expect(claude.meter).toBeCloseTo(0.1);
    expect(claude.status).toBe("3 of 30 calls today");
    expect(b.cards.filter((c) => c.meter !== undefined).map((c) => c.id).sort()).toEqual(["argus", "t3"]);
    expect(buildLevels(sample(), ctx()).cards.some((c) => c.meter !== undefined)).toBe(false);
  });

  it("shows a paused machine as paused, not as a problem", () => {
    const m = sample();
    m.nodes.find((n) => n.id === "worker-pc")!.paused = true;
    const b = buildLevels(m, ctx());
    expect(b.cards.find((c) => c.id === "worker-pc")!.status).toBe("paused");
    expect(b.problems).toEqual([]);
    expect(line(b, "worker-pc", "argus")?.kind).toBe("quiet");
  });

  it("puts the other apps after the phone, Helios and Ari", () => {
    const m = sample({ nodes: [node("lifehub", "app", { label: "Life Hub" }), node("tracker", "app", { label: "Tracker" })] });
    expect(buildLevels(m, ctx()).cards.filter((c) => c.level === L.apps).map((c) => c.id)).toEqual(["phone", "helios", "ari", "lifehub", "tracker"]);
  });
});

describe("one machine, several workers", () => {
  it("shows two workers on the same host as one box, following the one that is online", () => {
    const m = sample({ nodes: [node("worker-saspc", "worker", { state: "online", group: "SasPC" }), node("desktop-saspc", "worker", { state: "offline", group: "SasPC" })], edges: [edge("desktop-saspc", "argus", { count: 4 })] });
    const b = buildLevels(m, ctx());
    const pcs = b.cards.filter((c) => c.label === "SasPC");
    expect(pcs.map((c) => c.id)).toEqual(["worker-saspc"]);
    expect(pcs[0].tone).toBe("ok");
    expect(pcs[0].status).toContain("1 offline");
    expect(b.problems.some((p) => /SasPC/.test(p.text))).toBe(false);
    expect(boxOf(m)("desktop-saspc")).toBe("worker-saspc");
  });

  it("follows the offline one when none is online, so the box still says so", () => {
    const m = sample({ nodes: [node("worker-saspc", "worker", { state: "offline", group: "SasPC" }), node("desktop-saspc", "worker", { state: "offline", group: "SasPC" })] });
    const b = buildLevels(m, ctx());
    expect(b.cards.filter((c) => c.label === "SasPC")).toHaveLength(1);
    expect(b.problems.some((p) => /SasPC is offline/.test(p.text))).toBe(true);
  });
});

describe("line health", () => {
  it("ranks broken over slow over busy over quiet", () => {
    expect(lineKind({ tone: "bad", recent: 9 })).toBe("broken");
    expect(lineKind({ tone: "warn", recent: 9 })).toBe("slow");
    expect(lineKind({ tone: "ok", recent: 1 })).toBe("busy");
    expect(lineKind({ tone: "ok", recent: 0 })).toBe("quiet");
    expect(lineKind({ tone: "idle", recent: 0 })).toBe("quiet");
  });

  it("is busy only when there was traffic in the last minute", () => {
    const fresh = buildLevels(sample({ edges: [edge("helios", "argus")] }), ctx([ev("helios", "argus", 10)]));
    expect(line(fresh, "helios", "argus")?.kind).toBe("busy");
    const stale = buildLevels(sample({ edges: [edge("helios", "argus")] }), ctx([ev("helios", "argus", 120)]));
    expect(line(stale, "helios", "argus")?.kind).toBe("quiet");  // connected but quiet: plain grey, never green
    expect(line(buildLevels(sample(), ctx([ev("helios", "argus", 59)])), "helios", "argus")?.kind).toBe("busy");
    expect(line(buildLevels(sample(), ctx([ev("helios", "argus", 61)])), "helios", "argus")?.kind).toBe("quiet");
  });

  it("lights a line only for the traffic on that connection", () => {
    const b = buildLevels(sample(), ctx([ev("worker-pc", "advisor", 3), ev("advisor", "t1", 4)]));
    expect(line(b, "worker-pc", "plugins")?.kind).toBe("busy");
    expect(line(b, "worker-laptop", "plugins")?.kind).toBe("quiet");
    expect(line(b, "plugins", "t1")?.kind).toBe("busy");
    expect(line(b, "plugins", "t2")?.kind).toBe("quiet");
    expect(line(b, "worker-laptop", "argus")?.kind).toBe("quiet");
  });

  it("counts traffic to a plugin as traffic on the Plugins box", () => {
    const b = buildLevels(sample(), ctx([ev("scheduler", "advisor", 3)]));
    expect(line(b, "scheduler", "argus")?.kind).toBe("busy");
    expect(line(b, "plugins", "argus")).toBeUndefined();  // Plugins joins the machines and the models, not Argus itself
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
    expect(line(b, "worker-pc", "plugins")?.kind).toBe("broken");
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

describe("problems", () => {
  it("names what is wrong, broken first", () => {
    const m = sample();
    m.nodes.find((n) => n.id === "t2")!.state = "half_open";
    m.nodes.find((n) => n.id === "phone")!.meta = { online: false };
    expect(buildLevels(m, ctx()).problems.map((p) => p.text)).toEqual(["Phone is offline", "qwen2.5-coder:14b is slow to recover"]);
  });

  it("says plugins wait for a worker when jobs are queued and none runs", () => {
    const m = sample();
    m.nodes.find((n) => n.id === "advisor")!.jobs = { active: 0, queued: 2, waiting: 0 };
    expect(buildLevels(m, ctx()).problems.map((p) => p.text)).toEqual(["Plugins are waiting for a worker"]);
  });

  it("has none when everything is fine", () => {
    expect(buildLevels(sample(), ctx()).problems).toEqual([]);
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
    expect(orderColumns(cols, edges, { top: 0, bottom: 200, fixed: { a1: 50 } }).ys.a1).toBe(50);
  });
});

// Geometry: sample every route as a polyline and count the pairs of lines that cross each other.
type Pt = [number, number];
function pts(d: string): Pt[] {
  const [x0, y0, x1, y1, x2, y2, x3, y3] = d.match(/-?[\d.]+/g)!.map(Number);
  return Array.from({ length: 31 }, (_, i) => {
    const t = i / 30, u = 1 - t;
    return [u * u * u * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t * t * t * x3, u * u * u * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y3];
  });
}
const orient = (a: Pt, b: Pt, c: Pt) => (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
const cuts = (p: Pt, q: Pt, r: Pt, s: Pt) => orient(p, q, r) * orient(p, q, s) < 0 && orient(r, s, p) * orient(r, s, q) < 0;
function lineCrossings(l: Layout): number {
  const P = l.routes.map((r) => pts(r.d));
  let n = 0;
  for (let i = 0; i < P.length; i++) {
    for (let j = i + 1; j < P.length; j++) {
      let hit = false;
      for (let a = 0; a < 30 && !hit; a++) for (let b = 0; b < 30 && !hit; b++) hit = cuts(P[i][a], P[i][a + 1], P[j][b], P[j][b + 1]);
      if (hit) n++;
    }
  }
  return n;
}

describe("layout", () => {
  const dims = (c: { big?: boolean; meter?: number; skin?: string }) => (c.big ? { w: 196, h: c.meter !== undefined ? 86 : 76 } : { w: c.skin === "model" ? 196 : 176, h: c.meter !== undefined ? 68 : 54 });
  const opt = { orient: "h" as const, dims, xs: [100, 385, 615, 850, 1100], width: 1200, height: 600, locked: [L.models], compact: [L.models, L.plugins] };
  const lay = (m = sample(), c = ctx()) => { const b = buildLevels(m, c); return layoutLevels(b.cards, b.lines, opt); };

  it("puts Argus in the middle of its level, Scheduler above and Approvals and Backups below", () => {
    const p = lay().placed;
    expect(p.argus.y).toBe(300);
    expect(p.scheduler.y).toBeLessThan(p.argus.y);
    expect(p.scheduler.x).toBe(p.argus.x);
    expect(p.approvals.y).toBeGreaterThan(p.argus.y);
    expect(p.backup.y).toBeGreaterThan(p.argus.y);
    expect(p.approvals.x).toBeLessThan(p.argus.x);
    expect(p.backup.x).toBeGreaterThan(p.argus.x);
  });

  it("keeps the columns in order: apps, Argus, machines, Plugins, models", () => {
    const p = lay().placed;
    const xs = [p.phone.x, p.argus.x, p["worker-pc"].x, p.plugins.x, p.t1.x];
    expect([...xs].sort((a, b) => a - b)).toEqual(xs);
    expect(p["worker-laptop"].y).toBeLessThan(p["worker-pc"].y);  // Calypso above PC
    expect(p.plugins.y).toBe(300);
    expect(p.t1.y).toBeLessThan(p.t3.y);  // the models keep the escalation order
  });

  it("draws every line edge to edge with a cubic curve and spreads lines that meet one side", () => {
    const b = buildLevels(sample(), ctx());
    const l = layoutLevels(b.cards, b.lines, opt);
    expect(l.routes).toHaveLength(b.lines.length);
    for (const r of l.routes) expect(r.d).toMatch(/^M[\d. -]+ C [\d. ,-]+$/);
    const left = l.routes.filter((r) => r.b === "argus" && ["phone", "helios", "ari"].includes(r.a)).map((r) => r.d.match(/ ([\d.-]+)$/)![1]);
    expect(new Set(left).size).toBe(3);  // three lines end at three different heights on Argus's left side
    const first = l.routes.find((r) => r.a === "helios")!;
    expect(Number(first.d.slice(1).split(" ")[0])).toBeCloseTo(l.placed.helios.x + 88, 0);  // starts on the box's right edge
  });

  it("has no line crossings for the standard shape: two machines, Plugins, local models and Claude", () => {
    const l = lay();
    expect(l.crossings).toBe(0);
    expect(lineCrossings(l)).toBe(0);
  });

  it("stays free of crossings with all five apps and the phone's outbox lines", () => {
    const m = sample({ nodes: [node("lifehub", "app"), node("tracker", "app"), node("phone-app", "service", { unsent: 1 })] });
    expect(lineCrossings(lay(m))).toBe(0);
  });

  it("stacks more machines and models without overlap, growing the canvas when needed", () => {
    const hosts = ["a", "b", "c", "d", "e"].map((h) => node(`worker-${h}`, "worker", { state: "online", group: h }));
    const m = sample({ nodes: [...hosts, node("t4", "model", { state: "closed", meta: { tier: "T4", provider: "ollama", model: "x" } })] });
    const l = lay(m);
    const col = (lv: number) => Object.values(l.placed).filter((c) => c.level === lv).sort((a, b) => a.y - b.y);
    for (const lv of [L.machines, L.models]) {
      const c = col(lv);
      for (let i = 1; i < c.length; i++) expect(c[i].y - c[i].h / 2).toBeGreaterThanOrEqual(c[i - 1].y + c[i - 1].h / 2);
    }
    expect(col(L.machines)).toHaveLength(7);
    expect(l.height).toBeGreaterThanOrEqual(7 * 64 + 120);
    for (const c of Object.values(l.placed)) expect(c.y + c.h / 2).toBeLessThanOrEqual(l.height);
  });

  it("lays the phone out in rows that fit the width", () => {
    const b = buildLevels(sample(), ctx());
    const l = layoutLevels(b.cards, b.lines, { orient: "v", dims: () => ({ w: 102, h: 74 }), width: 346, perRow: 3, locked: [L.models] });
    for (const c of Object.values(l.placed)) {
      expect(c.x - c.w / 2).toBeGreaterThanOrEqual(0);
      expect(c.x + c.w / 2).toBeLessThanOrEqual(346);
    }
    expect(l.placed.phone.y).toBeLessThan(l.placed.argus.y);
    expect(l.placed.argus.y).toBeLessThan(l.placed["worker-pc"].y);
    expect(l.placed["worker-pc"].y).toBeLessThan(l.placed.plugins.y);
    expect(l.placed.plugins.y).toBeLessThan(l.placed.t1.y);
    expect(l.height).toBeGreaterThan(l.placed.v1.y);
  });
});
