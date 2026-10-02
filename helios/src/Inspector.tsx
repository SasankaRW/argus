// Inspector: details for whatever is selected on the map or in the event list.

import { useEffect, useRef, useState } from "react";
import { api, Approval, ArgusEvent, ArgusMap, Job, Status } from "./api";
import { ago, clock, pretty, shortId, tone, uptime } from "./format";
import type { Selection } from "./MapView";

type Props = {
  sel: Selection;
  map: ArgusMap;
  status: Status | null;
  events: ArgusEvent[];
  onSelect: (s: Selection) => void;
  onClose?: () => void;  // shown as a drawer: the × also closes the overview
};

function KV({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="kv">
      <span className="k">{k}</span>
      <span className="mono v">{v}</span>
    </div>
  );
}

function EventList({ evs, onSelect, empty }: { evs: ArgusEvent[]; onSelect: (s: Selection) => void; empty: string }) {
  if (!evs.length) return <div className="tip">{empty}</div>;
  return (
    <div className="mini">
      {[...evs].reverse().map((e) => (
        <button key={e.seq} type="button" className="mrow" onClick={() => e.job_id && onSelect({ type: "job", id: e.job_id })} disabled={!e.job_id}>
          <span className="t mono">{clock(e.at).slice(0, 8)}</span>
          <span className={`kind mono ${tone(e.kind)}`}>{e.kind}</span>
          <span className="x mono">{e.job_id ? shortId(e.job_id) : ""}{e.step ? ` · ${e.step}` : ""}</span>
        </button>
      ))}
    </div>
  );
}

