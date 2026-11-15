// The side panel for a selected box (a drawer on the desktop, the bottom sheet on the phone): its name, a status chip
// and type, the controls it has (docs/controls.md; Stop, Restart and Pause need a hold) and its recent log.

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type MapNode } from "./api";
import { Btn, buttonsFor, chipFor, clockSec, controlPath, firesOn, hasLogs, LogLevel, LogLine, logComponent, logPath, run, targetOf, typeOf } from "./controls";
import { HoldButton, Ms } from "./m3";
import { labelOf } from "./mapGraph";
import { iconOf } from "./mapInfo";

// The newest lines of a component's log, newest last. Refreshed every few seconds while the panel is open, and at once
// when `tick` changes (after a control call).
export function useLogs(component: string | null, level: LogLevel, tick: number): LogLine[] | null {
  const [lines, setLines] = useState<LogLine[] | null>(null);
  useEffect(() => {
    setLines(null);
    if (!component) return;
    let off = false;
    const load = () => api<LogLine[]>(logPath(component, level)).then((r) => { if (!off) setLines(Array.isArray(r) ? r : []); }).catch(() => { if (!off) setLines((o) => o ?? []); });
    load();
    const id = window.setInterval(load, 5000);
    return () => { off = true; window.clearInterval(id); };
  }, [component, level, tick]);
  return lines;
}

export function LogList({ lines }: { lines: LogLine[] | null }) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => { const el = box.current; if (el) el.scrollTop = el.scrollHeight; }, [lines]);
  return (
    <div className="cp-log" ref={box} tabIndex={0} aria-label="Log lines, newest last">
      {lines === null ? <div className="cp-t">Loading…</div>
        : lines.length === 0 ? <div className="cp-t">Nothing logged lately.</div>
        : lines.map((l, i) => (
          <div key={i}><span className="cp-t">{clockSec(l.ts)}</span> <span className={l.level === "error" ? "cp-e" : l.level === "warn" ? "cp-w" : undefined}>{l.msg}</span></div>
        ))}
    </div>
  );
}

// One control: a tap button, or a hold button that fires when the hold completes.
export function Control({ b, onFire, busy }: { b: Btn; onFire: () => void; busy: boolean }) {
  if (firesOn(b) === "hold") {
    return <HoldButton icon={b.icon} label={b.label} caption={b.label} tone={b.action === "restart" ? "accent" : "coral"} disabled={busy} onConfirm={onFire} />;
  }
  return <button type="button" className="cp-btn" disabled={busy} onClick={onFire}><Ms n={b.icon} fill />{b.label}</button>;
}

export function ControlPanel({ node, logsFirst, onClose, onChanged, onFullLog, onDetails }: {
  node: MapNode; logsFirst?: boolean; onClose: () => void; onChanged: () => void; onFullLog: () => void; onDetails?: () => void;
}) {
  const t = targetOf(node);
  const chip = chipFor(node);
  const btns = buttonsFor(t);
  const [level, setLevel] = useState<LogLevel>("all");
  const [tick, setTick] = useState(0);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const logsRef = useRef<HTMLElement>(null);
  const lines = useLogs(hasLogs(t) ? logComponent(t) : null, level, tick);
  useEffect(() => { setMsg(null); setLevel("all"); }, [node.id]);
  useEffect(() => { if (logsFirst) logsRef.current?.scrollIntoView({ block: "nearest" }); }, [logsFirst, node.id]);
  const fire = useCallback(async (b: Btn) => {
    setBusy(true);
    const r = await run(controlPath(t, b.action), b.action);
    setBusy(false);
    setMsg({ ok: r.ok, text: r.ok ? r.note : r.error });
    setTick((n) => n + 1);
    onChanged();
  }, [t, onChanged]);
  return (
    <div className="cp" aria-label={`${labelOf(node)} controls`}>
      <div className="cp-h">
        <span className="cp-i"><Ms n={iconOf(node)} fill /></span>
        <h2 className="cp-n">{labelOf(node)}</h2>
        <button type="button" className="ic-x" aria-label="Close" onClick={onClose}><Ms n="close" /></button>
      </div>
      <div className="cp-chips"><span className={`cp-chip t-${chip.tone}`}>{chip.text}</span><span className="cp-type">{typeOf(node)}</span></div>
      {btns.length > 0 ? (
        <div className="cp-acts">{btns.map((b) => <Control key={b.action} b={b} busy={busy} onFire={() => fire(b)} />)}</div>
      ) : (
        <div className="cp-sub">{t.kind === "core" ? "Argus itself has no controls here. Its log is below." : "This one has no controls of its own."}</div>
      )}
      {msg && <div className={`cp-msg${msg.ok ? "" : " t-bad"}`} role="status">{msg.text}</div>}
      {hasLogs(t) && (
        <section className="cp-logs" ref={logsRef} aria-label="Logs">
          <div className="cp-lh">
            <b>Logs</b>
            <button type="button" className={`cp-tab${level === "all" ? " sel" : ""}`} aria-pressed={level === "all"} onClick={() => setLevel("all")}>All</button>
            <button type="button" className={`cp-tab${level === "error" ? " sel" : ""}`} aria-pressed={level === "error"} onClick={() => setLevel("error")}>Errors</button>
            <button type="button" className="cp-tab cp-full" onClick={onFullLog}>Open full log</button>
          </div>
          <LogList lines={lines} />
        </section>
      )}
      {onDetails && <button type="button" className="sn-link cp-more" onClick={onDetails}>Details</button>}
    </div>
  );
}
