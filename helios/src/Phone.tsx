import { ReactNode, useEffect, useRef, useState } from "react";
import { api, Approval, ArgusEvent, Job, Status } from "./api";
import { ApprovalCard } from "./Inspector";
import type { IconName } from "./icons";
import { IconTile, Ms, Tone } from "./sonoma";

// Helios on the phone, in the Sonoma look (design/sonoma/SPEC.md): plain panels on #0b0b0e, Inter, tonal icon tiles,
// a floating pill tab bar at the thumb. The desktop's home (SonomaDesk.tsx) reuses Pane, Sparkline, useHomeData
// and statusLine from here.

export const PHONE_TABS = ["home", "ari", "plug", "map", "more"] as const;
export type PhoneTab = (typeof PHONE_TABS)[number];

type LogLine = { source: string; ts: string | null; level: string; msg: string };

// A live log window: the newest lines from Argus's log files (the Logs page, in small), stuck to the bottom.
export function MiniLog({ n = 40, empty = "no logs yet" }: { n?: number; empty?: string }) {
  const [lines, setLines] = useState<LogLine[]>([]);
  const offsets = useRef<Record<string, number>>({});
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const list = await api<{ name: string }[]>("/logs");
        const fresh: LogLine[] = [];
        for (const s of list) {
          const after = offsets.current[s.name];
          const r = await api<{ entries: LogLine[]; offset: number }>(
            `/logs/${encodeURIComponent(s.name)}${after === undefined ? `?lines=${n}` : `?after=${after}`}`);
          offsets.current[s.name] = r.offset;
          fresh.push(...r.entries);
        }
        if (!stop && fresh.length) {
          setLines((old) => {
            const all = [...old, ...fresh];
            if (!old.length) all.sort((a, b) => (a.ts ? Date.parse(a.ts) : 0) - (b.ts ? Date.parse(b.ts) : 0));
            return all.slice(-n);
          });
        }
      } catch { /* argusd restarting */ }
    };
    tick();
    const i = window.setInterval(() => { if (!document.hidden) tick(); }, 3000);  // not while the tab is hidden
    return () => { stop = true; window.clearInterval(i); };
  }, [n]);
  useEffect(() => { if (box.current) box.current.scrollTop = box.current.scrollHeight; }, [lines]);
  const lvl = (l: string) => (/error|critical|fatal/.test(l) ? "bad" : /warn/.test(l) ? "warn" : "");
  return (
    <div ref={box} className="minilog">
      {lines.length === 0 && <div className="dim">{empty}</div>}
      {lines.map((e, i) => (
        <div key={i} className={lvl(e.level)}>
          <span className="dim">{e.ts ? e.ts.slice(11, 19) : "--:--:--"}</span> <span className="src">{e.source.replace(/\.log$/, "")}</span> {e.msg}
        </div>
      ))}
    </div>
  );
}

function startOfDay() { const d = new Date(); d.setHours(0, 0, 0, 0); return d.getTime() / 1000; }

// What the home screen shows, refreshed whenever a job or approval changes.
export function useHomeData(events: ArgusEvent[]) {
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
  const hours = Array.from({ length: new Date().getHours() + 1 }, (_, h) =>
    today.filter((j) => new Date(j.created_at * 1000).getHours() === h).length);
  return {
    hours,
    today: today.length,
    ok: today.filter((j) => j.state === "succeeded").length,
    failed: today.filter((j) => j.state === "dead").length,
    running: jobs.filter((j) => j.state === "running" || j.state === "leased"),
    queued: jobs.filter((j) => j.state === "queued" || j.state === "retry").length,
    pending, lastAri, reload: () => api<Approval[]>("/approvals?state=pending").then(setPending).catch(() => {}),
  };
}

// ---------------------------------------------------------------- the phone (Sonoma)

const pad2 = (n: number) => String(n).padStart(2, "0");

