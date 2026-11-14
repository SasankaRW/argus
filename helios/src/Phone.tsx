import { ReactNode, useEffect, useRef, useState } from "react";
import { api, Approval, ArgusEvent, Job, Status } from "./api";
import { startOfDay, useJobs } from "./jobsFeed";
import { ApprovalCard } from "./Inspector";
import type { IconName } from "./icons";
import { IconTile, Ms, Tone } from "./m3";

// Helios on the phone, in Material 3 Expressive (design/material3): a greeting header, a status card, tonal cards on
// #0b0b0e, Roboto Flex, and the standard M3 navigation bar (Home, Map, Ari, Inbox, More). The desktop (DeskLayer.tsx)
// reuses Pane, Sparkline, useHomeData and statusLine from here.

export const PHONE_TABS = ["home", "map", "ari", "inbox", "more"] as const;
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

// What the home screen shows, refreshed whenever a job or approval changes.
export function useHomeData(events: ArgusEvent[]) {
  const jobs = useJobs(events);
  const [pending, setPending] = useState<Approval[]>([]);
  const [lastAri, setLastAri] = useState<string | null>(null);
  const last = events.filter((e) => /^(job|approval|ari)\./.test(e.kind)).map((e) => e.seq).pop() ?? 0;
  useEffect(() => {
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
    jobs,
    hours,
    today: today.length,
    ok: today.filter((j) => j.state === "succeeded").length,
    failed: today.filter((j) => j.state === "dead").length,
    running: jobs.filter((j) => j.state === "running" || j.state === "leased"),
    queued: jobs.filter((j) => j.state === "queued" || j.state === "retry").length,
    pending, lastAri, reload: () => api<Approval[]>("/approvals?state=pending").then(setPending).catch(() => {}),
  };
}

// ---------------------------------------------------------------- the phone (Material 3 Expressive)

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

// The headline of the status card and the desktop's status bar.
export function headline(status: Status | null, live: boolean, down: boolean): string {
  if (down) return "Can't reach Argus";
  if (!live || !status) return "Connecting to Argus";
  if (status.status !== "ok") return "Something needs a look";
  const j = status.jobs ?? {};
  return (j.running ?? 0) + (j.leased ?? 0) > 0 ? "Everything's running" : "Everything's quiet";
}

const greeting = (h: number) => (h < 5 ? "Good night" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening");

// The page header: the home screen gets a greeting with the date and the notifications bell; every other page its
// name with the status line under it.
export function PhoneHeader({ status, live, title, down = false, onBell }: { status: Status | null; live: boolean; title: string | null; down?: boolean; onBell?: () => void }) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const i = window.setInterval(() => setNow(new Date()), 60000);
    return () => window.clearInterval(i);
  }, []);
  const s = statusLine(status, live, down);
  if (title) {
    return (
      <header className="sn-head">
        <h1 className="sn-h1">{title}</h1>
        <div className="sn-status"><span className={`sn-dot d-${s.tone}`} />{s.text}</div>
      </header>
    );
  }
  const date = now.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" }).replace(",", "");
  return (
    <header className="sn-head home">
      <div className="sn-greet"><div className="sn-date">{date}</div><h1 className="sn-h1">{greeting(now.getHours())}</h1></div>
      <button type="button" className="sn-bell" aria-label="Notifications" onClick={onBell}><Ms n="notifications" /></button>
    </header>
  );
}

