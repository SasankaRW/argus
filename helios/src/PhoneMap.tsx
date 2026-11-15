import { FormEvent, useState } from "react";
import type { ArgusEvent, ArgusMap, Status } from "./api";
import { ControlPanel } from "./ControlPanel";
import { Layout, LayoutSwitch, PAGES } from "./Desk";
import { useJobs } from "./jobsFeed";
import { Ms } from "./m3";
import { labelOf } from "./mapGraph";
import { MapView } from "./MapView";
import type { Selection } from "./mapInfo";

// The phone's Map: its own screen between the greeting and the nav bar (design/material3/boards/Map.dc.html). The map
// fills the screen; a Live chip and the Desk/Command switch float over it, and a tapped box opens a bottom sheet with
// the same side panel as the desktop (status, controls, logs).

export function PhoneMap({ map, status, events, layout, onLayout, onPlugins, onDetails, onAsk, onView, onChanged }: {
  map: ArgusMap; status: Status | null; events: ArgusEvent[];
  layout: Layout; onLayout: (l: Layout) => void; onPlugins: () => void; onDetails: (s: Selection) => void;
  onAsk: (text: string) => void; onView: (v: string) => void; onChanged: () => void;
}) {
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
        <MapView map={map} selection={sel} onSelect={pick} flow onPlugins={onPlugins} onReview={() => onView("inbox")} onChanged={onChanged}
          events={events} queue={queue} byWorker={byWorker} vertical fitPad={{ top: "120px", right: "70px", bottom: sel ? "250px" : "110px", left: "20px" }} />
      </div>
      <div className="pm-top">
        <div className="float pm-live"><span className="sn-dot d-green live" /><b>Live</b></div>
        <span className="grow" />
        <LayoutSwitch layout={layout} onChange={onLayout} />
      </div>
      {layout === "command" && !node && (
        <form className="float pm-cmd" onSubmit={run}>
          <Ms n="search" />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask Ari, or type / for a page" aria-label="Ask Ari or open a page" enterKeyHint="send" />
          <button type="button" className="sn-fab" aria-label="Talk to Ari" onClick={() => { try { sessionStorage.setItem("ari.mic", "1"); } catch { /* private */ } onView("ari"); }}><Ms n="mic" fill /></button>
        </form>
      )}
      {node && (
        <section className="float pm-sheet" aria-label={`Selected: ${labelOf(node)}`}>
          <div className="pm-grip" aria-hidden="true" />
          <ControlPanel node={node} logsFirst={sel?.type === "node" && sel.logs} onClose={() => setSel(null)} onChanged={onChanged} onFullLog={() => onView("logs")} onDetails={() => onDetails(sel)} />
        </section>
      )}
    </div>
  );
}