// History from the server for this box or line, merged with what has arrived live since.
function useHistory(key: string, query: string, live: ArgusEvent[], keep: (e: ArgusEvent) => boolean) {
  const [hist, setHist] = useState<ArgusEvent[]>([]);
  useEffect(() => {
    let off = false;
    setHist([]);
    api<{ events: ArgusEvent[] }>(`/events?newest=true&${query.includes("limit=") ? "" : "limit=60&"}${query}`)
      .then((r) => { if (!off) setHist(r.events.filter(keep)); })
      .catch(() => {});
    return () => { off = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const top = hist.length ? hist[hist.length - 1].seq : 0;
  return hist.concat(live.filter((e) => e.seq > top && keep(e))).slice(-40);
}

// Display only: the totals that matter are added up by Argus in code.
function shownAmount(v: unknown): string {
  const n = Number(String(v ?? "").replace(/,/g, ""));
  return v === undefined || v === null || v === "" ? "" : Number.isFinite(n)
    ? n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : String(v);
}

// One approval with Approve / Reject. Entry fields can be edited before approving.
export function ApprovalCard({ id, onDone }: { id: string; onDone?: () => void }) {
  const [a, setA] = useState<Approval | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { api<Approval>(`/approvals/${id}`).then(setA).catch((e) => setErr(String(e))); }, [id]);
  if (err) return <div className="tip">{err}</div>;
  if (!a) return <div className="tip">Loading…</div>;
  const p = a.payload;
  const decide = async (answer: "approve" | "reject") => {
    setBusy(true);
    setErr(null);
    try {
      const fields = answer === "approve" && Object.keys(edits).length ? edits : undefined;
      const r = await api<{ state: Approval["state"] }>(`/approvals/${id}/decide`, {
        method: "POST", body: JSON.stringify({ answer, fields, by: "helios" }) });
      setA({ ...a, state: r.state, decided_by: "helios" });
      onDone?.();
    } catch (e) { setErr(String(e)); } finally { setBusy(false); }
  };
  const pending = a.state === "pending";
  return (
    <div className="appr">
      <div className="appr-h"><b>{a.title}</b><span className={`pill ${tone("approval." + a.state)}`}>{a.state}</span></div>
      <div className="tip">{a.plugin} · {a.type} · asked {ago(a.created_at)}{a.decided_by ? ` · by ${a.decided_by}` : ""}</div>
      {a.type === "batch" && (
        <>
          <div className="appr-big mono">{p.count ?? 0} items{p.total ? ` · ${p.total}` : ""}</div>
          {(p.items ?? []).slice(0, 8).map((it, i) => (
            <KV key={i} k={String(it.label ?? it.name ?? it.title ?? `#${i + 1}`)} v={shownAmount(it.amount)} />
          ))}
        </>
      )}
      {a.type === "draft" && (p.summary ?? []).map((l, i) => <p key={i} className="appr-line">{l}</p>)}
      {p.image?.startsWith("data:image/") && <img className="appr-img" src={p.image} alt="" />}
      {a.type === "entry" && (
        <>
          {p.amount && <div className="appr-big mono">{p.amount}</div>}
          {(p.summary ?? []).map((l, i) => <p key={i} className="appr-line">{l}</p>)}
          {Object.entries(p.fields ?? {}).map(([k, v]) => (
            <div key={k} className="kv">
              <span className="k">{k}</span>
              {pending && (typeof v === "string" || typeof v === "number") ? (
                <input className="appr-in mono" aria-label={k} defaultValue={String(v)}
                  onChange={(e) => setEdits((x) => {
                    const n = { ...x };
                    if (e.target.value === String(v)) delete n[k]; else n[k] = e.target.value;
                    return n;
                  })} />
              ) : <span className="mono v">{String(v)}</span>}
            </div>
          ))}
        </>
      )}
      {p.link && <div className="tip"><a href={p.link} target="_blank" rel="noreferrer">Open the file</a></div>}
      {pending && (
        <div className="appr-actions">
          <button type="button" className="btn" disabled={busy} onClick={() => decide("reject")}>Reject</button>
          <button type="button" className="primary" disabled={busy} onClick={() => decide("approve")}>
            {a.type === "batch" ? "Approve all" : Object.keys(edits).length ? "Approve with edits" : "Approve"}
          </button>
        </div>
      )}
    </div>
  );
}

export function PendingApprovals({ events, onSelect }: { events: ArgusEvent[]; onSelect: (s: Selection) => void }) {
  const [list, setList] = useState<Approval[] | null>(null);
  const last = events.filter((e) => e.kind.startsWith("approval.")).map((e) => e.seq).pop() ?? 0;
  useEffect(() => { api<Approval[]>("/approvals?state=pending").then(setList).catch(() => setList([])); }, [last]);
  if (!list) return <div className="tip">Loading…</div>;
  if (!list.length) return <div className="tip">Nothing waiting for you.</div>;
  return (
    <>
      {list.map((a) => (
        <div key={a.id}>
          <ApprovalCard id={a.id} />
          {a.job_id && <button type="button" className="btn" onClick={() => onSelect({ type: "job", id: a.job_id! })}>Open job {shortId(a.job_id)}</button>}
        </div>
      ))}
    </>
  );
}

type Schedule = { id: string; plugin: string; workflow: string; cron: string; enabled: boolean;
  next_run_at: number | null; last_run_at: number | null; last_job_id: string | null; spec: { window?: string | null } };

function when(t: number | null): string {
  if (!t) return "—";
  const s = t - Date.now() / 1000;
  if (s < 0) return ago(t);
  if (s < 3600) return `in ${Math.round(s / 60)} min`;
  if (s < 86400) return `in ${(s / 3600).toFixed(1)} h`;
  return new Date(t * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false });
}

// The scheduler's box: every schedule, when it runs next, and a Run now button.
function Schedules({ onSelect }: { onSelect: (s: Selection) => void }) {
  const [list, setList] = useState<Schedule[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const load = () => api<Schedule[]>("/schedules").then(setList).catch(() => setList([]));
  useEffect(() => { load(); }, []);
  if (!list) return <div className="tip">Loading…</div>;
  if (!list.length) return <div className="tip">No schedules yet. Add them under <code>schedules:</code> in argus.yaml.</div>;
  return (
    <>
      {list.map((s) => (
        <div key={s.id} className="appr">
          <div className="appr-h"><b>{s.id}</b><span className="pill mono">{s.cron}</span></div>
          <div className="tip">{s.plugin}.{s.workflow}{s.spec?.window ? ` · ${s.spec.window} window` : ""}{s.enabled ? "" : " · off"}</div>
          <KV k="Next" v={s.enabled ? when(s.next_run_at) : "off"} />
          <KV k="Last" v={s.last_job_id
            ? <button type="button" className="btn" onClick={() => onSelect({ type: "job", id: s.last_job_id! })}>{when(s.last_run_at)}</button>
            : "never"} />
          <div className="appr-actions">
            <button type="button" className="btn" disabled={busy === s.id} onClick={async () => {
              setBusy(s.id);
              try {
                const r = await api<{ job_id: string }>(`/schedules/${s.id}/run`, { method: "POST" });
                onSelect({ type: "job", id: r.job_id });
              } finally { setBusy(null); load(); }
            }}>Run now</button>
          </div>
        </div>
      ))}
    </>
  );
}

type PluginInfo = { id: string; version: string; live: boolean; runs_on: string; description: string; rules: string | null;
  triggers: { manual?: { workflow: string; label: string }; schedule?: { cron: string }; folder_watch?: { paths: string[] } }[] };

// A plugin's box: dry-run or live, what starts it, and its manual buttons.
function PluginControls({ id, onSelect }: { id: string; onSelect: (s: Selection) => void }) {
  const [p, setP] = useState<PluginInfo | null | undefined>(undefined);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api<{ plugins: PluginInfo[] }>("/plugins").then((r) => setP(r.plugins.find((x) => x.id === id) ?? null)).catch(() => setP(null));
  }, [id]);
  if (p === undefined) return null;
  if (p === null) return <div className="tip">Not loaded from the plugins folder (built in, or its manifest has an error).</div>;
  const starts = p.triggers.map((t) => t.schedule ? `cron ${t.schedule.cron}` : t.folder_watch ? `new files in ${t.folder_watch.paths.join(", ")}`
    : t.manual ? `button: ${t.manual.label}` : "webhook");
  return (
    <>
      <KV k="Mode" v={p.live ? <span className="ok">live</span> : <span className="warn">dry-run (changes nothing)</span>} />
      <KV k="Version" v={p.version} />
      <KV k="Runs on" v={p.runs_on} />
      <KV k="Started by" v={starts.join(" · ")} />
      {p.description && <div className="tip">{p.description}</div>}
      {!p.live && <div className="tip">To let it change things, add <code>{p.id}</code> under <code>plugins.live</code> in argus.yaml.</div>}
      <div className="appr-actions">
        {p.rules && <button type="button" className="btn" onClick={() => { location.hash = `rules/${encodeURIComponent(p.id)}`; }}>Edit {p.rules.toLowerCase()}</button>}
        {p.triggers.filter((t) => t.manual).map((t) => (
          <button key={t.manual!.workflow} type="button" className="btn" disabled={busy !== null} onClick={async () => {
            setBusy(t.manual!.workflow); setErr(null);
            try {
              const r = await api<{ id: string }>(`/plugins/${p.id}/run`, { method: "POST", body: JSON.stringify({ workflow: t.manual!.workflow }) });
              onSelect({ type: "job", id: r.id });
            } catch (e) { setErr(String(e)); } finally { setBusy(null); }
          }}>{t.manual!.label}</button>
        ))}
      </div>
      {err && <div className="tip bad">{err}</div>}
    </>
  );
}

