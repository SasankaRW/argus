import { useEffect, useMemo, useState } from "react";
import { api, ArgusEvent } from "./api";
import { ago, shortId } from "./format";
import type { Selection } from "./MapView";
import { Empty, Skeleton } from "./kit";

export type QItem = { id: string; plugin: string; workflow: string; state: string; priority: number; attempt: number;
  max_attempts: number; created_at: number; updated_at: number; needs: string[]; model: string | null; window: string | null;
  worker?: string | null; step?: string | null; since?: number; reason?: string | null; position?: number;
  why?: string; until?: number | null; error?: string | null };
export type Queue = { running: QItem[]; queued: QItem[]; waiting: QItem[]; gpu_model: string | null; workers_online: number };

const PRIO: [number, string][] = [[90, "now"], [80, "resumed"], [50, "scheduled"], [0, "batch"]];
export const prio = (p: number) => PRIO.find(([n]) => p >= n)![1];

function inT(t: number): string {
  const s = t - Date.now() / 1000;
  if (s <= 0) return "now";
  if (s < 90) return `in ${Math.round(s)} s`;
  if (s < 5400) return `in ${Math.round(s / 60)} min`;
  return `at ${new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}`;
}

export function whyText(q: QItem): string {
  switch (q.why) {
    case "next": return "next up";
    case "plugin_busy": return `after the running ${q.plugin} job (one at a time)`;
    case "gpu_busy": return "GPU busy: one model job at a time";
    case "window": return `waits for the ${q.window} window, ${q.until ? inT(q.until) : ""}`;
    case "no_worker": return `no worker online that can run it${q.needs.length ? ` (${q.needs.join(", ")})` : ""}`;
    case "retry": return `try ${q.attempt + 1} of ${q.max_attempts} ${q.until ? inT(q.until) : ""}${q.error ? ` · ${q.error.split("\n")[0].slice(0, 80)}` : ""}`;
    case "later": return `starts ${q.until ? inT(q.until) : "later"}`;
    default: return q.why ?? "";
  }
}

export function waitText(q: QItem): string {
  const r = q.reason ?? "";
  return r.startsWith("approval:") ? "your approval" : r || "a resume";
}

export function useQueue(events: ArgusEvent[]) {
  const [q, setQ] = useState<Queue | null>(null);
  const tick = useMemo(() => events.filter((e) => e.kind.startsWith("job.") || e.kind.startsWith("step.")).map((e) => e.seq).pop() ?? 0, [events]);
  const [clock, setClock] = useState(0);
  useEffect(() => { const i = window.setInterval(() => setClock((c) => c + 1), 5000); return () => window.clearInterval(i); }, []);
  useEffect(() => { api<Queue>("/queue").then(setQ).catch(() => {}); }, [tick, clock]);
  return q;
}

function elapsed(t?: number) {
  if (!t) return "";
  const s = Date.now() / 1000 - t;
  return s < 60 ? `${Math.round(s)} s` : s < 3600 ? `${Math.floor(s / 60)} min` : `${(s / 3600).toFixed(1)} h`;
}

// Helios > Queue: what runs now, what waits for you, and what runs next (in order, with the reason it waits).
export function QueueView({ events, onSelect }: { events: ArgusEvent[]; onSelect: (s: Selection) => void }) {
  const q = useQueue(events);
  const [busy, setBusy] = useState<string | null>(null);
  const cancel = async (id: string) => {
    setBusy(id);
    try { await api(`/jobs/${id}/cancel`, { method: "POST", body: JSON.stringify({ reason: "cancelled in Helios" }) }); }
    finally { setBusy(null); }
  };
  if (!q) return <div className="lanes">{[0, 1, 2].map((i) => <Skeleton key={i} rows={3} />)}</div>;
  const card = (j: QItem, right: React.ReactNode, sub: string, cls = "") => (
    <div key={j.id} className={`qcard ${cls}`}>
      <button type="button" className="qmain" onClick={() => onSelect({ type: "job", id: j.id })}>
        <span className="w mono">{j.plugin}.<b>{j.workflow}</b></span>
        <span className="sub">{sub}</span>
      </button>
      <div className="qmeta">
        <span className="pill mono">{prio(j.priority)}</span>
        {right}
      </div>
    </div>
  );
  const lane = (title: string, cls: string, n: number, body: React.ReactNode, empty: string) => (
    <section className={`panel lane ${cls}`} aria-label={title}>
      <div className="lane-h"><i /><span>{title}</span><b className="mono">{n}</b></div>
      <div className="lane-b">{n === 0 ? <Empty title={empty} /> : body}</div>
    </section>
  );
  return (
    <div className="queuepage fillp">
      <div className="chips">
        <span className="chipx"><i className="flow" />GPU <b>{q.gpu_model ?? "idle"}</b></span>
        <span className="chipx"><i className="ok" /><b>{q.workers_online}</b> worker{q.workers_online === 1 ? "" : "s"} online</span>
      </div>
      <div className="lanes">
        {lane("Running", "lane-run", q.running.length, q.running.map((j) => card(j,
          <span className="t mono">{elapsed(j.since)}</span>,
          `${j.worker ?? "?"}${j.step ? ` · ${j.step}` : ""}${j.attempt > 1 ? ` · try ${j.attempt}` : ""}`, "on")), "Nothing running")}
        {lane("Waiting for you", "wait", q.waiting.length, q.waiting.map((j) => card(j,
          <span className="t mono">{ago(j.updated_at)}</span>, `waits for ${waitText(j)}`, "wait")), "Nothing waiting")}
        {lane("Up next", "next", q.queued.length, q.queued.map((j) => card(j,
          <button type="button" className="btn mini" disabled={busy === j.id} onClick={() => cancel(j.id)}>Cancel</button>,
          `#${j.position} · ${whyText(j)} · queued ${ago(j.created_at)} · ${shortId(j.id)}`, j.why === "next" ? "next" : "")), "The queue is empty")}
      </div>
    </div>
  );
}
