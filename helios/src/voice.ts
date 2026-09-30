// Ari's ears and voice in the browser.
// Hearing: the browser's speech recognition (Chrome, Edge). Voice: Argus's natural voice (Piper on the PC) when it
// is set up and the PC is on, else the browser's own voice. "Hey Ari": a continuous listener that hands over what
// follows the wake phrase.

import { getToken } from "./api";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const W = window as any;
export const Speech: any = W.SpeechRecognition || W.webkitSpeechRecognition; // eslint-disable-line @typescript-eslint/no-explicit-any
export const canSpeak = typeof window !== "undefined" && "speechSynthesis" in window;

export const WAKE = /^\s*(?:(?:hey|hi|ok|okay|a)\s*,?\s*)?(ari|arie|ary|harry|hari|artie|argus)\b[\s,.!?]*/i;

let speaking = 0;
export const isSpeaking = () => speaking > 0;

export function pref(key: string, def = false): boolean {
  try { const v = localStorage.getItem(`ari.${key}`); return v === null ? def : v === "1"; } catch { return def; }
}
export function setPref(key: string, v: boolean) {
  try { localStorage.setItem(`ari.${key}`, v ? "1" : "0"); } catch { /* private window */ }
  window.dispatchEvent(new CustomEvent("ari-pref", { detail: { key, v } }));
}

function browserVoice(): SpeechSynthesisVoice | null {
  const vs = window.speechSynthesis.getVoices().filter((v) => v.lang.startsWith("en"));
  // the nicer voices first (Chrome's Google voices, Edge's "Natural" ones)
  return vs.find((v) => /natural|neural/i.test(v.name)) ?? vs.find((v) => /google (uk|us) english/i.test(v.name)) ?? vs[0] ?? null;
}

async function speakBrowser(text: string): Promise<void> {
  if (!canSpeak) return;
  await new Promise<void>((res) => {
    const u = new SpeechSynthesisUtterance(text);
    const v = browserVoice();
    if (v) u.voice = v;
    u.rate = 1.02;
    u.onend = () => res(); u.onerror = () => res();
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(u);
  });
}

// Piper through Argus (POST /ari/say -> audio). Falls back to the browser voice when it isn't available.
async function speakPiper(text: string): Promise<boolean> {
  try {
    const t = getToken();
    const r = await fetch("/ari-voice/say", { method: "POST", headers: { "Content-Type": "application/json", ...(t ? { Authorization: `Bearer ${t}` } : {}) },
      body: JSON.stringify({ text }) });
    if (!r.ok) return false;
    const url = URL.createObjectURL(await r.blob());
    await new Promise<void>((res) => { const a = new Audio(url); a.onended = () => res(); a.onerror = () => res(); a.play().catch(() => res()); });
    URL.revokeObjectURL(url);
    return true;
  } catch { return false; }
}

