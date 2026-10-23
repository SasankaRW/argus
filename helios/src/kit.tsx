// The shared Helios kit: every page uses the same header, vitals strip, sparkline, skeleton and empty state,
// so the pages read as one product. Styles live in kit.css.
import { Component, ErrorInfo, ReactNode, useEffect, useRef, useState } from "react";
import { Status } from "./api";

export type PageMeta = { title: string; sub: string; tag: string };

// One line of identity per page: the eyebrow (tag), the title and what the page is for.
export const PAGES: Record<string, PageMeta> = {
  inbox: { tag: "waiting", title: "Inbox", sub: "Things Argus needs from you: approvals, questions and results." },
  notifications: { tag: "signals", title: "Notifications", sub: "What Argus told you and when." },
  ari: { tag: "assistant", title: "Ari", sub: "Talk to Ari, review chats and scheduled things." },
  plugins: { tag: "modules", title: "Plugins", sub: "Every capability, its mode and its rules." },
  queue: { tag: "pipeline", title: "Queue", sub: "What is running now and what is next." },
  runs: { tag: "history", title: "Runs", sub: "Every job, step by step." },
  share: { tag: "send", title: "Share", sub: "Send a file, a link or a note to a device." },
  power: { tag: "hardware", title: "Power", sub: "Wake, sleep and find your machines." },
  models: { tag: "intelligence", title: "Models", sub: "Local models, Claude usage and levels." },
  settings: { tag: "control", title: "Settings", sub: "Tune Argus without touching files." },
  logs: { tag: "trace", title: "Logs", sub: "The raw log, searchable and live." },
  voice: { tag: "voice", title: "Voice", sub: "Train how Ari hears you." },
  rules: { tag: "modules", title: "Rules", sub: "Edit a plugin's rules." },
};

export function pageMeta(view: string): PageMeta | null {
  const root = view.split("/")[0];
  return PAGES[root] ?? null;
}

// ---- history: a short ring buffer of a number, for sparklines
export function useTrail(value: number, size = 40): number[] {
  const [trail, setTrail] = useState<number[]>(() => Array(size).fill(value));
  const last = useRef(value);
  useEffect(() => {
    last.current = value;
  }, [value]);
  useEffect(() => {
    // sample every 3s so the line moves even while the value is flat
    const id = window.setInterval(() => {
      if (document.hidden) return;
      setTrail((t) => [...t.slice(-(size - 1)), last.current]);
    }, 3000);
    return () => window.clearInterval(id);
  }, [size]);
  return trail;
}

// A tiny area sparkline (SVG, no library).
export function Spark({ data, color = "var(--tg)", w = 84, h = 26 }: { data: number[]; color?: string; w?: number; h?: number }) {
  const max = Math.max(1, ...data);
  const min = Math.min(0, ...data);
  const span = max - min || 1;
  const step = w / Math.max(1, data.length - 1);
  const pts = data.map((v, i) => [i * step, h - 2 - ((v - min) / span) * (h - 4)] as const);
  const line = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
  const area = `${line} L${w} ${h} L0 ${h} Z`;
  const id = useRef(`sg${Math.random().toString(36).slice(2, 8)}`).current;
  const end = pts[pts.length - 1];
  return (
    <svg className="sparkl" width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden="true" style={{ color }}>
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="currentColor" stopOpacity=".35" />
          <stop offset="1" stopColor="currentColor" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill={`url(#${id})`} />
      <path d={line} fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" strokeLinecap="round" />
      {end && <circle cx={end[0]} cy={end[1]} r="2" fill="currentColor" />}
    </svg>
  );
}

