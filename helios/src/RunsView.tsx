import { useEffect, useMemo, useState } from "react";
import { api, ArgusEvent, Job } from "./api";
import { ago, shortId, tone } from "./format";
import type { Selection } from "./MapView";

const STATES = ["", "running", "queued", "waiting", "succeeded", "retry", "dead", "cancelled"];
const PAGE = 100;

function took(j: Job): string {
  if (!j.finished_at) return "";
  const s = j.finished_at - j.created_at;
  return s < 60 ? `${s.toFixed(1)} s` : s < 3600 ? `${(s / 60).toFixed(1)} min` : `${(s / 3600).toFixed(1)} h`;
}

// One line saying what a run did, from its result: "3 moved", "dry-run: 2 would move", the error.
function summary(j: Job): string {
  if (j.error) return j.error.split("\n")[0];
  const r = j.result as Record<string, unknown> | null;
  if (!r || typeof r !== "object") return "";
  const n = (k: string) => (Array.isArray(r[k]) ? (r[k] as unknown[]).length : 0);
  if ("would_move" in r) return `dry-run: ${n("would_move")} would move${n("skipped") ? `, ${n("skipped")} skipped` : ""}`;
  if ("moved" in r) return `${n("moved")} moved${n("skipped") ? `, ${n("skipped")} skipped` : ""}`;
  if ("back_to" in r) return `put back: ${String(r.back_to).split(/[\\/]/).pop()}`;
  const keys = Object.keys(r).slice(0, 3);
  return keys.map((k) => `${k}: ${typeof r[k] === "object" ? "…" : String(r[k])}`).join(" · ");
}

export function RunsView({ events, selected, onSelect }: { events: ArgusEvent[]; selected: string | null; onSelect: (s: Selection) => void }) {
  const [plugin, setPlugin] = useState("");
  const [state, setState] = useState("");
  const [plugins, setPlugins] = useState<string[]>([]);
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [more, setMore] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // Refresh when any job changes (job.* events), not on every model message.
  const tick = useMemo(() => events.filter((e) => e.kind.startsWith("job.")).map((e) => e.seq).pop() ?? 0, [events]);

  useEffect(() => {
    api<{ plugins: { id: string }[] }>("/plugins").then((r) => setPlugins(r.plugins.map((p) => p.id))).catch(() => {});
  }, []);

  const query = (before?: number) => {
    const q = new URLSearchParams({ limit: String(PAGE) });
    if (plugin) q.set("plugin", plugin);
    if (state && state !== "running" && state !== "queued") q.set("state", state);
    if (before) q.set("before", String(before));
    return `/jobs?${q}`;
  };
  const keep = (j: Job) => state === "running" ? ["leased", "running"].includes(j.state)
    : state === "queued" ? ["queued", "retry"].includes(j.state) : true;

  useEffect(() => {
    api<Job[]>(query()).then((r) => { setJobs(r.filter(keep)); setMore(r.length === PAGE); setErr(null); })
      .catch((e) => setErr(String(e)));
  }, [plugin, state, tick]); // eslint-disable-line react-hooks/exhaustive-deps

  const older = async () => {
    if (!jobs?.length) return;
    const r = await api<Job[]>(query(jobs[jobs.length - 1].created_at));
    setJobs([...jobs, ...r.filter(keep)]);
    setMore(r.length === PAGE);
  };

  const names = Array.from(new Set([...plugins, ...(jobs ?? []).map((j) => j.plugin)])).sort();
  return (
    <section className="panel runs" aria-label="Runs">
      <div className="ph">
        <span className="pt">Runs</span>
        <select value={plugin} onChange={(e) => setPlugin(e.target.value)} aria-label="Plugin">
          <option value="">All plugins</option>
          {names.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <select value={state} onChange={(e) => setState(e.target.value)} aria-label="State">
          {STATES.map((s) => <option key={s} value={s}>{s || "Any state"}</option>)}
        </select>
        <span className="muted">{jobs ? `${jobs.length}${more ? "+" : ""} runs` : "loading…"}</span>
      </div>
      {err && <div className="tip bad" style={{ padding: "0 18px" }}>{err}</div>}
      <div className="runlist">
        {jobs?.length === 0 && <div className="tip" style={{ padding: "8px 18px" }}>No runs match.</div>}
        {jobs?.map((j) => (
          <button key={j.id} type="button" className="run" aria-current={selected === j.id ? "true" : undefined}
            onClick={() => onSelect({ type: "job", id: j.id })}>
            <span className="t mono">{ago(j.created_at)}</span>
            <span className="w mono">{j.plugin}.<b>{j.workflow}</b></span>
            <span className={`pill ${tone("job." + j.state)}`}>{j.state}</span>
            <span className="d">{summary(j)}</span>
            <span className="t mono">{took(j)}{j.attempt > 1 ? ` · try ${j.attempt}` : ""}</span>
            <span className="t mono hide-sm">{shortId(j.id)}</span>
          </button>
        ))}
        {more && <button type="button" className="btn" style={{ margin: "6px 12px" }} onClick={older}>Older runs</button>}
      </div>
    </section>
  );
}
