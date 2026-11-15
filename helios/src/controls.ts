// Controls for the side panel and the header: which buttons a box gets, which need a hold, the route each one calls
// (docs/controls.md), the log request, and what the panel says about the box. Pure except `run`, which calls the API.

import { api, type MapNode } from "./api";
import type { IconName } from "./icons";
import { sub } from "./mapInfo";

export type Action = "stop" | "start" | "restart" | "pause" | "resume";
export type Btn = { action: Action; label: string; icon: IconName; hold: boolean };
export type Target = { id: string; kind: "plugin" | "worker" | "core" | "other"; paused: boolean; phone: boolean };

// Stopping, pausing and restarting need a 1.2 s hold; starting and resuming are plain taps.
const BTN: Record<Action, Btn> = {
  stop: { action: "stop", label: "Stop", icon: "stop", hold: true },
  start: { action: "start", label: "Start", icon: "play_arrow", hold: false },
  restart: { action: "restart", label: "Restart", icon: "restart_alt", hold: true },
  pause: { action: "pause", label: "Pause", icon: "pause", hold: true },
  resume: { action: "resume", label: "Resume", icon: "play_arrow", hold: false },
};
export const PAUSE_ALL: Btn = { action: "pause", label: "Pause everything", icon: "bolt", hold: true };
export const RESUME_ALL: Btn = { action: "resume", label: "Resume everything", icon: "play_arrow", hold: false };

export function targetOf(n: MapNode): Target {
  const caps = Array.isArray(n.meta?.capabilities) ? (n.meta.capabilities as string[]) : [];
  return { id: n.id, kind: n.kind === "plugin" ? "plugin" : n.kind === "worker" ? "worker" : n.kind === "core" ? "core" : "other", paused: !!n.paused, phone: caps.includes("phone") };
}

// The core has no controls (Logs only); only plugins and workers do. A stopped plugin offers Start first.
export function buttonsFor(t: Target): Btn[] {
  if (t.kind === "plugin") return t.paused ? [BTN.start, BTN.restart] : [BTN.stop, BTN.restart];
  if (t.kind === "worker") {
    const main = t.paused ? BTN.resume : BTN.pause;
    return t.phone ? [main] : [main, BTN.restart];  // the phone app's worker has no process to restart
  }
  return [];
}

export const hasLogs = (t: Target) => t.kind !== "other";
export const logComponent = (t: Target) => (t.kind === "core" ? "argus" : t.id);

export function controlPath(t: Target, a: Action): string {
  return `/${t.kind === "plugin" ? "plugins" : "workers"}/${encodeURIComponent(t.id)}/${a}`;
}
export const allPath = (b: Btn) => (b.action === "pause" ? "/control/pause-all" : "/control/resume-all");

// level is always sent: without it the route answers with the old Logs page's shape
export type LogLevel = "all" | "error";
export const logPath = (id: string, level: LogLevel, lines = 200) => `/logs/${encodeURIComponent(id)}?level=${level}&lines=${lines}`;
export type LogLine = { ts: number; level: "debug" | "info" | "warn" | "error"; source: string; msg: string };

// A tap fires at once; a hold button fires when the hold completes.
export const firesOn = (b: Btn): "tap" | "hold" => (b.hold ? "hold" : "tap");

export type Result = { ok: true; note: string } | { ok: false; error: string };
const NOTE: Record<Action, string> = {
  stop: "Stopped. Its queued jobs wait; the job running now finishes.",
  start: "Started.",
  restart: "Restart asked. It loads its code again.",
  pause: "Paused. It finishes the job it has.",
  resume: "Resumed.",
};

// Calls a control route. A refusal comes back as the server's own text (for example the plugins.off message).
export async function run(path: string, action: Action, call: typeof api = api): Promise<Result> {
  try {
    await call(path, { method: "POST" });
    return { ok: true, note: path.startsWith("/control/") ? (action === "pause" ? "Everything is paused." : "Resumed what was paused.") : NOTE[action] };
  } catch (e) {
    return { ok: false, error: e instanceof Error && e.message ? e.message : "That did not work." };
  }
}

// ---------------------------------------------------------------- what the panel says

export type Chip = { tone: "ok" | "warn" | "bad" | "idle"; text: string };
const hhmm = (t: number) => new Date(t * 1000).toTimeString().slice(0, 5);

export function chipFor(n: MapNode): Chip {
  if (n.kind === "plugin") {
    const j = n.jobs ?? { active: 0, queued: 0, waiting: 0 };
    if (n.paused) return { tone: "bad", text: n.since ? `Stopped since ${hhmm(n.since)}` : "Stopped" };
    if (j.active > 0) return { tone: "ok", text: `Running · ${j.active} job${j.active === 1 ? "" : "s"}` };
    if (j.queued + (j.waiting ?? 0) > 0) return { tone: "warn", text: "Waiting for a worker" };
    return { tone: "idle", text: "Idle" };
  }
  if (n.kind === "worker") {
    if (n.paused) return { tone: "warn", text: n.since ? `Paused since ${hhmm(n.since)}` : "Paused" };
    return n.state === "online" ? { tone: "ok", text: "Online" } : n.state === "offline" ? { tone: "bad", text: "Offline" } : { tone: "warn", text: n.state ?? "Unknown" };
  }
  if (n.kind === "model") return n.state === "open" ? { tone: "bad", text: "Paused" } : n.state === "half_open" ? { tone: "warn", text: "Trying again" } : { tone: "ok", text: "Ready" };
  if (n.kind === "core") return { tone: "ok", text: "Running" };
  if (n.id === "phone") {
    const m = n.meta as { online?: boolean | null };
    return m.online === false ? { tone: "bad", text: "Offline" } : m.online ? { tone: "ok", text: "Online" } : { tone: "idle", text: "Checking" };
  }
  return { tone: "ok", text: sub(n) };
}

export function typeOf(n: MapNode): string {
  if (n.kind === "worker") return Array.isArray(n.meta?.capabilities) && (n.meta.capabilities as string[]).includes("phone") ? "Phone" : "Machine";
  return { plugin: "Plugin", core: "Core", model: "Model", app: "App", service: "Service" }[n.kind];
}

export const clockSec = (ts: number) => new Date(ts * 1000).toTimeString().slice(0, 8);
