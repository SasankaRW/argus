// Train Ari on your voice: read sentences aloud, one at a time. The recordings stay on your machines; on the PC,
// `python -m argus.voice_train --apply` fine-tunes Whisper on them (argus/voice_train.py).
import { useCallback, useEffect, useRef, useState } from "react";
import { api, getToken } from "./api";
import { earsPaused, pauseEars, resumeEars } from "./ariState";

type Sentence = { id: string; text: string; kind: string; done: boolean; seconds: number | null };
type State = { sentences: Sentence[]; recorded: number; seconds: number; goal_seconds: number; model: string };

const MIME = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];

function mmss(s: number) { return `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`; }

export function VoiceTrain() {
  const [st, setSt] = useState<State | null>(null);
  const [at, setAt] = useState(0);  // which sentence is showing
  const [rec, setRec] = useState(false);
  const [level, setLevel] = useState(0);
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const stream = useRef<MediaStream | null>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const started = useRef(0);
  const raf = useRef(0);
  const audio = useRef<HTMLAudioElement | null>(null);

  useEffect(() => {  // Ari stops listening while you record here (or it would answer the sentences you read)
    const mine = !earsPaused();
    if (!mine) return;
    pauseEars(3).catch(() => {});
    const keep = setInterval(() => { pauseEars(3).catch(() => {}); }, 60000);
    return () => { clearInterval(keep); resumeEars().catch(() => {}); };
  }, []);

  const load = useCallback(async (jump = false) => {
    const s = await api<State>("/voice-train");
    setSt(s);
    if (jump) { const i = s.sentences.findIndex((x) => !x.done); setAt(i < 0 ? 0 : i); }
  }, []);
  useEffect(() => { load(true).catch((e) => setErr(e instanceof Error ? e.message : String(e))); }, [load]);
  useEffect(() => () => { stream.current?.getTracks().forEach((t) => t.stop()); cancelAnimationFrame(raf.current); }, []);

  const mic = async () => {
    if (stream.current) return stream.current;
    // the raw microphone, like Ari's listener hears it: no echo cancelling, noise suppression or auto gain
    const s = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1 } });
    stream.current = s;
    const ctx = new AudioContext();
    const an = ctx.createAnalyser();
    an.fftSize = 1024;
    ctx.createMediaStreamSource(s).connect(an);
    const buf = new Float32Array(an.fftSize);
    const tick = () => {
      an.getFloatTimeDomainData(buf);
      let sum = 0; for (const v of buf) sum += v * v;
      setLevel(Math.min(1, Math.sqrt(sum / buf.length) * 6));
      raf.current = requestAnimationFrame(tick);
    };
    tick();
    return s;
  };

  const cur = st?.sentences[at];

  const start = async () => {
    if (!cur || rec || saving) return;
    setErr(null);
    try {
      const s = await mic();
      const type = MIME.find((m) => MediaRecorder.isTypeSupported(m)) ?? "";
      const r = new MediaRecorder(s, type ? { mimeType: type } : undefined);
      const chunks: Blob[] = [];
      r.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
      r.onstop = () => upload(new Blob(chunks, { type: r.mimeType || type }), (performance.now() - started.current) / 1000);
      recorder.current = r;
      started.current = performance.now();
      r.start();
      setRec(true);
    } catch (e) { setErr(e instanceof Error ? `microphone: ${e.message}` : String(e)); }
  };

  const stop = () => { if (recorder.current?.state === "recording") recorder.current.stop(); setRec(false); };

  const upload = async (blob: Blob, seconds: number) => {
    if (!cur) return;
    if (seconds < 0.6) { setErr("That was very short: press record, read the whole sentence, then stop."); return; }
    setSaving(true);
    try {
      await api(`/voice-train/${cur.id}?seconds=${seconds.toFixed(2)}`, { method: "POST", body: blob, headers: { "Content-Type": blob.type.split(";")[0] || "audio/webm" } });
      await load();
      setAt((i) => Math.min(i + 1, (st?.sentences.length ?? 1) - 1));
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); } finally { setSaving(false); }
  };

  const play = async (s: Sentence) => {
    const t = getToken();
    const r = await fetch(`/voice-train/${s.id}/audio`, { headers: t ? { Authorization: `Bearer ${t}` } : {} });
    if (!r.ok) return;
    audio.current?.pause();
    audio.current = new Audio(URL.createObjectURL(await r.blob()));
    audio.current.play().catch(() => {});
  };

  const redo = async (s: Sentence) => {
    await api(`/voice-train/${s.id}`, { method: "DELETE" }).catch(() => {});
    await load();
  };

  useEffect(() => {  // Space: record / stop; arrows: previous / next
    const on = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement)?.tagName === "INPUT") return;
      if (e.code === "Space") { e.preventDefault(); if (rec) stop(); else start(); }
      else if (e.code === "ArrowRight" && !rec) setAt((i) => Math.min(i + 1, (st?.sentences.length ?? 1) - 1));
      else if (e.code === "ArrowLeft" && !rec) setAt((i) => Math.max(i - 1, 0));
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  });

  if (!st) return <section className="panel voice-train"><div className="ph"><span className="pt">ari/voice</span></div><div className="empty">{err ?? "loading…"}</div></section>;
  const pct = Math.min(1, st.seconds / st.goal_seconds);
  const enough = st.seconds >= 10 * 60;
  return (
    <section className="panel voice-train">
      <div className="ph"><span className="pt">ari/voice</span><span className="muted">train Ari on your voice</span></div>
      <div className="vt-body">
        <div className="vt-progress">
          <div className="bar"><i style={{ width: `${pct * 100}%` }} /></div>
          <span>{mmss(st.seconds)} of {mmss(st.goal_seconds)} recorded · {st.recorded} / {st.sentences.length} sentences</span>
        </div>
        {cur && (
          <div className={`vt-card${rec ? " rec" : ""}${cur.done ? " done" : ""}`}>
            <span className="vt-kind">{at + 1}. {cur.kind}{cur.done ? " · recorded" : ""}</span>
            <p className="vt-text">{cur.text}</p>
            <div className="vt-level" aria-hidden="true"><i style={{ width: `${(rec ? level : 0) * 100}%` }} /></div>
            <div className="vt-actions">
              {!rec
                ? <button type="button" className="primary" disabled={saving} onClick={start}>{saving ? "saving…" : cur.done ? "record again" : "record"}</button>
                : <button type="button" className="primary rec" onClick={stop}>stop</button>}
              <button type="button" className="btn" disabled={rec || at === 0} onClick={() => setAt(at - 1)}>‹ back</button>
              <button type="button" className="btn" disabled={rec} onClick={() => setAt(Math.min(at + 1, st.sentences.length - 1))}>skip ›</button>
              {cur.done && !rec && <button type="button" className="btn" onClick={() => play(cur)}>play</button>}
              {cur.done && !rec && <button type="button" className="btn" onClick={() => redo(cur)}>delete</button>}
              <span className="muted">space: record / stop</span>
            </div>
          </div>
        )}
        {err && <div className="tip bad">{err}</div>}
        <div className="vt-tips">
          <p className="ok">Ari isn't listening while this page is open, so it won't answer what you read.</p>
          <p>Read each sentence the way you talk to Ari, at your desk, with the microphone Ari listens on. Mistakes are fine: just record it again. Names matter most, so say them the way you say them.</p>
          {enough
            ? <p className="ok">Enough to train. On the PC: <code>python -m argus.voice_train --apply</code> (about 20–60 minutes on the GPU). It only switches Ari to the new model if it makes fewer mistakes than <code>{st.model}</code>.</p>
            : <p>Train once you have 10 minutes or more (20 is better).</p>}
        </div>
      </div>
    </section>
  );
}
