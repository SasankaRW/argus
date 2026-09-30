import { useEffect, useState } from "react";
import { api, Approval, ArgusEvent, Job, Status, useTimeSaved } from "./api";
import { ago, tone } from "./format";
import { AskBox } from "./AskBox";
import { ApprovalCard } from "./Inspector";
import type { Selection } from "./MapView";
import { PowerControls } from "./PowerView";
import { prio, useQueue, waitText, whyText } from "./QueueView";

// The phone's Helios: what needs you, what runs, what's next, what just finished. The full dashboard is one tap away.
export function PhoneHome({ status, events, onSelect, onShare, onFull, onView, onAri }: {
  status: Status | null; events: ArgusEvent[]; onSelect: (s: Selection) => void; onShare: () => void; onFull: () => void;
  onView: (v: string) => void; onAri: () => void;
}) {
  const q = useQueue(events);
  const saved = useTimeSaved();
  const [pending, setPending] = useState<Approval[]>([]);
  const [recent, setRecent] = useState<Job[]>([]);
  const last = events.filter((e) => e.kind.startsWith("approval.") || e.kind.startsWith("job.")).map((e) => e.seq).pop() ?? 0;
  useEffect(() => {
    api<Approval[]>("/approvals?state=pending").then(setPending).catch(() => {});
    api<Job[]>("/jobs?limit=30").then((js) => setRecent(js.filter((j) => ["succeeded", "dead", "cancelled"].includes(j.state)).slice(0, 8))).catch(() => {});
  }, [last]);
  const ok = status?.status === "ok";
  return (
    <div className="phone">
      <div className={`pstatus ${ok ? "ok" : "bad"}`}>
        <span className="dot" style={{ background: ok ? "var(--ok)" : "var(--amber)" }} />
        <b>{ok ? "Argus is running" : status ? "Argus needs a look" : "Connecting…"}</b>
        {q && <span className="muted">{q.workers_online ? "PC online" : "PC offline"}</span>}
      </div>

      <AskBox big onSelect={onSelect} onView={onView} />

      <button type="button" className="pshare pari" onClick={onAri}>
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M4 5h16v11H9l-5 4z" /></svg>
        Talk to Ari
      </button>

      <button type="button" className="pshare" onClick={onShare}>
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M12 15V3M7 8l5-5 5 5M5 13v6h14v-6" /></svg>
        Send something to Argus
      </button>

      {pending.length > 0 && (
        <section className="pcard">
          <h3>Needs you <span className="count">{pending.length}</span></h3>
          {pending.map((a) => <ApprovalCard key={a.id} id={a.id} />)}
        </section>
      )}

      <section className="pcard">
        <h3>Now {q && q.running.length > 0 && <span className="count">{q.running.length}</span>}</h3>
        {!q ? <div className="tip">Loading…</div> : q.running.length === 0 && q.waiting.length === 0 ? <div className="tip">Nothing running.</div> : null}
        {q?.running.map((j) => (
          <button key={j.id} type="button" className="pitem" onClick={() => onSelect({ type: "job", id: j.id })}>
            <span className="sdot flow" /><span className="pname">{j.plugin} · {j.workflow}</span>
            <span className="psub">{j.step ?? "starting"}</span>
          </button>
        ))}
        {q?.waiting.map((j) => (
          <button key={j.id} type="button" className="pitem" onClick={() => onSelect({ type: "job", id: j.id })}>
            <span className="sdot warn" /><span className="pname">{j.plugin} · {j.workflow}</span>
            <span className="psub">waits for {waitText(j)}</span>
          </button>
        ))}
      </section>

      <section className="pcard">
        <h3>Up next {q && q.queued.length > 0 && <span className="count">{q.queued.length}</span>}</h3>
        {q && q.queued.length === 0 && <div className="tip">Queue is empty.</div>}
        {q?.queued.slice(0, 6).map((j) => (
          <button key={j.id} type="button" className="pitem" onClick={() => onSelect({ type: "job", id: j.id })}>
            <span className="pos mono">{j.position}</span><span className="pname">{j.plugin} · {j.workflow}</span>
            <span className="psub">{whyText(j)} · {prio(j.priority)}</span>
          </button>
        ))}
        {q && q.queued.length > 6 && <div className="tip">+ {q.queued.length - 6} more</div>}
      </section>

      {saved && saved.seconds > 0 && (
        <section className="pcard">
          <h3>Saved you this week</h3>
          <div className="psaved"><b>{saved.text}</b>
            <span className="muted">{saved.plugins.slice(0, 3).map((p) => p.plugin).join(" · ")}</span></div>
        </section>
      )}

      <section className="pcard">
        <h3>PC power</h3>
        <PowerControls events={events} onSelect={onSelect} compact />
      </section>

      <section className="pcard">
        <h3>Just finished</h3>
        {recent.length === 0 && <div className="tip">Nothing yet.</div>}
        {recent.map((j) => (
          <button key={j.id} type="button" className="pitem" onClick={() => onSelect({ type: "job", id: j.id })}>
            <span className={`sdot ${tone("job." + j.state)}`} /><span className="pname">{j.plugin} · {j.workflow}</span>
            <span className="psub">{j.state === "succeeded" ? "done" : j.state} · {ago(j.finished_at ?? j.updated_at)}</span>
          </button>
        ))}
      </section>

      <button type="button" className="btn pfull" onClick={onFull}>Open the full dashboard</button>
    </div>
  );
}
