import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Job } from "./api";

// The Ticket fixer's "Fixes" tab: where each ticket is, and the buttons that start a plan or approve a fix.
// Everything goes through the plugin's own workflows (status, queue, approve), so Helios and Ari do the same thing.

type Row = { key: string; title: string; stage: string };
type Status = { fixes: Row[]; projects: string[]; problems: string[]; today: number; tracker?: string; dry_run?: boolean };

const MODELS = ["", "opus", "sonnet", "haiku"];

function actions(stage: string): { workflow: "queue" | "approve"; label: string; ask: string }[] {
  if (stage.startsWith("plan ready") || stage === "fix failed, branch kept" || stage.startsWith("fix approved")) {
    return [{ workflow: "approve", label: stage.startsWith("fix approved") ? "Start now" : "Run the fix", ask: "Run the fix on a new branch now?" }];
  }
  if (stage === "waiting for a plan" || stage === "plan rejected" || stage === "plan failed" || stage === "fix done, in review") {
    return [{ workflow: "queue", label: stage === "fix done, in review" ? "Plan again" : "Plan", ask: "" }];
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

  const go = async (workflow: "queue" | "approve", k: string, ask: string) => {
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
          : workflow === "queue" ? `${k} is queued: planning starts within two minutes.` : `${k} is approved: the fix starts within two minutes.` });
        setKey("");
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
      {data?.problems?.length ? <div className="tip bad">Project settings: {data.problems.join("; ")}</div> : null}
      {data && data.projects.length === 0 && <div className="tip">No project is mapped yet: add one in Settings (KEY | folder | test command).</div>}
      {data && (
        <p className="muted">
          {data.projects.length ? `Projects: ${data.projects.join(", ")} · ` : ""}{data.today} plan{data.today === 1 ? "" : "s"} and fixes today
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
      <p className="muted">Plans wait for your answer on the phone or in the Inbox. The proof for a finished fix (screenshots, test log, report) is attached to the ticket in Tracker.</p>
    </div>
  );
}