// A number that counts to its new value instead of jumping.
export function Tick({ v }: { v: number | string }) {
  const [shown, setShown] = useState(v);
  const from = useRef(v);
  useEffect(() => {
    if (typeof v !== "number" || typeof from.current !== "number" || from.current === v ||
        window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      from.current = v; setShown(v); return;
    }
    const a = from.current, b = v, t0 = performance.now(), dur = 420;
    let raf = 0;
    const step = (t: number) => {
      const p = Math.min(1, (t - t0) / dur);
      setShown(Math.round(a + (b - a) * (1 - Math.pow(1 - p, 3))));
      if (p < 1) raf = requestAnimationFrame(step); else from.current = b;
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [v]);
  return <>{shown}</>;
}

// The system strip: always mounted under the top bar (never re-mounts between pages), so the trails keep moving.
export function Vitals({ status }: { status: Status | null }) {
  const j = status?.jobs ?? {};
  const running = (j.leased ?? 0) + (j.running ?? 0);
  const queued = (j.queued ?? 0) + (j.retry ?? 0);
  const waiting = j.waiting ?? 0;
  const dead = j.dead ?? 0;
  const online = status?.workers.filter((w) => w.state === "online").length ?? 0;
  const total = status?.workers.length ?? 0;
  const tr = useTrail(running), tq = useTrail(queued), tw = useTrail(waiting), td = useTrail(dead), to = useTrail(online);
  const tiles: { k: string; v: number; sub: string; trail: number[]; c: string; of?: number }[] = [
    { k: "running", v: running, sub: queued ? `${queued} queued` : "queue empty", trail: running ? tr : tq, c: "var(--flow)" },
    { k: "waiting", v: waiting, sub: "for you or a resume", trail: tw, c: waiting ? "var(--amber)" : "var(--tx3)" },
    { k: "failed", v: dead, sub: "after retries", trail: td, c: dead ? "var(--bad)" : "var(--tx3)" },
    { k: "workers", v: online, of: total, sub: total - online ? `${total - online} offline` : "all online", trail: to, c: online ? "var(--tg)" : "var(--bad)" },
  ];
  return (
    <div className="vitals" role="group" aria-label="Argus vitals">
      {tiles.map((t) => (
        <div key={t.k} className="vt" style={{ ["--c" as string]: t.c }}>
          <div className="vt-txt">
            <span className="vt-k"><i />{t.k}</span>
            <span className="vt-line"><span className="vt-v"><Tick v={t.v} />{t.of !== undefined && <small>/{t.of}</small>}</span><span className="vt-s">{t.sub}</span></span>
          </div>
          <Spark data={t.trail} color={t.c} w={96} h={30} />
        </div>
      ))}
    </div>
  );
}

// The header every page starts with: eyebrow, title, one-line purpose, live pill, page actions and the vitals.
export function PageHead({ meta, live, children }: { meta: PageMeta; live: boolean; children?: ReactNode }) {
  return (
    <header className="pagehead">
      <div className="ph-row">
        <div className="ph-id">
          <span className="ph-tag">// {meta.tag}</span>
          <h1>{meta.title}</h1>
          <p>{meta.sub}</p>
        </div>
        <div className="ph-side">
          <span className={`livepill${live ? " on" : ""}`}><i />{live ? "live" : "offline"}</span>
          {children}
        </div>
      </div>
    </header>
  );
}

export function Skeleton({ rows = 5 }: { rows?: number }) {
  return (
    <div className="skel panel" aria-busy="true" aria-label="Loading">
      <div className="skel-h" />
      {Array.from({ length: rows }, (_, i) => <div key={i} className="skel-r" style={{ width: `${92 - ((i * 13) % 38)}%` }} />)}
    </div>
  );
}

export function Empty({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="empty-state">
      <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.2" aria-hidden="true">
        <circle cx="12" cy="12" r="9" strokeDasharray="2 3" /><circle cx="12" cy="12" r="3" />
      </svg>
      <b>{title}</b>
      {hint && <span>{hint}</span>}
    </div>
  );
}

// Pause every CSS animation while the tab is hidden (set on <html>, read by kit.css).
export function usePauseWhenHidden() {
  useEffect(() => {
    const on = () => { document.documentElement.dataset.paused = document.hidden ? "1" : ""; };
    on();
    document.addEventListener("visibilitychange", on);
    return () => document.removeEventListener("visibilitychange", on);
  }, []);
}

// One page failing shows a card with a retry, instead of a black screen for the whole app.
export class PageBoundary extends Component<{ children: ReactNode; name: string }, { err: Error | null }> {
  state = { err: null as Error | null };
  static getDerivedStateFromError(err: Error) { return { err }; }
  componentDidCatch(err: Error, info: ErrorInfo) { console.error(`[helios] ${this.props.name} failed`, err, info.componentStack); }
  componentDidUpdate(prev: { name: string }) { if (prev.name !== this.props.name && this.state.err) this.setState({ err: null }); }
  render() {
    if (!this.state.err) return this.props.children;
    return (
      <div className="panel errcard" role="alert">
        <span className="ph-tag">// error</span>
        <b>This page hit a problem.</b>
        <code>{this.state.err.message}</code>
        <button type="button" className="btn" onClick={() => this.setState({ err: null })}>Try again</button>
      </div>
    );
  }
}
