import { useClaude } from "./chips";
import { Ms } from "./m3";
import { avgRun, lastHour, startOfDay } from "./jobsFeed";
import { boxInfo, sub } from "./MapView";
import type { ArgusMap, Job } from "./api";

// The compact inspector for a selected box (the board's top-right card): ring gauges, throughput in the last hour and the
// newest jobs. Every number comes from the jobs list, the box's own counts or Claude's usage; a gauge with no data is not drawn.

function Ring({ pct, label, tone }: { pct: number; label: string; tone: "pri" | "ter" | "ok" }) {
  const v = Math.max(0, Math.min(1, pct));
  return (
    <div className="ic-gauge">
      <svg aria-hidden="true" width="38" height="38" viewBox="0 0 44 44">
        <circle cx="22" cy="22" r="18" fill="none" stroke="#33333a" strokeWidth="5" />
        <circle className={`ring ${tone}`} cx="22" cy="22" r="18" fill="none" strokeWidth="5" strokeLinecap="round" strokeDasharray="113" strokeDashoffset={113 * (1 - v)} transform="rotate(-90 22 22)" />
      </svg>
      <div className="num ic-v">{Math.round(v * 100)}%</div>
      <div className="ic-k">{label}</div>
    </div>
  );
}

export function InspectCard({ id, map, jobs, onClose, onDetails, queue, byWorker }: {
  id: string; map: ArgusMap; jobs: Job[]; onClose: () => void; onDetails: () => void;
  queue: { running: number; queued: number } | null; byWorker: Record<string, number>;
}) {
  const claude = useClaude();
  const n = map.nodes.find((x) => x.id === id);
  if (!n) return null;
  const info = boxInfo(n, { queue, byWorker });
  const today = jobs.filter((j) => j.created_at >= startOfDay());
  const done = today.filter((j) => j.state === "succeeded").length, bad = today.filter((j) => j.state === "dead").length;
  const mine = n.kind === "plugin" ? jobs.filter((j) => j.plugin === n.id) : n.kind === "worker" ? jobs.filter((j) => j.lease_owner === n.id) : jobs;
  const hour = lastHour(mine);
  const avg = avgRun(mine);
  const max = Math.max(1, ...hour.buckets);
  const d = hour.buckets.map((v, i) => `${i ? "L" : "M"}${(i * 300 / 11).toFixed(1)} ${(52 - (v / max) * 44).toFixed(1)}`).join(" ");
  const busy = info.meter;
  const recent = mine.filter((j) => j.finished_at || j.state === "running").slice(0, 3);
  return (
    <div className="ic">
      <div className="ic-h">
        <span className={`ni t-${info.tone}`}><Ms n={info.icon} fill /></span>
        <div className="grow"><div className="ic-t">{n.label}</div><div className="ns">{sub(n)}</div></div>
        <button type="button" className="ic-x" aria-label="Close inspector" onClick={onClose}><Ms n="close" /></button>
      </div>
      <div className="ic-gauges">
        {done + bad > 0 && <Ring pct={done / (done + bad)} label="Success" tone="ok" />}
        {busy !== undefined && <Ring pct={busy} label="Busy" tone="pri" />}
        {claude && (n.kind === "model" || n.kind === "core") && claude.calls_per_day > 0 && <Ring pct={claude.calls_today / claude.calls_per_day} label="Claude" tone="ter" />}
      </div>
      <div className="ic-card">
        <div className="ic-row"><span>Throughput · last hour</span><span className="num ic-v">{hour.count} / hr</span></div>
        <svg aria-hidden="true" width="100%" viewBox="0 0 300 56" className="ic-spark"><path className="spark" d={d} pathLength={1} /></svg>
      </div>
      {recent.length > 0 && (
        <div className="ic-jobs">
          {recent.map((j) => (
            <div key={j.id} className="ic-job">
              <span className="dot" style={{ background: j.state === "dead" ? "var(--bad)" : j.plugin === "ari" ? "var(--violet)" : "var(--ok)" }} />
              <span className="grow">{j.plugin} · {j.workflow.replace(/_/g, " ")}</span>
              <span className="mono num">{j.finished_at ? `${(j.finished_at - j.created_at).toFixed(1)} s` : "running"}</span>
            </div>
          ))}
        </div>
      )}
      <div className="ic-foot">{avg !== null && <span className="ns">Average run today {avg.toFixed(1)} s</span>}<span className="grow" /><button type="button" className="sn-link" onClick={onDetails}>Details</button></div>
    </div>
  );
}
