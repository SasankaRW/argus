import { useCallback, useEffect, useMemo, useState } from "react";
import { api, Job } from "./api";
import { ago, tone } from "./format";
import type { Selection } from "./MapView";

type PluginInfo = { id: string; name: string; description: string; live: boolean; runs_on: string; workflows: string[];
  rules: string | null; ari_tools?: string[]; group: string | null };
type Setting = { name: string; type: string; label: string; choices: string[] | null; default: unknown; value: unknown;
  source: "you" | "argus.yaml" | "default" };
type Lesson = { id: number; text: string; state: "active" | "proposed"; evals: { before?: Score | null; after?: Score | null } | null };
type Score = { passed: number; total: number };
type Playbook = { key: string; name: string; samples: number; escalated: number; correct: number; wrong: number;
  to_review: number; lessons: Lesson[] };
type Detail = {
  id: string; name: string; version: string; description: string; runs_on: string; needs: string[]; live: boolean;
  live_from: string; group: string | null; buttons: { workflow: string; label: string }[];
  schedules: { id: string; cron: string; workflow: string; enabled: boolean; next_run_at: number | null; last_run_at: number | null }[];
  watches: { workflow: string; paths: string[] }[]; share: { label: string; accepts: string[] }[];
  tools: { name: string; description: string; risky: boolean }[]; rules: string | null; wrong: string | null;
  permissions: { files: { read: string[]; write: string[]; delete: string }; models: string[]; network: string[] };
  settings: Setting[]; runs: Job[]; saved: { seconds: number; jobs: number }; learning: Playbook[];
};

const GROUPS: [string, string][] = [["files", "Files"], ["pc", "PC control"], ["assistant", "Assistant"], ["", "Other"]];
const TABS = ["Overview", "Settings", "Runs", "Learning"] as const;
type Tab = typeof TABS[number];

