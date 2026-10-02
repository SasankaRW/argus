import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { Selection } from "./MapView";
import { pauseEars, resumeEars, useAri, useEars } from "./ariState";
import { canSpeak, listen, pref, setPref, speak, Speech, stopSpeaking, voiceStatus, VoiceStatus } from "./voice";
import { plain } from "./spoken";

type Turn = { id: number; role: "you" | "ari"; text: string | null; action: string | null; label?: string | null;
  pending: { kind: string; action: string } | null; job_id: string | null; created_at: number;
  used?: { tool: string; ok: boolean }[] | null };
type Said = { conv: string; reply: string | null; job_id?: string; view?: string; power?: { id?: string };
  schedule?: { id: string } };
type Schedule = { id: string; plugin: string; workflow: string; cron: string; enabled: boolean; next_run_at: number | null;
  last_run_at: number | null; owner: string; label: string | null; spec: { once?: boolean; when?: string } };

type Chat = { conv: string; turns: number; started: number; updated: number; title: string | null; last: string | null };

export function newConv() { return Math.random().toString(36).slice(2, 12); }
// The chat you had open last: "Hey Ari" and questions from other screens continue it.
export function currentConv(): string {
  try { const c = localStorage.getItem("ari.conv"); if (c) return c; } catch { /* private */ }
  const c = newConv();
  try { localStorage.setItem("ari.conv", c); } catch { /* private */ }
  return c;
}
function when(t: number | null) {
  if (!t) return "—";
  const d = new Date(t * 1000);
  const today = new Date();
  const same = d.toDateString() === today.toDateString();
  return `${same ? "today" : d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })} ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
}

function ago(t: number) {
  const s = Date.now() / 1000 - t;
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h`;
  return new Date(t * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

// The Ari page: your chats (open one to continue it), Ari's settings, schedules and what Ari remembers.
export function AriHome({ onOpen }: { onOpen: (conv: string) => void }) {
  const [chats, setChats] = useState<Chat[] | null>(null);
  const [q, setQ] = useState("");
  const [find, setFind] = useState("");
  const load = useCallback(() => { api<Chat[]>("/ari-chats").then(setChats).catch(() => setChats([])); }, []);
  useEffect(load, [load]);
  const start = (text?: string) => {
    const c = newConv();
    if (text) { try { sessionStorage.setItem("ari.command", text); } catch { /* private */ } }
    onOpen(c);
  };
  const del = async (c: Chat) => {
    await api(`/ari-chats/${c.conv}`, { method: "DELETE" }).catch(() => {});
    load();
  };
  const needle = find.trim().toLowerCase();
  const shown = (chats ?? []).filter((c) => !needle || `${c.title ?? ""} ${c.last ?? ""}`.toLowerCase().includes(needle));
  return (
    <div className="ari-home">
      <section className="panel" aria-label="Chats">
        <div className="ph">
          <span className="pt">ari</span>
          <span className="muted">{chats ? `${chats.length} chat${chats.length === 1 ? "" : "s"}` : "…"}</span>
          <div className="tools">
            <input className="chat-find" value={find} onChange={(e) => setFind(e.target.value)} placeholder="find a chat…" aria-label="Find a chat" />
            <button type="button" className="primary chat-new" onClick={() => start()}>+ new chat</button>
          </div>
        </div>
        <form className="ari-start" onSubmit={(e) => { e.preventDefault(); if (q.trim()) start(q.trim()); }}>
          <span className="caret">›</span>
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="ask ari something new…" aria-label="Start a new chat" />
        </form>
        <div className="chat-list">
          {chats && chats.length === 0 && <div className="tip">No chats yet. Ask Ari something above.</div>}
          {shown.map((c) => (
            <div key={c.conv} className="chat-row">
              <button type="button" className="chat-open" onClick={() => onOpen(c.conv)}>
                <span className="chat-t">{c.title ?? "(Hey Ari on the PC)"}</span>
                <span className="chat-l">{c.last ?? ""}</span>
                <span className="chat-m">{ago(c.updated)} · {c.turns} msg</span>
              </button>
              <button type="button" className="chat-del" aria-label="Delete this chat" title="Delete this chat" onClick={() => del(c)}>×</button>
            </div>
          ))}
        </div>
      </section>
      <AriSettings />
    </div>
  );
}

// Talk to Ari in one chat: type or speak; Ari answers (aloud if you like) and asks before doing anything.
export function AriView({ conv, onBack, onNew, onSelect, onView, compact = false }: {
  conv: string; onBack?: () => void; onNew: (conv: string) => void;
  onSelect: (s: Selection) => void; onView: (v: string) => void; compact?: boolean;
}) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const ari = useAri();  // what Ari is doing right now ("looking at your screen"), shown in the waiting bubble
  const asked = useRef(0);  // when this screen last asked something (only then does it speak the answer)
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [listening, setListening] = useState(false);
  const [talk, setTalk] = useState(() => pref("speak", true));
  const [wake, setWake] = useState(() => pref("wake", false));
  const [vs, setVs] = useState<VoiceStatus>(voiceStatus);
  useEffect(() => {
    const st = () => setVs(voiceStatus());
    const pr = () => setWake(pref("wake", false));
    window.addEventListener("ari-status", st); window.addEventListener("ari-pref", pr);
    return () => { window.removeEventListener("ari-status", st); window.removeEventListener("ari-pref", pr); };
  }, []);
  const spoken = useRef<Set<number>>(new Set());
  const log = useRef<HTMLDivElement>(null);

  useEffect(() => { try { localStorage.setItem("ari.conv", conv); } catch { /* private */ } }, [conv]);

  const load = useCallback(async () => {
    const r = await api<{ turns: Turn[] }>(`/ari/${conv}`);
    setTurns(r.turns);
    return r.turns;
  }, [conv]);

  useEffect(() => {  // first load: what's already said isn't read out again
    setTurns([]);
    load().then((ts) => ts.forEach((t) => spoken.current.add(t.id))).catch(() => setTurns([]));
  }, [load]);

  useEffect(() => {  // a model's answer fills in: poll while one is on its way
    if (!turns.some((t) => t.role === "ari" && t.text === null)) return;
    const id = setTimeout(() => { load().catch(() => {}); }, 800);
    return () => clearTimeout(id);
  }, [turns, load]);

  useEffect(() => {  // read new answers aloud; keep the newest in view
    for (const t of turns) {
      if (t.role !== "ari" || t.text === null || spoken.current.has(t.id)) continue;
      spoken.current.add(t.id);
      // only the screen you asked on reads the answer out (other tabs, the phone, the PC's listener stay quiet)
      if (talk && Date.now() - asked.current < 180000) {
        speak(t.text).then(() => { if (t.pending) window.dispatchEvent(new CustomEvent("ari-arm")); });
      }
    }
    if (log.current) log.current.scrollTop = log.current.scrollHeight;
  }, [turns, talk]);

  const after = useCallback((r: Said) => {
    if (r.view) onView(r.view);
    else if (r.job_id && !r.reply) { /* a model's answer: it fills in */ }
    else if (r.job_id) onSelect({ type: "job", id: r.job_id });
    else if (r.power?.id) onSelect({ type: "job", id: r.power.id });
  }, [onSelect, onView]);

  const send = useCallback(async (q: string) => {
    if (!q.trim()) return;
    asked.current = Date.now();
    setBusy(true); setErr(null); setText(""); stopSpeaking();
    try {
      const r = await api<Said>("/ari", { method: "POST", body: JSON.stringify({ text: q, conv }) });
      await load();
      after(r);
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  }, [conv, load, after]);

  const answer = async (yes: boolean) => {
    asked.current = Date.now();
    setBusy(true); setErr(null); stopSpeaking();
    try {
      const r = await api<Said>(`/ari/${conv}/answer`, { method: "POST", body: JSON.stringify({ yes }) });
      await load();
      after(r);
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  };

  useEffect(() => {  // "Hey Ari ..." heard anywhere in Helios, or a question typed on another screen
    const on = (e: Event) => send((e as CustomEvent<string>).detail);
    window.addEventListener("ari-command", on);
    let queued: string | null = null;
    try { queued = sessionStorage.getItem("ari.command"); sessionStorage.removeItem("ari.command"); } catch { /* private */ }
    if (queued) send(queued);
    return () => window.removeEventListener("ari-command", on);
  }, [send]);

  const mic = async () => {
    if ((!Speech && !vs.whisper_ready) || listening) return;
    stopSpeaking(); setListening(true); setErr(null);
    try {
      const said = await listen();
      if (said) send(said);
      else setErr("I didn't hear anything. Press the mic and speak.");
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); } finally { setListening(false); }
  };

  useEffect(() => {  // the phone's mic button opens Ari listening
    let go = false;
    try { go = sessionStorage.getItem("ari.mic") === "1"; sessionStorage.removeItem("ari.mic"); } catch { /* private */ }
    if (go) setTimeout(() => { mic(); }, 250);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const toggle = (k: string, v: boolean, set: (v: boolean) => void) => { set(v); setPref(k, v); };
  const last = [...turns].reverse().find((t) => t.role === "ari");
  const open = last?.pending && last.text !== null ? last : null;
  const title = turns.find((t) => t.role === "you")?.text ?? "new chat";

  return (
    <section className={`panel ari${compact ? " compact" : ""}`} aria-label="Ari">
      <div className="ph">
        {onBack && <button type="button" className="btn chat-back" onClick={() => { stopSpeaking(); onBack(); }}>‹ chats</button>}
        <span className="pt">ari</span>
        <span className="chat-title" title={title}>{title}</span>
        <div className="tools">
          <button type="button" className="btn" onClick={() => { stopSpeaking(); onNew(newConv()); }}>+ new chat</button>
        </div>
      </div>
      <div ref={log} className="ari-log">
        {turns.length === 0 && (
          <div className="ari-hello">
            <p>Hi, I'm Ari. Try:</p>
            {["What's running?", "Sort my downloads every morning at 7", "Remind me to call mum tomorrow at 5 pm", "Shut down the PC at 11 pm"].map((x) => (
              <button key={x} type="button" className="chip" onClick={() => send(x)}>{x}</button>
            ))}
          </div>
        )}
        {turns.map((t) => (
          <div key={t.id} className={`msg ${t.role}`}>
            <span className="who">{t.role === "you" ? "you" : "ari"}</span>
            <div className="bubble">
              {t.text !== null ? plain(t.text) : <span className="waiting"><span className="typing" aria-label="Ari is thinking"><i /><i /><i /></span>{ari.phase === "working" && ari.text && <span className="doing">{ari.text}…</span>}</span>}
              {t.used && t.used.length > 0 && (
                <div className="used">{t.used.map((u, i) => <span key={i} className={u.ok ? "" : "bad"}>{u.tool.replace(/_/g, " ")}</span>)}</div>
              )}
            </div>
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
      </div>
      <form className="ari-in" onSubmit={(e) => { e.preventDefault(); send(text); }}>
        <span className="caret">›</span>
        <input value={text} onChange={(e) => setText(e.target.value)} placeholder={listening ? "listening… speak now" : "talk to ari…"} aria-label="Message Ari" enterKeyHint="send" />
        {(Speech || vs.whisper_ready) && <button type="button" className={`mic${listening ? " on" : ""}`} onClick={mic} aria-label="Speak" title={vs.whisper_ready ? "Whisper on the PC hears you" : "The browser hears you"}>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v3" /></svg>
        </button>}
        <button type="submit" className="primary" disabled={busy || !text.trim()}>send</button>
      </form>
      <div className="ari-opts">
        {canSpeak && <label><input type="checkbox" checked={talk} onChange={(e) => toggle("speak", e.target.checked, setTalk)} /> speak replies</label>}
        {Speech && <label title="While Helios is open: say &quot;Hey Ari&quot;, then what you want"><input type="checkbox" checked={wake} onChange={(e) => toggle("wake", e.target.checked, setWake)} /> &ldquo;hey ari&rdquo;</label>}
        <EarsButton />
      </div>
    </section>
  );
}

// Pause Ari's ears everywhere (this PC's microphone and every Helios), e.g. while others talk or you record.
export function EarsButton() {
  const ears = useEars();
  const [busy, setBusy] = useState(false);
  const go = async (f: () => Promise<void>) => { setBusy(true); try { await f(); } catch { /* offline */ } finally { setBusy(false); } };
  const left = ears.paused && Number.isFinite(ears.until) ? Math.max(1, Math.round((ears.until - Date.now()) / 60000)) : null;
  return ears.paused
    ? <button type="button" className="btn ears off" disabled={busy} onClick={() => go(resumeEars)} title="Ari is not listening anywhere">
        not listening{left ? ` · ${left} min` : ""} · resume</button>
    : <button type="button" className="btn ears" disabled={busy} onClick={() => go(() => pauseEars(null))} title="Stop Ari listening (the PC's microphone and Helios) until you turn it back on">
        pause listening</button>;
}

// Ari's settings (voice), your schedules and what Ari remembers: on the chat list page.
function AriSettings() {
  const [piper, setPiper] = useState(() => pref("piper", true));
  const [vs, setVs] = useState<VoiceStatus>(voiceStatus);
  useEffect(() => {
    const st = () => setVs(voiceStatus());
    window.addEventListener("ari-status", st);
    return () => window.removeEventListener("ari-status", st);
  }, []);
  const [scheds, setScheds] = useState<Schedule[]>([]);
  const [mem, setMem] = useState<{ id: number; fact: string; noted: string }[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const loadMem = useCallback(() => { api<typeof mem>("/ari-memory").then(setMem).catch(() => {}); }, []);
  const loadScheds = useCallback(() => { api<Schedule[]>("/schedules").then(setScheds).catch(() => {}); }, []);
  useEffect(() => { loadMem(); loadScheds(); }, [loadMem, loadScheds]);
  const mine = scheds.filter((s) => s.owner === "you");
  const editSched = async (s: Schedule, how: "pause" | "resume" | "delete") => {
    try {
      if (how === "delete") await api(`/schedules/${s.id}`, { method: "DELETE" });
      else await api(`/schedules/${s.id}`, { method: "PATCH", body: JSON.stringify({ enabled: how === "resume" }) });
      loadScheds();
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  return (
    <div className="ari-side">
      <section className="panel">
        <div className="ph"><span className="pt">ari/settings</span></div>
        <div className="ari-set">
          <a className="btn" href="#voice" title="Read sentences aloud so Ari learns your accent and names">train on my voice</a>
          <EarsButton />
          {vs.voice && <label title="Piper: Argus's own natural voice (off: the browser's voice)"><input type="checkbox" checked={piper} onChange={(e) => { setPiper(e.target.checked); setPref("piper", e.target.checked); }} /> natural voice</label>}
          {vs.voice && piper && <VoicePick />}
          {vs.hearing === "whisper" && <span className="muted">{vs.whisper_ready ? "Whisper hears you" : "Whisper: the PC is off, the browser hears you"}</span>}
        </div>
      </section>
      <IslandSettings />
      <section className="panel">
        <div className="ph"><span className="pt">ari/schedules</span><span className="muted">{mine.length}</span></div>
        <div className="ari-sched">
          {err && <div className="tip bad">{err}</div>}
          {mine.length === 0 ? <div className="tip">None yet. Tell Ari: "sort downloads every morning at 7".</div> : mine.map((s) => (
            <div key={s.id} className={`srow${s.enabled ? "" : " off"}`}>
              <span className="sl">{s.label ?? `${s.plugin} · ${s.workflow}`}</span>
              <span className="muted mono">{s.enabled ? `next ${when(s.next_run_at)}` : s.spec.once && s.last_run_at ? `done ${when(s.last_run_at)}` : "paused"}</span>
              {!(s.spec.once && s.last_run_at) && <button type="button" className="btn" onClick={() => editSched(s, s.enabled ? "pause" : "resume")}>{s.enabled ? "pause" : "resume"}</button>}
              <button type="button" className="btn" onClick={() => editSched(s, "delete")}>delete</button>
            </div>
          ))}
          {scheds.some((s) => s.owner !== "you") && (
            <details><summary className="muted">from settings and plugins ({scheds.filter((s) => s.owner !== "you").length})</summary>
              {scheds.filter((s) => s.owner !== "you").map((s) => (
                <div key={s.id} className="srow"><span className="sl mono">{s.id}</span><span className="muted mono">{s.cron} · next {when(s.next_run_at)}</span></div>
              ))}
            </details>
          )}
        </div>
      </section>
      <section className="panel">
        <div className="ph"><span className="pt">ari/memory</span><span className="muted">{mem.length}</span></div>
        <div className="ari-sched">
          {mem.length === 0 ? <div className="tip">Nothing yet. Say "remember my car service is due in December".</div> : mem.map((m) => (
            <div key={m.id} className="srow">
              <span className="sl">{m.fact}</span><span className="muted mono">{m.noted}</span>
              <button type="button" className="btn" onClick={() => api(`/ari-memory/${m.id}`, { method: "DELETE" }).then(loadMem).catch(() => {})}>forget</button>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

type VoiceList = { current: string | null; speed: number; voices: { id: string; label: string; installed: boolean }[] };

// Ari's voice and speed (Piper, in argusd): the pick is kept by Argus, so every screen and "Hey Ari" on the PC use it.
function VoicePick() {
  const [list, setList] = useState<VoiceList | null>(null);
  const [voice, setVoice] = useState("");
  const [speed, setSpeed] = useState(1);
  const [note, setNote] = useState<string | null>(null);
  useEffect(() => {
    api<VoiceList>("/ari-voice/voices").then((l) => { setList(l); setVoice(l.current ?? ""); setSpeed(l.speed); }).catch(() => {});
  }, []);
  const save = async (v: string, sp: number): Promise<boolean> => {
    const fresh = list?.voices.find((x) => x.id === v && !x.installed);
    setNote(fresh ? "downloading the voice (~60 MB)…" : null);
    try {
      const l = await api<VoiceList>("/ari-voice/voice", { method: "PUT", body: JSON.stringify({ voice: v, speed: sp }) });
      setList(l); setNote(null); return true;
    } catch (e) { setNote(e instanceof Error ? e.message : "could not change the voice"); return false; }
  };
  const hear = async () => { if (await save(voice, speed)) await speak("Hi, I'm Ari. This is how I sound."); };
  if (!list) return null;
  return (
    <span className="voice-pick" role="group" aria-label="Ari's voice">
      Voice
      <select value={voice} aria-label="Voice" onChange={(e) => { setVoice(e.target.value); save(e.target.value, speed); }}>
        {list.voices.map((v) => <option key={v.id} value={v.id}>{v.label}{v.installed ? "" : " · download"}</option>)}
      </select>
      <label title="How fast Ari talks">speed
        <input type="range" min={0.6} max={1.6} step={0.1} value={speed} aria-label="Speed"
          onChange={(e) => setSpeed(Number(e.target.value))} onPointerUp={() => save(voice, speed)} onKeyUp={() => save(voice, speed)} />
        <span className="mono">{speed.toFixed(1)}×</span>
      </label>
      <button type="button" className="btn" onClick={hear}>▶ hear it</button>
      {note && <span className="muted">{note}</span>}
    </span>
  );
}

type IslandW = { id: string; on: boolean };
type IslandS = { label: string; action: string };
type IslandData = { widgets: IslandW[]; shortcuts: IslandS[]; widget_labels: Record<string, string>;
  choices: { action: string; label: string; group: string }[]; max_shortcuts: number };

// What Ari's island on the PC shows when you click it: widgets (on/off, order) and your shortcuts.
function IslandSettings() {
  const [d, setD] = useState<IslandData | null>(null);
  const [w, setW] = useState<IslandW[]>([]);
  const [sc, setSc] = useState<IslandS[]>([]);
  const [msg, setMsg] = useState<string | null>(null);
  const load = useCallback(() => {
    api<IslandData>("/island").then((x) => { setD(x); setW(x.widgets); setSc(x.shortcuts); }).catch(() => {});
  }, []);
  useEffect(load, [load]);
  if (!d) return null;
  const dirty = JSON.stringify({ w, sc }) !== JSON.stringify({ w: d.widgets, sc: d.shortcuts });
  const move = <T,>(list: T[], i: number, by: number) => {
    const n = [...list];
    const k = i + by;
    if (k < 0 || k >= n.length) return list;
    [n[i], n[k]] = [n[k], n[i]];
    return n;
  };
  const save = async () => {
    setMsg(null);
    try {
      const x = await api<IslandData>("/island", { method: "PUT", body: JSON.stringify({ widgets: w, shortcuts: sc }) });
      setD(x); setW(x.widgets); setSc(x.shortcuts); setMsg("saved ✓");
      setTimeout(() => setMsg(null), 2000);
    } catch (e) { setMsg(e instanceof Error ? e.message : String(e)); }
  };
  const groups = [...new Set(d.choices.map((c) => c.group))];
  return (
    <section className="panel">
      <div className="ph">
        <span className="pt">ari/island</span>
        <span className="muted">what the island on the PC shows when you click it</span>
      </div>
      <div className="isl-set">
        <div className="sect">widgets</div>
        {w.map((x, i) => (
          <div key={x.id} className="isl-set-row">
            <label><input type="checkbox" checked={x.on} onChange={(e) => setW(w.map((y) => (y.id === x.id ? { ...y, on: e.target.checked } : y)))} /> {d.widget_labels[x.id] ?? x.id}</label>
            <span className="grow" />
            <button type="button" className="btn mini" aria-label="Up" disabled={i === 0} onClick={() => setW(move(w, i, -1))}>↑</button>
            <button type="button" className="btn mini" aria-label="Down" disabled={i === w.length - 1} onClick={() => setW(move(w, i, 1))}>↓</button>
          </div>
        ))}
        <div className="sect">shortcuts <span className="muted">{sc.length}/{d.max_shortcuts}</span></div>
        {sc.map((x, i) => {
          const isUrl = x.action.startsWith("url:");
          return (
            <div key={i} className="isl-set-row sc">
              <input value={x.label} maxLength={24} aria-label="Name" onChange={(e) => setSc(sc.map((y, k) => (k === i ? { ...y, label: e.target.value } : y)))} />
              <select value={isUrl ? "url:" : x.action} aria-label="What it does"
                onChange={(e) => setSc(sc.map((y, k) => (k === i ? { ...y, action: e.target.value === "url:" ? "url:https://" : e.target.value } : y)))}>
                {!isUrl && !d.choices.some((c) => c.action === x.action) && <option value={x.action}>{x.action} (not found)</option>}
                {groups.map((g) => (
                  <optgroup key={g} label={g}>{d.choices.filter((c) => c.group === g).map((c) => <option key={c.action} value={c.action}>{c.label}</option>)}</optgroup>
                ))}
                <option value="url:">a website…</option>
              </select>
              {isUrl && <input value={x.action.slice(4)} aria-label="Website" placeholder="https://" onChange={(e) => setSc(sc.map((y, k) => (k === i ? { ...y, action: `url:${e.target.value}` } : y)))} />}
              <button type="button" className="btn mini" aria-label="Up" disabled={i === 0} onClick={() => setSc(move(sc, i, -1))}>↑</button>
              <button type="button" className="btn mini" aria-label="Down" disabled={i === sc.length - 1} onClick={() => setSc(move(sc, i, 1))}>↓</button>
              <button type="button" className="btn mini" aria-label="Remove" onClick={() => setSc(sc.filter((_, k) => k !== i))}>×</button>
            </div>
          );
        })}
        <div className="isl-set-foot">
          <button type="button" className="btn" disabled={sc.length >= d.max_shortcuts}
            onClick={() => setSc([...sc, { label: "New shortcut", action: "show:inbox" }])}>+ shortcut</button>
          <span className="grow" />
          {msg && <span className={msg.endsWith("✓") ? "ok mono" : "bad"}>{msg}</span>}
          <button type="button" className="btn" disabled={!dirty} onClick={() => { setW(d.widgets); setSc(d.shortcuts); }}>undo</button>
          <button type="button" className="primary" disabled={!dirty} onClick={save}>save</button>
        </div>
      </div>
    </section>
  );
}
