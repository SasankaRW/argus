import { useMemo, useState } from "react";
import type { ArgusEvent, Job, Status } from "./api";
import { cap, niceHost } from "./Phone";
import { clock, detail, shortId, tone } from "./format";
import type { Selection } from "./MapView";
import { Ms } from "./m3";

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

// The event stream, a footer panel under every page: one line with the latest event, opens into the list (the board's
// "Events" footer). Open or folded is remembered per browser.
const kindText = (k: string) => k.replace(/[._]/g, " ").replace(/^./, (c) => c.toUpperCase());
const hhmm = (t: number) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

// The board's timeline: one lane per machine, a bar for every job of the last hour, a marker for now.
function Lanes({ jobs, workers }: { jobs: Job[]; workers: Status["workers"] }) {
  const now = Date.now() / 1000, from = now - 3600;
  const hosts = [...new Set(workers.map((w) => w.host))].slice(0, 3);
  const owner = (j: Job) => workers.find((w) => w.id === j.lease_owner || (j.lease_owner ?? "").replace(/-now$/, "") === w.id)?.host;
  return (
    <div className="tlk-lanes">
      {hosts.length === 0 && <div className="tlk-lane"><span className="ns">No machines yet</span></div>}
      {hosts.map((h) => (
        <div key={h} className="tlk-lane">
          <span className="ns">{cap(niceHost(h))}</span>
          <div className="tlk-track">
            {jobs.filter((j) => owner(j) === h && (j.finished_at ?? now) >= from).map((j) => {
              const a = Math.max(j.created_at, from), b = j.finished_at ?? now;
              const bad = j.state === "dead", ari = j.plugin === "ari", run = !j.finished_at;
              return <span key={j.id} className="tlk-job" title={`${j.plugin} · ${j.workflow}`}
                style={{ left: `${((a - from) / 3600) * 100}%`, width: `${Math.max(1.4, ((b - a) / 3600) * 100)}%`, background: bad ? "var(--bad)" : ari ? "var(--violet)" : run ? "var(--ac)" : "var(--ok)" }} />;
            })}
          </div>
        </div>
      ))}
      <span className="tlk-now" aria-hidden="true" />
    </div>
  );
}

export function EventsPanel({ events, conn, onSelect, lanes }: { events: ArgusEvent[]; conn: string; onSelect: (s: Selection) => void; lanes?: { jobs: Job[]; workers: Status["workers"] } }) {
  const [f, setF] = useState("all");
  const [open, setOpen] = useState(() => { try { return localStorage.getItem("helios.dock") === "1"; } catch { return false; } });
  const toggle = () => { setOpen(!open); try { localStorage.setItem("helios.dock", open ? "0" : "1"); } catch { /* private */ } };
  const rows = useMemo(() => {
    const test = FILTERS.find((x) => x[0] === f)![2];
    return events.filter((e) => test(e.kind)).slice(-150).reverse();
  }, [events, f]);
  const last = events[events.length - 1];
  return (
    <section className={`dock${open ? " open" : ""}${lanes ? " tlk float" : ""}`} aria-label={lanes ? "Timeline" : "Events"}>
      {lanes && (
        <div className="dock-h tlk-h">
          <span className="tlk-t">Timeline · last hour</span>
          <span className="grow" />
          <span className="mono tlk-tick">{hhmm(Date.now() / 1000 - 3600)}</span><span className="mono tlk-tick">{hhmm(Date.now() / 1000 - 1800)}</span><span className="mono tlk-tick now">now</span>
          <button type="button" className="dock-x" onClick={toggle} aria-expanded={open} aria-label={open ? "Fold the events" : "Expand timeline"}><Ms n={open ? "expand_more" : "expand_less"} /></button>
        </div>
      )}
      {lanes && <Lanes jobs={lanes.jobs} workers={lanes.workers} />}
      {lanes && open && (
        <div className="dock-h tlk-filters">
          <div className="tabs-t" role="group" aria-label="Filter events">
            {FILTERS.map(([id, label]) => (
              <button key={id} type="button" aria-pressed={f === id} onClick={() => setF(id)}>{label}</button>
            ))}
          </div>
        </div>
      )}
      {!lanes && <div className="dock-h">
        <button type="button" className="dock-t" onClick={toggle} aria-expanded={open} aria-label="Events">
          <span className="sn-lbl">Events</span>
        </button>
        {open ? (
          <div className="tabs-t" role="group" aria-label="Filter events">
            {FILTERS.map(([id, label]) => (
              <button key={id} type="button" aria-pressed={f === id} onClick={() => setF(id)}>{label}</button>
            ))}
          </div>
        ) : last ? (
          <button type="button" className="dock-last" onClick={toggle} tabIndex={-1} aria-hidden="true"><span className="dim">{clock(last.at).slice(0, 8)}</span><span className={tone(last.kind)}>{kindText(last.kind)}</span><span className="dim">{last.from ?? ""}{last.to ? ` → ${last.to}` : ""}</span></button>
        ) : null}
        <span className="live-ind">
          <span className={`sn-dot ${conn === "live" ? "d-green" : conn === "connecting" ? "d-amber" : "d-coral"}`} />
          {conn === "live" ? "Live" : conn === "connecting" ? "Connecting" : "Offline"}
        </span>
        <button type="button" className="dock-x" onClick={toggle} aria-label={open ? "Fold the events" : "Show the events"} tabIndex={-1}><Ms n={open ? "expand_more" : "expand_less"} /></button>
      </div>}
      {open && (
        <div className="log">
          {rows.length === 0 && <div className="ev"><span className="dim">Waiting for events. Start a worker and run a demo job.</span></div>}
          {rows.map((e) => (
            <button key={e.seq} type="button" className="ev" onClick={() => e.job_id ? onSelect({ type: "job", id: e.job_id }) : e.from && e.to ? onSelect({ type: "edge", src: e.from, dst: e.to }) : e.from && onSelect({ type: "node", id: e.from })}>
              <span className="t">{clock(e.at)}</span>
              <span className={`k ${tone(e.kind)}`}>{kindText(e.kind)}</span>
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
