import { useEffect, useState } from "react";
import { api, ArgusEvent, Job } from "./api";
import { ago, tone } from "./format";
import type { Selection } from "./MapView";

type Power = { mode: string; state: string; idle_since: number | null; idle_minutes: number; pc_online: boolean;
  shutdown_at: number | null; woken_by_argus: boolean;
  pc: { id: string; host: string; state: string; last_seen: number | null } | null; wol: boolean; delay: number;
  recent: Job[]; pending: Job[];
  wake_check?: { at: number; to: string[]; result: "waiting" | "online" | "no_answer"; after_s?: number } | null;
  laptop?: { available: boolean; host: string; scheduled: { mode: "reboot" | "poweroff"; at: number } | null } };

const HHMM = (t: number) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

const ASK: Record<string, string> = {
  sleep: "Put the PC to sleep now? Running jobs pause and continue when it wakes.",
  shutdown: "Shut the PC down? It waits {d} s first; you can cancel until then. Running jobs are retried later.",
  restart: "Restart the PC? It waits {d} s first; you can cancel until then.",
};

function usePower(events: ArgusEvent[]) {
  const [p, setP] = useState<Power | null>(null);
  const tick = events.filter((e) => e.kind.startsWith("job.") || e.kind.startsWith("worker.") || e.kind.startsWith("power.")).map((e) => e.seq).pop() ?? 0;
  const load = () => api<Power>("/power").then(setP).catch(() => {});
  useEffect(() => { load(); }, [tick]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { const i = window.setInterval(load, 10000); return () => window.clearInterval(i); }, []);
  return [p, load] as const;
}

