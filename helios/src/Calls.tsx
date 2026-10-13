// The model call log (C0): every call to a model, with the prompt as sent and the raw reply.
// Models > Calls lists them (live, with filters); the Inspector shows a job's own calls in order.
import { useEffect, useState } from "react";
import { api, ArgusEvent } from "./api";
import { ago, pretty } from "./format";

export type Call = {
  id: number; created_at: number; job_id: string | null; plugin: string | null; workflow: string | null;
  step: string | null; worker: string | null; tier: string | null; provider: string | null; model: string | null;
  attempt: number | null; verdict: "ok" | "rejected" | "error"; reason: string | null; latency_ms: number | null;
  tokens_in: number | null; tokens_out: number | null; images: number;
  reply_start?: string; system?: string | null; prompt?: { role: string; content: string }[] | string | null;
  reply?: string | null;
};
type Page = { calls: Call[]; models: string[]; plugins: string[]; count: number; mb: number; enabled: boolean };

const TONE = { ok: "ok", rejected: "warn", error: "bad" } as const;
const secs = (ms: number | null) => (ms == null ? "" : ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`);
const toks = (c: Call) => (c.tokens_in != null || c.tokens_out != null ? `${c.tokens_in ?? "?"}→${c.tokens_out ?? "?"} tok` : "");
const who = (c: Call) => [c.plugin, c.workflow].filter(Boolean).join(".") || "?";
const model = (c: Call) => `${c.model ?? c.provider ?? "?"}${c.tier ? ` · ${c.tier}` : ""}`;

function copy(text: string) {
  navigator.clipboard?.writeText(text).catch(() => {});
}

// One call in full: what was sent (playbook, messages) and what came back, side by side.
export function CallDetail({ call }: { call: Call }) {
  const [c, setC] = useState<Call | null>(call.system !== undefined ? call : null);
  const [sys, setSys] = useState(false);
  useEffect(() => { if (call.system === undefined) api<Call>(`/models/calls/${call.id}`).then(setC).catch(() => {}); }, [call]);
  if (!c) return <div className="tip">Loading…</div>;
  const msgs = Array.isArray(c.prompt) ? c.prompt : c.prompt ? [{ role: "user", content: String(c.prompt) }] : [];
  const all = `SYSTEM\n${c.system ?? ""}\n\n${msgs.map((m) => `${m.role.toUpperCase()}\n${m.content}`).join("\n\n")}\n\nREPLY\n${c.reply ?? ""}`;
  return (
    <div className="calld">
      <div className="calld-h">
        <span className="muted mono">{model(c)}{c.attempt ? ` · try ${c.attempt}` : ""} · {secs(c.latency_ms)} {toks(c)}</span>
        {c.images > 0 && <span className="pill" title="pictures are never kept">{c.images} picture{c.images > 1 ? "s" : ""} (not kept)</span>}
        <span className="grow" />
        <button type="button" className="linkbtn" onClick={() => copy(all)}>copy all</button>
      </div>
      {c.reason && <div className={`tip ${c.verdict === "error" ? "bad" : "warn"}`}>{c.verdict === "error" ? "Error" : "Rejected"}: {c.reason}</div>}
      <div className="calld-cols">
        <div>
          <div className="sect">Sent</div>
          {c.system == null && msgs.length === 0 && <div className="muted">prompts aren't kept (models.call_log_prompts)</div>}
          {c.system != null && (
            <div className="calld-msg">
              <button type="button" className="linkbtn" onClick={() => setSys(!sys)}>{sys ? "▾" : "▸"} playbook ({c.system.length.toLocaleString()} chars)</button>
              {sys && <pre className="code tall">{c.system}</pre>}
            </div>
          )}
          {msgs.map((m, i) => (
            <div key={i} className="calld-msg"><span className="muted mono">{m.role}</span><pre className="code tall">{prettyJson(m.content)}</pre></div>
          ))}
        </div>
        <div>
          <div className="sect">Reply</div>
          <pre className={`code tall ${c.verdict === "error" ? "bad" : ""}`}>{c.reply != null ? prettyJson(c.reply) : "(no reply)"}</pre>
        </div>
      </div>
    </div>
  );
}

// a JSON text shown indented; anything else as it is
function prettyJson(t: string): string {
  const s = t.trim();
  if (!(s.startsWith("{") || s.startsWith("["))) return t;
  try { return pretty(JSON.parse(s)); } catch { return t; }
}

function CallRow({ c, open, onToggle, showWho = true }: { c: Call; open: boolean; onToggle: () => void; showWho?: boolean }) {
  return (
    <div className={`callr ${open ? "open" : ""}`}>
      <button type="button" className="callr-h" onClick={onToggle}>
        <span className="mono muted">{ago(c.created_at)}</span>
        {showWho && <span className="mono">{who(c)}</span>}
        <span className="mono">{model(c)}</span>
        <span className={`pill ${TONE[c.verdict]}`}>{c.verdict}</span>
        <span className="mono muted">{secs(c.latency_ms)}</span>
        <span className="mono muted">{toks(c)}</span>
        <span className="d">{c.reason ?? c.reply_start ?? ""}</span>
      </button>
      {open && <CallDetail call={c} />}
    </div>
  );
}

// Helios > Models > Calls
export function CallLog({ events }: { events: ArgusEvent[] }) {
  const [page, setPage] = useState<Page | null>(null);
  const [more, setMore] = useState<Call[]>([]);
  const [f, setF] = useState({ model: "", plugin: "", verdict: "", q: "" });
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const tick = events.filter((e) => e.kind === "model.reply" || e.kind === "check.failed" || e.kind === "model.error" || e.kind === "model.timeout").map((e) => e.seq).pop() ?? 0;
  const query = (before?: number) => {
    const p = new URLSearchParams({ limit: "60" });
    for (const [k, v] of Object.entries(f)) if (v) p.set(k, v);
    if (before) p.set("before", String(before));
    return api<Page>(`/models/calls?${p}`);
  };
  useEffect(() => { query().then((r) => { setPage(r); setMore([]); }).catch(() => {}); }, [f, tick]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { const t = setTimeout(() => setF((x) => (x.q === q ? x : { ...x, q })), 350); return () => clearTimeout(t); }, [q]);
  const list = [...(page?.calls ?? []), ...more];
  const last = list[list.length - 1];
  return (
    <section className="panel">
      <div className="ph">
        <span className="pt">calls</span>
        <span className="muted">every model call: what was sent, what came back{page ? ` · ${page.count} kept (${page.mb} MB)` : ""}</span>
      </div>
      <div className="modb">
        {page && !page.enabled && <div className="tip">The call log is off (models.call_log in argus.yaml).</div>}
        <div className="callf">
          <select value={f.model} onChange={(e) => setF({ ...f, model: e.target.value })} aria-label="Model">
            <option value="">all models</option>
            {page?.models.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
          <select value={f.plugin} onChange={(e) => setF({ ...f, plugin: e.target.value })} aria-label="Who asked">
            <option value="">everyone</option>
            {page?.plugins.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
          <select value={f.verdict} onChange={(e) => setF({ ...f, verdict: e.target.value })} aria-label="Verdict">
            <option value="">all answers</option>
            <option value="problems">rejected or failed</option>
            <option value="ok">accepted</option>
            <option value="rejected">rejected</option>
            <option value="error">failed</option>
          </select>
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="text in the prompt or reply" aria-label="Search" />
        </div>
        {page && list.length === 0 && <div className="muted">No calls{Object.values(f).some(Boolean) ? " match" : " yet"}.</div>}
        {list.map((c) => <CallRow key={c.id} c={c} open={open === c.id} onToggle={() => setOpen(open === c.id ? null : c.id)} />)}
        {page && list.length >= 60 && last && (
          <button type="button" className="btn" onClick={() => query(last.id).then((r) => setMore((m) => [...m, ...r.calls])).catch(() => {})}>Older</button>
        )}
      </div>
    </section>
  );
}

// The Inspector: a job's calls in order (an escalation reads as a chain: rejected, rejected, then ok).
export function JobCalls({ id, tick }: { id: string; tick: number }) {
  const [list, setList] = useState<Call[]>([]);
  const [open, setOpen] = useState<number | null>(null);
  useEffect(() => { api<Call[]>(`/jobs/${id}/calls`).then(setList).catch(() => setList([])); }, [id, tick]);
  if (list.length === 0) return null;
  return (
    <>
      <div className="sect">Model calls ({list.length})</div>
      {list.map((c) => <CallRow key={c.id} c={c} showWho={false} open={open === c.id} onToggle={() => setOpen(open === c.id ? null : c.id)} />)}
    </>
  );
}
