import { useMemo, useState } from "react";
import type { ArgusEvent } from "./api";
import { clock, detail, shortId, tone } from "./format";
import type { Selection } from "./MapView";

const FILTERS: [string, string, (k: string) => boolean][] = [
  ["all", "All", () => true],
  ["jobs", "Jobs", (k) => k.startsWith("job.")],
  ["steps", "Steps", (k) => k.startsWith("step.")],
  ["models", "Models", (k) => k.startsWith("model.") || k.startsWith("check.")],
  ["workers", "Workers", (k) => k.startsWith("worker.")],
  ["map", "Map", (k) => k === "component.added" || k === "edge.added"],
  ["problems", "Problems", (k) => /dead|failed|offline|retry|timeout|error|opened/.test(k)],
];

export function EventsPanel({ events, conn, onSelect }: { events: ArgusEvent[]; conn: string; onSelect: (s: Selection) => void }) {
  const [f, setF] = useState("all");
  const rows = useMemo(() => {
    const test = FILTERS.find((x) => x[0] === f)![2];
    return events.filter((e) => test(e.kind)).slice(-150).reverse();
  }, [events, f]);
  return (
    <section className="panel events" aria-label="Events">
      <div className="ph">
        <span className="pt">Events</span>
        <div className="seg" role="group" aria-label="Filter events">
          {FILTERS.map(([id, label]) => (
            <button key={id} type="button" aria-pressed={f === id} onClick={() => setF(id)}>{label}</button>
          ))}
        </div>
        <span className="muted live-ind">
          <span className={`dot ${conn === "live" ? "pulse" : ""}`} style={{ background: conn === "live" ? "var(--ok)" : conn === "connecting" ? "var(--amber)" : "var(--bad)" }} />
          {conn}
        </span>
      </div>
      <div className="log">
        {rows.length === 0 && <div className="tip" style={{ padding: "8px 18px" }}>No events yet. Start a worker and run a demo job.</div>}
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
    </section>
  );
}
