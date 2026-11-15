// What a box says about itself in the inspector card and the phone's sheet: a one-line summary, an icon, one row of
// numbers. Only live data: counts from the registry, the status and the jobs list. Nothing is made up.

import type { ArgusMap, MapNode } from "./api";
import type { IconName } from "./icons";
import { PLUGINS } from "./mapGraph";
import { ago } from "./format";

export type Selection = { type: "node"; id: string } | { type: "edge"; src: string; dst: string } | { type: "job"; id: string } | null;

const KIND_LABEL: Record<string, string> = { core: "core", worker: "worker", plugin: "plugin", model: "model", app: "app", service: "service" };

export function sub(n: MapNode): string {
  if (n.kind === "core") return `core${n.meta?.version ? ` · v${n.meta.version}` : ""}`;
  if (n.kind === "worker") return `${n.state ?? "unknown"}${n.group ? ` · ${n.group}` : ""}`;
  if (n.id === PLUGINS) {
    const j = n.jobs ?? { active: 0, queued: 0, waiting: 0 };
    const busy = [j.active ? `${j.active} running` : "", j.queued ? `${j.queued} queued` : "", j.waiting ? `${j.waiting} waiting` : ""].filter(Boolean);
    return busy.length ? `${n.meta.count} plugins · ${busy.join(" · ")}` : `${n.meta.count} plugins · idle`;
  }
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

// What a box shows beyond its name: an icon tile (colour by meaning), one key/value row and, where the numbers exist,
// a meter. Only live data: counts from the registry, the status and the jobs list. Nothing is made up.
export type BoxInfo = { icon: IconName; tone: "core" | "ok" | "sec" | "ter" | "err" | "plain"; row?: [string, string]; meter?: number };
export type Ctx = { queue: { running: number; queued: number } | null; byWorker: Record<string, number> };

export function boxInfo(n: MapNode, ctx: Ctx): BoxInfo {
  if (n.kind === "core") {
    const q = ctx.queue;
    return { icon: "dns", tone: "core", row: q ? ["Queue", `${q.running} running · ${q.queued} waiting`] : undefined,
      meter: q && q.running + q.queued > 0 ? q.running / (q.running + q.queued) : undefined };
  }
  if (n.kind === "worker") {
    const run = ctx.byWorker[n.id] ?? ctx.byWorker[n.id.replace(/-now$/, "")] ?? 0;
    return { icon: /pc|desktop|gpu/i.test(n.id + n.label) ? "desktop_windows" : "laptop_chromebook", tone: n.state === "online" ? "ok" : "err",
      row: ["Running", `${run} ${run === 1 ? "job" : "jobs"}`] };
  }
  if (n.kind === "plugin") {
    const j = n.jobs ?? { active: 0, queued: 0, waiting: 0 };
    const all = j.active + j.queued + (j.waiting ?? 0);
    return { icon: "extension", tone: "sec", row: ["Jobs", all ? `${j.active} running · ${j.queued + (j.waiting ?? 0)} waiting` : "none"], meter: all ? j.active / all : undefined };
  }
  if (n.kind === "model") {
    const calls = (n as MapNode & { calls?: number }).calls ?? 0;
    return { icon: "neurology", tone: n.state === "open" ? "err" : "ter", row: ["Calls", String(calls)] };
  }
  if (n.id === "phone" || n.id === "phone-app") return { icon: "smartphone", tone: n.id === "phone" && n.meta?.online === true ? "ok" : "plain", row: n.id === "phone-app" ? ["Waiting", String(n.unsent ?? 0)] : undefined };
  if (n.id === "power") return { icon: "power_settings_new", tone: "plain" };
  if (n.id === "approvals") return { icon: "pending_actions", tone: "ter", row: ["Waiting", String(n.pending ?? 0)] };
  if (n.id === "scheduler") return { icon: "schedule", tone: "sec" };
  if (n.id === "helios") return { icon: "dashboard", tone: "plain" };
  if (n.id === "ari") return { icon: "mic", tone: "plain" };
  if (n.id === "backup") return { icon: "dns", tone: "plain" };
  return { icon: "hub", tone: "plain" };
}

// The inspector and the phone's sheet read the folded map: all plugins are one "Plugins" box (their own page has the
// details), and lines to or from any plugin go to that box.
export function foldPlugins(map: ArgusMap): ArgusMap {
  const plug = new Set(map.nodes.filter((n) => n.kind === "plugin").map((n) => n.id));
  if (!plug.size) return map;
  const to = (id: string) => (plug.has(id) ? PLUGINS : id);
  const ps = map.nodes.filter((n) => plug.has(n.id));
  const sum = (k: "active" | "queued" | "waiting") => ps.reduce((a, n) => a + (n.jobs?.[k] ?? 0), 0);
  const group: MapNode = {
    id: PLUGINS, kind: "plugin", label: "Plugins", group: null, first_seen: Math.min(...ps.map((n) => n.first_seen)),
    meta: { count: ps.length }, jobs: { active: sum("active"), queued: sum("queued"), waiting: sum("waiting") },
  };
  const nodes = [...map.nodes.filter((n) => !plug.has(n.id)), group];
  const edges = map.edges.map((e) => ({ ...e, src: to(e.src), dst: to(e.dst) })).filter((e) => e.src !== e.dst);
  return { ...map, nodes, edges };
}