// One glanceable line: "Argus is fine · 1 running · 3 queued", with the tone of its status dot.
export function statusLine(status: Status | null, live: boolean, down = false): { tone: "green" | "amber" | "coral"; text: string } {
  if (down) return { tone: "coral", text: "Can't reach Argus" };
  if (!live) return { tone: status ? "coral" : "amber", text: status ? "Can't reach Argus" : "Connecting to Argus" };
  if (!status) return { tone: "amber", text: "Connecting to Argus" };
  const j = status.jobs ?? {};
  const running = (j.running ?? 0) + (j.leased ?? 0);
  const queued = (j.queued ?? 0) + (j.retry ?? 0);
  const ok = status.status === "ok";
  const parts = [ok ? "Argus is fine" : "Argus needs a look", running ? `${running} running` : "nothing running"];
  if (queued) parts.push(`${queued} queued`);
  return { tone: ok ? "green" : "amber", text: parts.join(" · ") };
}

// The page header: the home screen gets the date, a live clock and the status line; every other page its name
// with an accent full stop, under the app name.
export function PhoneHeader({ status, live, title, down = false }: { status: Status | null; live: boolean; title: string | null; down?: boolean }) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    if (title) return;
    const i = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(i);
  }, [title]);
  const s = statusLine(status, live, down);
  const line = <div className="sn-status"><span className={`sn-dot d-${s.tone}`} />{s.text}</div>;
  if (title) {
    return (
      <header className="sn-head">
        <div className="sn-eye">Argus</div>
        <h1 className="sn-h1">{title}<span className="ac">.</span></h1>
        {line}
      </header>
    );
  }
  const date = now.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "short" }).replace(",", "");
  return (
    <header className="sn-head home">
      <div className="sn-eye">{date}</div>
      <div className="sn-clock" role="timer" aria-label={`${pad2(now.getHours())}:${pad2(now.getMinutes())}`}>
        {pad2(now.getHours())}<span className="ac">:</span><span className="mm">{pad2(now.getMinutes())}</span>
      </div>
      {line}
    </header>
  );
}

// Today's jobs per hour as a line that draws itself in.
export function Sparkline({ data }: { data: number[] }) {
  const pts = data.length > 1 ? data.slice(-9) : [0, ...data];
  const max = Math.max(1, ...pts);
  const step = 144 / (pts.length - 1);
  const xy = pts.map((v, i) => [3 + i * step, 30 - (v / max) * 24] as const);
  const d = xy.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
  const [ex, ey] = xy[xy.length - 1];
  return (
    <svg className="sn-spark" aria-hidden="true" width="120" height="28" viewBox="0 0 150 34">
      <path d={d} pathLength={1} />
      <circle cx={ex} cy={ey} r="3.4" />
    </svg>
  );
}

export function Pane({ label, icon, tone, meta, children, className = "" }: {
  label: string; icon?: IconName; tone?: Tone; meta?: ReactNode; children?: ReactNode; className?: string;
}) {
  return (
    <section className={`sn-pane ${className}`} aria-label={label}>
      <div className="sn-pane-h">
        {icon && <IconTile n={icon} tone={tone} />}
        <span className="sn-lbl">{label}</span>
        {meta !== undefined && <span className="sn-meta">{meta}</span>}
      </div>
      {children}
    </section>
  );
}

