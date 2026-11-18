import { useEffect, useRef, useState } from "react";
import { api, type Deploy } from "./api";
import { ago } from "./format";

// What the laptop runs and how its updater is doing (GET /deploy), as a few lines: the desktop's "Up ..." chip opens
// them in a card, the phone shows them in Settings.

export function useDeploy(on = true): Deploy | null {
  const [d, setD] = useState<Deploy | null>(null);
  useEffect(() => {
    if (!on) return;
    const load = () => api<Deploy>("/deploy").then(setD).catch(() => {});
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [on]);
  return d;
}

const RESULT: Record<string, string> = { updated: "Updated", rolled_back: "Rolled back", failed: "Failed" };

/** Short lines the user can read at a glance; `bad` ones show in the error colour. */
export function deployLines(d: Deploy): { text: string; bad?: boolean }[] {
  const lines: { text: string; bad?: boolean }[] = [];
  const short = d.commit ? d.commit.slice(0, 8) : "unknown";
  lines.push({ text: `Running ${short}${d.commit_subject ? ` · ${d.commit_subject}` : ""}` });
  if (d.started_at) lines.push({ text: `Started ${ago(d.started_at)}` });
  const u = d.last_update;
  if (u) {
    const what = RESULT[u.result] ?? u.result;
    const bad = u.result !== "updated";
    lines.push({ text: `${what} ${ago(u.at)}${bad ? ` at ${u.step}` : ""}${u.subject ? ` · ${u.subject}` : ""}`, bad });
    if (bad && u.detail) lines.push({ text: u.detail, bad: true });
  } else {
    lines.push({ text: "No update recorded yet" });
  }
  const behind = d.last_check?.behind ?? 0;
  if (behind > 0) lines.push({ text: `${behind} ${behind === 1 ? "merge" : "merges"} waiting` });
  if (d.last_check?.error) lines.push({ text: `Last check failed: ${d.last_check.error}`, bad: true });
  if (d.stale) lines.push({ text: "Updater not running", bad: true });
  return lines;
}

/** True when something needs a look: the last update did not take, or the updater has gone quiet. */
export function deployBad(d: Deploy | null): boolean {
  return !!d && (d.stale || (!!d.last_update && d.last_update.result !== "updated"));
}

export function DeployLines({ d }: { d: Deploy | null }) {
  if (!d) return <div className="dep-lines"><span className="dep-l faint">Not available</span></div>;
  return (
    <div className="dep-lines">
      {deployLines(d).map((l, i) => <span key={i} className={`dep-l${l.bad ? " bad" : ""}`}>{l.text}</span>)}
    </div>
  );
}

/** The desktop top bar's "Up 48 s" chip; a click opens the card under it. */
export function DeployChip({ label, title }: { label: string; title?: string }) {
  const [open, setOpen] = useState(false);
  const d = useDeploy(true);
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const down = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", down);
    document.addEventListener("keydown", key);
    return () => { document.removeEventListener("mousedown", down); document.removeEventListener("keydown", key); };
  }, [open]);
  const bad = deployBad(d);
  return (
    <span className="dep-wrap hide-sm" ref={ref}>
      <button type="button" className={`seg-s dep-chip${bad ? " bad" : " faint"}`} title={title} aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        {bad && <span className="sn-dot bad" />}{label}
      </button>
      {open && <div className="dep-card" role="dialog" aria-label="What Argus runs"><DeployLines d={d} /></div>}
    </span>
  );
}