function mins(s: number) { const m = Math.round(s / 60); return m < 1 ? "—" : m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`; }
function when(t: number | null) { return t ? new Date(t * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }) : "—"; }

// Every plugin with its own controls: what it does, its buttons, live or dry-run, settings, runs, and what its
// models learned. Clean list on the left, one plugin on the right.
export function PluginsView({ onSelect, selected, onPick, phone = false }: { onSelect: (s: Selection) => void; selected: string | null; onPick: (id: string | null) => void; phone?: boolean }) {
  const [list, setList] = useState<PluginInfo[] | null>(null);
  const [errors, setErrors] = useState<{ folder: string; error: string }[]>([]);
  const [q, setQ] = useState("");
  const load = useCallback(() => {
    api<{ plugins: PluginInfo[]; errors: { folder: string; error: string }[] }>("/plugins").then((r) => {
      setList([...r.plugins].sort((a, b) => a.name.localeCompare(b.name))); setErrors(r.errors);
    }).catch(() => setList([]));
  }, []);
  useEffect(load, [load]);
  useEffect(() => { if (!phone && !selected && list && list.length) onPick(list[0].id); }, [list, selected, onPick, phone]);  // the phone starts on the list

  const shown = (list ?? []).filter((p) => !q || `${p.name} ${p.description} ${p.id}`.toLowerCase().includes(q.toLowerCase()));
  return (
    <section className={`panel plugins${phone ? " phone" : ""}${phone && selected ? " has-sel" : ""}`} aria-label="Plugins">
      <div className="plist">
        <input className="psearch" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Find a plugin…" aria-label="Find a plugin" />
        {list === null && <div className="tip">Loading…</div>}
        {GROUPS.map(([g, label]) => {
          const inGroup = shown.filter((p) => (GROUPS.some(([x]) => x && x === p.group) ? p.group : "") === g);
          if (inGroup.length === 0) return null;
          return (
            <div key={g} className="pgroup">
              <div className="pgroup-h">{label}</div>
              {inGroup.map((p) => (
                <button key={p.id} type="button" className="prow" aria-current={p.id === selected ? "true" : undefined} onClick={() => onPick(p.id)}>
                  <span className="prow-name">{p.name}</span>
                  <span className={`pchip ${p.live ? "live" : "dry"}`}>{p.live ? "Live" : "Dry-run"}</span>
                  <span className="prow-desc">{p.description}</span>
                </button>
              ))}
            </div>
          );
        })}
        {errors.length > 0 && (
          <div className="pgroup">
            <div className="pgroup-h bad">Not loaded</div>
            {errors.map((e) => <div key={e.folder} className="perr"><b className="mono">{e.folder.split(/[\\/]/).pop()}</b><span>{e.error}</span></div>)}
          </div>
        )}
      </div>
      <div className="pdetail">
        {phone && selected && <button type="button" className="pback" onClick={() => onPick(null)}>‹ All plugins</button>}
        {selected ? <PluginDetail id={selected} onSelect={onSelect} onChanged={load} /> : <div className="tip">Pick a plugin.</div>}
      </div>
    </section>
  );
}

function PluginDetail({ id, onSelect, onChanged }: { id: string; onSelect: (s: Selection) => void; onChanged: () => void }) {
  const [d, setD] = useState<Detail | null>(null);
  const [tab, setTab] = useState<Tab>("Overview");
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(() => api<Detail>(`/plugins/${id}`).then(setD).catch((e) => setErr(String(e))), [id]);
  useEffect(() => { setD(null); setMsg(null); setErr(null); setTab("Overview"); load(); }, [id, load]);
  if (!d) return err ? <div className="tip bad">{err}</div> : <div className="tip">Loading…</div>;

  const run = async (workflow: string, label: string) => {
    setErr(null);
    try {
      const r = await api<{ id: string }>(`/plugins/${id}/run`, { method: "POST", body: JSON.stringify({ workflow, input: {} }) });
      setMsg(`${label}: started.`); onSelect({ type: "job", id: r.id });
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  const setLive = async (live: boolean | null) => {
    if (live && !window.confirm(`Let ${d.name} really change things? (Dry-run only shows what it would do.)`)) return;
    try {
      await api(`/plugins/${id}/settings`, { method: "PUT", body: JSON.stringify({ live }) });
      await load(); onChanged(); setMsg(live ? "Live: it changes things from the next job on." : "Dry-run: it only shows what it would do.");
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  const toReview = d.learning.reduce((a, p) => a + p.to_review + p.lessons.filter((l) => l.state === "proposed").length, 0);

  return (
    <>
      <div className="pd-head">
        <div className="pd-title">
          <h2>{d.name}</h2>
          <span className="muted mono">{d.id} · v{d.version}</span>
        </div>
        <p className="pd-desc">{d.description}</p>
        <div className="pd-mode" role="group" aria-label="Mode">
          <button type="button" className={`seg ${!d.live ? "on" : ""}`} onClick={() => d.live && setLive(false)}>Dry-run</button>
          <button type="button" className={`seg ${d.live ? "on" : ""}`} onClick={() => !d.live && setLive(true)}>Live</button>
          {d.live_from === "you" && <button type="button" className="linkbtn" onClick={() => setLive(null)} title="Back to plugins.live in argus.yaml">use argus.yaml</button>}
          <span className="grow" />
          {d.saved.seconds > 0 && <span className="pd-saved">saved you <b>{mins(d.saved.seconds)}</b> this week</span>}
        </div>
        {d.buttons.length > 0 && (
          <div className="pd-buttons">
            {d.buttons.map((b) => <button key={b.workflow} type="button" className="primary" onClick={() => run(b.workflow, b.label)}>{b.label}</button>)}
            {d.rules && <button type="button" className="btn" onClick={() => { location.hash = `rules/${encodeURIComponent(d.id)}`; }}>Edit {d.rules.toLowerCase()}</button>}
          </div>
        )}
        {msg && <div className="tip">{msg}</div>}
        {err && <div className="tip bad">{err}</div>}
      </div>
      <div className="tabs" role="tablist">
        {TABS.map((t) => (
          <button key={t} type="button" role="tab" aria-selected={tab === t} className={`tab ${tab === t ? "on" : ""}`} onClick={() => setTab(t)}>
            {t}{t === "Settings" && d.settings.length ? ` (${d.settings.length})` : ""}{t === "Learning" && toReview ? <span className="dotbadge">{toReview}</span> : null}
          </button>
        ))}
      </div>
      <div className="pd-body">
        {tab === "Overview" && <Overview d={d} />}
        {tab === "Settings" && <Settings d={d} onSaved={() => { load(); onChanged(); setMsg("Saved: used from the next job on."); }} />}
        {tab === "Runs" && <Runs runs={d.runs} onSelect={onSelect} />}
        {tab === "Learning" && <Learning d={d} reload={load} />}
      </div>
    </>
  );
}

function Overview({ d }: { d: Detail }) {
  const perms = d.permissions;
  return (
    <div className="pd-grid">
      <div className="card">
        <h4>When it runs</h4>
        {d.buttons.length === 0 && d.schedules.length === 0 && d.watches.length === 0 && d.tools.length > 0 && <p className="muted">When Ari needs it.</p>}
        {d.schedules.map((s) => <p key={s.id}><span className="mono">{s.cron}</span> · {s.workflow} · next {when(s.next_run_at)}</p>)}
        {d.watches.map((w, i) => <p key={i}>New files in {w.paths.join(", ")}</p>)}
        {d.buttons.map((b) => <p key={b.workflow}>“{b.label}” button</p>)}
        {d.share.map((s, i) => <p key={i}>Phone's share menu: {s.label}</p>)}
      </div>
      {d.tools.length > 0 && (
        <div className="card">
          <h4>What Ari can ask it</h4>
          {d.tools.map((t) => <p key={t.name}><b>{t.name.replace(/_/g, " ")}</b>{t.risky && <span className="pchip dry">asks you first</span>}<br /><span className="muted">{t.description}</span></p>)}
        </div>
      )}
      <div className="card">
        <h4>What it may touch</h4>
        {perms.files.read.length > 0 && <p>Reads: {perms.files.read.join(", ")}</p>}
        {perms.files.write.length > 0 && <p>Changes: {perms.files.write.join(", ")}</p>}
        <p>Deletes: {perms.files.delete === "recycle_bin" ? "to the Recycle Bin only" : "never"}</p>
        {perms.models.length > 0 && <p>Models: {perms.models.join(", ")}</p>}
        {perms.network.length > 0 && <p>Internet: {perms.network.join(", ")}</p>}
        <p className="muted">Runs on: {d.runs_on === "desktop" ? "the PC" : d.runs_on}{d.needs.includes("session") ? " (your Windows session)" : ""}</p>
      </div>
    </div>
  );
}

function Settings({ d, onSaved }: { d: Detail; onSaved: () => void }) {
  const initial = useMemo(() => Object.fromEntries(d.settings.map((s) => [s.name, s.value])), [d]);
  const [vals, setVals] = useState<Record<string, unknown>>(initial);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => setVals(initial), [initial]);
  if (d.settings.length === 0) return <p className="muted">This plugin has no settings.</p>;
  const changed = d.settings.filter((s) => JSON.stringify(vals[s.name]) !== JSON.stringify(s.value));
  const save = async (config: Record<string, unknown>) => {
    setErr(null);
    try { await api(`/plugins/${d.id}/settings`, { method: "PUT", body: JSON.stringify({ config }) }); onSaved(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  return (
    <form className="settings" onSubmit={(e) => { e.preventDefault(); save(Object.fromEntries(changed.map((s) => [s.name, vals[s.name]]))); }}>
      {d.settings.map((s) => (
        <label key={s.name} className="setting">
          <span className="setting-l">{s.label}{s.source !== "default" && <span className="muted"> · set {s.source === "you" ? "here" : "in argus.yaml"}</span>}</span>
          {s.type === "bool" ? (
            <input type="checkbox" checked={!!vals[s.name]} onChange={(e) => setVals({ ...vals, [s.name]: e.target.checked })} />
          ) : s.type === "choice" ? (
            <select value={String(vals[s.name] ?? "")} onChange={(e) => setVals({ ...vals, [s.name]: e.target.value })}>
              {(s.choices ?? []).map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          ) : s.type === "list" ? (
            <textarea rows={Math.min(6, Math.max(2, ((vals[s.name] as string[]) ?? []).length + 1))} value={((vals[s.name] as string[]) ?? []).join("\n")}
              onChange={(e) => setVals({ ...vals, [s.name]: e.target.value.split("\n") })} />
          ) : (
            <input type={s.type === "int" ? "number" : "text"} value={String(vals[s.name] ?? "")} onChange={(e) => setVals({ ...vals, [s.name]: e.target.value })} />
          )}
          {s.source === "you" && <button type="button" className="linkbtn" onClick={() => save({ [s.name]: null })}>reset</button>}
        </label>
      ))}
      {err && <div className="tip bad">{err}</div>}
      <div className="appr-actions">
        <button type="button" className="btn" disabled={!changed.length} onClick={() => setVals(initial)}>Discard</button>
        <button type="submit" className="primary" disabled={!changed.length}>Save{changed.length ? ` (${changed.length})` : ""}</button>
      </div>
    </form>
  );
}

function Runs({ runs, onSelect }: { runs: Job[]; onSelect: (s: Selection) => void }) {
  if (runs.length === 0) return <p className="muted">No runs yet.</p>;
  return (
    <div className="pruns">
      {runs.map((j) => (
        <button key={j.id} type="button" className="pitem" onClick={() => onSelect({ type: "job", id: j.id })}>
          <span className={`sdot ${tone("job." + j.state)}`} /><span className="pname">{j.workflow}</span>
          <span className="psub">{j.state} · {ago(j.finished_at ?? j.created_at)}{j.error ? ` · ${j.error.split("\n")[0].slice(0, 80)}` : ""}</span>
        </button>
      ))}
    </div>
  );
}

function Learning({ d, reload }: { d: Detail; reload: () => void }) {
  const [msg, setMsg] = useState<string | null>(null);
  const review = async () => {
    const r = await api<{ job_id: string | null; note?: string }>("/guidance/review", { method: "POST" }).catch(() => null);
    setMsg(r?.job_id ? "Review started: new lessons will wait for your OK (here and on the phone)." : r?.note ?? "Could not start.");
  };
  const decide = async (l: Lesson, approve: boolean) => { await api(`/lessons/${l.id}/decide`, { method: "POST", body: JSON.stringify({ approve }) }).catch(() => {}); reload(); };
  const drop = async (l: Lesson) => { if (window.confirm("Stop using these lessons?")) { await api(`/lessons/${l.id}`, { method: "DELETE" }).catch(() => {}); reload(); } };
  const score = (s?: Score | null) => (s ? `${s.passed}/${s.total}` : "—");
  if (d.learning.length === 0) {
    return <p className="muted">This plugin hasn't asked a model anything yet. After it has, mark answers Correct or Wrong on its runs, and the nightly review turns mistakes into lessons for you to approve.</p>;
  }
  return (
    <div className="learning">
      <p className="muted">Mark a run's model answers Correct (they become tests) or Wrong (the review learns from them). Every night Claude proposes lessons; the tests show if they help; you decide.</p>
      {d.learning.map((p) => (
        <div key={p.key} className="card">
          <h4>{p.name}</h4>
          <div className="lstats">
            <span><b>{p.samples}</b> answers</span><span><b>{p.escalated}</b> needed a bigger model</span>
            <span className="ok"><b>{p.correct}</b> correct</span><span className="bad"><b>{p.wrong}</b> wrong</span>
            {p.to_review > 0 && <span className="warn"><b>{p.to_review}</b> to review</span>}
          </div>
          {p.lessons.map((l) => (
            <div key={l.id} className={`lesson ${l.state}`}>
              <div className="lesson-h">
                <span className={`pchip ${l.state === "active" ? "live" : "dry"}`}>{l.state === "active" ? "In use" : "Waiting for you"}</span>
                {l.evals?.after && <span className="muted mono">tests {score(l.evals.before)} → {score(l.evals.after)}</span>}
              </div>
              <pre className="code">{l.text}</pre>
              <div className="appr-actions">
                {l.state === "proposed" ? (<>
                  <button type="button" className="btn" onClick={() => decide(l, false)}>Reject</button>
                  <button type="button" className="primary" onClick={() => decide(l, true)}>Use these</button>
                </>) : <button type="button" className="btn" onClick={() => drop(l)}>Stop using</button>}
              </div>
            </div>
          ))}
        </div>
      ))}
      <div className="appr-actions"><button type="button" className="btn" onClick={review}>Review mistakes now</button></div>
      {msg && <div className="tip">{msg}</div>}
    </div>
  );
}
