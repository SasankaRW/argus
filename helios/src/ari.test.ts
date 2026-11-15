import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// voice.ts reads `window` when it loads, and ariState.ts talks to it, so give both a minimal one.
beforeEach(() => {
  vi.resetModules();
  vi.stubGlobal("window", { dispatchEvent: () => true });
  vi.stubGlobal("localStorage", { getItem: () => null, setItem() {}, removeItem() {} });
});
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

describe("findWake", () => {
  it("returns what follows the wake phrase", async () => {
    const { findWake } = await import("./voice");
    expect(findWake("hey ari what is the weather")).toBe("what is the weather");
    expect(findWake("Ok Siri, turn it up")).toBe("turn it up");
    expect(findWake("Argus status please")).toBe("status please");
  });

  it("returns null when nobody was addressed", async () => {
    const { findWake } = await import("./voice");
    expect(findWake("the weather is nice")).toBeNull();
  });
});

describe("ariFromEvents", () => {
  const ev = (kind: string, data: Record<string, unknown> | null, job_id: string | null = null, step: string | null = null) =>
    ({ kind, data, job_id, step });

  it("follows ari.state events and strips mood tags", async () => {
    const { ariFromEvents, ariNow } = await import("./ariState");
    ariFromEvents([ev("ari.state", { phase: "thinking", text: "" }, "j1")]);
    expect(ariNow().phase).toBe("thinking");
    expect(ariNow().remote).toBe(true);
    ariFromEvents([ev("ari.state", { phase: "speaking", text: "[cheerful] Hello!" }, "j1")]);
    expect(ariNow()).toMatchObject({ phase: "speaking", text: "Hello!" });
  });

  it("names the tool being used while the answer is worked on", async () => {
    const { ariFromEvents, ariNow } = await import("./ariState");
    ariFromEvents([
      ev("ari.state", { phase: "thinking" }, "j2"),
      ev("step.running", null, "j2", "tool 1: web_search"),
    ]);
    expect(ariNow()).toMatchObject({ phase: "working", text: "searching the web" });
    ariFromEvents([ev("step.running", null, "j2", "tool 2: some_new_tool")]);
    expect(ariNow().text).toBe("some new tool");
    ariFromEvents([ev("step.running", null, "other", "tool 3: weather")]);
    expect(ariNow().text).toBe("some new tool"); // another job: ignored
  });

  it("ariEnd only ends the phase that is still showing", async () => {
    const { ariSet, ariEnd, ariNow } = await import("./ariState");
    ariSet("listening");
    ariEnd("thinking");
    expect(ariNow().phase).toBe("listening");
    ariEnd("listening");
    expect(ariNow().phase).toBe("idle");
  });

  it("folds a finished answer away after a moment", async () => {
    vi.useFakeTimers();
    const { ariSet, ariNow } = await import("./ariState");
    ariSet("done", "ok");
    expect(ariNow().phase).toBe("done");
    vi.advanceTimersByTime(10_000);
    expect(ariNow().phase).toBe("idle");
  });
});
