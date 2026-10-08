// Ari's ears and voice in the browser.
// Hearing: the browser's speech recognition (Chrome, Edge). Voice: Ari's one voice, made by Argus (the voice on
// the PC). When it can't speak (the PC is off, it is loading) the words stay on screen and nothing else speaks.
// "Hey Ari": a continuous listener that hands over what follows the wake phrase.

import { getToken } from "./api";
import { ariNow, ariReport, ariSet, ariTell, setServerLook } from "./ariState";
import { plain } from "./spoken";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const W = window as any;
export const Speech: any = W.SpeechRecognition || W.webkitSpeechRecognition; // eslint-disable-line @typescript-eslint/no-explicit-any

// "Hey Ari" as speech recognizers write it: Ari alone in its own spellings, or after hey/hi/ok also the words it
// is often misheard as (Harry, Siri, Audi, ...). Anywhere in what was heard; what follows is the command.
const STRONG = "ari|arie|arri|aree|ary|aari|arie|argus";
const WEAK = "harry|hari|hurry|siri|sorry|audi|ori|aria|ali|ally|arty|artie|are e|r e|ra|rie|lorry";
const WAKE_RX = new RegExp(`(?:^|\\b)(?:(?:hey|hi|hay|ok|okay|a)[\\s,.]*(?:${STRONG}|${WEAK})|(?:${STRONG}))\\b[\\s,.!?]*`, "i");
export function findWake(said: string): string | null {
  const m = said.match(WAKE_RX);
  return m ? said.slice((m.index ?? 0) + m[0].length).trim() : null;
}

let speaking = 0;
export const isSpeaking = () => speaking > 0;

export function pref(key: string, def = false): boolean {
  try { const v = localStorage.getItem(`ari.${key}`); return v === null ? def : v === "1"; } catch { return def; }
}
export function setPref(key: string, v: boolean) {
  try { localStorage.setItem(`ari.${key}`, v ? "1" : "0"); } catch { /* private window */ }
  window.dispatchEvent(new CustomEvent("ari-pref", { detail: { key, v } }));
}

// Ari's voice through Argus (POST /ari-voice/say -> audio). False when it can't speak right now.
async function speakAri(text: string): Promise<boolean> {
  try {
    const t = getToken();
    const r = await fetch("/ari-voice/say", { method: "POST", headers: { "Content-Type": "application/json", ...(t ? { Authorization: `Bearer ${t}` } : {}) },
      body: JSON.stringify({ text }) });
    if (!r.ok) return false;
    const url = URL.createObjectURL(await r.blob());
    await new Promise<void>((res) => { const a = new Audio(url); playing.add(a);
      const end = () => { playing.delete(a); res(); };
      a.onended = end; a.onerror = end; a.onpause = end; a.play().catch(end); });
    URL.revokeObjectURL(url);
    return true;
  } catch { return false; }
}

