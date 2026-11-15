import { describe, expect, it } from "vitest";
import type { Job, MapNode } from "./api";
import { avgRun, lastHour } from "./jobsFeed";
import { isDevice, splinePath } from "./layout";
import { pageMeta } from "./kit";
import { sentence } from "./m3";

const job = (over: Partial<Job>): Job => ({
  id: "j", plugin: "p", workflow: "w", state: "succeeded", attempt: 1, max_attempts: 3, lease_owner: null,
  wait_reason: null, input: null, result: null, error: null, created_at: 0, updated_at: 0, finished_at: null, ...over,
});

describe("lastHour", () => {
  it("counts jobs from the last hour into 12 five-minute buckets, newest last", () => {
    const now = 100_000;
    const r = lastHour([
      job({ created_at: now - 10 }),
      job({ created_at: now - 299 }),
      job({ created_at: now - 300 }),
      job({ created_at: now - 3599 }),
      job({ created_at: now - 3600 }), // too old
      job({ created_at: now + 5 }), // in the future
    ], now);
    expect(r.count).toBe(4);
    expect(r.buckets).toHaveLength(12);
    expect(r.buckets[11]).toBe(2);
    expect(r.buckets[10]).toBe(1);
    expect(r.buckets[0]).toBe(1);
  });
});

describe("avgRun", () => {
  it("is null when nothing finished today", () => {
    expect(avgRun([])).toBeNull();
    expect(avgRun([job({ created_at: 1, finished_at: 5 })])).toBeNull(); // before today
  });

  it("averages succeeded jobs that finished today", () => {
    const t = Date.now() / 1000;
    const r = avgRun([
      job({ created_at: t, finished_at: t + 2 }),
      job({ created_at: t, finished_at: t + 4 }),
      job({ created_at: t, finished_at: t + 100, state: "failed" }),
      job({ created_at: t, finished_at: null }),
    ]);
    expect(r).toBeCloseTo(3);
  });
});

describe("splinePath", () => {
  it("is empty for fewer than two points", () => {
    expect(splinePath([])).toBe("");
    expect(splinePath([{ x: 1, y: 1 }])).toBe("");
  });

  it("uses bend points as cubic control points when they fit", () => {
    const p = [{ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 2, y: 5 }, { x: 3, y: 5 }];
    expect(splinePath(p)).toBe("M 0 0 C 1 0 2 5 3 5");
  });

  it("falls back to a smooth curve through the points", () => {
    const d = splinePath([{ x: 0, y: 0 }, { x: 10, y: 0 }]);
    expect(d.startsWith("M 0 0 C ")).toBe(true);
    expect(d.endsWith("10 0")).toBe(true);
  });
});

describe("isDevice", () => {
  const node = (id: string, kind: MapNode["kind"]): MapNode => ({ id, kind, label: id, group: null, meta: {}, first_seen: 0 });
  it("treats workers, apps and the phone as devices", () => {
    expect(isDevice(node("pc", "worker"))).toBe(true);
    expect(isDevice(node("x", "app"))).toBe(true);
    expect(isDevice(node("phone", "service"))).toBe(true);
    expect(isDevice(node("core", "core"))).toBe(false);
  });
});

describe("pageMeta", () => {
  it("looks up the first segment of a view path", () => {
    expect(pageMeta("runs/abc")?.title).toBe("Runs");
    expect(pageMeta("nope")).toBeNull();
  });
});

describe("sentence", () => {
  it("capitalises the first letter and Ari's name", () => {
    expect(sentence("ask ari now")).toBe("Ask Ari now");
    expect(sentence("variable")).toBe("Variable");
  });
});
