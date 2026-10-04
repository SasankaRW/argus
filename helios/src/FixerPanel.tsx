import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type ArgusEvent, type Job } from "./api";

// The Ticket fixer's "Fixes" tab: where each ticket is, and the buttons that start a plan or approve a fix.
// Everything goes through the plugin's own workflows (status, queue, approve), so Helios and Ari do the same thing.

type Row = { key: string; title: string; stage: string };
type Status = { fixes: Row[]; projects: string[]; problems: string[]; today: number; limit?: number; waiting?: string; tracker?: string; dry_run?: boolean };

const MODELS = ["", "opus", "sonnet", "haiku"];

type Action = { workflow: "queue" | "approve" | "terminal"; label: string; ask: string };
const CONTINUE: Action = { workflow: "terminal", label: "Continue in terminal", ask: "" };

function actions(stage: string): Action[] {
  if (stage.startsWith("plan ready") || stage === "fix failed, branch kept" || stage.startsWith("fix approved")) {
    const run: Action = { workflow: "approve", label: stage.startsWith("fix approved") ? "Start now" : "Run the fix", ask: "Run the fix on a new branch now?" };
    return stage === "fix failed, branch kept" ? [CONTINUE, run] : [run];
  }
  if (stage === "waiting for a plan" || stage === "plan rejected" || stage === "plan failed" || stage === "fix done, in review") {
    const plan: Action = { workflow: "queue", label: stage === "fix done, in review" ? "Plan again" : "Plan", ask: "" };
    return stage === "fix done, in review" ? [CONTINUE, plan] : [plan];
  }
  return [];
}

async function runJob(workflow: string, input: Record<string, string>): Promise<Job> {
  const r = await api<{ id: string }>("/plugins/fixer/run", { method: "POST", body: JSON.stringify({ workflow, input }) });
  for (let i = 0; i < 90; i++) {  // up to about 90 s: the PC's worker has to pick it up
    await new Promise((ok) => setTimeout(ok, 1000));
    const j = await api<Job>(`/jobs/${r.id}`);
    if (["succeeded", "dead", "cancelled"].includes(j.state)) return j;
  }
  throw new Error("the PC didn't answer in time: is the worker running?");
}

// What Claude is doing right now (and did before): the fixer sends each message, command and result as an event.
type Line = { seq: number; at: number; key: string; phase: string; kind: string; text: string };
const LIVE = "/events?kinds=plugin.fixer.live";

function toLine(e: ArgusEvent): Line {
  const d = (e.data ?? {}) as Record<string, unknown>;
  return { seq: e.seq, at: e.at, key: String(d.key ?? ""), phase: String(d.phase ?? ""), kind: String(d.kind ?? "info"), text: String(d.text ?? "") };
}

