import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ArgusEvent } from "./api";
import { ApprovalCard } from "./Inspector";

function ago(t: number) {
  const s = Date.now() / 1000 - t;
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h`;
  return `${Math.floor(s / 86400)} d`;
}

// ---------------------------------------------------------------- Inbox: everything waiting for you

type Score = { passed: number; total: number } | null;
type InboxItem = { kind: "approval" | "lesson" | "question"; id: string | number; title: string; plugin?: string | null;
  text?: string; chat?: string | null; evals?: { before?: Score; after?: Score } | null; at: number };
export type InboxData = { items: InboxItem[]; counts: Record<string, number> };

// How many things wait for you (the nav badge), refreshed when approvals, lessons or Ari change.
export function useInboxCount(events: ArgusEvent[]): number {
  const [n, setN] = useState(0);
  const last = events.filter((e) => /^(approval|ari|lesson|guidance)\./.test(e.kind)).map((e) => e.seq).pop() ?? 0;
  useEffect(() => { api<InboxData>("/inbox").then((d) => setN(d.items.length)).catch(() => {}); }, [last]);
  return n;
}

export function InboxView({ events, onOpenChat }: { events: ArgusEvent[]; onOpenChat: (conv: string) => void }) {
  const [d, setD] = useState<InboxData | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [only, setOnly] = useState<string>("all");
  const load = useCallback(() => { api<InboxData>("/inbox").then(setD).catch(() => setD({ items: [], counts: {} })); }, []);
  const last = events.filter((e) => /^(approval|ari|lesson|guidance)\./.test(e.kind)).map((e) => e.seq).pop() ?? 0;
  useEffect(load, [load, last]);
  const act = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key);
    try { await fn(); } catch { /* shown by the reload */ } finally { setBusy(null); load(); }
  };
  const items = (d?.items ?? []).filter((i) => only === "all" || i.kind === only);
  const label: Record<string, string> = { approval: "approvals", lesson: "lessons", question: "ari asks" };
  return (
    <section className="panel inboxp" aria-label="Inbox">
      <div className="ph">
        <span className="pt">inbox</span>
        <span className="muted">{d ? (d.items.length ? `${d.items.length} waiting for you` : "nothing waits for you") : "…"}</span>
        <div className="tools seg-f">
          {["all", "approval", "lesson", "question"].map((k) => (
            <button key={k} type="button" className="btn" aria-pressed={only === k} onClick={() => setOnly(k)}>
              {k === "all" ? "all" : label[k]}{k !== "all" && d?.counts[k] ? ` ${d.counts[k]}` : ""}
            </button>
          ))}
        </div>
      </div>
      <div className="ibody">
        {d && items.length === 0 && <div className="empty-note">$ nothing here. Argus asks when it needs you.</div>}
        {items.map((i) => {
          const key = `${i.kind}:${i.id}`;
          return (
            <div key={key} className={`irow ${i.kind}`}>
              <div className="irow-h">
                <span className={`ikind ${i.kind}`}>{i.kind === "question" ? "ari asks" : i.kind}</span>
                <b className="it">{i.title}</b>
                <span className="muted">{i.plugin ?? ""} · {ago(i.at)}</span>
              </div>
              {i.kind === "approval" && (open === key
                ? <div className="ibox"><ApprovalCard id={String(i.id)} onDone={() => { setOpen(null); load(); }} /></div>
                : <div className="iact"><button type="button" className="primary" onClick={() => setOpen(key)}>review</button></div>)}
              {i.kind === "lesson" && (
                <>
                  <pre className="ilesson">{i.text}</pre>
                  {i.evals?.after && (
                    <div className="muted mono">tests: {fmtScore(i.evals.before)} now → {fmtScore(i.evals.after)} with these</div>
                  )}
                  <div className="iact">
                    <button type="button" className="primary" disabled={busy === key}
                      onClick={() => act(key, () => api(`/lessons/${i.id}/decide`, { method: "POST", body: JSON.stringify({ approve: true }) }))}>use it</button>
                    <button type="button" className="btn" disabled={busy === key}
                      onClick={() => act(key, () => api(`/lessons/${i.id}/decide`, { method: "POST", body: JSON.stringify({ approve: false }) }))}>no</button>
                  </div>
                </>
              )}
              {i.kind === "question" && (
                <>
                  {i.chat && <div className="muted">in the chat “{i.chat}”</div>}
                  <div className="iact">
                    <button type="button" className="primary" disabled={busy === key}
                      onClick={() => act(key, () => api(`/ari/${i.id}/answer`, { method: "POST", body: JSON.stringify({ yes: true }) }))}>yes</button>
                    <button type="button" className="btn" disabled={busy === key}
                      onClick={() => act(key, () => api(`/ari/${i.id}/answer`, { method: "POST", body: JSON.stringify({ yes: false }) }))}>no</button>
                    <button type="button" className="btn" onClick={() => onOpenChat(String(i.id))}>open chat ›</button>
                  </div>
                </>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function fmtScore(v?: Score) { return v ? `${v.passed}/${v.total}` : "—"; }

// ---------------------------------------------------------------- Settings: Argus's own, over argus.yaml

type Setting = { key: string; group: string; label: string; type: "bool" | "time" | "choice" | "int" | "number" | "text";
  options?: string[]; min?: number; max?: number; value: unknown; default: unknown; changed: boolean };

export function SettingsView() {
  const [rows, setRows] = useState<Setting[] | null>(null);
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const load = useCallback(() => { api<{ settings: Setting[] }>("/argus-settings").then((r) => setRows(r.settings)).catch((e) => setErr(String(e))); }, []);
  useEffect(load, [load]);
  const put = async (changes: Record<string, unknown>) => {
    setErr(null); setSaved(false);
    try {
      const r = await api<{ settings: Setting[] }>("/argus-settings", { method: "PUT", body: JSON.stringify(changes) });
      setRows(r.settings); setDraft({}); setSaved(true); setTimeout(() => setSaved(false), 2500);
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  const groups = useMemo(() => {
    const g: Record<string, Setting[]> = {};
    for (const s of rows ?? []) (g[s.group] ??= []).push(s);
    return g;
  }, [rows]);
  const dirty = Object.keys(draft).length > 0;
  const val = (s: Setting) => (s.key in draft ? draft[s.key] : s.value);
  const set = (s: Setting, v: unknown) => setDraft((d) => {
    const n = { ...d };
    if (v === s.value) delete n[s.key]; else n[s.key] = v;
    return n;
  });
  return (
    <div className="setp">
      <section className="panel">
        <div className="ph">
          <span className="pt">settings</span>
          <span className="muted">argus.yaml is the base; what you change here wins, and is kept</span>
          <div className="tools">
            {saved && <span className="ok mono">saved ✓</span>}
            <button type="button" className="btn" disabled={!dirty} onClick={() => setDraft({})}>undo</button>
            <button type="button" className="primary" disabled={!dirty} onClick={() => put(draft)}>save{dirty ? ` ${Object.keys(draft).length}` : ""}</button>
          </div>
        </div>
        {err && <div className="tip bad setp-err">{err}</div>}
        <div className="setgrid">
          {Object.entries(groups).map(([g, list]) => (
            <div key={g} className="setgroup">
              <div className="sect">{g.toLowerCase()}</div>
              {list.map((s) => (
                <div key={s.key} className={`setrow${s.key in draft ? " dirty" : ""}`}>
                  <label className="setl" htmlFor={`set-${s.key}`}>
                    {s.label}
                    <span className="setk mono">{s.key}{s.changed ? " · changed" : ""}</span>
                  </label>
                  <div className="setv">
                    <Field s={s} value={val(s)} onChange={(v) => set(s, v)} />
                    {s.changed && !(s.key in draft) && (
                      <button type="button" className="btn setback" title={`argus.yaml says ${String(s.default)}`} onClick={() => put({ [s.key]: null })}>
                        ↺ {String(s.default)}
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

function Field({ s, value, onChange }: { s: Setting; value: unknown; onChange: (v: unknown) => void }) {
  const id = `set-${s.key}`;
  if (s.type === "bool") {
    return (
      <button id={id} type="button" role="switch" aria-checked={!!value} className={`switch${value ? " on" : ""}`} onClick={() => onChange(!value)}>
        <i /><span>{value ? "on" : "off"}</span>
      </button>
    );
  }
  if (s.type === "choice") {
    return (
      <span className="seg-f" role="group" aria-labelledby={id}>
        {s.options!.map((o) => <button key={o} id={o === s.options![0] ? id : undefined} type="button" className="btn" aria-pressed={value === o} onClick={() => onChange(o)}>{o}</button>)}
      </span>
    );
  }
  if (s.type === "time") return <input id={id} type="time" value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} />;
  if (s.type === "int" || s.type === "number") {
    return <input id={id} type="number" value={String(value ?? "")} min={s.min} max={s.max} step={s.type === "int" ? 1 : "any"}
      onChange={(e) => onChange(e.target.value === "" ? "" : Number(e.target.value))} />;
  }
  return <input id={id} value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} />;
}

// ---------------------------------------------------------------- Models: tiers, calls, who hands up

type Tier = { tier: string; label: string; provider: string; model: string | null; state: string; retry_after: number | null;
  calls: number; failures: number; last_error: string | null; last_latency_ms: number | null };
type ModelsSnap = { tiers: Tier[]; chain: string[]; claude: { calls_today: number; calls_per_day: number; logged_in: boolean | null } };
type Usage = { days: number; daily: { day: string; tier: string; n: number }[]; plugins: { plugin: string; calls: number; escalations: number }[] };
type Ollama = { reachable: boolean; error?: string; models: { name: string; size_gb: number; family?: string; params?: string }[] };

export function ModelsView({ events }: { events: ArgusEvent[] }) {
  const [m, setM] = useState<ModelsSnap | null>(null);
  const [u, setU] = useState<Usage | null>(null);
  const [o, setO] = useState<Ollama | null>(null);
  const last = events.filter((e) => e.kind.startsWith("model.")).map((e) => e.seq).pop() ?? 0;
  useEffect(() => {
    api<ModelsSnap>("/models").then(setM).catch(() => {});
    api<Usage>("/models/usage").then(setU).catch(() => {});
  }, [last]);
  useEffect(() => { api<Ollama>("/models/ollama").then(setO).catch(() => {}); }, []);
  const days = useMemo(() => {
    const out: string[] = [];
    for (let i = (u?.days ?? 7) - 1; i >= 0; i--) {
      const d = new Date(Date.now() - i * 86400000);
      out.push(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`);
    }
    return out;
  }, [u]);
  const perDay = (tier: string) => days.map((d) => u?.daily.find((x) => x.day === d && x.tier === tier.toLowerCase())?.n ?? 0);
  const stateTone = (s: string) => (s === "closed" ? "ok" : s === "open" ? "bad" : "warn");
  const stateWord = (s: string) => (s === "closed" ? "ready" : s === "open" ? "paused" : "trying again");
  const cap = m?.claude;
  return (
    <div className="modp">
      <div className="modtiles">
        {m?.tiers.map((t) => {
          const series = perDay(t.tier);
          const max = Math.max(1, ...series);
          return (
            <section key={t.tier} className="panel modt">
              <div className="ph">
                <span className="pt">{t.tier.toLowerCase()}</span>
                <span className="muted">{t.label}</span>
                <span className={`mstate ${stateTone(t.state)}`}><i />{stateWord(t.state)}{t.retry_after ? ` · ${Math.ceil(t.retry_after)} s` : ""}</span>
              </div>
              <div className="modb">
                <div className="modn"><b className={t.provider === "claude" ? "warn" : "flow"}>{series[series.length - 1]}</b><span className="muted">calls today</span></div>
                <div className="spark" aria-label={`calls per day over ${days.length} days`}>
                  {series.map((n, i) => <i key={days[i]} title={`${days[i]}: ${n}`} style={{ height: `${Math.max(4, (n / max) * 100)}%` }} className={n ? "" : "zero"} />)}
                </div>
                <div className="muted mono">{t.provider}{t.model ? ` · ${t.model}` : ""} · {t.calls} calls{t.failures ? ` · ${t.failures} failed` : ""}{t.last_latency_ms ? ` · last ${(t.last_latency_ms / 1000).toFixed(1)} s` : ""}</div>
                {t.last_error && t.state !== "closed" && <div className="tip bad mono">{t.last_error}</div>}
              </div>
            </section>
          );
        })}
      </div>
      <div className="modrow">
        <section className="panel">
          <div className="ph"><span className="pt">claude</span><span className="muted">the paid tier: a daily cap</span></div>
          <div className="modb">
            {cap && (
              <>
                <div className="modn"><b className={cap.calls_today >= cap.calls_per_day ? "bad" : "warn"}>{cap.calls_today}/{cap.calls_per_day}</b><span className="muted">calls today</span></div>
                <div className="bar"><i style={{ width: `${Math.min(100, (cap.calls_today / Math.max(1, cap.calls_per_day)) * 100)}%` }} /></div>
                <div className={cap.logged_in === false ? "bad" : "muted"}>{cap.logged_in === false ? "logged out: run `claude` on the PC and /login" : cap.logged_in ? "logged in" : "login not checked yet"}</div>
                <div className="muted">escalation order: {m?.chain.join(" → ")}</div>
              </>
            )}
          </div>
        </section>
        <section className="panel">
          <div className="ph"><span className="pt">by plugin</span><span className="muted">last {u?.days ?? 7} days · a hand-up = a local model couldn't, a bigger one did</span></div>
          <div className="modb">
            {u && u.plugins.length === 0 && <div className="muted">no model calls yet</div>}
            {u?.plugins.map((p) => (
              <div key={p.plugin} className="mrow2">
                <span className="mono">{p.plugin}</span>
                <span className="muted mono">{p.calls} calls</span>
                <span className={`mono ${p.escalations ? "warn" : "muted"}`}>{p.escalations} hand-ups{p.calls ? ` (${Math.round((p.escalations / p.calls) * 100)}%)` : ""}</span>
              </div>
            ))}
          </div>
        </section>
        <section className="panel">
          <div className="ph"><span className="pt">ollama</span><span className="muted">{o ? (o.reachable ? `${o.models.length} models pulled` : "not reachable from Argus") : "…"}</span></div>
          <div className="modb">
            {o?.models.map((x) => (
              <div key={x.name} className="mrow2">
                <span className="mono">{x.name}</span>
                <span className="muted mono">{x.params ?? ""}</span>
                <span className="muted mono">{x.size_gb} GB</span>
              </div>
            ))}
            {o && !o.reachable && <div className="muted">{o.error}</div>}
          </div>
        </section>
      </div>
    </div>
  );
}