function NodePanel({ id, map, status, events, onSelect }: { id: string } & Omit<Props, "sel">) {
  const n = map.nodes.find((x) => x.id === id);
  const evs = useHistory(`n:${id}`, `component=${encodeURIComponent(id)}`, events, (e) => e.from === id || e.to === id);
  if (!n) return <div className="tip">This box is no longer on the map.</div>;
  const lines = map.edges.filter((e) => e.src === id || e.dst === id);
  const kind = n.implicit ? "Seen in events" : n.id === "phone" ? "Device" : n.kind[0].toUpperCase() + n.kind.slice(1);
  return (
    <>
      <div className="ih">
        <span className="eyebrow">{kind}</span>
        <h2>{n.label}</h2>
        <p>{n.id !== n.label ? n.id : n.group ? `on ${n.group}` : ""}</p>
      </div>
      <div className="ib">
        {n.kind === "core" && status && (
          <>
            <KV k="Version" v={status.version} />
            <KV k="Instance" v={`${status.instance} on ${status.host}`} />
            <KV k="Uptime" v={uptime(status.uptime_seconds)} />
            <KV k="Live viewers" v={status.events.subscribers} />
          </>
        )}
        {n.kind === "worker" && (
          <>
            <KV k="State" v={<span className={n.state === "online" ? "ok" : "bad"}>{n.state}</span>} />
            <KV k="Host" v={n.group ?? "?"} />
            <KV k="Version" v={String(n.meta?.version ?? "?")} />
            <KV k="Capabilities" v={((n.meta?.capabilities as string[]) ?? []).join(", ") || "none"} />
          </>
        )}
        {n.kind === "model" && <ModelDetails n={n} />}
        {n.kind === "plugin" && (
          <>
            <KV k="Running" v={n.jobs?.active ?? 0} />
            <KV k="Queued" v={n.jobs?.queued ?? 0} />
            <KV k="Waiting" v={n.jobs?.waiting ?? 0} />
            <PluginControls id={n.id} onSelect={onSelect} />
          </>
        )}
        {n.id === "phone" && (
          <>
            <KV k="State" v={n.meta?.online === true ? <span className="ok">online</span>
              : n.meta?.online === false ? <span className="bad">offline</span> : "checking…"} />
            <KV k="Device" v={String(n.meta?.device ?? "?")} />
            {typeof n.meta?.since === "number" && <KV k="Since" v={ago(n.meta.since as number)} />}
            <div className="tip">Seen through Tailscale every 30 s. Approve / Reject on the phone need it online;
              when it comes back, anything still waiting is sent again.</div>
          </>
        )}
        {n.id === "power" && (
          <>
            <KV k="Mode" v={String(n.meta?.mode ?? "?")} />
            <KV k="State" v={String(n.meta?.state ?? "?").replace("_", " ")} />
            {typeof n.meta?.idle_since === "number" && <KV k="Idle since" v={ago(n.meta.idle_since as number)} />}
            <KV k="Shut down after" v={`${n.meta?.idle_minutes ?? "?"} min idle`} />
            <div className="tip">Simulated while developing: it only logs "would wake" and "would shut down" in
              Events. The real switch comes with the laptop.</div>
          </>
        )}
        {n.id === "phone-app" && (
          <>
            <KV k="Waiting for the phone" v={n.unsent ?? 0} />
            <KV k="Not delivered" v={<span className={n.failed ? "bad" : ""}>{n.failed ?? 0}</span>} />
          </>
        )}
        {n.id === "scheduler" && (<><div className="sect">Schedules</div><Schedules onSelect={onSelect} /></>)}
        {n.id === "approvals" && (<><div className="sect">Waiting for you</div><PendingApprovals events={events} onSelect={onSelect} /></>)}
        <KV k="First seen" v={ago(n.first_seen)} />
        <KV k="Lines" v={lines.length ? lines.map((l) => (l.src === id ? `→ ${l.dst}` : `← ${l.src}`)).join(", ") : "none yet"} />
        <div className="sect">Recent activity</div>
        <EventList evs={evs} onSelect={onSelect} empty="Nothing yet. Activity appears here live." />
      </div>
    </>
  );
}

