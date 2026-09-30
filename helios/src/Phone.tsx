import { ReactNode, useEffect, useState } from "react";
import { api, Approval, ArgusEvent, Job, Status, useTimeSaved } from "./api";
import { ApprovalCard } from "./Inspector";
import { clock, tone } from "./format";

// The phone's Helios: terminal widgets. Each tile is a small terminal window ("~/jobs", "~/ari") on a near-black
// ground with soft colour glows; a floating pill tab bar at the thumb.

export const PHONE_TABS = ["home", "ari", "plug", "map", "more"] as const;
export type PhoneTab = (typeof PHONE_TABS)[number];

export function Tile({ path, dot = "var(--ok)", span = false, alert = false, children, onOpen }: {
  path: string; dot?: string; span?: boolean; alert?: boolean; children: ReactNode; onOpen?: () => void;
}) {
  return (
    <section className={`tt${span ? " span" : ""}${alert ? " alert" : ""}`}>
      <header className="tt-h">
        <i style={{ background: dot, boxShadow: `0 0 8px ${dot}` }} />
        {onOpen ? <button type="button" className="tt-open" onClick={onOpen}>{path} <span aria-hidden="true">›</span></button> : <span>{path}</span>}
      </header>
      <div className="tt-b">{children}</div>
    </section>
  );
}

export function Meter({ value, n = 10, color = "var(--ok)" }: { value: number; n?: number; color?: string }) {
  const f = Math.max(0, Math.min(n, Math.round(value * n)));
  return <span className="meter" aria-hidden="true"><span style={{ color }}>{"▮".repeat(f)}</span><span className="off">{"▮".repeat(n - f)}</span></span>;
}

function startOfDay() { const d = new Date(); d.setHours(0, 0, 0, 0); return d.getTime() / 1000; }

// What the home screen shows, refreshed whenever a job or approval changes.
function useHomeData(events: ArgusEvent[]) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [pending, setPending] = useState<Approval[]>([]);
  const [lastAri, setLastAri] = useState<string | null>(null);
  const last = events.filter((e) => /^(job|approval|ari)\./.test(e.kind)).map((e) => e.seq).pop() ?? 0;
  useEffect(() => {
    api<Job[]>("/jobs?limit=200").then(setJobs).catch(() => {});
    api<Approval[]>("/approvals?state=pending").then(setPending).catch(() => {});
    let conv: string | null = null;
    try { conv = localStorage.getItem("ari.conv"); } catch { /* private */ }
    if (conv) {
      api<{ turns: { role: string; text: string | null }[] }>(`/ari/${conv}`)
        .then((r) => setLastAri([...r.turns].reverse().find((t) => t.role === "ari" && t.text)?.text ?? null)).catch(() => {});
    }
  }, [last]);
  const today = jobs.filter((j) => j.created_at >= startOfDay());
  return {
    today: today.length,
    ok: today.filter((j) => j.state === "succeeded").length,
    failed: today.filter((j) => j.state === "dead").length,
    running: jobs.filter((j) => j.state === "running" || j.state === "leased"),
    queued: jobs.filter((j) => j.state === "queued" || j.state === "retry").length,
    pending, lastAri, reload: () => api<Approval[]>("/approvals?state=pending").then(setPending).catch(() => {}),
  };
}

