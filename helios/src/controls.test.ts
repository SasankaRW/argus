import { describe, expect, it, vi } from "vitest";
import type { MapNode } from "./api";
import { allPath, buttonsFor, chipFor, controlPath, firesOn, hasLogs, logComponent, logPath, PAUSE_ALL, RESUME_ALL, run, targetOf, typeOf } from "./controls";

const node = (id: string, kind: MapNode["kind"], over: Partial<MapNode> = {}): MapNode =>
  ({ id, kind, label: id, group: null, meta: {}, first_seen: 0, ...over });
const names = (n: MapNode) => buttonsFor(targetOf(n)).map((b) => `${b.label}${b.hold ? " (hold)" : ""}`);

describe("which controls a box gets", () => {
  it("lets a running plugin be stopped or restarted, both held", () => {
    expect(names(node("advisor", "plugin"))).toEqual(["Stop (hold)", "Restart (hold)"]);
  });
  it("offers Start first on a stopped plugin, as a plain tap", () => {
    expect(names(node("workstation", "plugin", { paused: true }))).toEqual(["Start", "Restart (hold)"]);
  });
  it("lets a worker be paused (held) or resumed (plain) and restarted (held)", () => {
    expect(names(node("worker-pc", "worker"))).toEqual(["Pause (hold)", "Restart (hold)"]);
    expect(names(node("worker-pc", "worker", { paused: true }))).toEqual(["Resume", "Restart (hold)"]);
  });
  it("does not offer a restart for the phone's worker", () => {
    expect(names(node("worker-phone", "worker", { meta: { capabilities: ["phone"] } }))).toEqual(["Pause (hold)"]);
  });
  it("gives the core and everything else no controls, only the core a log", () => {
    expect(buttonsFor(targetOf(node("argus", "core")))).toEqual([]);
    expect(hasLogs(targetOf(node("argus", "core")))).toBe(true);
    expect(logComponent(targetOf(node("argus", "core")))).toBe("argus");
    expect(buttonsFor(targetOf(node("scheduler", "service")))).toEqual([]);
    expect(hasLogs(targetOf(node("scheduler", "service")))).toBe(false);
  });
});

describe("hold gating", () => {
  it("holds Stop, Restart, Pause and Pause everything; taps Start, Resume and Resume everything", () => {
    const all = [
      ...buttonsFor(targetOf(node("p", "plugin"))), ...buttonsFor(targetOf(node("p", "plugin", { paused: true }))),
      ...buttonsFor(targetOf(node("w", "worker"))), ...buttonsFor(targetOf(node("w", "worker", { paused: true }))), PAUSE_ALL, RESUME_ALL,
    ];
    const held = new Set(all.filter((b) => firesOn(b) === "hold").map((b) => b.label));
    const tapped = new Set(all.filter((b) => firesOn(b) === "tap").map((b) => b.label));
    expect([...held].sort()).toEqual(["Pause", "Pause everything", "Restart", "Stop"]);
    expect([...tapped].sort()).toEqual(["Resume", "Resume everything", "Start"]);
  });
});

describe("routes", () => {
  it("calls the step-1 routes", () => {
    expect(controlPath(targetOf(node("advisor", "plugin")), "stop")).toBe("/plugins/advisor/stop");
    expect(controlPath(targetOf(node("workstation", "plugin")), "start")).toBe("/plugins/workstation/start");
    expect(controlPath(targetOf(node("advisor", "plugin")), "restart")).toBe("/plugins/advisor/restart");
    expect(controlPath(targetOf(node("worker-pc", "worker")), "pause")).toBe("/workers/worker-pc/pause");
    expect(controlPath(targetOf(node("worker-pc", "worker")), "resume")).toBe("/workers/worker-pc/resume");
    expect(controlPath(targetOf(node("worker-pc", "worker")), "restart")).toBe("/workers/worker-pc/restart");
    expect(allPath(PAUSE_ALL)).toBe("/control/pause-all");
    expect(allPath(RESUME_ALL)).toBe("/control/resume-all");
  });
  it("always sends a log level, so the route does not answer with the old Logs page", () => {
    expect(logPath("workstation", "all")).toBe("/logs/workstation?level=all&lines=200");
    expect(logPath("argus", "error", 50)).toBe("/logs/argus?level=error&lines=50");
  });
});

describe("calling a control", () => {
  it("POSTs and says what happened", async () => {
    const call = vi.fn().mockResolvedValue({});
    const r = await run("/plugins/advisor/stop", "stop", call as never);
    expect(call).toHaveBeenCalledWith("/plugins/advisor/stop", { method: "POST" });
    expect(r).toMatchObject({ ok: true });
    expect(await run("/control/pause-all", "pause", call as never)).toEqual({ ok: true, note: "Everything is paused." });
  });
  it("shows the server's own text when a call is refused", async () => {
    const call = vi.fn().mockRejectedValue(new Error("web is switched off in plugins.off in argus.yaml"));
    expect(await run("/plugins/web/start", "start", call as never)).toEqual({ ok: false, error: "web is switched off in plugins.off in argus.yaml" });
  });
  it("falls back to a plain sentence when there is no text", async () => {
    const call = vi.fn().mockRejectedValue(new Error(""));
    expect(await run("/plugins/web/start", "start", call as never)).toEqual({ ok: false, error: "That did not work." });
  });
});

describe("what the panel says", () => {
  it("chips by state", () => {
    expect(chipFor(node("a", "plugin", { paused: true, since: 1 }))).toMatchObject({ tone: "bad" });
    expect(chipFor(node("a", "plugin", { jobs: { active: 2, queued: 0 } }))).toEqual({ tone: "ok", text: "Running · 2 jobs" });
    expect(chipFor(node("a", "plugin", { jobs: { active: 0, queued: 1 } }))).toEqual({ tone: "warn", text: "Waiting for a worker" });
    expect(chipFor(node("a", "plugin", { jobs: { active: 0, queued: 0 } }))).toEqual({ tone: "idle", text: "Idle" });
    expect(chipFor(node("w", "worker", { state: "online" }))).toEqual({ tone: "ok", text: "Online" });
    expect(chipFor(node("w", "worker", { state: "offline" }))).toEqual({ tone: "bad", text: "Offline" });
    expect(chipFor(node("w", "worker", { state: "online", paused: true })).text).toBe("Paused");
    expect(chipFor(node("m", "model", { state: "open" }))).toEqual({ tone: "bad", text: "Paused" });
  });
  it("types", () => {
    expect([typeOf(node("a", "plugin")), typeOf(node("w", "worker")), typeOf(node("x", "core")), typeOf(node("p", "worker", { meta: { capabilities: ["phone"] } }))]).toEqual(["Plugin", "Machine", "Core", "Phone"]);
  });
});