type ModelsSnap = {
  tiers: { tier: string; component: string; state: string; retry_after: number | null; calls: number; failures: number;
    last_error: string | null; last_latency_ms: number | null; provider: string; model: string | null }[];
  claude: { calls_today: number; calls_per_day: number };
};

function ModelDetails({ n }: { n: ArgusMap["nodes"][number] }) {
  const [snap, setSnap] = useState<ModelsSnap | null>(null);
  useEffect(() => {
    let off = false;
    const load = () => api<ModelsSnap>("/models").then((m) => { if (!off) setSnap(m); }).catch(() => {});
    load();
    const iv = window.setInterval(load, 3000);
    return () => { off = true; window.clearInterval(iv); };
  }, [n.id]);
  const t = snap?.tiers.find((x) => x.component === n.id);
  const state = t?.state ?? n.state ?? "closed";
  const label = state === "open" ? "paused (circuit breaker)" : state === "half_open" ? "trying again" : "ready";
  return (
    <>
      <KV k="State" v={<span className={state === "open" ? "bad" : state === "half_open" ? "warn" : "ok"}>{label}</span>} />
      {t?.retry_after ? <KV k="Retry in" v={`${Math.ceil(t.retry_after)} s`} /> : null}
      <KV k="Provider" v={`${t?.provider ?? n.meta?.provider ?? "?"}${t?.model ? ` · ${t.model}` : ""}`} />
      <KV k="Calls" v={t ? `${t.calls} (${t.failures} failed)` : "–"} />
      {t?.last_latency_ms != null && <KV k="Last reply" v={`${(t.last_latency_ms / 1000).toFixed(1)} s`} />}
      {t?.provider === "claude" && snap && <KV k="Today" v={`${snap.claude.calls_today} of ${snap.claude.calls_per_day} calls`} />}
      {t?.last_error && <KV k="Last error" v={<span className="bad">{t.last_error}</span>} />}
    </>
  );
}