export function PhoneHome({ status, events, onSelect, onAri, onOpen, onInbox }: {
  status: Status | null; events: ArgusEvent[]; onSelect: (s: { type: "job"; id: string }) => void;
  onAri: (mic: boolean) => void; onOpen: (view: string) => void; onInbox: () => void;
}) {
  const d = useHomeData(events);
  const saved = useTimeSaved();
  const [busy, setBusy] = useState<string | null>(null);
  const online = status?.workers.filter((w) => w.state === "online") ?? [];
  const first = d.pending[0];
  const decide = async (id: string, answer: "approve" | "reject") => {
    setBusy(id);
    try { await api(`/approvals/${id}/decide`, { method: "POST", body: JSON.stringify({ answer, by: "phone" }) }); await d.reload(); }
    finally { setBusy(null); }
  };
  const tail = events.filter((e) => /^(job\.(succeeded|dead|queued)|model\.escalated|approval\.|step\.failed)/.test(e.kind)).slice(-5).reverse();
  const mark = (k: string) => (k.endsWith("succeeded") ? "✓" : k.includes("escalated") ? "↑" : k.startsWith("approval") ? "?" : k.includes("dead") || k.includes("failed") ? "✗" : "•");
  return (
    <div className="tgrid">
      <Tile path="~/ari" dot="var(--amber)" span>
        <p className="ari-last">› {d.lastAri ?? "hi, I'm Ari. Ask me anything, or tell me what to do."}</p>
        <div className="prompt">
          <button type="button" className="prompt-in" onClick={() => onAri(false)}>
            <span className="caret">›</span><span className="ph">ask ari…</span><span className="cur" aria-hidden="true" />
          </button>
          <button type="button" className="prompt-mic" aria-label="Talk to Ari" onClick={() => onAri(true)}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v3" /></svg>
          </button>
        </div>
      </Tile>

      {first && (
        <Tile path="! awaiting input" dot="var(--amber)" span alert>
          <div className="await">
            <div className="await-t">
              <b>{first.title}</b>
              <span>{first.plugin}{d.pending.length > 1 ? ` · +${d.pending.length - 1} more` : ""}</span>
            </div>
            <button type="button" className="k-yes" disabled={busy === first.id} onClick={() => decide(first.id, "approve")}>[Y] ok</button>
            <button type="button" className="k-no" disabled={busy === first.id} onClick={() => decide(first.id, "reject")}>[n]</button>
          </div>
          <button type="button" className="tt-link" onClick={onInbox}>details{d.pending.length > 1 ? " · all waiting" : ""} ›</button>
        </Tile>
      )}

      <Tile path="~/jobs" dot="var(--ok)" onOpen={() => onOpen("runs")}>
        <div className="big ok">{d.today}</div>
        <div className="sub">today · {d.ok} ok{d.failed ? ` · ${d.failed} failed` : ""}{d.running.length ? ` · ${d.running.length} run` : ""}</div>
        <Meter value={d.today ? d.ok / d.today : 0} />
      </Tile>

      <Tile path="~/saved" dot="var(--flow)">
        <div className="big flow">{saved?.seconds ? saved.text.replace(/\s/g, "") : "—"}</div>
        <div className="sub">this week</div>
        {saved?.plugins[0] && <div className="sub dim">top: {saved.plugins[0].plugin.replace(/-.*/, "")}</div>}
      </Tile>

      {d.running.length > 0 && (
        <Tile path="~/running" dot="var(--flow)" span>
          {d.running.slice(0, 3).map((j) => (
            <button key={j.id} type="button" className="run-row" onClick={() => onSelect({ type: "job", id: j.id })}>
              <span className="flow">●</span><span className="run-name">{j.plugin} · {j.workflow}</span>
              <span className="run-bar stripe" aria-hidden="true" />
            </button>
          ))}
        </Tile>
      )}

      <Tile path="~/pc" dot={online.length ? "var(--ok)" : "var(--bad)"} onOpen={() => onOpen("power")}>
        <div className="mid">{online.length ? "online" : "offline"}</div>
        <div className="sub">{online.length} worker{online.length === 1 ? "" : "s"}</div>
        <div className="sub">queue <Meter value={Math.min(1, d.queued / 10)} n={6} color="var(--amber)" /> {d.queued}</div>
      </Tile>

      <Tile path="~/tail -f" dot="var(--flow)" onOpen={() => onOpen("logs")}>
        <div className="tail">
          {tail.length === 0 && <div className="dim">quiet</div>}
          {tail.map((e) => (
            <div key={e.seq} className={tone(e.kind)}><span>{mark(e.kind)}</span> {(e.to ?? e.from ?? e.kind).replace(/-.*/, "")}</div>
          ))}
        </div>
      </Tile>
    </div>
  );
}

// "More": the other pages, as a terminal listing.
export function PhoneMore({ onOpen, onFull }: { onOpen: (v: string) => void; onFull: () => void }) {
  const rows: [string, string, string][] = [["queue", "queue", "what runs next"], ["runs", "runs", "everything that ran"],
    ["share", "share", "send files to argus"], ["power", "power", "wake · sleep · shut down"], ["logs", "logs", "what argus wrote"]];
  return (
    <div className="tgrid">
      <Tile path="~/more" dot="var(--flow)" span>
        <div className="ls">
          {rows.map(([id, name, sub]) => (
            <button key={id} type="button" className="ls-row" onClick={() => onOpen(id)}>
              <span className="ok">drwx</span><b>{name}/</b><span className="dim">{sub}</span><span aria-hidden="true">›</span>
            </button>
          ))}
          <button type="button" className="ls-row" onClick={onFull}>
            <span className="flow">-rwx</span><b>full-dashboard</b><span className="dim">the desktop layout</span><span aria-hidden="true">›</span>
          </button>
        </div>
      </Tile>
    </div>
  );
}

// Everything waiting for you, full size (from the awaiting-input tile).
export function PhoneInbox({ onDone }: { onDone: () => void }) {
  const [list, setList] = useState<Approval[] | null>(null);
  const load = () => api<Approval[]>("/approvals?state=pending").then(setList).catch(() => setList([]));
  useEffect(() => { load(); }, []);
  return (
    <div className="inbox">
      <div className="sheet-title mono">! awaiting input <span className="dim">{list ? list.length : "…"}</span></div>
      {list?.length === 0 && <p className="dim mono">nothing waits for you.</p>}
      {list?.map((a) => <ApprovalCard key={a.id} id={a.id} onDone={() => { load(); if (list.length <= 1) onDone(); }} />)}
    </div>
  );
}

export function PhoneTabs({ tab, onTab }: { tab: PhoneTab; onTab: (t: PhoneTab) => void }) {
  return (
    <nav className="ptabbar" aria-label="Sections">
      {PHONE_TABS.map((t) => (
        <button key={t} type="button" aria-current={tab === t ? "page" : undefined} onClick={() => onTab(t)}>{t}</button>
      ))}
    </nav>
  );
}

export function PhoneHeader({ status, live }: { status: Status | null; live: boolean }) {
  const [t, setT] = useState(() => clock(Date.now() / 1000).slice(0, 5));
  useEffect(() => { const i = setInterval(() => setT(clock(Date.now() / 1000).slice(0, 5)), 20000); return () => clearInterval(i); }, []);
  const ok = live && status?.status === "ok";
  return (
    <header className="phead">
      <span className="who"><span className="ok">sas@argus</span><span className="dim">:~$</span></span>
      <span className="dim hide-xs">{t}</span>
      <span className={`badge ${ok ? "ok" : "warn"}`}><i />{ok ? "ONLINE" : live ? "DEGRADED" : "OFFLINE"}</span>
    </header>
  );
}
