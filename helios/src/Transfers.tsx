// Argus Send in Helios (approved 2026-11-17, docs/superpowers/specs/2026-11-17-send-transfers-design.md):
// - the side pop-up: while files move it slides in at the bottom right (above the dock on the phone); the newest
//   transfer large (from-device, the line that fills with beads running ahead, to-device, the name, 62 %, the pace),
//   any others as thin bars; a finished one pops a green "Saved to …" chip with the turning scalloped badge, then fades.
// - the Transfers page: what's moving now, then the history.
import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { HoldButton, Ms } from "./m3";
import { deviceIcon, fraction, isMoving, pace, result, size, type SendHistoryItem, type SendLive, type SendLiveItem } from "./sendLive";

// The line between the two devices: it fills with the progress, beads run ahead while it moves.
export function FlowLine({ t }: { t: SendLiveItem }) {
  return (
    <span className={`tx-line${isMoving(t) ? " moving" : ""}`} aria-hidden="true">
      <span className="tx-fill" style={{ width: `${Math.round(fraction(t) * 100)}%` }} />
      {isMoving(t) && <><i /><i /><i /></>}
    </span>
  );
}

function Ends({ t, name }: { t: SendLiveItem; name: (id: string) => string }) {
  return (
    <div className="tx-ends">
      <span className="tx-dev from" title={name(t.from)}><Ms n={deviceIcon(t.from)} /></span>
      <FlowLine t={t} />
      <span className="tx-dev to" title={name(t.to)}><Ms n={deviceIcon(t.to)} /></span>
    </div>
  );
}

function Badge() {  // the scalloped badge with a tick (Helios's house shape)
  const pts = Array.from({ length: 73 }, (_, j) => {
    const th = (j / 72) * Math.PI * 2, r = 11 * (1 + 0.1 * Math.cos(9 * th));
    return `${(12 + r * Math.cos(th)).toFixed(2)},${(12 + r * Math.sin(th)).toFixed(2)}`;
  }).join(" ");
  return (
    <svg className="tx-badge" viewBox="0 0 24 24" aria-hidden="true">
      <polygon className="spin" points={pts} />
      <path d="M7.3 12.2l3.1 3.1 6.3-6.6" />
    </svg>
  );
}

export function TransferPopup({ live, onOpen }: { live: SendLive; onOpen: () => void }) {
  const moving = live.items.filter(isMoving);
  // finished ones show as a chip for a moment, each once
  const seen = useRef(new Set<string>());
  const [chip, setChip] = useState<SendLiveItem | null>(null);
  useEffect(() => {
    const fresh = live.items.find((t) => !isMoving(t) && !seen.current.has(`${t.offer_id}:${t.leg}`));
    if (!fresh) return;
    seen.current.add(`${fresh.offer_id}:${fresh.leg}`);
    setChip(fresh);
    const id = window.setTimeout(() => setChip((c) => (c === fresh ? null : c)), 2600);
    return () => window.clearTimeout(id);
  }, [live.items]);
  if (!moving.length && !chip) return null;
  const top = moving[0];
  return (
    <div className="tx-layer" role="status" aria-live="polite">
      {chip && (
        <div className={`tx-chip t-${chip.state}`} key={chip.offer_id + chip.leg}>
          {chip.state === "done" ? <Badge /> : <Ms n={chip.state === "cancelled" ? "close" : "info"} />}
          <span>{result(chip, live.name)} · {chip.label}</span>
        </div>
      )}
      {top && (
        <div className="tx-pop">
          <button type="button" className="tx-open" onClick={onOpen} aria-label="Open transfers">
            <div className="tx-hd"><b>To {live.name(top.to)}</b><span>from {live.name(top.from)}</span></div>
            <Ends t={top} name={live.name} />
            <div className="tx-name">{top.label}</div>
            <div className="tx-sub"><span>{Math.round(fraction(top) * 100)}%</span><span>{pace(top)}</span></div>
          </button>
          {moving.slice(1, 4).map((t) => (
            <div className="tx-more" key={t.offer_id + t.leg}>
              <Ms n={deviceIcon(t.to)} /><span>To {live.name(t.to)}: {t.label}</span>
              <span className="tx-mini"><i style={{ width: `${Math.round(fraction(t) * 100)}%` }} /></span>
            </div>
          ))}
          <div className="tx-act"><HoldButton icon="close" label="Cancel" caption="Hold to cancel" onConfirm={() => live.cancel(top.offer_id)} /></div>
        </div>
      )}
    </div>
  );
}

const KIND: Record<string, string> = { sent: "Sent to", received: "Got from", delivered: "Delivered to", held: "Waiting on", failed: "Couldn't send to", cancelled: "Cancelled for", not_delivered: "Didn't reach", keep_waiting: "Still waiting for" };

export function TransfersView({ live }: { live: SendLive }) {
  const [hist, setHist] = useState<SendHistoryItem[] | null>(null);
  const done = live.items.filter((t) => !isMoving(t)).length;
  useEffect(() => { api<SendHistoryItem[]>("/send/history?limit=60").then(setHist).catch(() => setHist([])); }, [done]);
  const moving = live.items.filter(isMoving);
  return (
    <div className="tx-page">
      <h2 className="tx-h">Moving now</h2>
      {moving.length === 0 ? <p className="tx-empty">Nothing is moving. Right-click a file and pick Send, or share from your phone.</p> : moving.map((t) => (
        <div className="tx-row" key={t.offer_id + t.leg}>
          <div className="tx-hd"><b>{t.label}</b><span>{live.name(t.from)} to {live.name(t.to)}{t.leg === "hold" ? " (passed on by Calypso)" : ""}</span></div>
          <Ends t={t} name={live.name} />
          <div className="tx-sub"><span>{Math.round(fraction(t) * 100)}% · {size(t.done)} of {size(t.total)}</span><span>{pace(t)}</span></div>
          <div className="tx-act"><HoldButton icon="close" label="Cancel" caption="Hold to cancel" onConfirm={() => live.cancel(t.offer_id)} /></div>
        </div>
      ))}
      <h2 className="tx-h">Earlier</h2>
      {hist === null ? <p className="tx-empty">Loading…</p> : hist.length === 0 ? <p className="tx-empty">No transfers yet.</p> : (
        <ul className="tx-hist">
          {hist.map((h) => (
            <li key={h.eid}>
              <Ms n={h.kind === "received" ? "inbox" : h.kind === "failed" || h.kind === "not_delivered" ? "info" : "send"} />
              <span className="tx-hl"><b>{h.label ?? "Files"}</b><span>{KIND[h.kind] ?? h.kind} {h.peer ?? "a device"}{h.bytes ? ` · ${size(h.bytes)}` : ""}{h.detail ? ` · ${h.detail}` : ""}</span></span>
              <time>{new Date(h.at * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}</time>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