// Today's jobs per hour as a line that draws itself in.
export function Sparkline({ data }: { data: number[] }) {
  const pts = data.length > 1 ? data.slice(-9) : [0, ...data];
  const max = Math.max(1, ...pts);
  const step = 144 / (pts.length - 1);
  const d = pts.map((v, i) => `${i ? "L" : "M"}${(3 + i * step).toFixed(1)} ${(28 - (v / max) * 24).toFixed(1)}`).join(" ");
  return (
    <svg className="sn-spark" aria-hidden="true" width="100%" viewBox="0 0 150 30">
      <path d={d} pathLength={1} />
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

// A 12-point scallop: a circle of radius 31 with 12 soft bumps of 3.
const COOKIE = (() => {
  const pts: string[] = [];
  for (let i = 0; i < 144; i++) { const a = (i / 144) * Math.PI * 2, r = 31 + 3 * Math.cos(12 * a); pts.push(`${(r * Math.cos(a)).toFixed(2)} ${(r * Math.sin(a)).toFixed(2)}`); }
  return `M${pts.join("L")}Z`;
})();
// The cookie: a scalloped badge that turns slowly behind the status icon.
function Cookie({ icon }: { icon: IconName }) {
  return (
    <div className="sn-cookie">
      <svg className="cookie" aria-hidden="true" viewBox="-36 -36 72 72"><path d={COOKIE} /></svg>
      <Ms n={icon} fill />
    </div>
  );
}

export const sentence = (s: string) => s.charAt(0).toUpperCase() + s.slice(1).toLowerCase();  // "ask" is Ask, never ASK
const ago = (t: number) => {
  const m = Math.max(0, Math.round((Date.now() / 1000 - t) / 60));
  return m < 1 ? "now" : m < 60 ? `${m} min` : m < 1440 ? `${Math.round(m / 60)} h` : `${Math.round(m / 1440)} d`;
};
export const niceHost = (h: string) => h.replace(/^(worker|desktop)-/, "").replace(/\.local$/, "");
export const cap = (s: string) => (s.length <= 3 ? s.toUpperCase() : s.charAt(0).toUpperCase() + s.slice(1));  // "pc" is PC, "laptop" is Laptop

// The newest finished jobs as a list: a round tonal avatar for the kind, what happened, where and how long ago.
function recentRows(jobs: Job[]) {
  return jobs.filter((j) => j.finished_at && (j.state === "succeeded" || j.state === "dead")).sort((a, b) => b.finished_at! - a.finished_at!).slice(0, 4).map((j) => {
    const ari = j.plugin === "ari";
    const bad = j.state === "dead";
    const took = j.finished_at! - j.created_at;
    return {
      id: j.id, icon: (bad ? "close" : ari ? "graphic_eq" : "done_all") as IconName, tone: bad ? "err" : ari ? "ter" : "ok",
      title: ari ? `Ari ${j.workflow.replace(/_/g, " ")}` : `${sentence(j.workflow.replace(/_/g, " "))} ${bad ? "failed" : "finished"}`,
      sub: ari && !bad ? `Answered in ${took.toFixed(1)} s` : `${j.plugin}${j.lease_owner ? ` · ${niceHost(j.lease_owner)}` : ""}`, at: j.finished_at!,
    };
  });
}

export function PhoneHome({ status, events, live, down, onSelect, onAri, onOpen, onInbox, onAsk }: {
  status: Status | null; events: ArgusEvent[]; live: boolean; down: boolean; onSelect: (s: { type: "job"; id: string }) => void;
  onAri: (mic: boolean) => void; onOpen: (view: string) => void; onInbox: () => void; onAsk: (text: string) => void;
}) {
  const [q, setQ] = useState("");
  const d = useHomeData(events);
  const [busy, setBusy] = useState<string | null>(null);
  const j = status?.jobs ?? {};
  const running = (j.running ?? 0) + (j.leased ?? 0);
  const queued = (j.queued ?? 0) + (j.retry ?? 0);
  const workers = status?.workers ?? [];
  const hosts = [...new Set(workers.map((w) => w.host))];
  const hostsOn = new Set(workers.filter((w) => w.state === "online").map((w) => w.host));
  const first = d.pending[0];
  const ok = live && !down && status?.status === "ok";
  const decide = async (id: string, answer: "approve" | "reject") => {
    setBusy(id);
    try { await api(`/approvals/${id}/decide`, { method: "POST", body: JSON.stringify({ answer, by: "phone" }) }); await d.reload(); }
    finally { setBusy(null); }
  };
  const recent = recentRows(d.jobs);
  return (
    <div className="sn-stack home">
      <section className="sn-status-card" aria-label="Status">
        <div className="grow">
          <div className="sn-sc-l">Status</div>
          <div className="sn-sc-h">{headline(status, live, down)}</div>
          <div className="sn-chips">
            <span className="sn-mini"><Ms n="play_arrow" fill />{running} running</span>
            <span className="sn-mini"><Ms n="schedule" />{queued} queued</span>
            <span className="sn-mini"><Ms n="devices" />{hosts.length} {hosts.length === 1 ? "machine" : "machines"}</span>
          </div>
        </div>
        <Cookie icon={ok ? "check" : "info"} />
      </section>

      {first && (
        <section className="sn-waiting-card" aria-label="Waiting for you">
          <button type="button" className="sn-wc-main" onClick={onInbox}>
            <span className="sn-wc-tile"><Ms n={/delete|remove|clean/i.test(first.title) ? "folder_delete" : "pending_actions"} fill /></span>
            <span className="sn-row-t"><span className="sn-wc-l">Needs you{d.pending.length > 1 ? ` · 1 of ${d.pending.length}` : ""}</span><b>{first.title}</b></span>
          </button>
          <button type="button" className="sn-wc-btn" disabled={busy === first.id} onClick={() => decide(first.id, "approve")}>Approve</button>
        </section>
      )}

      <form className="sn-askbar" onSubmit={(e) => { e.preventDefault(); if (q.trim()) { onAsk(q.trim()); setQ(""); } }}>
        <Ms n="graphic_eq" />
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask Ari" aria-label="Ask Ari" enterKeyHint="send" />
        <button type="button" className="sn-fab" aria-label="Talk to Ari" onClick={() => onAri(true)}><Ms n="mic" fill /></button>
      </form>

      <div className="sn-pair">
        <button type="button" className="sn-card sn-tap" aria-label={`Today: ${d.today} jobs ran. Open runs`} onClick={() => onOpen("runs")}>
          <span className="sn-lbl">Today</span>
          <span className="sn-big">{d.today} <small>{d.today === 1 ? "job" : "jobs"}</small></span>
          {d.failed > 0 && <span className="sn-sub"><span className="c-coral">{d.failed} failed</span></span>}
          <Sparkline data={d.hours} />
        </button>
        <button type="button" className="sn-card sn-tap" aria-label="Machines. Open power" onClick={() => onOpen("power")}>
          <span className="sn-lbl">Machines</span>
          {hosts.length === 0 && <span className="sn-mach"><span className="sn-dot d-coral" />No machines</span>}
          {hosts.slice(0, 3).map((h) => (
            <span key={h} className="sn-mach"><span className={`sn-dot ${hostsOn.has(h) ? "d-green live" : "d-coral"}`} />{cap(niceHost(h))} {hostsOn.has(h) ? "online" : "offline"}</span>
          ))}
        </button>
      </div>

      {recent.length > 0 && (
        <section className="sn-recent" aria-label="Recent">
          {recent.map((r) => (
            <button key={r.id} type="button" className="sn-item" onClick={() => onSelect({ type: "job", id: r.id })}>
              <span className={`sn-av av-${r.tone}`}><Ms n={r.icon} fill /></span>
              <span className="sn-row-t"><b>{r.title}</b><span>{r.sub}</span></span>
              <span className="sn-when">{ago(r.at)}</span>
            </button>
          ))}
        </section>
      )}
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
    ["plugins", "extension", "violet", "Plugins", "What Argus can do, and when it runs"],
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
  home: ["Home", "home"], map: ["Map", "hub"], ari: ["Ari", "graphic_eq"], inbox: ["Inbox", "inbox"], more: ["More", "menu"],
};

export function PhoneTabs({ tab, onTab, waiting = 0 }: { tab: PhoneTab; onTab: (t: PhoneTab) => void; waiting?: number }) {
  return (
    <nav className="sn-tabs" aria-label="Pages">
      {PHONE_TABS.map((t) => (
        <button key={t} type="button" className="sn-tab" aria-current={tab === t ? "page" : undefined} onClick={() => onTab(t)}
          aria-label={t === "inbox" && waiting > 0 ? `Inbox, ${waiting} waiting` : TAB_INFO[t][0]}>
          <span className="ind"><Ms n={TAB_INFO[t][1]} fill={tab === t} />{t === "inbox" && waiting > 0 && <span className="sn-badge" aria-hidden="true">{waiting}</span>}</span>{TAB_INFO[t][0]}
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
