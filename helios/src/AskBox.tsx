import { useEffect, useRef, useState } from "react";
import { api, Job } from "./api";
import type { Selection } from "./MapView";

type Answer = { reply: string; action: string | null; label?: string | null; via: string; tier?: string };

const CONFIRM: Record<string, string> = {
  "power:shutdown": "Shut the PC down?", "power:restart": "Restart the PC?", "power:sleep": "Put the PC to sleep?",
};

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const Speech: any = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;

// Ask Argus: plain words in; an answer and at most one action, which runs only when you tap it.
export function AskBox({ onSelect, onView, big = false }: { onSelect: (s: Selection) => void; onView: (v: string) => void; big?: boolean }) {
  const [text, setText] = useState("");
  const [ans, setAns] = useState<Answer | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [listening, setListening] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const run = useRef(0);

  useEffect(() => {  // Ctrl+K / "/" focuses the box
    const on = (e: KeyboardEvent) => {
      if ((e.key === "k" && (e.ctrlKey || e.metaKey)) || (e.key === "/" && document.activeElement?.tagName !== "INPUT" && document.activeElement?.tagName !== "TEXTAREA")) {
        e.preventDefault(); input.current?.focus();
      }
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, []);

  const ask = async (q: string) => {
    if (!q.trim()) return;
    const me = ++run.current;
    setBusy(true); setErr(null); setAns(null);
    try {
      const r = await api<Answer & { job_id?: string }>("/ask", { method: "POST", body: JSON.stringify({ text: q }) });
      if (r.via !== "model" || !r.job_id) { if (me === run.current) setAns(r); return; }
      for (let i = 0; i < 120 && me === run.current; i++) {  // the model answers as a job: wait for it
        await new Promise((res) => setTimeout(res, 700));
        const j = await api<Job>(`/jobs/${r.job_id}`);
        if (j.state === "succeeded") { setAns({ ...(j.result as Answer), via: "model" }); return; }
        if (["dead", "cancelled"].includes(j.state)) throw new Error(j.error?.split("\n")[0] ?? "the model could not answer");
      }
      if (me === run.current) throw new Error("no answer yet: is the PC worker running?");
    } catch (e) { if (me === run.current) setErr(e instanceof Error ? e.message : String(e)); }
    finally { if (me === run.current) setBusy(false); }
  };

  const doIt = async (action: string) => {
    if (CONFIRM[action] && !window.confirm(CONFIRM[action])) return;
    setBusy(true); setErr(null);
    try {
      const r = await api<{ view?: string; job_id?: string; power?: { id?: string } }>("/ask/do", { method: "POST", body: JSON.stringify({ action }) });
      if (r.view) onView(r.view);
      else if (r.job_id) onSelect({ type: "job", id: r.job_id });
      else if (r.power?.id) onSelect({ type: "job", id: r.power.id });
      setAns(null); setText("");
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  };

  const listen = () => {
    if (!Speech) return;
    const rec = new Speech();
    rec.lang = "en-US"; rec.interimResults = false; rec.maxAlternatives = 1;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    rec.onresult = (e: any) => { const t = e.results[0][0].transcript as string; setText(t); ask(t); };
    rec.onend = () => setListening(false);
    rec.onerror = () => setListening(false);
    setListening(true); rec.start();
  };

  return (
    <div className={`ask${big ? " big" : ""}`}>
      <form onSubmit={(e) => { e.preventDefault(); ask(text); }}>
        {!big && <span className="prompt" aria-hidden="true">›</span>}
        <input ref={input} value={text} onChange={(e) => setText(e.target.value)} placeholder={big ? "Ask Argus…" : "ask argus…"}
          aria-label="Ask Argus" enterKeyHint="send" />
        {!big && <kbd className="hide-sm">ctrl k</kbd>}
        {Speech && <button type="button" className={`mic${listening ? " on" : ""}`} onClick={listen} aria-label="Speak">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v3" /></svg>
        </button>}
      </form>
      {(busy || ans || err) && (
        <div className="ask-out">
          {busy && !ans && <div className="tip">Thinking…</div>}
          {err && <div className="tip bad">{err}</div>}
          {ans && (
            <>
              <p>{ans.reply}</p>
              <div className="ask-acts">
                {ans.action && <button type="button" className="btn primary-btn" disabled={busy} onClick={() => doIt(ans.action!)}>
                  {ans.action.startsWith("show:") ? (ans.label ?? "Open") : `Do it: ${ans.label ?? ans.action}`}</button>}
                <button type="button" className="btn" onClick={() => { setAns(null); setErr(null); }}>Close</button>
                <span className="muted via">{ans.via === "rules" ? "instant" : `answered by ${ans.tier ?? "a model"}`}</span>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
