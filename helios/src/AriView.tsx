import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { Selection } from "./MapView";
import { canSpeak, listenOnce, pref, setPref, speak, Speech, stopSpeaking } from "./voice";

type Turn = { id: number; role: "you" | "ari"; text: string | null; action: string | null; label?: string | null;
  pending: { kind: string; action: string } | null; job_id: string | null; created_at: number };
type Said = { conv: string; reply: string | null; job_id?: string; view?: string; power?: { id?: string };
  schedule?: { id: string } };
type Schedule = { id: string; plugin: string; workflow: string; cron: string; enabled: boolean; next_run_at: number | null;
  last_run_at: number | null; owner: string; label: string | null; spec: { once?: boolean; when?: string } };

function newConv() { return Math.random().toString(36).slice(2, 12); }
function loadConv(): string {
  try { const c = localStorage.getItem("ari.conv"); if (c) return c; } catch { /* private */ }
  return newConv();
}
function when(t: number | null) {
  if (!t) return "—";
  const d = new Date(t * 1000);
  const today = new Date();
  const same = d.toDateString() === today.toDateString();
  return `${same ? "today" : d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })} ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
}

// Talk to Ari: type or speak; Ari answers (aloud if you like) and asks before doing anything.
export function AriView({ onSelect, onView, compact = false }: { onSelect: (s: Selection) => void; onView: (v: string) => void; compact?: boolean }) {
  const [conv, setConv] = useState(loadConv);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [listening, setListening] = useState(false);
  const [talk, setTalk] = useState(() => pref("speak", true));
  const [wake, setWake] = useState(() => pref("wake", false));
  const [piper, setPiper] = useState(() => pref("piper", false));
  const [scheds, setScheds] = useState<Schedule[]>([]);
  const spoken = useRef<Set<number>>(new Set());
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => { try { localStorage.setItem("ari.conv", conv); } catch { /* private */ } }, [conv]);

  const loadScheds = useCallback(() => { api<Schedule[]>("/schedules").then(setScheds).catch(() => {}); }, []);
  useEffect(loadScheds, [loadScheds]);

  const load = useCallback(async () => {
    const r = await api<{ turns: Turn[] }>(`/ari/${conv}`);
    setTurns(r.turns);
    return r.turns;
  }, [conv]);

  useEffect(() => {  // first load: what's already said isn't read out again
    load().then((ts) => ts.forEach((t) => spoken.current.add(t.id))).catch(() => setTurns([]));
  }, [load]);

  useEffect(() => {  // a model's answer fills in: poll while one is on its way
    if (!turns.some((t) => t.role === "ari" && t.text === null)) return;
    const id = setTimeout(() => { load().catch(() => {}); }, 800);
    return () => clearTimeout(id);
  }, [turns, load]);

  useEffect(() => {  // read new answers aloud
    for (const t of turns) {
      if (t.role !== "ari" || t.text === null || spoken.current.has(t.id)) continue;
      spoken.current.add(t.id);
      if (talk) {
        speak(t.text).then(() => { if (t.pending) window.dispatchEvent(new CustomEvent("ari-arm")); });
      }
    }
    end.current?.scrollIntoView({ block: "end" });
  }, [turns, talk]);

  const after = useCallback((r: Said) => {
    if (r.view) onView(r.view);
    else if (r.job_id && !r.reply) { /* a model's answer: it fills in */ }
    else if (r.job_id) onSelect({ type: "job", id: r.job_id });
    else if (r.power?.id) onSelect({ type: "job", id: r.power.id });
    if (r.schedule) loadScheds();
  }, [onSelect, onView, loadScheds]);

  const send = useCallback(async (q: string) => {
    if (!q.trim()) return;
    setBusy(true); setErr(null); setText(""); stopSpeaking();
    try {
      const r = await api<Said>("/ari", { method: "POST", body: JSON.stringify({ text: q, conv }) });
      await load();
      after(r);
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  }, [conv, load, after]);

  const answer = async (yes: boolean) => {
    setBusy(true); setErr(null); stopSpeaking();
    try {
      const r = await api<Said>(`/ari/${conv}/answer`, { method: "POST", body: JSON.stringify({ yes }) });
      await load();
      after(r);
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  };

  useEffect(() => {  // "Hey Ari ..." heard anywhere in Helios
    const on = (e: Event) => send((e as CustomEvent<string>).detail);
    window.addEventListener("ari-command", on);
    let queued: string | null = null;
    try { queued = sessionStorage.getItem("ari.command"); sessionStorage.removeItem("ari.command"); } catch { /* private */ }
    if (queued) send(queued);
    return () => window.removeEventListener("ari-command", on);
  }, [send]);

  const mic = async () => {
    if (!Speech || listening) return;
    stopSpeaking(); setListening(true);
    const said = await listenOnce();
    setListening(false);
    if (said) send(said);
  };

  const toggle = (k: string, v: boolean, set: (v: boolean) => void) => { set(v); setPref(k, v); };
  const mine = scheds.filter((s) => s.owner === "you");
  const last = [...turns].reverse().find((t) => t.role === "ari");
  const open = last?.pending && last.text !== null ? last : null;

  const editSched = async (s: Schedule, how: "pause" | "resume" | "delete") => {
    try {
      if (how === "delete") await api(`/schedules/${s.id}`, { method: "DELETE" });
      else await api(`/schedules/${s.id}`, { method: "PATCH", body: JSON.stringify({ enabled: how === "resume" }) });
      loadScheds();
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };

  return (
    <section className={`panel ari${compact ? " compact" : ""}`} aria-label="Ari">
      <div className="ph">
        <span className="pt">Ari</span>
        <span className="muted">your assistant · asks before doing anything</span>
        <div className="tools">
          <button type="button" className="btn" onClick={() => { stopSpeaking(); setConv(newConv()); setTurns([]); }}>New chat</button>
        </div>
      </div>
      <div className="ari-log">
        {turns.length === 0 && (
          <div className="ari-hello">
            <p>Hi, I'm Ari. Try:</p>
            {["What's running?", "Sort my downloads every morning at 7", "Remind me to call mum tomorrow at 5 pm", "Shut down the PC at 11 pm"].map((x) => (
              <button key={x} type="button" className="chip" onClick={() => send(x)}>{x}</button>
            ))}
          </div>
        )}
        {turns.map((t) => (
          <div key={t.id} className={`bubble ${t.role}`}>
            {t.text ?? <span className="typing" aria-label="Ari is thinking"><i /><i /><i /></span>}
          </div>
        ))}
        {open && (
          <div className="ari-ask">
            <button type="button" className="primary" disabled={busy} onClick={() => answer(true)}>Yes</button>
            <button type="button" className="btn" disabled={busy} onClick={() => answer(false)}>No</button>
            {talk && Speech && <span className="muted">or say yes / no</span>}
          </div>
        )}
        {err && <div className="tip bad">{err}</div>}
        <div ref={end} />
      </div>
      <form className="ari-in" onSubmit={(e) => { e.preventDefault(); send(text); }}>
        <input value={text} onChange={(e) => setText(e.target.value)} placeholder="Talk to Ari…" aria-label="Message Ari" enterKeyHint="send" />
        {Speech && <button type="button" className={`mic${listening ? " on" : ""}`} onClick={mic} aria-label="Speak">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v3" /></svg>
        </button>}
        <button type="submit" className="primary" disabled={busy || !text.trim()}>Send</button>
      </form>
      <div className="ari-opts">
        {canSpeak && <label><input type="checkbox" checked={talk} onChange={(e) => toggle("speak", e.target.checked, setTalk)} /> Speak replies</label>}
        {Speech && <label title="While Helios is open: say &quot;Hey Ari&quot;, then what you want"><input type="checkbox" checked={wake} onChange={(e) => toggle("wake", e.target.checked, setWake)} /> &ldquo;Hey Ari&rdquo;</label>}
        <label title="Piper on the PC: a natural voice (needs ari.voice set up; falls back to the browser voice)"><input type="checkbox" checked={piper} onChange={(e) => toggle("piper", e.target.checked, setPiper)} /> Natural voice</label>
      </div>
      {!compact && (
        <div className="ari-sched">
          <div className="sect">Your schedules</div>
          {mine.length === 0 ? <div className="tip">None yet. Tell Ari: "sort downloads every morning at 7".</div> : mine.map((s) => (
            <div key={s.id} className={`srow${s.enabled ? "" : " off"}`}>
              <span className="sl">{s.label ?? `${s.plugin} · ${s.workflow}`}</span>
              <span className="muted mono">{s.enabled ? `next ${when(s.next_run_at)}` : s.spec.once && s.last_run_at ? `done ${when(s.last_run_at)}` : "paused"}</span>
              {!(s.spec.once && s.last_run_at) && <button type="button" className="btn" onClick={() => editSched(s, s.enabled ? "pause" : "resume")}>{s.enabled ? "Pause" : "Resume"}</button>}
              <button type="button" className="btn" onClick={() => editSched(s, "delete")}>Delete</button>
            </div>
          ))}
          {scheds.some((s) => s.owner !== "you") && (
            <details><summary className="muted">From settings and plugins ({scheds.filter((s) => s.owner !== "you").length})</summary>
              {scheds.filter((s) => s.owner !== "you").map((s) => (
                <div key={s.id} className="srow"><span className="sl mono">{s.id}</span><span className="muted mono">{s.cron} · next {when(s.next_run_at)}</span></div>
              ))}
            </details>
          )}
        </div>
      )}
    </section>
  );
}