// The PC's power: its state and the buttons. Used on the Power page and on the phone.
export function PowerControls({ events, onSelect, compact = false }: { events: ArgusEvent[]; onSelect: (s: Selection) => void; compact?: boolean }) {
  const [p, reload] = usePower(events);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  if (!p) return <div className="tip">Loading…</div>;

  // a shutdown or restart that ran in the last `delay` seconds can still be cancelled
  const countdown = p.recent.find((j) => ["shutdown", "restart"].includes(j.workflow) && j.state === "succeeded"
    && j.finished_at && Date.now() / 1000 - j.finished_at < p.delay);
  const auto = p.state === "warned" && p.shutdown_at ? p.shutdown_at : null;
  const pending = p.pending.length > 0 || !!countdown || !!auto;

  const act = async (a: string) => {
    if (ASK[a] && !window.confirm(ASK[a].replace("{d}", String(p.delay)))) return;
    setBusy(a); setMsg(null);
    try {
      const r = await api<{ id?: string; sent?: boolean }>(`/power/${a}`, { method: "POST" });
      setMsg(a === "wake" ? "Wake signal sent. The PC takes about a minute to come online."
        : a === "cancel" ? "Cancelled." : "Sent to the PC.");
      if (r.id && !compact) onSelect({ type: "job", id: r.id });
      reload();
    } catch (e) {
      setMsg(e instanceof Error ? e.message.replace(/^\/power\/\w+: /, "") : String(e));
    } finally { setBusy(null); }
  };

  const state = p.pc_online ? "On" : p.pc ? "Off or asleep" : "No PC worker yet";
  return (
    <div className="power">
      <div className="pwr-head">
        <span className="dot" style={{ background: p.pc_online ? "var(--ok)" : "var(--tx3)" }} />
        <b>PC · {state}</b>
        <span className="muted">{p.pc ? `${p.pc.host}${p.pc.last_seen ? ` · seen ${ago(p.pc.last_seen)}` : ""}` : ""}</span>
      </div>
      {!compact && (
        <div className="tip">
          Auto power ({p.mode}): {p.state === "busy" ? "PC has work" : p.state === "would_shutdown" ? "would shut it down now (idle)"
            : p.state === "held" ? "kept on until the PC has work again" : p.state === "warned" ? "warned on the phone; shutting down soon"
            : p.state === "shutting_down" ? "shutting down" : p.state === "idle" && p.idle_since ? `idle for ${ago(p.idle_since).replace(/ ago$/, "")}; shuts down after ${p.idle_minutes} min${p.mode === "real" && !p.woken_by_argus ? " (only when Argus woke it)" : ""}` : p.state}
          {p.mode === "simulated" ? " — only logged while developing; these buttons act for real." : ""}
        </div>
      )}
      <div className="pwr-btns">
        <button type="button" className="btn pbtn" disabled={busy !== null || p.pc_online || !p.wol}
          title={!p.wol ? "Set power.pc_mac in argus.yaml" : p.pc_online ? "The PC is on" : ""} onClick={() => act("wake")}>Wake</button>
        <button type="button" className="btn pbtn" disabled={busy !== null || !p.pc_online} onClick={() => act("sleep")}>Sleep</button>
        <button type="button" className="btn pbtn" disabled={busy !== null || !p.pc_online} onClick={() => act("restart")}>Restart</button>
        <button type="button" className="btn pbtn danger" disabled={busy !== null || !p.pc_online} onClick={() => act("shutdown")}>Shut down</button>
      </div>
      {pending && (
        <div className="pwr-pending">
          <span>{auto ? `Idle: shuts down at ${new Date(auto * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}`
            : countdown ? `${countdown.workflow === "restart" ? "Restarting" : "Shutting down"} in about ${Math.max(0, Math.round(p.delay - (Date.now() / 1000 - (countdown.finished_at ?? 0))))} s`
            : `${p.pending[0].workflow.replace("_", " ")} is queued`}</span>
          <button type="button" className="btn primary-btn" disabled={busy !== null} onClick={() => act("cancel")}>Cancel</button>
        </div>
      )}
      {msg && <div className="tip">{msg}</div>}
      {p.wake_check && !p.pc_online && p.wake_check.result !== "online" && (
        <div className="tip">
          {p.wake_check.result === "waiting"
            ? `Wake sent ${ago(p.wake_check.at)} to ${p.wake_check.to.join(", ")}; waiting for the PC (about 2 min).`
            : <>The PC didn't come online after the wake ({ago(p.wake_check.at)}, sent to {p.wake_check.to.join(", ")}).
              On the PC run <code>.\scripts\dev.ps1 wol-check</code> (network card's "Wake on Magic Packet" on, Fast Startup off, a cable);
              in the BIOS: Power On By PCI-E on, ErP off.</>}
        </div>
      )}
      {p.wake_check?.result === "online" && p.wake_check.after_s !== undefined && Date.now() / 1000 - p.wake_check.at < 600 && (
        <div className="tip">Woke up {p.wake_check.after_s} s after the signal.</div>
      )}
      {!p.wol && !compact && <div className="tip">Wake needs the PC's network card address: set <code>power.pc_mac</code> in argus.yaml (from <code>ipconfig /all</code>). It works once Argus runs on the laptop; while Argus runs on the PC itself there is nothing to wake it.</div>}
      {!compact && p.recent.length > 0 && (
        <>
          <div className="sect">Recent</div>
          {p.recent.map((j) => (
            <button key={j.id} type="button" className="run" onClick={() => onSelect({ type: "job", id: j.id })}>
              <span className="t mono">{ago(j.created_at)}</span>
              <span className="w mono">{j.workflow}</span>
              <span className={`pill ${tone("job." + j.state)}`}>{j.state}</span>
              <span className="d">{j.error ?? ""}</span>
            </button>
          ))}
        </>
      )}
    </div>
  );
}

const LAPTOP_ASK: Record<string, string> = {
  "restart-argus": "Restart Argus? Helios, Ari and the phone are away for about 10 seconds.",
  reboot: "Restart the laptop in 1 minute? Argus is away for a minute or two; you can cancel until then.",
  shutdown: "Shut the laptop down in 1 minute? Argus stays off until someone presses the laptop's power button.",
};

// The laptop's own buttons (Argus's server): restart Argus, restart, shut down (asked twice), cancel.
export function LaptopControls({ events }: { events: ArgusEvent[] }) {
  const [p, reload] = usePower(events);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const l = p?.laptop;
  if (!l) return null;
  const act = async (a: string) => {
    if (LAPTOP_ASK[a] && !window.confirm(LAPTOP_ASK[a])) return;
    if (a === "shutdown" && !window.confirm("Really shut it down? With no screen and no battery, only its power button brings it back.")) return;
    setBusy(a); setMsg(null);
    try {
      await api(`/power/laptop/${a}`, { method: "POST", body: JSON.stringify({ confirm: a === "shutdown" }) });
      setMsg(a === "restart-argus" ? "Argus is restarting; this page reconnects by itself."
        : a === "cancel" ? "Cancelled." : `The laptop ${a === "reboot" ? "restarts" : "shuts down"} in 1 minute.`);
      reload();
    } catch (e) {
      setMsg(e instanceof Error ? e.message.replace(/^\/power\/laptop\/[\w-]+: /, "") : String(e));
    } finally { setBusy(null); }
  };
  return (
    <div className="power">
      <div className="pwr-head">
        <span className="dot" style={{ background: "var(--ok)" }} />
        <b>Laptop · On</b>
        <span className="muted">{l.host} · runs Argus</span>
      </div>
      {!l.available && <div className="tip">These work when Argus runs on the Linux laptop.</div>}
      <div className="pwr-btns">
        <button type="button" className="btn pbtn" disabled={busy !== null || !l.available} onClick={() => act("restart-argus")}>Restart Argus</button>
        <button type="button" className="btn pbtn" disabled={busy !== null || !l.available || !!l.scheduled} onClick={() => act("reboot")}>Restart</button>
        <button type="button" className="btn pbtn danger" disabled={busy !== null || !l.available || !!l.scheduled} onClick={() => act("shutdown")}>Shut down</button>
      </div>
      {l.scheduled && (
        <div className="pwr-pending">
          <span>{l.scheduled.mode === "reboot" ? "Restarting" : "Shutting down"} at {HHMM(l.scheduled.at)}</span>
          <button type="button" className="btn primary-btn" disabled={busy !== null} onClick={() => act("cancel")}>Cancel</button>
        </div>
      )}
      {msg && <div className="tip">{msg}</div>}
    </div>
  );
}

export function PowerView({ events, onSelect }: { events: ArgusEvent[]; onSelect: (s: Selection) => void }) {
  return (
    <section className="panel powerpage" aria-label="Power">
      <div className="ph"><span className="pt">Power</span><span className="muted">the PC and the laptop: wake, sleep, restart, shut down</span></div>
      <div className="qbody"><PowerControls events={events} onSelect={onSelect} /></div>
      <div className="qbody"><div className="sect">The laptop</div><LaptopControls events={events} /></div>
      <div className="qbody"><div className="sect">Your phone</div><FindPhone /></div>
    </section>
  );
}

// Where Tailscale sees the phone, and a loud ring to find it.
function FindPhone() {
  const [where, setWhere] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const look = () => api<{ text: string }>("/phone").then((r) => setWhere(r.text)).catch((e) => setWhere(String(e)));
  useEffect(() => { look(); }, []);
  const ring = async () => {
    setBusy(true); setMsg(null);
    try { await api("/phone/ring", { method: "POST" }); setMsg("Ringing: three loud notifications, 20 s apart."); }
    catch (e) { setMsg(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  };
  return (
    <div className="findphone">
      <p>{where ?? "Looking…"}</p>
      <div className="appr-actions">
        <button type="button" className="primary" disabled={busy} onClick={ring}>Ring my phone</button>
        <button type="button" className="btn" onClick={look}>Look again</button>
      </div>
      {msg && <div className="tip">{msg}</div>}
    </div>
  );
}