function EdgePanel({ src, dst, map, events, onSelect }: { src: string; dst: string } & Omit<Props, "sel" | "status">) {
  const both = map.edges.filter((x) => (x.src === src && x.dst === dst) || (x.src === dst && x.dst === src));
  const e = both.length ? {
    count: both.reduce((a, x) => a + x.count, 0),
    first_seen: Math.min(...both.map((x) => x.first_seen)),
    last: both.reduce((a, x) => (x.last_seen > a.last_seen ? x : a)),
  } : null;
  const evs = useHistory(`e:${src}>${dst}`, `component=${encodeURIComponent(src)}&limit=400`, events,
    (x) => (x.from === src && x.to === dst) || (x.from === dst && x.to === src));
  return (
    <>
      <div className="ih">
        <span className="eyebrow">Line</span>
        <h2>{src} ⇄ {dst}</h2>
        <p>{e ? `first used ${ago(e.first_seen)}` : ""}</p>
      </div>
      <div className="ib">
        {e && (
          <>
            <KV k="Messages" v={e.count.toLocaleString()} />
            <KV k="Last" v={`${e.last.last_kind} · ${ago(e.last.last_seen)}`} />
          </>
        )}
        <div className="sect">Recent messages</div>
        <EventList evs={evs} onSelect={onSelect} empty="No messages yet." />
      </div>
    </>
  );
}

type Change = { event_id: string; kind: string; at: number; from: string | null; to: string | null; path: string | null;
  dry_run: boolean; can_undo: boolean; can_fix: boolean; undo: { job_id: string; state: string } | null;
  fix: { job_id: string; state: string; value: string | null } | null };

const live = (x: { state: string } | null) => x && x.state !== "dead" && x.state !== "cancelled";

const base = (p: string | null) => (p ?? "").split(/[\\/]/).pop() ?? "";
// Where a move went, relative to where the file was: "Documents/lecture".
const whereTo = (c: { from: string | null; to: string | null }) => {
  const from = (c.from ?? "").split(/[\\/]/).slice(0, -1);
  const to = (c.to ?? "").split(/[\\/]/).slice(0, -1);
  let i = 0;
  while (i < from.length && i < to.length && from[i].toLowerCase() === to[i].toLowerCase()) i++;
  return to.slice(i).join("/") || to.slice(-1)[0] || "";
};

type Sample = { id: number; tier: string | null; escalated: boolean; verdict: "correct" | "wrong" | null;
  input: unknown; output: unknown; correction: unknown };

