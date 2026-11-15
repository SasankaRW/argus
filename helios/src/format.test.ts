import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ago, detail, pretty, shortId, tone, uptime } from "./format";

describe("ago", () => {
  beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(new Date(1_000_000 * 1000)); });
  afterEach(() => vi.useRealTimers());

  it("picks the unit by age", () => {
    const now = 1_000_000;
    expect(ago(now - 2)).toBe("just now");
    expect(ago(now - 30)).toBe("30 s ago");
    expect(ago(now - 300)).toBe("5 min ago");
    expect(ago(now - 7200)).toBe("2 h ago");
    expect(ago(now - 3 * 86400)).toBe("3 d ago");
  });

  it("treats a time in the future as just now", () => {
    expect(ago(1_000_000 + 500)).toBe("just now");
  });
});

describe("uptime", () => {
  it("switches between seconds, minutes, hours and days", () => {
    expect(uptime(90)).toBe("90 s");
    expect(uptime(600)).toBe("10 min");
    expect(uptime(5400)).toBe("90 min");
    expect(uptime(7200)).toBe("2.0 h");
    expect(uptime(3 * 86400)).toBe("3 d");
  });
});

describe("shortId", () => {
  it("keeps the last six characters and tolerates missing ids", () => {
    expect(shortId("job-1234567890")).toBe("567890");
    expect(shortId(null)).toBe("");
    expect(shortId(undefined)).toBe("");
  });
});

describe("tone", () => {
  it("colours event kinds", () => {
    expect(tone("job.succeeded")).toBe("ok");
    expect(tone("job.failed")).toBe("bad");
    expect(tone("job.retry")).toBe("warn");
    expect(tone("step.running")).toBe("dim");
    expect(tone("map.updated")).toBe("flow");
  });
});

describe("detail", () => {
  it("lists known keys in a fixed order and skips empty values", () => {
    expect(detail(null)).toBe("");
    expect(detail({ attempt: 2, title: "Backup", reason: "", by: null, zzz: 1 })).toBe("title=Backup  attempt=2");
  });

  it("writes objects as JSON", () => {
    expect(detail({ from: { a: 1 } })).toBe('from={"a":1}');
  });
});

describe("pretty", () => {
  it("passes strings through and indents everything else", () => {
    expect(pretty(null)).toBe("");
    expect(pretty(undefined)).toBe("");
    expect(pretty("hi")).toBe("hi");
    expect(pretty({ a: 1 })).toBe('{\n  "a": 1\n}');
  });
});