function LiveLog() {
  const [lines, setLines] = useState<Line[]>([]);
  const [pick, setPick] = useState("");
  const [now, setNow] = useState(Date.now() / 1000);
  const box = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  useEffect(() => {
    let stop = false;
    (async () => {
      let after = 0;
      try {
        const r = await api<{ events: ArgusEvent[] }>(`${LIVE}&newest=true&limit=400`);
        const first = r.events.map(toLine);
        if (first.length) after = first[first.length - 1].seq;
        if (!stop) setLines(first);
      } catch { /* the loop below tries again */ }
      while (!stop) {
        try {
          const r = await api<{ events: ArgusEvent[] }>(`${LIVE}&after=${after}&wait=20&limit=200`);
          if (stop) return;
          if (r.events.length) {
            after = r.events[r.events.length - 1].seq;
            setLines((old) => [...old, ...r.events.map(toLine)].slice(-1500));
          }
        } catch { await new Promise((ok) => setTimeout(ok, 5000)); }
      }
    })();
    const tick = window.setInterval(() => setNow(Date.now() / 1000), 15000);
    return () => { stop = true; window.clearInterval(tick); };
  }, []);

  const keys = useMemo(() => {
    const last = new Map<string, number>();
    for (const l of lines) last.set(l.key, l.seq);
    return [...last.entries()].sort((a, b) => b[1] - a[1]).map(([k]) => k);
  }, [lines]);
  const key = pick && keys.includes(pick) ? pick : keys[0] ?? "";
  const mine = useMemo(() => lines.filter((l) => l.key === key), [lines, key]);
  const newest = mine.length ? mine[mine.length - 1] : null;
  const running = !!newest && newest.kind !== "done" && now - newest.at < 120 && !(newest.kind === "error" && newest.text.startsWith("The fix stopped"));

  useEffect(() => {
    const el = box.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [mine.length, key]);

  if (!lines.length) {
    return <p className="muted">When Claude works on a ticket, its messages and the commands it runs show up here as they happen.</p>;
  }
  const time = (t: number) => new Date(t * 1000).toLocaleTimeString([], { hour12: false });
  return (
    <div className="fx-live">
      <div className="fx-bar">
        <b>Live</b>
        <select value={key} onChange={(e) => setPick(e.target.value)} aria-label="Ticket">
          {keys.map((k) => <option key={k} value={k}>{k}</option>)}
        </select>
        {newest && <span className="muted">{newest.phase === "plan" ? "planning" : "fixing"}</span>}
        <span className="grow" />
        {running ? <span className="fx-pulse">● running</span> : <span className="muted">idle</span>}
      </div>
      <div className="fx-log" ref={box}
        onScroll={(e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40; }}>
        {mine.map((l) => (
          <div key={l.seq} className={`fx-ln ${l.kind}`}>
            <span className="fx-t">{time(l.at)}</span>
            <span className="fx-tag">{l.kind === "say" ? "Claude" : l.kind === "tool" ? "$" : l.kind === "result" ? "out" : l.kind === "error" ? "!" : l.kind === "done" ? "✓" : "·"}</span>
            <span className="fx-tx">{l.text}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function FixerPanel() {
  const [data, setData] = useState<Status | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [planModel, setPlanModel] = useState("");
  const [fixModel, setFixModel] = useState("");
  const [key, setKey] = useState("");
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const j = await runJob("status", {});
      if (!alive.current) return;
      if (j.state === "succeeded") setData(j.result as Status);
      else setMsg({ ok: false, text: (j.error ?? "the status check failed").split("\n")[0] });
    } catch (e) {
      if (alive.current) setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) });
    } finally { if (alive.current) setBusy(false); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const go = async (workflow: "queue" | "approve" | "terminal", k: string, ask: string) => {
    if (ask && !window.confirm(`${k}: ${ask}`)) return;
    setBusy(true); setMsg(null);
    const input: Record<string, string> = { key: k };
    if (planModel && workflow === "queue") input.plan_model = planModel;
    if (fixModel) input.fix_model = fixModel;
    try {
      const j = await runJob(workflow, input);
      if (!alive.current) return;
      if (j.state === "succeeded") {
        const r = j.result as Record<string, unknown>;
        setMsg({ ok: true, text: r.dry_run ? `${k}: dry run, nothing was changed (the plugin isn't live).`
          : workflow === "terminal" ? (r.opened ? `${k}: a terminal is open on your PC with Claude's session, on branch ${r.branch}.`
            : `${k}: couldn't open a terminal here. Run: ${r.command}`)
          : workflow === "queue" ? `${k} is queued: planning starts within two minutes.` : `${k} is approved: the fix starts within two minutes.` });
        if (workflow !== "terminal") setKey("");
        await load();
      } else setMsg({ ok: false, text: (j.error ?? "failed").split("\n")[0] });
    } catch (e) { if (alive.current) setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) }); }
    finally { if (alive.current) setBusy(false); }
  };

  const pick = (label: string, value: string, set: (v: string) => void) => (
    <label className="fx-pick">{label}
      <select value={value} onChange={(e) => set(e.target.value)}>
        {MODELS.map((m) => <option key={m} value={m}>{m || "default"}</option>)}
      </select>
    </label>
  );

  return (
    <div className="fixes">
      <div className="fx-bar">
        {pick("Plan with", planModel, setPlanModel)}
        {pick("Fix with", fixModel, setFixModel)}
        <span className="grow" />
        <button type="button" className="btn" disabled={busy} onClick={load}>{busy ? "working…" : "Refresh"}</button>
      </div>
      <form className="fx-add" onSubmit={(e) => { e.preventDefault(); if (key.trim()) go("queue", key.trim().toUpperCase(), ""); }}>
        <input type="text" placeholder="Ticket, e.g. TRK-5" value={key} onChange={(e) => setKey(e.target.value)} aria-label="Ticket key" />
        <button type="submit" className="primary" disabled={busy || !key.trim()}>Plan a fix</button>
      </form>
      {msg && <div className={`tip${msg.ok ? "" : " bad"}`}>{msg.text}</div>}
      {data?.waiting ? <div className="tip bad">Waiting: {data.waiting}. Raise it in Settings, or it starts again tomorrow.</div> : null}
      {data?.problems?.length ? <div className="tip bad">Project settings: {data.problems.join("; ")}</div> : null}
      {data && data.projects.length === 0 && <div className="tip">No project is mapped yet: add one in Settings (KEY | folder | test command).</div>}
      {data && (
        <p className="muted">
          {data.projects.length ? `Projects: ${data.projects.join(", ")} · ` : ""}{data.today}{data.limit ? ` of ${data.limit}` : ""} plan{data.today === 1 ? "" : "s"} and fixes today
          {data.tracker ? <> · <a href={data.tracker} target="_blank" rel="noreferrer">open Tracker</a></> : null}
        </p>
      )}
      {data && data.fixes.length === 0 && <p className="muted">Nothing is being planned or fixed. Label a ticket <b>ai-fix</b> in Tracker, or use the box above.</p>}
      {data && data.fixes.length > 0 && (
        <div className="fx-rows">
          {data.fixes.map((r) => (
            <div key={r.key} className="fx-row">
              <span className="mono fx-key">{r.key}</span>
              <span className="fx-title">{r.title}</span>
              <span className="fx-stage">{r.stage}</span>
              <span className="fx-act">
                {actions(r.stage).map((a) => (
                  <button key={a.workflow} type="button" className="btn" disabled={busy} onClick={() => go(a.workflow, r.key, a.ask)}>{a.label}</button>
                ))}
              </span>
            </div>
          ))}
        </div>
      )}
      <LiveLog />
      <p className="muted">Plans wait for your answer on the phone or in the Inbox. The proof for a finished fix (screenshots, test log, report) is attached to the ticket in Tracker.</p>
    </div>
  );
}