export async function speak(text: string): Promise<void> {
  const clean = text.replace(/[*_`#>]/g, "").trim();
  if (!clean) return;
  speaking++;
  try {
    if (status.voice && pref("piper", true) && await speakPiper(clean)) return;
    await speakBrowser(clean);
  } finally { speaking--; }
}

export function stopSpeaking() { if (canSpeak) window.speechSynthesis.cancel(); }

// What Argus offers (GET /ari-voice): Piper for speaking, Whisper on the PC for hearing.
export type VoiceStatus = { voice: boolean; hearing: "browser" | "whisper"; whisper_ready: boolean };
let status: VoiceStatus = { voice: false, hearing: "browser", whisper_ready: false };
export function setVoiceStatus(s: VoiceStatus) { status = s; }

// Record until you stop talking (about a second of quiet), at most 15 s; then Whisper on the PC. null: not
// available now (the caller uses the browser's recognition instead).
async function hearWhisper(onLevel?: (on: boolean) => void): Promise<string | null> {
  if (!status.whisper_ready || !navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") return null;
  let stream: MediaStream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); } catch { return null; }
  const rec = new MediaRecorder(stream);
  const chunks: Blob[] = [];
  rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  const done = new Promise<void>((res) => { rec.onstop = () => res(); });
  const ctx = new AudioContext();
  const an = ctx.createAnalyser();
  ctx.createMediaStreamSource(stream).connect(an);
  const buf = new Uint8Array(an.fftSize);
  const t0 = Date.now();
  let lastLoud = Date.now(), spoke = false;
  rec.start(250);
  onLevel?.(true);
  await new Promise<void>((res) => {
    const tick = () => {
      an.getByteTimeDomainData(buf);
      let peak = 0;
      for (const v of buf) peak = Math.max(peak, Math.abs(v - 128));
      if (peak > 12) { lastLoud = Date.now(); spoke = true; }
      const quiet = Date.now() - lastLoud;
      if ((spoke && quiet > 1100) || (!spoke && Date.now() - t0 > 6000) || Date.now() - t0 > 15000) return res();
      requestAnimationFrame(tick);
    };
    tick();
  });
  rec.stop();
  await done;
  onLevel?.(false);
  stream.getTracks().forEach((t) => t.stop());
  ctx.close().catch(() => {});
  if (!spoke) return "";
  const blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
  try {
    const t = getToken();
    const r = await fetch("/ari-voice/hear", { method: "POST", body: blob,
      headers: { "Content-Type": blob.type.split(";")[0], ...(t ? { Authorization: `Bearer ${t}` } : {}) } });
    if (r.status === 409) return null;
    if (!r.ok) return "";
    return String((await r.json()).text ?? "");
  } catch { return null; }
}

// One utterance: Whisper on the PC when it is set up and on, else the browser. "" when nothing was heard.
export async function listen(): Promise<string> {
  const w = await hearWhisper();
  if (w !== null) return w;
  return listenOnce();
}

// One utterance with the browser's recognition.
export function listenOnce(): Promise<string> {
  return new Promise((res) => {
    if (!Speech) return res("");
    const rec = new Speech();
    rec.lang = "en-US"; rec.interimResults = false; rec.maxAlternatives = 1;
    let got = "";
    rec.onresult = (e: any) => { got = e.results[0][0].transcript as string; }; // eslint-disable-line @typescript-eslint/no-explicit-any
    rec.onend = () => res(got);
    rec.onerror = () => res(got);
    try { rec.start(); } catch { res(""); }
  });
}

// "Hey Ari": listens while on; calls onCommand with what follows the wake phrase (or the next thing said within
// 8 seconds when the wake phrase came alone). Chrome stops a continuous listener now and then: it restarts.
export class WakeListener {
  private rec: any = null; // eslint-disable-line @typescript-eslint/no-explicit-any
  private on = false;
  private armedUntil = 0;
  constructor(private onCommand: (text: string) => void, private onWake: () => void) {}

  start() {
    if (!Speech || this.on) return;
    this.on = true;
    this.spawn();
  }

  // listen for a follow-up without the wake phrase (after Ari asked "Shall I?")
  arm(ms = 8000) { this.armedUntil = Date.now() + ms; }

  stop() {
    this.on = false;
    try { this.rec?.stop(); } catch { /* already stopped */ }
    this.rec = null;
  }

  private spawn() {
    if (!this.on) return;
    const rec = new Speech();
    rec.lang = "en-US"; rec.continuous = true; rec.interimResults = false;
    rec.onresult = (e: any) => { // eslint-disable-line @typescript-eslint/no-explicit-any
      if (isSpeaking()) return; // don't hear ourselves
      for (let i = e.resultIndex; i < e.results.length; i++) {
        if (!e.results[i].isFinal) continue;
        const said = String(e.results[i][0].transcript || "").trim();
        const m = said.match(WAKE);
        if (m) {
          const rest = said.slice(m[0].length).trim();
          this.onWake();
          if (rest.length > 1) { this.armedUntil = 0; this.onCommand(rest); } else this.armedUntil = Date.now() + 8000;
        } else if (Date.now() < this.armedUntil && said) {
          this.armedUntil = 0;
          this.onCommand(said);
        }
      }
    };
    rec.onend = () => { if (this.on) setTimeout(() => this.spawn(), 300); };
    rec.onerror = (e: any) => { if (e?.error === "not-allowed") this.on = false; }; // eslint-disable-line @typescript-eslint/no-explicit-any
    this.rec = rec;
    try { rec.start(); } catch { /* starting twice */ }
  }
}
