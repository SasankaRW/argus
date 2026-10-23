import { useEffect, useMemo, useState } from "react";
import { api, ArgusEvent, Job } from "./api";
import { ago, shortId, tone } from "./format";
import type { Selection } from "./MapView";
import { Empty } from "./kit";

const STATES: [string, string][] = [["", "All"], ["running", "Running"], ["queued", "Queued"], ["waiting", "Waiting"], ["succeeded", "Done"], ["retry", "Retry"], ["dead", "Failed"], ["cancelled", "Cancelled"]];
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
  if (Array.isArray(r.trace)) {  // an app or browser task: how many steps, how many did nothing, how it ended
    const t = r.trace as { kind: string; worked?: boolean; changed?: boolean | null }[];
    const acts = t.filter((x) => x.kind === "act");
    const idle = acts.filter((x) => !x.worked || x.changed === false).length;
    const end = typeof r.answer === "string" ? r.answer : typeof r.problem === "string" ? r.problem : "";
    return `${acts.length} steps${idle ? `, ${idle} did nothing` : ""}${end ? ` · ${end}` : ""}`;
  }
  if (typeof r.say === "string") return r.say;  // a tool's own sentence (watchers, the tools test)
  if (typeof r.answer === "string") return r.answer;
  const show = (v: unknown) => Array.isArray(v) ? String(v.length) : v && typeof v === "object" ? "…" : String(v);
  const keys = Object.keys(r).slice(0, 3);
  return keys.map((k) => `${k}: ${show(r[k])}`).join(" · ");
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
    <section className="panel runs fillp" aria-label="Runs">
      <div className="toolbar">
        <div className="seg" role="group" aria-label="State">
          {STATES.map(([v, l]) => <button key={v} type="button" aria-pressed={state === v} onClick={() => setState(v)}>{l}</button>)}
        </div>
        <select value={plugin} onChange={(e) => setPlugin(e.target.value)} aria-label="Plugin">
          <option value="">All plugins</option>
          {names.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <span className="count mono">{jobs ? `${jobs.length}${more ? "+" : ""} runs` : "loading…"}</span>
      </div>
      {err && <div className="tip bad" style={{ padding: "8px 18px" }}>{err}</div>}
      <div className="thead run" aria-hidden="true">
        <span>when</span><span>run</span><span>state</span><span>result</span><span>took</span><span className="hide-sm">id</span>
      </div>
      <div className="runlist">
        {jobs === null && Array.from({ length: 8 }, (_, i) => <div key={i} className="skel-row" />)}
        {jobs?.length === 0 && <Empty title="No runs match" hint="Try another state or plugin." />}
        {jobs?.map((j) => (
          <button key={j.id} type="button" className={`run st-${tone("job." + j.state) || "idle"}`} aria-current={selected === j.id ? "true" : undefined}
            onClick={() => onSelect({ type: "job", id: j.id })}>
            <span className="t mono">{ago(j.created_at)}</span>
            <span className="w mono">{j.plugin}.<b>{j.workflow}</b></span>
            <span className={`pill ${tone("job." + j.state)}`}>{j.state}</span>
            <span className="d">{summary(j)}</span>
            <span className="t mono">{took(j)}{j.attempt > 1 ? ` · try ${j.attempt}` : ""}</span>
            <span className="t mono hide-sm">{shortId(j.id)}</span>
          </button>
        ))}
        {more && <button type="button" className="btn more" onClick={older}>Older runs</button>}
      </div>
    </section>
  );
}
