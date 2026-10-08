// What Ari is doing right now, for the Ari pill (listening, thinking, working, speaking, done). Anything in Helios
// can report; the pill shows the latest. "done" shows the answer for a moment, then the pill folds away.
import { useSyncExternalStore } from "react";
import { api } from "./api";
import { plain } from "./spoken";

export type AriPhase = "idle" | "listening" | "thinking" | "working" | "speaking" | "done";
export type AriState = { phase: AriPhase; text: string; at: number; remote?: boolean };

let state: AriState = { phase: "idle", text: "", at: 0 };
const subs = new Set<() => void>();
let fold: ReturnType<typeof setTimeout> | undefined;

export function ariSet(phase: AriPhase, text = "", remote = false) {
  clearTimeout(fold);
  text = plain(text);
  state = { phase, text, at: Date.now(), remote };
  subs.forEach((f) => f());
  if (phase === "done") fold = setTimeout(() => ariSet("idle"), Math.min(9000, 3500 + text.length * 40));
}

// End a phase only if it is still the one showing (a later phase wins).
export function ariEnd(phase: AriPhase, then: AriPhase = "idle", text = "") {
  if (state.phase === phase) ariSet(then, text);
}

export const ariNow = () => state;

export function useAri(): AriState {
  return useSyncExternalStore((f) => { subs.add(f); return () => subs.delete(f); }, () => state);
}

// Tell Argus (so the PC's popup and other screens show it too). Fire and forget.
export function ariReport(phase: AriPhase, text = "") {
  api("/ari/state", { method: "POST", body: JSON.stringify({ phase, text: text.slice(0, 300), by: "helios" }) }).catch(() => {});
}

// Local and reported at once.
export function ariTell(phase: AriPhase, text = "") { ariSet(phase, text); ariReport(phase, text); }

type Ev = { kind: string; job_id: string | null; step: string | null; data: Record<string, unknown> | null };
let thinkJob: string | null = null;
const TOOL = /^tool \d+: (.+)$/;
const NICE: Record<string, string> = { search_my_files: "searching your files", find_file: "looking for the file",
  open_app: "opening it", set_volume: "setting the volume", argus_status: "checking Argus",
  look_at_screen: "looking at your screen", summarise_clipboard: "reading what you copied", weather: "checking the weather",
  web_search: "searching the web", read_page: "reading the page", search_everything: "finding the file" };

// Follow Ari from the event stream: ari.state events, and the steps of the answer being worked on.
export function ariFromEvents(evs: Ev[]) {
  for (const e of evs) {
    if (e.kind === "ari.listening") {
      setEars(e.data?.listening !== false, (e.data?.until as number | null) ?? null);
      continue;
    }
    if (e.kind === "ari.state") {
      const phase = String(e.data?.phase ?? "idle") as AriPhase;
      const text = String(e.data?.text ?? "");
      if (phase === "thinking") thinkJob = e.job_id;
      if (phase === "done" && e.job_id && e.job_id === thinkJob) thinkJob = null;
      // a "done" while this screen is itself speaking the answer: keep speaking
      const mine = !state.remote && (state.phase === "speaking" || state.phase === "listening");
      if (phase === "done" && mine && state.phase === "speaking") continue;
      if (phase === "idle") { if (!mine) ariSet("idle"); continue; }
      if ((phase as string) === "following") continue;  // the PC's island shows it; nothing changes here
      ariSet(phase, text, true);
    } else if (e.kind === "step.running" && e.job_id && e.job_id === thinkJob && e.step) {
      const m = TOOL.exec(e.step);
      if (m) ariSet("working", NICE[m[1]] ?? m[1].replace(/_/g, " "), true);
      else if (e.step === "web") ariSet("working", "searching the web", true);
    }
  }
}

// The pill's look: "pulse" (a soft light that breathes with the voice) or "comet" (a soft light that travels
// round the edge). Argus's default (ari.pill), unless this browser picked one.
export type PillLook = "pulse" | "comet";
let serverLook: PillLook = "pulse";
export function setServerLook(l: string | undefined) {
  if (l === "pulse" || l === "comet") { serverLook = l; window.dispatchEvent(new CustomEvent("ari-look")); }
}
export function pillLook(): PillLook {
  try { const v = localStorage.getItem("helios.pill"); if (v === "pulse" || v === "comet") return v; } catch { /* private */ }
  return serverLook;
}
export function setPillLook(l: PillLook) {
  try { localStorage.setItem("helios.pill", l); } catch { /* private */ }
  window.dispatchEvent(new CustomEvent("ari-look"));
}
export function usePillLook(): PillLook {
  return useSyncExternalStore((f) => { window.addEventListener("ari-look", f); return () => window.removeEventListener("ari-look", f); }, pillLook);
}

// Ari's ears: paused with the button (or while you train your voice), on every screen and the PC's microphone.
let hushUntil = 0;  // ms; Infinity = until turned back on
let hushTimer: ReturnType<typeof setTimeout> | undefined;
export const earsPaused = () => hushUntil > Date.now();
export function setEars(listening: boolean, until: number | null) {
  hushUntil = listening ? 0 : until ? until * 1000 : Infinity;
  clearTimeout(hushTimer);
  if (!listening && until) hushTimer = setTimeout(() => setEars(true, null), Math.max(0, until * 1000 - Date.now()) + 50);
  window.dispatchEvent(new CustomEvent("ari-ears"));
  window.dispatchEvent(new CustomEvent("ari-pref"));  // the "Hey Ari" listener in Helios follows it
}
export function useEars(): { paused: boolean; until: number } {
  const get = () => (earsPaused() ? hushUntil : 0);
  const v = useSyncExternalStore((f) => { window.addEventListener("ari-ears", f); return () => window.removeEventListener("ari-ears", f); }, get);
  return { paused: v > 0, until: v };
}
export async function pauseEars(minutes: number | null) {
  const r = await api<{ listening: boolean; until: number | null }>("/ari/listening", { method: "POST", body: JSON.stringify({ on: false, minutes }) });
  setEars(r.listening, r.until);
}
export async function resumeEars() {
  const r = await api<{ listening: boolean; until: number | null }>("/ari/listening", { method: "POST", body: JSON.stringify({ on: true }) });
  setEars(r.listening, r.until);
}
// Ari off / on: no listening anywhere, quiet at once, answers on their way dropped (the island's power key too)
export async function powerAri(on: boolean) {
  const r = await api<{ listening: boolean; until: number | null }>("/ari/power", { method: "POST", body: JSON.stringify({ on }) });
  setEars(r.listening, r.until);
}
export function loadEars() {
  api<{ listening: boolean; until: number | null }>("/ari/listening").then((r) => setEars(r.listening, r.until)).catch(() => {});
}
