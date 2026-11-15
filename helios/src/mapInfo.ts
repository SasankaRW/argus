// What a box says about itself: a one-line summary (the panel's fallback chip) and an icon. Only live data.

import type { MapNode } from "./api";
import type { IconName } from "./icons";
import { ago } from "./format";

export type Selection = { type: "node"; id: string; logs?: boolean } | { type: "edge"; src: string; dst: string } | { type: "job"; id: string } | null;

const KIND_LABEL: Record<string, string> = { core: "core", worker: "worker", plugin: "plugin", model: "model", app: "app", service: "service" };

export function sub(n: MapNode): string {
  if (n.kind === "core") return `core${n.meta?.version ? ` · v${n.meta.version}` : ""}`;
  if (n.kind === "worker") return `${n.state ?? "unknown"}${n.group ? ` · ${n.group}` : ""}`;
  if (n.kind === "plugin") {
    const j = n.jobs ?? { active: 0, queued: 0, waiting: 0 };
    if (!j.active && !j.queued && !j.waiting) return "idle";
    return [j.active ? `${j.active} running` : "", j.queued ? `${j.queued} queued` : "",
      j.waiting ? `${j.waiting} waiting` : ""].filter(Boolean).join(" · ");
  }
  if (n.id === "phone") {
    const m = n.meta as { online?: boolean | null; since?: number | null; device?: string };
    if (m.online === true) return `online · ${m.device ?? "tailscale"}`;
    if (m.online === false) return `offline${m.since ? ` · ${ago(m.since)}` : ""}`;
    return "checking…";
  }
  if (n.id === "power") {
    const m = n.meta as { state?: string; mode?: string; idle_since?: number };
    const sim = m.mode === "simulated" ? " · sim" : "";
    if (m.state === "busy") return `PC busy${sim}`;
    if (m.state === "would_shutdown") return `would shut down${sim}`;
    if (m.state === "idle") return `idle${m.idle_since ? ` ${ago(m.idle_since).replace(/ ago$/, "")}` : ""}${sim}`;
    return `watching${sim}`;
  }
  if (n.id === "approvals") return n.pending ? `${n.pending} waiting for you` : "nothing to decide";
  if (n.id === "scheduler") return "cron · triggers";
  if (n.id === "helios") return "dashboard";
  if (n.id === "ari") return "assistant";
  if (n.id === "backup") return "backups";
  if (n.id === "phone-app") return n.failed ? `${n.failed} not delivered` : n.unsent ? `${n.unsent} waiting for the phone` : "all delivered";
  if (n.kind === "model") {
    const st = n.state === "open" ? "paused" : n.state === "half_open" ? "trying again" : "ready";
    const calls = (n as MapNode & { calls?: number }).calls ?? 0;
    return `${st} · ${calls} call${calls === 1 ? "" : "s"}`;
  }
  return n.implicit ? "seen in events" : KIND_LABEL[n.kind];
}

// The icon a box wears in its panel (the same ones the map draws).
export function iconOf(n: MapNode): IconName {
  if (n.kind === "core") return "hub";
  if (n.kind === "plugin") return "extension";
  if (n.kind === "worker") {
    if (Array.isArray(n.meta?.capabilities) && (n.meta.capabilities as string[]).includes("phone")) return "smartphone";
    return /laptop|calypso/i.test(`${n.id} ${n.group ?? ""}`) ? "laptop_chromebook" : "desktop_windows";
  }
  if (n.kind === "model") return n.meta?.provider === "claude" ? "token" : "neurology";
  return ({ phone: "smartphone", "phone-app": "smartphone", approvals: "pending_actions", scheduler: "schedule", helios: "dashboard", ari: "mic", backup: "dns", power: "power_settings_new" } as Record<string, IconName>)[n.id] ?? "hub";
}
