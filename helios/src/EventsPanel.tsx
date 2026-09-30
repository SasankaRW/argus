import { useMemo, useState } from "react";
import type { ArgusEvent } from "./api";
import { clock, detail, shortId, tone } from "./format";
import type { Selection } from "./MapView";

const FILTERS: [string, string, (k: string) => boolean][] = [
  ["all", "All", () => true],
  ["jobs", "Jobs", (k) => k.startsWith("job.")],
  ["steps", "Steps", (k) => k.startsWith("step.")],
  ["models", "Models", (k) => k.startsWith("model.") || k.startsWith("check.")],
  ["approvals", "Approvals", (k) => k.startsWith("approval.") || k.startsWith("outbox.") || k.startsWith("notify.")],
  ["workers", "Workers", (k) => k.startsWith("worker.")],
  ["map", "Map", (k) => k === "component.added" || k === "edge.added"],
  ["problems", "Problems", (k) => /dead|failed|offline|retry|timeout|error|opened/.test(k)],
];

// The event stream as a terminal pane docked at the bottom (tail -f); folds to one line, remembered per browser.
export function EventsPanel({ events, conn, onSelect }: { events: ArgusEvent[]; conn: string; onSelect: (s: Selection) => void }) {
  const [f, setF] = useState("all");
  const [open, setOpen] = useState(() => { try { return localStorage.getItem("helios.dock") !== "0"; } catch { return true; } });
  const toggle = () => { setOpen(!open); try { localStorage.setItem("helios.dock", open ? "0" : "1"); } catch { /* private */ } };
  const rows = useMemo(() => {
    const test = FILTERS.find((x) => x[0] === f)![2];
    return events.filter((e) => test(e.kind)).slice(-150).reverse();
  }, [events, f]);
  const last = events[events.length - 1];
  return (
    <section className={`dock${open ? " open" : ""}`} aria-label="Events">
      <div className="dock-h">
        <button type="button" className="dock-t" onClick={toggle} aria-expanded={open}>
          <span className="caret">{open ? "▾" : "▸"}</span>events <span className="dim">tail -f</span>
        </button>
        {open ? (
          <div className="tabs-t" role="group" aria-label="Filter events">
            {FILTERS.map(([id, label]) => (
              <button key={id} type="button" aria-pressed={f === id} onClick={() => setF(id)}>{label.toLowerCase()}</button>
            ))}
          </div>
        ) : last ? (
          <span className="dock-last mono"><span className="dim">{clock(last.at)}</span> <span className={tone(last.kind)}>{last.kind}</span> <span className="dim">{last.from ?? ""}{last.to ? ` → ${last.to}` : ""}</span></span>
        ) : null}
        <span className="live-ind mono">
          <span className={`dot ${conn === "live" ? "pulse" : ""}`} style={{ background: conn === "live" ? "var(--ok)" : conn === "connecting" ? "var(--amber)" : "var(--bad)" }} />
          {conn}
        </span>
      </div>
      {open && (
        <div className="log">
          {rows.length === 0 && <div className="ev mono"><span className="dim">$ waiting for events… start a worker and run a demo job</span></div>}
          {rows.map((e) => (
            <button key={e.seq} type="button" className="ev mono" onClick={() => e.job_id ? onSelect({ type: "job", id: e.job_id }) : e.from && e.to ? onSelect({ type: "edge", src: e.from, dst: e.to }) : e.from && onSelect({ type: "node", id: e.from })}>
              <span className="t">{clock(e.at)}</span>
              <span className={`k ${tone(e.kind)}`}>{e.kind}</span>
              <span className="r">{e.from ?? ""}{e.to ? ` → ${e.to}` : ""}</span>
              <span className="j">{shortId(e.job_id)}{e.step ? ` · ${e.step}` : ""}</span>
              <span className="d">{detail(e.data)}</span>
            </button>
          ))}
        </div>
      )}
    </section>
  );
}