// The model answers this job got, each with Correct / Wrong: correct ones become tests, wrong ones teach the
// nightly review (Helios > Plugins > the plugin > Learning).
function ModelAnswers({ id, tick }: { id: string; tick: number }) {
  const [list, setList] = useState<Sample[]>([]);
  const [fixing, setFixing] = useState<number | null>(null);
  const [fix, setFix] = useState("");
  const load = () => api<Sample[]>(`/jobs/${id}/samples`).then(setList).catch(() => setList([]));
  useEffect(() => { load(); }, [id, tick]); // eslint-disable-line react-hooks/exhaustive-deps
  if (list.length === 0) return null;
  const mark = async (s: Sample, verdict: Sample["verdict"], correction?: string) => {
    await api(`/samples/${s.id}/verdict`, { method: "POST", body: JSON.stringify({ verdict, correction }) }).catch(() => {});
    setFixing(null); setFix(""); load();
  };
  return (
    <>
      <div className="sect">Model answers</div>
      {list.map((s) => (
        <div key={s.id} className="sample">
          <div className="sample-h">
            <span className="mono muted">{s.tier ?? "?"}{s.escalated ? " · after a rejected answer" : ""}</span>
            <span className="grow" />
            {s.verdict ? (
              <>
                <span className={`pill ${s.verdict === "correct" ? "ok" : "bad"}`}>{s.verdict}</span>
                <button type="button" className="linkbtn" onClick={() => mark(s, null)}>undo</button>
              </>
            ) : (
              <>
                <button type="button" className="btn" title="Right: keep it as a test" onClick={() => mark(s, "correct")}>Correct</button>
                <button type="button" className="btn" title="Wrong: say what it should have been" onClick={() => { setFixing(s.id); setFix(""); }}>Wrong</button>
              </>
            )}
          </div>
          <pre className="code">{pretty(s.output)}</pre>
          {s.verdict === "wrong" && s.correction != null && <div className="tip">Should have been: {pretty(s.correction)}</div>}
          <TryAnother sample={s} />
          {fixing === s.id && (
            <form className="sample-fix" onSubmit={(e) => { e.preventDefault(); mark(s, "wrong", fix.trim() || undefined); }}>
              <input value={fix} onChange={(e) => setFix(e.target.value)} placeholder="What it should have been (optional)" aria-label="Correct answer" autoFocus />
              <button type="submit" className="primary">Mark wrong</button>
            </form>
          )}
        </div>
      ))}
    </>
  );
}

let tierList: Promise<string[]> | null = null;
const tiersOnce = () => (tierList ??= api<{ tiers: { tier: string }[] }>("/models").then((m) => m.tiers.map((t) => t.tier)).catch(() => []));

type Replayed = { tier: string; answer: unknown; error: string | null; same: boolean };