export function SonomaHome({ status, events, onSelect, onAri, onOpen, onInbox, onAsk }: {
  status: Status | null; events: ArgusEvent[]; onSelect: (s: { type: "job"; id: string }) => void;
  onAri: (mic: boolean) => void; onOpen: (view: string) => void; onInbox: () => void; onAsk: (text: string) => void;
}) {
  const [q, setQ] = useState("");
  const d = useHomeData(events);
  const [busy, setBusy] = useState<string | null>(null);
  const online = status?.workers.filter((w) => w.state === "online") ?? [];
  const machines = new Set(online.map((w) => w.host)).size;
  const first = d.pending[0];
  const decide = async (id: string, answer: "approve" | "reject") => {
    setBusy(id);
    try { await api(`/approvals/${id}/decide`, { method: "POST", body: JSON.stringify({ answer, by: "phone" }) }); await d.reload(); }
    finally { setBusy(null); }
  };
  return (
    <div className="sn-stack">
      {first && (
        <Pane label="Waiting for you" className="sn-waiting" meta={d.pending.length > 1 ? `1 of ${d.pending.length}` : undefined}>
          <div className="sn-row">
            <IconTile n="pending_actions" tone="amber" />
            <div className="sn-row-t"><b>{first.title}</b><span>{first.plugin}</span></div>
          </div>
          <div className="sn-acts">
            <button type="button" className="sn-btn primary" disabled={busy === first.id} onClick={() => decide(first.id, "approve")}>
              <Ms n="check" />Approve
            </button>
            <button type="button" className="sn-btn quiet" disabled={busy === first.id} onClick={() => decide(first.id, "reject")}>Reject</button>
            <span className="grow" />
            <button type="button" className="sn-btn quiet" onClick={onInbox}>{d.pending.length > 1 ? "All waiting" : "Details"}</button>
          </div>
        </Pane>
      )}

      <Pane label="Ari" icon="graphic_eq" tone="violet">
        <p className="sn-ari-last">{d.lastAri ?? "Hi, I'm Ari. Ask me anything, or tell me what to do."}</p>
        <form className="sn-ask" onSubmit={(e) => { e.preventDefault(); if (q.trim()) { onAsk(q.trim()); setQ(""); } }}>
          <span className="sn-field"><input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask Ari anything" aria-label="Ask Ari" enterKeyHint="send" /></span>
          <button type="button" className="sn-iconbtn accent breathe" aria-label="Talk to Ari" onClick={() => onAri(true)}><Ms n="mic" fill /></button>
        </form>
      </Pane>

      <div className="sn-pair">
        <button type="button" className="sn-pane sn-tap" aria-label={`Today: ${d.today} jobs ran. Open runs`} onClick={() => onOpen("runs")}>
          <span className="sn-pane-h"><IconTile n="bolt" tone="blue" /><span className="sn-lbl">Today</span></span>
          <span className="sn-big">{d.today}</span>
          <span className="sn-sub">{d.today === 1 ? "job ran" : "jobs ran"}{d.failed ? <> · <span className="c-coral">{d.failed} failed</span></> : null}</span>
          <Sparkline data={d.hours} />
        </button>
        <button type="button" className="sn-pane sn-tap" aria-label={`PC ${online.length ? "online" : "offline"}. Open power`} onClick={() => onOpen("power")}>
          <span className="sn-pane-h"><IconTile n="computer" tone={online.length ? "green" : "coral"} /><span className="sn-lbl">PC</span></span>
          <span className="sn-state"><span className={`sn-dot ${online.length ? "d-green" : "d-coral"}`} />{online.length ? "Online" : "Offline"}</span>
          <span className="sn-sub">{d.queued ? `${d.queued} queued` : "Queue empty"}</span>
          {machines > 1 && <span className="sn-sub">{machines} machines</span>}
        </button>
      </div>

      {d.running.length > 0 && (
        <Pane label="Running" icon="play_arrow" tone="green" meta={d.running.length > 3 ? `${d.running.length}` : undefined}>
          <div className="sn-list">
            {d.running.slice(0, 3).map((j) => (
              <button key={j.id} type="button" className="sn-li" onClick={() => onSelect({ type: "job", id: j.id })}>
                <span className="sn-dot d-green" /><span className="sn-li-t">{j.plugin} · {j.workflow.replace(/_/g, " ")}</span>
                <span className="sn-meter" aria-hidden="true"><i /></span>
              </button>
            ))}
          </div>
        </Pane>
      )}

      <Pane label="Logs" icon="receipt_long" meta={<button type="button" className="sn-link" onClick={() => onOpen("logs")}>Open</button>}>
        <MiniLog n={12} empty="No logs yet" />
      </Pane>
    </div>
  );
}

type InstallEvent = Event & { prompt: () => Promise<void>; userChoice: Promise<{ outcome: string }> };

// "Install app": Chrome's own install prompt, when it offers one (not yet installed, served over https).
function useInstall(): (() => void) | null {
  const [ev, setEv] = useState<InstallEvent | null>(() => (window as unknown as { heliosInstall?: InstallEvent }).heliosInstall ?? null);
  useEffect(() => {
    const on = () => setEv((window as unknown as { heliosInstall?: InstallEvent }).heliosInstall ?? null);
    window.addEventListener("helios-installable", on);
    return () => window.removeEventListener("helios-installable", on);
  }, []);
  if (!ev) return null;
  return () => { ev.prompt().then(() => ev.userChoice).finally(() => { (window as unknown as { heliosInstall?: InstallEvent }).heliosInstall = undefined; setEv(null); }); };
}

