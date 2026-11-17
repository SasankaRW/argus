import { FormEvent, useEffect, useState } from "react";
import type { ArgusEvent, ArgusMap, Status } from "./api";
import { ControlPanel } from "./ControlPanel";
import { Layout, LayoutSwitch, PAGES } from "./Desk";
import { useJobs } from "./jobsFeed";
import { Ms } from "./m3";
import { labelOf } from "./mapGraph";
import { MapView } from "./MapView";
import type { Selection } from "./mapInfo";
import type { SendLiveItem } from "./sendLive";

// The phone's Map: its own screen between the greeting and the nav bar (design/material3/boards/Map.dc.html). The map
// fills the screen; a Live chip and the Desk/Command switch float over it, and a tapped box opens a bottom sheet with
// the same side panel as the desktop (status, controls, logs).

// Held sideways the map is the desktop's, drawn for a phone. Asking for landscape goes through the Android app
// (ArgusNative.landscape) because a WebView ignores screen.orientation.lock; a browser gets the standard call.
function useLandscape(): boolean {
  const q = "(orientation: landscape) and (max-height: 520px)";
  const [m, setM] = useState(() => window.matchMedia(q).matches);
  useEffect(() => {
    const mm = window.matchMedia(q);
    const on = () => setM(mm.matches);
    mm.addEventListener("change", on);
    return () => mm.removeEventListener("change", on);
  }, []);
  return m;
}

// "9:41", or "Nov 16, 9:41" when it isn't today
function seenAt(at: number | null): string {
  if (!at) return "earlier";
  const d = new Date(at * 1000), now = new Date();
  const hm = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return d.toDateString() === now.toDateString() ? hm : `${d.toLocaleDateString([], { month: "short", day: "numeric" })}, ${hm}`;
}

export function PhoneMap({ map, status, events, layout, onLayout, onPlugins, onDetails, onAsk, onView, onChanged, onClose, offline, onRetry, transfers, sendName, onTransfers }: {
  map: ArgusMap; status: Status | null; events: ArgusEvent[];
  layout: Layout; onLayout: (l: Layout) => void; onPlugins: () => void; onDetails: (s: Selection) => void;
  onAsk: (text: string) => void; onView: (v: string) => void; onChanged: () => void; onClose: () => void;
  offline?: { at: number | null } | null; onRetry?: () => void;
  transfers?: SendLiveItem[]; sendName?: (id: string) => string; onTransfers?: () => void;
}) {
  const land = useLandscape();
  useEffect(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const native = (window as any).ArgusNative as { landscape?: (on: boolean) => void } | undefined;
    try { native?.landscape?.(true); } catch { /* old app */ }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    try { (screen.orientation as any)?.lock?.("landscape")?.catch?.(() => {}); } catch { /* not supported */ }
    return () => {
      try { native?.landscape?.(false); } catch { /* old app */ }
      try { screen.orientation?.unlock?.(); } catch { /* not supported */ }
    };
  }, []);
  const [sel, setSel] = useState<Selection>(null);
  const [q, setQ] = useState("");
  const jobs = useJobs(events);
  const j = status?.jobs ?? {};
  const queue = { running: (j.running ?? 0) + (j.leased ?? 0), queued: (j.queued ?? 0) + (j.retry ?? 0) };
  const byWorker: Record<string, number> = {};
  for (const x of jobs) if ((x.state === "running" || x.state === "leased") && x.lease_owner) byWorker[x.lease_owner] = (byWorker[x.lease_owner] ?? 0) + 1;
  const node = sel?.type === "node" ? map.nodes.find((n) => n.id === sel.id) : undefined;
  const pick = (s: Selection) => { if (s?.type === "edge" || s?.type === "job") onDetails(s); else setSel(s); };
  const run = (e: FormEvent) => {
    e.preventDefault();
    const t = q.trim();
    if (!t) return;
    if (t.startsWith("/")) { const hit = PAGES.find((p) => p.startsWith(t.slice(1).split(/\s+/)[0].toLowerCase())); if (hit) { onView(hit); setQ(""); } return; }
    onAsk(t); setQ("");
  };
  return (
    <div className="phonemap" role="region" aria-label="Live map">
      <div className="pm-canvas">
        <MapView map={map} selection={sel} onSelect={pick} flow={!offline} stale={!!offline} transfers={transfers} sendName={sendName} onTransfers={onTransfers} onPlugins={onPlugins} onReview={() => onView("inbox")} onChanged={onChanged}
          events={events} queue={queue} byWorker={byWorker} vertical={!land} compact={land}
          fitPad={land ? { top: "60px", right: "12px", bottom: "8px", left: "12px" } : { top: "120px", right: "70px", bottom: sel ? "250px" : "110px", left: "20px" }} />
      </div>
      <div className="pm-top">
        {offline ? (
          <div className="pm-off" role="status">
            <span className="pm-off-dot" aria-hidden="true" />
            <span className="pm-off-t"><b>Argus is offline</b><span>Showing the map from {seenAt(offline.at)}</span></span>
            <button type="button" className="pm-off-retry" onClick={() => onRetry?.()}>Retry</button>
          </div>
        ) : (<>
          {!land && <div className="float pm-live"><span className="sn-dot d-green live" /><b>Live</b></div>}
          <span className="grow" />
        </>)}
        {land
          ? <button type="button" className="float pm-close" aria-label="Close the map" onClick={onClose}><Ms n="close" /></button>
          : !offline && <LayoutSwitch layout={layout} onChange={onLayout} />}
      </div>
      {layout === "command" && !land && !node && !offline && (
        <form className="float pm-cmd" onSubmit={run}>
          <Ms n="search" />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask Ari, or type / for a page" aria-label="Ask Ari or open a page" enterKeyHint="send" />
          <button type="button" className="sn-fab" aria-label="Talk to Ari" onClick={() => { try { sessionStorage.setItem("ari.mic", "1"); } catch { /* private */ } onView("ari"); }}><Ms n="mic" fill /></button>
        </form>
      )}
      {node && (
        <section className={`float pm-sheet${land ? " side" : ""}${offline ? " offline" : ""}`} aria-label={`Selected: ${labelOf(node)}`}>
          <div className="pm-grip" aria-hidden="true" />
          {offline && <p className="pm-off-note">Last known details. Controls are off until Argus is back.</p>}
          <ControlPanel node={node} logsFirst={sel?.type === "node" && sel.logs} onClose={() => setSel(null)} onChanged={onChanged} onFullLog={() => onView("logs")} onDetails={() => onDetails(sel)} />
        </section>
      )}
    </div>
  );
}
