import { useEffect, useState } from "react";
import { api } from "./api";

// Small live readouts the desktop shows in its status bar: the clock and Claude's calls today.

export function Clock() {
  const [t, setT] = useState(new Date());
  useEffect(() => { const i = window.setInterval(() => setT(new Date()), 1000); return () => window.clearInterval(i); }, []);
  const p = (n: number) => String(n).padStart(2, "0");
  return <b className="clock num" role="timer" aria-label={`${p(t.getHours())}:${p(t.getMinutes())}`}>{p(t.getHours())}:{p(t.getMinutes())}</b>;
}

export type ClaudeUse = { calls_today: number; calls_per_day: number; logged_in: boolean | null };

// Claude today: calls used of the daily cap, and whether the CLI is logged in (checked by the workers).
export function useClaude(): ClaudeUse | null {
  const [c, setC] = useState<ClaudeUse | null>(null);
  useEffect(() => {
    const load = () => api<{ claude: ClaudeUse }>("/models").then((m) => setC(m.claude)).catch(() => {});
    load();
    const id = setInterval(load, 60000);
    return () => clearInterval(id);
  }, []);
  return c;
}

export function ClaudeChip() {
  const c = useClaude();
  if (!c) return null;
  if (c.logged_in === false) {
    return <span className="seg-s claude-chip bad" title="Run `claude` on the PC and log in (/login)"><span className="dot" style={{ background: "var(--bad)" }} />Claude is logged out</span>;
  }
  const left = c.calls_per_day - c.calls_today;
  return (
    <span className="seg-s claude-chip hide-sm" title={`Claude calls today: ${c.calls_today} of ${c.calls_per_day}`}>
      Claude <b>{c.calls_today} of {c.calls_per_day}</b>
      {left <= 3 && <span className={left <= 0 ? "bad" : "warn"}>{left <= 0 ? " · none left" : ` · ${left} left`}</span>}
    </span>
  );
}