export async function speak(text: string): Promise<void> {
  const voiced = text.replace(/[*_`#>]/g, "").trim();  // the mood and [laugh] stay for Ari's expressive voice
  const clean = plain(voiced);
  if (!clean) return;
  speaking++;
  ariTell("speaking", clean);
  try {
    if (status.voice) await speakAri(voiced);
  } finally {
    speaking--;
    if (!speaking && ariNow().phase === "speaking") { ariSet("done", clean); ariReport("idle"); }
  }
}

export function stopSpeaking() { for (const a of playing) a.pause(); }
const playing = new Set<HTMLAudioElement>();

// What Argus offers (GET /ari-voice): Ari's voice for speaking (while it can), Whisper on the PC for hearing.
export type VoiceStatus = { voice: boolean; hearing: "browser" | "whisper"; whisper_ready: boolean; popup_here?: boolean; pill?: string };
let status: VoiceStatus = { voice: false, hearing: "browser", whisper_ready: false };
export function setVoiceStatus(s: VoiceStatus) { status = s; setServerLook(s.pill); window.dispatchEvent(new CustomEvent("ari-status")); }
export const voiceStatus = () => status;

// One microphone user at a time: the "Hey Ari" listener steps aside while the mic button records.
let micBusy = 0;
export const isMicBusy = () => micBusy > 0;

export class HearingError extends Error {}

function why(code: string): string {
  switch (code) {
    case "not-allowed": case "service-not-allowed":
      return "The microphone is blocked for this page. Allow it (the mic icon in the address bar), then try again.";
    case "network":
      return "The browser's speech service can't be reached (Chrome and Edge send speech to their cloud). "
        + "Whisper on the PC can hear you instead: ari.hearing: whisper in argus.yaml.";
    case "audio-capture": return "No microphone found.";
    case "language-not-supported": return "The browser can't recognise English here.";
    default: return `Speech recognition failed (${code}).`;
  }
}

// Record one utterance: from the first sound until about a second of quiet (at most 15 s). "" when nobody spoke.
async function record(stream: MediaStream, maxWaitMs = 6000, quietMs = 1100): Promise<Blob | null> {
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
  await new Promise<void>((res) => {
    const tick = () => {
      an.getByteTimeDomainData(buf);
      let peak = 0;
      for (const v of buf) peak = Math.max(peak, Math.abs(v - 128));
      if (peak > 12) { lastLoud = Date.now(); spoke = true; }
      if ((spoke && Date.now() - lastLoud > quietMs) || (!spoke && Date.now() - t0 > maxWaitMs) || Date.now() - t0 > 15000) return res();
      setTimeout(tick, 50);
    };
    tick();
  });
  rec.stop();
  await done;
  ctx.close().catch(() => {});
  return spoke ? new Blob(chunks, { type: rec.mimeType || "audio/webm" }) : null;
}

// A recording to text with Whisper on the PC. null: Whisper isn't available right now.
async function transcribe(blob: Blob): Promise<string | null> {
  const t = getToken();
  const r = await fetch("/ari-voice/hear", { method: "POST", body: blob,
    headers: { "Content-Type": blob.type.split(";")[0] || "audio/webm", ...(t ? { Authorization: `Bearer ${t}` } : {}) } });
  if (r.status === 409) return null;
  if (!r.ok) throw new HearingError("Whisper on the PC could not write that down.");
  return String((await r.json()).text ?? "");
}

async function hearWhisper(): Promise<string | null> {
  if (!status.whisper_ready || !navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") return null;
  let stream: MediaStream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
  catch { throw new HearingError(why("not-allowed")); }
  try {
    const blob = await record(stream);
    return blob ? await transcribe(blob) : "";
  } finally { stream.getTracks().forEach((t) => t.stop()); }
}

// One utterance with the browser's recognition. Throws HearingError with a plain reason when it fails.
function hearBrowser(): Promise<string> {
  return new Promise((res, rej) => {
    if (!Speech) return rej(new HearingError("This browser can't recognise speech (use Chrome or Edge, or turn on Whisper)."));
    const rec = new Speech();
    rec.lang = "en-US"; rec.interimResults = false; rec.maxAlternatives = 1;
    let got = "", err = "";
    rec.onresult = (e: any) => { got = String(e.results[0][0].transcript || ""); }; // eslint-disable-line @typescript-eslint/no-explicit-any
    rec.onerror = (e: any) => { err = e?.error || "unknown"; }; // eslint-disable-line @typescript-eslint/no-explicit-any
    rec.onend = () => (err && err !== "no-speech" && err !== "aborted" ? rej(new HearingError(why(err))) : res(got));
    try { rec.start(); } catch (e) { rej(new HearingError(`Couldn't start the microphone: ${e}`)); }
  });
}

// The mic button: Whisper on the PC when it is set up and on, else the browser. "" when nothing was heard.
export async function listen(): Promise<string> {
  micBusy++;
  window.dispatchEvent(new CustomEvent("ari-mic"));
  ariTell("listening");
  try {
    await new Promise((r) => setTimeout(r, 150)); // let the wake listener release the microphone
    const w = await hearWhisper();
    const said = w !== null ? w : await hearBrowser();
    if (said && ariNow().phase === "listening") ariSet("thinking", said);  // Argus reports the rest
    return said;
  } finally {
    micBusy--;
    window.dispatchEvent(new CustomEvent("ari-mic"));
    if (ariNow().phase === "listening") ariTell("idle");
  }
}

