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
    const r = await fetch("/ari/say", { method: "POST", headers: { "Content-Type": "application/json", ...(t ? { Authorization: `Bearer ${t}` } : {}) },
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
    if (pref("piper", false) && await speakPiper(clean)) return;
    await speakBrowser(clean);
  } finally { speaking--; }
}

export function stopSpeaking() { if (canSpeak) window.speechSynthesis.cancel(); }

// One utterance: resolves with what was said ("" when nothing).
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
