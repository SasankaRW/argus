// Inspector: details for whatever is selected on the map or in the event list.

import { useEffect, useState } from "react";
import { api, ArgusEvent, ArgusMap, Job, Status } from "./api";
import { ago, clock, pretty, shortId, tone, uptime } from "./format";
import type { Selection } from "./MapView";

type Props = {
  sel: Selection;
  map: ArgusMap;
  status: Status | null;
  events: ArgusEvent[];
  onSelect: (s: Selection) => void;
};

function KV({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="kv">
      <span className="k">{k}</span>
      <span className="mono v">{v}</span>
    </div>
  );
}

function EventList({ evs, onSelect, empty }: { evs: ArgusEvent[]; onSelect: (s: Selection) => void; empty: string }) {
  if (!evs.length) return <div className="tip">{empty}</div>;
  return (
    <div className="mini">
      {[...evs].reverse().map((e) => (
        <button key={e.seq} type="button" className="mrow" onClick={() => e.job_id && onSelect({ type: "job", id: e.job_id })} disabled={!e.job_id}>
          <span className="t mono">{clock(e.at).slice(0, 8)}</span>
          <span className={`kind mono ${tone(e.kind)}`}>{e.kind}</span>
          <span className="x mono">{e.job_id ? shortId(e.job_id) : ""}{e.step ? ` · ${e.step}` : ""}</span>
        </button>
      ))}
    </div>
  );
}

