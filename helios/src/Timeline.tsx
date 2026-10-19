// Ari > Timeline: each request as one story (heard → what Ari did → answer, ok / failed / ignored and why), and
// what isn't working lately, grouped by cause. Click a request for its full trace.
import { useEffect, useState } from "react";
import { api } from "./api";

type Tool = { tool: string; ok: boolean; why: string | null };
type ModelCall = { model: string; verdict: string; ms: number | null; why: string | null };
type Req = {
  at: number; text: string | null; result: "ok" | "failed" | "stopped" | "ignored" | "working" | "waiting";
  why: string | null; reply?: string | null; job_id?: string | null; ms?: number | null; answer_ms?: number | null;
  source?: string; heard?: { accepted?: number; top?: string; top_p?: number; second_listen?: boolean; lang?: string; english?: number } | null;
  tools?: Tool[]; models?: ModelCall[]; turn?: number;
};
type Step = { at: number | null; kind: "step" | "model"; what: string; ok: boolean; ms: number | null; why: string | null };
type Trace = Req & { steps: Step[] };
type Problem = { area: string; what: string; count: number; last_at: number; hint: string | null; example: { job_id?: string; text?: string } | null };
type Problems = { headline: string; count: number; hours: number; problems: Problem[] };

const MARK: Record<Req["result"], string> = { ok: "✓", failed: "✗", stopped: "■", ignored: "·", working: "…", waiting: "…" };
const TONE: Record<Req["result"], string> = { ok: "ok", failed: "bad", stopped: "warn", ignored: "dim", working: "flow", waiting: "flow" };
const hhmm = (t: number) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
const secs = (ms?: number | null) => (ms == null ? "" : ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`);
const pct = (x?: number) => (x == null ? "" : `${Math.round(x * 100)}%`);

function heardNote(r: Req): string {
  const h = r.heard;
  if (!h) return r.source && r.source !== "voice" ? r.source : "";
  const bits = [];
  if (h.accepted != null) bits.push(`English ${pct(h.accepted)}${h.top && h.top !== "en" ? ` (most like ${h.top} ${pct(h.top_p)})` : ""}`);
  if (h.english != null) bits.push(`English ${pct(h.english)}${h.lang && h.lang !== "en" ? `, ${h.lang} ${pct(h.top_p)}` : ""}`);
  if (h.second_listen) bits.push("\"Hey Ari\" caught on a second listen");
  return bits.join(" · ");
}

function TraceView({ r }: { r: Req }) {
  const [t, setT] = useState<Trace | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    const ref = r.job_id ?? (r.turn ? String(r.turn) : null);
    if (!ref) return;
    api<Trace>(`/ari/trace/${ref}`).then(setT).catch((e) => setErr(String(e)));
  }, [r]);
  const note = heardNote(r);
  return (
    <div className="trace">
      {r.result === "ignored" && <div className="tip">Not taken as a request: {r.why}{note ? ` · ${note}` : ""}</div>}
      {note && r.result !== "ignored" && <div className="tl-step"><span className="mono muted">heard</span><span>{note}</span></div>}
      {err && <div className="tip bad">{err}</div>}
      {t?.steps.map((s, i) => (
        <div key={i} className={`tl-step ${s.ok ? "" : "bad"}`}>
          <span className="mono muted">{s.kind === "model" ? "model" : "step"}</span>
          <span className="mono">{s.what}</span>
          <span className="mono muted">{secs(s.ms)}</span>
          <span className={s.ok ? "muted" : "bad"}>{s.why ?? (s.ok ? "" : "failed")}</span>
        </div>
      ))}
      {r.tools?.map((x, i) => (
        <div key={`t${i}`} className={`tl-step ${x.ok ? "" : "bad"}`}>
          <span className="mono muted">tool</span><span className="mono">{x.tool}</span><span />
          <span className={x.ok ? "ok" : "bad"}>{x.ok ? "worked" : x.why}</span>
        </div>
      ))}
      {r.reply && <div className="tl-step"><span className="mono muted">said</span><span>{r.reply}</span></div>}
      {(r.ms || r.answer_ms) && <div className="tl-step"><span className="mono muted">time</span>
        <span>{r.answer_ms ? `answer ${secs(r.answer_ms)} after you finished speaking` : `${secs(r.ms)} to the answer`}</span></div>}
    </div>
  );
}

export function ProblemsCard() {
  const [p, setP] = useState<Problems | null>(null);
  const [hours, setHours] = useState(24);
  useEffect(() => { api<Problems>(`/problems?hours=${hours}`).then(setP).catch(() => {}); }, [hours]);
  return (
    <section className="panel wide" aria-label="Not working">
      <div className="ph">
        <span className="pt">not working</span>
        <span className={`muted ${p && p.count ? "bad" : ""}`}>{p ? p.headline : "…"}</span>
        <div className="tools">
          <select value={hours} onChange={(e) => setHours(Number(e.target.value))} aria-label="Since">
            <option value={6}>6 h</option><option value={24}>24 h</option><option value={168}>7 days</option>
          </select>
        </div>
      </div>
      <div className="modb">
        {p && p.problems.length === 0 && <div className="muted">Nothing failed in the last {hours} h.</div>}
        {p?.problems.slice(0, 12).map((g, i) => (
          <div key={i} className="prob">
            <span className="mono pill bad">×{g.count}</span>
            <span className="mono">{g.area}</span>
            <span className="prob-w">{g.what}{g.hint ? <span className="muted"> — {g.hint}</span> : null}
              {g.example?.text ? <span className="muted"> (“{g.example.text}”)</span> : null}</span>
            <span className="mono muted">{hhmm(g.last_at)}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

export function Timeline() {
  const [rows, setRows] = useState<Req[] | null>(null);
  const [only, setOnly] = useState<"all" | "problems" | "requests">("all");
  const [open, setOpen] = useState<number | null>(null);
  const load = () => api<{ requests: Req[] }>(`/ari/timeline?limit=60&ignored=${only !== "requests"}`).then((r) => setRows(r.requests)).catch(() => setRows([]));
  useEffect(() => { load(); const i = window.setInterval(load, 8000); return () => window.clearInterval(i); }, [only]); // eslint-disable-line react-hooks/exhaustive-deps
  const shown = (rows ?? []).filter((r) => only !== "problems" || r.result === "failed" || r.result === "ignored" || r.result === "stopped");
  return (
    <section className="panel wide" aria-label="Timeline">
      <div className="ph">
        <span className="pt">timeline</span>
        <span className="muted">each request: what was heard, what Ari did, the answer</span>
        <div className="tools">
          <select value={only} onChange={(e) => setOnly(e.target.value as typeof only)} aria-label="Show">
            <option value="all">everything</option><option value="problems">failed and ignored</option><option value="requests">requests only</option>
          </select>
        </div>
      </div>
      <div className="modb">
        {rows && shown.length === 0 && <div className="muted">Nothing yet.</div>}
        {shown.map((r, i) => (
          <div key={`${r.at}-${i}`} className={`tl ${open === i ? "open" : ""}`}>
            <button type="button" className={`tl-h ${TONE[r.result]}`} onClick={() => setOpen(open === i ? null : i)}>
              <span className="mono muted">{hhmm(r.at)}</span>
              <span className={`mono ${TONE[r.result]}`}>{MARK[r.result]}</span>
              <span className="tl-text">“{r.text ?? ""}”</span>
              <span className="tl-what">
                {r.tools && r.tools.length > 0 && <span className="mono">{r.tools.map((t) => t.tool).join(", ")} </span>}
                <span className={r.result === "failed" ? "bad" : "muted"}>{r.why ?? (r.reply ? `“${r.reply.slice(0, 70)}”` : "")}</span>
              </span>
              <span className="mono muted">{secs(r.answer_ms ?? r.ms)}</span>
            </button>
            {open === i && <TraceView r={r} />}
          </div>
        ))}
      </div>
    </section>
  );
}
