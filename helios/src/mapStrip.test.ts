import { describe, expect, it } from "vitest";
import type { ArgusEvent } from "./api";
import { stripLine } from "./mapStrip";

const ev = (from: string | null, to: string | null, kind: string, data: Record<string, unknown> | null = null): ArgusEvent =>
  ({ seq: 1, id: "e1", kind, job_id: null, step: null, from, to, data, at: 0 });
const NAMES: Record<string, string> = { "screenshot-renamer": "Screenshot renamer", "worker-laptop": "Calypso", t3: "Claude", watchers: "Watchers" };
const names = (id: string) => NAMES[id] ?? null;

describe("stripLine", () => {
  it("a finished job reads as the plugin's name", () => {
    expect(stripLine(ev("worker-laptop", "screenshot-renamer", "job.succeeded"), names))
      .toEqual({ key: "1", icon: "task_alt", text: "Screenshot renamer finished", tone: "ok" });
  });
  it("a failed job is bad news", () => {
    expect(stripLine(ev("worker-laptop", "screenshot-renamer", "job.dead"), names)).toMatchObject({ text: "Screenshot renamer failed", tone: "bad" });
  });
  it("a wishlist alert reads as the news, without the prefixes", () => {
    const e = ev("watchers", "argus", "notify.queued", { title: "Watcher: Wishlist: Dune book", tags: ["eyes", "wishlist"] });
    expect(stripLine(e, names)).toMatchObject({ icon: "sell", text: "Told your phone: Dune book", tone: "warn" });
  });
  it("other messages are plain", () => {
    expect(stripLine(ev("watchers", "argus", "notify.queued", { title: "Good morning" }), names))
      .toMatchObject({ icon: "notifications", text: "Told your phone: Good morning", tone: "ok" });
  });
  it("machines coming and going", () => {
    expect(stripLine(ev("worker-laptop", null, "worker.offline"), names)).toMatchObject({ text: "Calypso went offline", tone: "bad" });
    expect(stripLine(ev("worker-laptop", null, "worker.online"), names)).toMatchObject({ text: "Calypso came online", tone: "ok" });
  });
  it("never shows a raw id", () => {
    expect(stripLine(ev("worker-zz", null, "worker.online"), names)).toBeNull();
    expect(stripLine(ev("worker-laptop", "nobody", "job.succeeded"), names)).toBeNull();
  });
  it("skips kinds it doesn't know", () => {
    expect(stripLine(ev("a", "b", "edge.added"), names)).toBeNull();
  });
});

describe("approvals", () => {
  it("names the plugin that is waiting, not the approvals service", () => {
    expect(stripLine(ev("screenshot-renamer", "approvals", "approval.requested"), names)).toMatchObject({ text: "Screenshot renamer is waiting for you", tone: "warn" });
  });
});