// History from the server for this box or line, merged with what has arrived live since.
function useHistory(key: string, query: string, live: ArgusEvent[], keep: (e: ArgusEvent) => boolean) {
  const [hist, setHist] = useState<ArgusEvent[]>([]);
  useEffect(() => {
    let off = false;
    setHist([]);
    api<{ events: ArgusEvent[] }>(`/events?newest=true&limit=60&${query}`)
      .then((r) => { if (!off) setHist(r.events.filter(keep)); })
      .catch(() => {});
    return () => { off = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const top = hist.length ? hist[hist.length - 1].seq : 0;
  return hist.concat(live.filter((e) => e.seq > top && keep(e))).slice(-40);
}

function NodePanel({ id, map, status, events, onSelect }: { id: string } & Omit<Props, "sel">) {
  const n = map.nodes.find((x) => x.id === id);
  const evs = useHistory(`n:${id}`, `component=${encodeURIComponent(id)}`, events, (e) => e.from === id || e.to === id);
  if (!n) return <div className="tip">This box is no longer on the map.</div>;
  const lines = map.edges.filter((e) => e.src === id || e.dst === id);
  const kind = n.implicit ? "Seen in events" : n.kind[0].toUpperCase() + n.kind.slice(1);
  return (
    <>
      <div className="ih">
        <span className="eyebrow">{kind}</span>
        <h2>{n.label}</h2>
        <p>{n.id !== n.label ? n.id : n.group ? `on ${n.group}` : ""}</p>
      </div>
      <div className="ib">
        {n.kind === "core" && status && (
          <>
            <KV k="Version" v={status.version} />
            <KV k="Instance" v={`${status.instance} on ${status.host}`} />
            <KV k="Uptime" v={uptime(status.uptime_seconds)} />
            <KV k="Live viewers" v={status.events.subscribers} />
          </>
        )}
        {n.kind === "worker" && (
          <>
            <KV k="State" v={<span className={n.state === "online" ? "ok" : "bad"}>{n.state}</span>} />
            <KV k="Host" v={n.group ?? "?"} />
            <KV k="Version" v={String(n.meta?.version ?? "?")} />
            <KV k="Capabilities" v={((n.meta?.capabilities as string[]) ?? []).join(", ") || "none"} />
          </>
        )}
        {n.kind === "plugin" && (
          <>
            <KV k="Running" v={n.jobs?.active ?? 0} />
            <KV k="Queued" v={n.jobs?.queued ?? 0} />
          </>
        )}
        <KV k="First seen" v={ago(n.first_seen)} />
        <KV k="Lines" v={lines.length ? lines.map((l) => (l.src === id ? `→ ${l.dst}` : `← ${l.src}`)).join(", ") : "none yet"} />
        <div className="sect">Recent activity</div>
        <EventList evs={evs} onSelect={onSelect} empty="Nothing yet. Activity appears here live." />
      </div>
    </>
  );
}

function EdgePanel({ src, dst, map, events, onSelect }: { src: string; dst: string } & Omit<Props, "sel" | "status">) {
  const e = map.edges.find((x) => x.src === src && x.dst === dst);
  const evs = useHistory(`e:${src}>${dst}`, `component=${encodeURIComponent(src)}`, events, (x) => x.from === src && x.to === dst);
  return (
    <>
      <div className="ih">
        <span className="eyebrow">Line</span>
        <h2>{src} → {dst}</h2>
        <p>{e ? `first used ${ago(e.first_seen)}` : ""}</p>
      </div>
      <div className="ib">
        {e && (
          <>
            <KV k="Messages" v={e.count.toLocaleString()} />
            <KV k="Last" v={`${e.last_kind} · ${ago(e.last_seen)}`} />
          </>
        )}
        <div className="sect">Recent messages</div>
        <EventList evs={evs} onSelect={onSelect} empty="No messages yet." />
      </div>
    </>
  );
}

function JobPanel({ id, events, onSelect }: { id: string; events: ArgusEvent[]; onSelect: (s: Selection) => void }) {
  const [job, setJob] = useState<Job | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const lastSeq = events.filter((e) => e.job_id === id).map((e) => e.seq).pop() ?? 0;
  useEffect(() => {
    api<Job>(`/jobs/${id}`).then(setJob).catch((e) => setErr(String(e)));
  }, [id, lastSeq]); // refresh whenever this job moves
  if (err) return <div className="ib"><div className="tip">Could not load job: {err}</div></div>;
  if (!job) return <div className="ib"><div className="tip">Loading…</div></div>;
  return (
    <>
      <div className="ih">
        <span className="eyebrow">Job · {shortId(job.id)}</span>
        <h2>{job.plugin}.{job.workflow}</h2>
        <p>
          <span className={`pill ${tone("job." + job.state)}`}>{job.state}</span>
          {" "}attempt {job.attempt} of {job.max_attempts}{job.lease_owner ? ` · ${job.lease_owner}` : ""}
        </p>
      </div>
      <div className="ib">
        <KV k="Created" v={ago(job.created_at)} />
        {job.finished_at && <KV k="Total time" v={`${(job.finished_at - job.created_at).toFixed(2)} s`} />}
        {job.wait_reason && <KV k="Waiting for" v={job.wait_reason} />}
        <div className="sect">Steps</div>
        {job.steps?.length ? (
          <div className="steps">
            {job.steps.map((s) => (
              <div key={s.idx} className="step">
                <span className={`sdot ${tone("step." + s.state)}`} />
                <span className="mono">{s.idx}. {s.name}</span>
                <span className="muted mono">{s.state}{s.tier_used ? ` · ${s.tier_used}` : ""}</span>
                {s.error && <div className="err mono">{s.error}</div>}
              </div>
            ))}
          </div>
        ) : <div className="tip">No steps recorded.</div>}
        {job.error && (<><div className="sect">Error</div><pre className="code bad">{job.error}</pre></>)}
        {job.result !== null && job.result !== undefined && (<><div className="sect">Result</div><pre className="code">{pretty(job.result)}</pre></>)}
        <div className="sect">Input</div>
        <pre className="code">{pretty(job.input) || "{}"}</pre>
        <div className="sect">Events</div>
        <EventList evs={events.filter((e) => e.job_id === id)} onSelect={onSelect} empty="Older than the live window." />
      </div>
    </>
  );
}

function Overview({ map, status }: { map: ArgusMap; status: Status | null }) {
  return (
    <>
      <div className="ih">
        <span className="eyebrow">Overview</span>
        <h2>{status ? `${status.instance} on ${status.host}` : "Argus"}</h2>
        <p>Select a box, a line or an event to inspect it.</p>
      </div>
      <div className="ib">
        <KV k="Boxes" v={map.nodes.length} />
        <KV k="Lines" v={map.edges.length} />
        <KV k="Messages on lines" v={map.edges.reduce((a, e) => a + e.count, 0).toLocaleString()} />
        {status && <KV k="Workers online" v={status.workers.filter((w) => w.state === "online").length} />}
        <div className="tip" style={{ marginTop: 16 }}>
          The map grows by itself: a new plugin, worker or app gets a box the first time it shows up, and a line appears the
          first time two parts talk. Drag boxes to arrange them; Auto layout puts them back.
        </div>
      </div>
    </>
  );
}

export function Inspector(p: Props) {
  const s = p.sel;
  return (
    <section className="panel insp" aria-label="Inspector">
      {!s && <Overview map={p.map} status={p.status} />}
      {s?.type === "node" && <NodePanel key={s.id} id={s.id} {...p} />}
      {s?.type === "edge" && <EdgePanel key={`${s.src}>${s.dst}`} src={s.src} dst={s.dst} {...p} />}
      {s?.type === "job" && <JobPanel key={s.id} id={s.id} events={p.events} onSelect={p.onSelect} />}
      {s && <button type="button" className="close" onClick={() => p.onSelect(null)} aria-label="Close">×</button>}
    </section>
  );
}