// "Try with another model": the same question (same playbook and lessons) to another tier, answers side by side.
function TryAnother({ sample }: { sample: Sample }) {
  const [tiers, setTiers] = useState<string[]>([]);
  const [tier, setTier] = useState("");
  const [job, setJob] = useState<string | null>(null);
  const [res, setRes] = useState<Replayed | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { tiersOnce().then((t) => { const others = t.filter((x) => x !== sample.tier); setTiers(others); setTier(others[0] ?? ""); }); }, [sample.tier]);
  useEffect(() => {
    if (!job) return;
    const t = setInterval(async () => {
      const j = await api<{ state: string; result: Replayed | null; error: string | null }>(`/jobs/${job}`).catch(() => null);
      if (!j || !["succeeded", "dead", "cancelled"].includes(j.state)) return;
      setJob(null);
      if (j.state === "succeeded" && j.result) setRes(j.result); else setErr((j.error ?? "it failed").split("\n")[0]);
    }, 1500);
    return () => clearInterval(t);
  }, [job]);
  if (!tiers.length) return null;
  const go = async () => {
    setErr(null); setRes(null);
    try { setJob((await api<{ job_id: string }>(`/samples/${sample.id}/replay`, { method: "POST", body: JSON.stringify({ tier }) })).job_id); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  return (
    <div className="try">
      <div className="try-h">
        <span className="muted">try with</span>
        <select value={tier} onChange={(e) => setTier(e.target.value)} aria-label="Model tier">{tiers.map((t) => <option key={t} value={t}>{t}</option>)}</select>
        <button type="button" className="btn" disabled={!!job} onClick={go}>{job ? "asking…" : "ask"}</button>
      </div>
      {err && <div className="tip bad">{err}</div>}
      {res && (
        <div className="try-cmp">
          <div><span className="muted mono">{sample.tier ?? "kept"}</span><pre className="code">{pretty(sample.output)}</pre></div>
          <div><span className="muted mono">{res.tier} · <span className={res.same ? "ok" : "warn"}>{res.error ? "failed" : res.same ? "same answer" : "different"}</span></span>
            <pre className="code">{res.error ?? pretty(res.answer)}</pre></div>
        </div>
      )}
    </div>
  );
}

// What a job changed on disk (the undo log), with an Undo button per move.
function Changes({ id, plugin, tick, onSelect }: { id: string; plugin: string; tick: number; onSelect: (s: Selection) => void }) {
  const [list, setList] = useState<Change[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [wrong, setWrong] = useState<{ label: string; choices: string[] } | null>(null);
  const [picking, setPicking] = useState<string | null>(null);
  const load = () => api<Change[]>(`/jobs/${id}/changes`).then(setList).catch(() => setList([]));
  useEffect(() => { load(); }, [id, tick]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    Promise.all([api<{ plugins: { id: string; wrong: { label: string } | null }[] }>("/plugins"),
      api<{ value: string[] | null }>(`/plugins/${plugin}/state/choices`)])
      .then(([ps, ch]) => {
        const w = ps.plugins.find((x) => x.id === plugin)?.wrong;
        setWrong(w && ch.value?.length ? { label: w.label, choices: ch.value } : null);
      }).catch(() => setWrong(null));
  }, [plugin]);
  const act = async (c: Change, path: string, body?: object) => {
    setBusy(c.event_id); setErr(null);
    try { await api(`/jobs/${id}/changes/${c.event_id}/${path}`, { method: "POST", body: body ? JSON.stringify(body) : undefined }); setPicking(null); await load(); }
    catch (e) { setErr(String(e)); } finally { setBusy(null); }
  };
  if (!list || list.length === 0) return null;
  const dry = list.every((c) => c.dry_run);
  return (
    <>
      <div className="sect">{dry ? "Would change (dry-run)" : "Changes"}</div>
      <div className="changes">
        {list.map((c) => (
          <div key={c.event_id} className="chg">
            <span className="mono">{c.kind === "file.moved" ? base(c.from) : base(c.path)}</span>
            <span className="muted">{c.kind === "file.moved" ? `→ ${whereTo(c)}${base(c.to) !== base(c.from) ? ` as ${base(c.to)}` : ""}` : c.kind.slice(5)}</span>
            <span className="acts">
              {live(c.fix) ? (
                <button type="button" className="btn" onClick={() => onSelect({ type: "job", id: c.fix!.job_id })}>
                  {c.fix!.state === "succeeded" ? `fixed → ${c.fix!.value}` : "fixing…"}
                </button>
              ) : live(c.undo) ? (
                <button type="button" className="btn" onClick={() => onSelect({ type: "job", id: c.undo!.job_id })}>
                  {c.undo!.state === "succeeded" ? "undone" : "undoing…"}
                </button>
              ) : (
                <>
                  {c.can_fix && wrong && picking !== c.event_id && (
                    <button type="button" className="btn" disabled={busy !== null} onClick={() => setPicking(c.event_id)}>{wrong.label}</button>
                  )}
                  {c.can_undo && picking !== c.event_id && (
                    <button type="button" className="btn" disabled={busy !== null} onClick={() => act(c, "undo")}>Undo</button>
                  )}
                </>
              )}
            </span>
            {picking === c.event_id && wrong && (
              <div className="pick">
                {wrong.choices.filter((x) => x !== whereTo(c).split("/")[0]).map((x) => (
                  <button key={x} type="button" className="btn" disabled={busy !== null} onClick={() => act(c, "wrong", { value: x })}>{x}</button>
                ))}
                <button type="button" className="btn dim" onClick={() => setPicking(null)}>cancel</button>
              </div>
            )}
          </div>
        ))}
      </div>
      {err && <div className="tip bad">{err}</div>}
    </>
  );
}

function JobPanel({ id, events, onSelect }: { id: string; events: ArgusEvent[]; onSelect: (s: Selection) => void }) {
  const [job, setJob] = useState<Job | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const lastSeq = events.filter((e) => e.job_id === id).map((e) => e.seq).pop() ?? 0;
  useEffect(() => {
    api<Job>(`/jobs/${id}`).then((j) => { setJob(j); setErr(null); }).catch((e) => setErr(String(e)));
  }, [id, lastSeq]); // refresh whenever this job moves
  if (err) return <div className="ib"><div className="tip">Could not load job: {err}</div></div>;
  if (!job) return <div className="ib"><div className="tip">Loading…</div></div>;
  return (
    <>
      <div className="ih">
        <span className="eyebrow">Job · {shortId(job.id)}</span>
        <h2>{job.plugin}.{job.workflow}</h2>
        <p>
          <span className={`pill ${tone("job." + job.state)}`}>{job.state}</span>
          {" "}attempt {job.attempt} of {job.max_attempts}{job.lease_owner ? ` · ${job.lease_owner}` : ""}
        </p>
      </div>
      <div className="ib">
        <KV k="Created" v={ago(job.created_at)} />
        {job.finished_at && <KV k="Total time" v={`${(job.finished_at - job.created_at).toFixed(2)} s`} />}
        {job.wait_reason && <KV k="Waiting for" v={job.wait_reason.startsWith("approval:") ? "your approval" : job.wait_reason} />}
        {job.state === "waiting" && job.wait_reason?.startsWith("approval:") && (
          <><div className="sect">Approval</div><ApprovalCard id={job.wait_reason.slice(9)} /></>
        )}
        <div className="sect">Steps</div>
        {job.steps?.length ? (
          <div className="steps">
            {job.steps.map((s) => (
              <div key={s.idx} className="step">
                <span className={`sdot ${tone("step." + s.state)}`} />
                <span className="mono">{s.idx}. {s.name}</span>
                <span className="muted mono">{s.state}{s.tier_used ? ` · ${s.tier_used}` : ""}</span>
                {s.error && <div className="err mono">{s.error}</div>}
              </div>
            ))}
          </div>
        ) : <div className="tip">No steps recorded.</div>}
        {job.error && (<><div className="sect">Error</div><pre className="code bad">{job.error}</pre></>)}
        <Changes id={id} plugin={job.plugin} tick={lastSeq + events.filter((e) => e.kind.startsWith("job.")).length} onSelect={onSelect} />
        <ModelAnswers id={id} tick={job.state === "succeeded" ? 1 : 0} />
        {job.result !== null && job.result !== undefined && (<><div className="sect">Result</div><pre className="code">{pretty(job.result)}</pre></>)}
        <div className="sect">Input</div>
        <pre className="code">{pretty(job.input) || "{}"}</pre>
        <div className="sect">Events</div>
        <EventList evs={events.filter((e) => e.job_id === id)} onSelect={onSelect} empty="Older than the live window." />
      </div>
    </>
  );
}

function Overview({ map, status }: { map: ArgusMap; status: Status | null }) {
  return (
    <>
      <div className="ih">
        <span className="eyebrow">Overview</span>
        <h2>{status ? `${status.instance} on ${status.host}` : "Argus"}</h2>
        <p>Select a box, a line or an event to inspect it.</p>
      </div>
      <div className="ib">
        <KV k="Boxes" v={map.nodes.length} />
        <KV k="Lines" v={map.edges.length} />
        <KV k="Messages on lines" v={map.edges.reduce((a, e) => a + e.count, 0).toLocaleString()} />
        {status && <KV k="Workers online" v={status.workers.filter((w) => w.state === "online").length} />}
        <div className="tip" style={{ marginTop: 16 }}>
          The map grows by itself: a new plugin, worker or app gets a box the first time it shows up, and a line appears the
          first time two parts talk. Drag boxes to arrange them; Auto layout puts them back.
        </div>
      </div>
    </>
  );
}

export function Inspector(p: Props) {
  const s = p.sel;
  const ref = useRef<HTMLElement>(null);
  // On a narrow screen the inspector sits below the map: bring it into view when something is picked.
  useEffect(() => {
    if (s && window.innerWidth < 900) ref.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [s]);
  return (
    <section ref={ref} className="panel insp" aria-label="Inspector">
      {!s && <Overview map={p.map} status={p.status} />}
      {s?.type === "node" && <NodePanel key={s.id} id={s.id} {...p} />}
      {s?.type === "edge" && <EdgePanel key={`${s.src}>${s.dst}`} src={s.src} dst={s.dst} {...p} />}
      {s?.type === "job" && <JobPanel key={s.id} id={s.id} events={p.events} onSelect={p.onSelect} />}
      {(s || p.onClose) && <button type="button" className="close" onClick={() => (p.onClose ? p.onClose() : p.onSelect(null))} aria-label="Close">×</button>}
    </section>
  );
}