type WakeEvents = { onCommand: (text: string) => void; onWake: () => void; onError: (msg: string) => void };

// "Hey Ari" while Helios is open. With Whisper on the PC: short recordings of whatever is said, written down on the
// PC, checked for the wake phrase (private; any browser). Otherwise the browser's continuous recognition.
export class WakeListener {
  private on = false;
  private armedUntil = 0;
  private rec: any = null; // eslint-disable-line @typescript-eslint/no-explicit-any
  private stream: MediaStream | null = null;
  private errors: number[] = [];
  constructor(private ev: WakeEvents) {}

  start() {
    if (this.on) return;
    this.on = true;
    const onMic = () => { if (isMicBusy()) this.pause(); else this.resume(); };
    window.addEventListener("ari-mic", onMic);
    this.cleanup = () => window.removeEventListener("ari-mic", onMic);
    this.resume();
  }

  private cleanup = () => {};

  // listen for a follow-up without the wake phrase (after Ari asked "Shall I?")
  arm(ms = 8000) { this.armedUntil = Date.now() + ms; }

  stop() {
    this.on = false;
    this.cleanup();
    this.pause();
  }

  private pause() {
    try { this.rec?.abort(); } catch { /* stopped */ }
    this.rec = null;
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
  }

  private resume() {
    if (!this.on || isMicBusy() || this.rec || this.stream) return;
    if (status.whisper_ready && !!navigator.mediaDevices && typeof MediaRecorder !== "undefined") this.whisperLoop();
    else if (Speech) this.browser();
    else { this.on = false; this.ev.onError("This browser can't listen for \"Hey Ari\" (use Chrome or Edge, or turn on Whisper)."); }
  }

  private heard(said: string) {
    if (!said || isSpeaking()) return;
    const rest = findWake(said);
    if (rest !== null) {
      this.ev.onWake();
      if (rest.replace(/[^a-z0-9]/gi, "").length > 1) { this.armedUntil = 0; this.ev.onCommand(rest); }
      else this.armedUntil = Date.now() + 8000;
    } else if (Date.now() < this.armedUntil) {
      this.armedUntil = 0;
      this.ev.onCommand(said);
    }
  }

  private failed(code: string) {
    const now = Date.now();
    this.errors = [...this.errors.filter((t) => now - t < 60000), now];
    if (code === "not-allowed" || code === "service-not-allowed" || code === "audio-capture" || this.errors.length >= 4) {
      this.on = false;
      this.pause();
      this.ev.onError(`"Hey Ari" stopped: ${why(code)}`);
    }
  }

  private browser() {
    const rec = new Speech();
    rec.lang = "en-US"; rec.continuous = true; rec.interimResults = false;
    rec.onresult = (e: any) => { // eslint-disable-line @typescript-eslint/no-explicit-any
      for (let i = e.resultIndex; i < e.results.length; i++) {
        if (e.results[i].isFinal) this.heard(String(e.results[i][0].transcript || "").trim());
      }
    };
    rec.onerror = (e: any) => { const c = e?.error || "unknown"; if (c !== "no-speech" && c !== "aborted") this.failed(c); }; // eslint-disable-line @typescript-eslint/no-explicit-any
    rec.onend = () => { if (this.rec === rec) { this.rec = null; setTimeout(() => this.resume(), 400); } };
    this.rec = rec;
    try { rec.start(); } catch { this.rec = null; }
  }

  private async whisperLoop() {
    try { this.stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch { this.failed("not-allowed"); return; }
    const stream = this.stream;
    while (this.on && this.stream === stream) {
      if (isSpeaking()) { await new Promise((r) => setTimeout(r, 300)); continue; }
      const blob = await record(stream, 60000, 700).catch(() => null);
      if (!blob || this.stream !== stream) continue;
      if (blob.size < 2000) continue; // a click or a cough
      try {
        const text = await transcribe(blob);
        if (text === null) { this.pause(); status = { ...status, whisper_ready: false }; this.resume(); return; }
        this.heard(text.trim());
      } catch { this.failed("whisper"); }
    }
  }
}