function MenuRow({ icon, tone, title, sub, onClick, pill }: { icon: IconName; tone?: Tone; title: string; sub: string; onClick: () => void; pill?: ReactNode }) {
  return (
    <button type="button" className="sn-menu-row" onClick={onClick}>
      <IconTile n={icon} tone={tone} />
      <span className="sn-row-t"><b>{title}</b><span>{sub}</span></span>
      {pill}
      <Ms n="chevron_right" />
    </button>
  );
}

// "More": the other pages, as a list.
export function PhoneMore({ onOpen, onFull, waiting = 0 }: { onOpen: (v: string) => void; onFull: () => void; waiting?: number }) {
  const install = useInstall();
  const rows: [string, IconName, Tone, string, string][] = [
    ["inbox", "inbox", "amber", "Inbox", "Everything waiting for you"],
    ["notifications", "notifications", "blue", "Notifications", "What was sent to this phone"],
    ["queue", "queue", "blue", "Queue", "What runs next"],
    ["runs", "history", "plain", "Runs", "Everything that ran"],
    ["share", "ios_share", "plain", "Share", "Send files to Argus"],
    ["power", "power_settings_new", "plain", "Power", "Wake, sleep, shut down"],
    ["models", "memory", "violet", "Models", "Tiers, calls, hand-ups"],
    ["settings", "tune", "plain", "Settings", "Quiet hours, summaries, power"],
    ["logs", "receipt_long", "plain", "Logs", "What Argus wrote"],
  ];
  return (
    <div className="sn-stack">
      <section className="sn-pane flush" aria-label="Pages">
        {rows.map(([id, icon, tone, title, sub]) => (
          <MenuRow key={id} icon={icon} tone={tone} title={title} sub={sub} onClick={() => onOpen(id)}
            pill={id === "inbox" && waiting > 0 ? <span className="sn-pill t-amber">{waiting} waiting</span> : undefined} />
        ))}
      </section>
      <section className="sn-pane flush" aria-label="This phone">
        {install && <MenuRow icon="install_mobile" title="Install app" sub="Argus on your home screen" onClick={install} />}
        <MenuRow icon="desktop_windows" title="Full dashboard" sub="The desktop layout" onClick={onFull} />
      </section>
    </div>
  );
}

// Everything waiting for you, full size.
export function PhoneInbox({ onDone }: { onDone: () => void }) {
  const [list, setList] = useState<Approval[] | null>(null);
  const load = () => api<Approval[]>("/approvals?state=pending").then(setList).catch(() => setList([]));
  useEffect(() => { load(); }, []);
  return (
    <div className="inbox">
      <div className="sn-lbl">Waiting for you {list ? `· ${list.length}` : ""}</div>
      {list?.length === 0 && <p className="sn-empty">Nothing waits for you.</p>}
      {list?.map((a) => <ApprovalCard key={a.id} id={a.id} onDone={() => { load(); if (list.length <= 1) onDone(); }} />)}
    </div>
  );
}

const TAB_INFO: Record<PhoneTab, [string, IconName]> = {
  home: ["Home", "home"], ari: ["Ari", "graphic_eq"], plug: ["Plugins", "extension"], map: ["Map", "hub"], more: ["More", "more_horiz"],
};

export function PhoneTabs({ tab, onTab }: { tab: PhoneTab; onTab: (t: PhoneTab) => void }) {
  return (
    <nav className="sn-tabs" aria-label="Sections">
      {PHONE_TABS.map((t) => (
        <button key={t} type="button" className="sn-tab" aria-current={tab === t ? "page" : undefined} onClick={() => onTab(t)}>
          <span className="ind"><Ms n={TAB_INFO[t][1]} fill={tab === t} /></span>{TAB_INFO[t][0]}
        </button>
      ))}
    </nav>
  );
}

// When Argus doesn't answer (the PC is off, or Tailscale isn't connected on the phone).
export function PhoneOffline({ onRetry }: { onRetry: () => void }) {
  return (
    <section className="sn-pane sn-offline" role="status">
      <div className="sn-row">
        <IconTile n="cloud_off" tone="coral" />
        <div className="sn-row-t"><b>Can't reach Argus</b><span>Is the PC on, and Tailscale connected on this phone?</span></div>
      </div>
      <div className="sn-acts"><button type="button" className="sn-btn tonal" onClick={onRetry}><Ms n="refresh" />Try again</button></div>
    </section>
  );
}
