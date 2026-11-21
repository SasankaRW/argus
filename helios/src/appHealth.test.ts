import { describe, expect, it } from "vitest";
import type { ArgusMap, MapNode } from "./api";
import { attentionItems } from "./attention";
import { buildLevels, L } from "./mapGraph";

const NOW = 1_000_000;
const node = (id: string, kind: MapNode["kind"], over: Partial<MapNode> = {}): MapNode =>
  ({ id, kind, label: id, group: null, meta: {}, first_seen: 0, ...over });
const app = (id: string, over: Partial<MapNode> = {}) =>
  node(id, "app", { label: id === "app-lifehub" ? "Life Hub" : "Tracker", status: "up", every: 120, ...over });

const build = (nodes: MapNode[]) => {
  const map: ArgusMap = { seq: 1, edges: [], nodes: [node("argus", "core"), node("helios", "app"), node("ari", "app"),
    node("worker-laptop", "worker", { state: "online", group: "calypso" }), ...nodes] };
  return buildLevels(map, { now: NOW, events: [] });
};
const card = (b: ReturnType<typeof build>, id: string) => b.cards.find((c) => c.id === id);
const line = (b: ReturnType<typeof build>, a: string, c: string) => b.lines.find((l) => (l.a === a && l.b === c) || (l.a === c && l.b === a));

describe("your apps (Life Hub, Tracker)", () => {
  it("shows the one line and a quiet line to Argus while it answers", () => {
    const b = build([app("app-lifehub", { line: "2 back in stock" }), app("app-tracker", { line: "7 open issues" })]);
    expect(card(b, "app-lifehub")).toMatchObject({ status: "2 back in stock", tone: "ok", level: L.apps, icon: "home" });
    expect(card(b, "app-tracker")?.status).toBe("7 open issues");
    expect(line(b, "app-lifehub", "argus")?.kind).toBe("quiet");
    expect(b.problems.some((p) => /answering/.test(p.text))).toBe(false);
  });

  it("is broken when down, with the real interval in what it says", () => {
    const since = new Date(2026, 10, 21, 14, 20).getTime() / 1000;
    const b = build([app("app-lifehub", { status: "down", since, error: "refused" })]);
    expect(card(b, "app-lifehub")).toMatchObject({ tone: "bad", status: "not answering", problem: "Life Hub isn't answering",
      detail: "since 14:20 · Argus keeps trying every 2 minutes", top: true });
    expect(line(b, "app-lifehub", "argus")?.kind).toBe("broken");
  });

  it("says every minute when the interval is a minute", () => {
    expect(card(build([app("app-tracker", { status: "down", every: 60 })]), "app-tracker")?.detail).toBe("Argus keeps trying every minute");
  });

  it("has no box without a registry node, and 'checking' is quiet", () => {
    expect(card(build([]), "app-lifehub")).toBeUndefined();
    expect(card(build([app("app-lifehub", { status: "checking" })]), "app-lifehub")).toMatchObject({ tone: "idle", status: "checking" });
  });

  it("tops the attention row when down", () => {
    const b = build([app("app-lifehub", { status: "down", since: NOW }), node("phone-app", "service", { failed: 2 }),
      node("worker-phone", "worker", { state: "online", meta: { capabilities: ["phone"] } })]);
    const items = attentionItems(b.cards, { now: NOW, approvals: { count: 0, oldest: null }, nodeKind: () => "app" });
    expect(items[0]).toMatchObject({ id: "app-lifehub", title: "Life Hub isn't answering" });
    expect(items.map((i) => i.id)).toContain("phone");
  });
});
