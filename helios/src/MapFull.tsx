import { useEffect, useState } from "react";
import type { ArgusEvent, ArgusMap, Status } from "./api";
import { ControlPanel } from "./ControlPanel";
import { Inspector } from "./Inspector";
import { MapView } from "./MapView";
import type { Selection } from "./mapInfo";
import { Ms } from "./m3";

// The map as a full-screen overlay, for a narrow window showing the full dashboard (the phone has its own Map screen,
// PhoneMap.tsx). Held in landscape it fills the screen.
export function MapFull({ map, status, events, onClose, onPlugins, onReview, onFullLog, onChanged }: {
  map: ArgusMap; status: Status | null; events: ArgusEvent[];
  onClose: () => void; onPlugins: () => void; onReview: () => void; onFullLog: () => void; onChanged: () => void;
}) {
  const [sel, setSel] = useState<Selection>(null);
  const [flow, setFlow] = useState(true);
  useEffect(() => {  // hide the browser bars and hold landscape where the browser allows it (Android Chrome)
    const el = document.documentElement;
    el.requestFullscreen?.().then(() => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (screen.orientation as any)?.lock?.("landscape").catch(() => {});
    }).catch(() => {});
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", esc);
    return () => {
      window.removeEventListener("keydown", esc);
      try { screen.orientation?.unlock?.(); } catch { /* not supported */ }
      if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    };
  }, [onClose]);
  const node = sel?.type === "node" ? map.nodes.find((n) => n.id === sel.id) : undefined;
  return (
    <div className="mapfull" role="dialog" aria-label="Live map">
      <div className="mapfull-in">
        <div className="canvas">
          <MapView map={map} events={events} selection={sel} onSelect={setSel} flow={flow}
            onPlugins={onPlugins} onReview={onReview} onChanged={onChanged}
            fitPad={{ top: "140px", right: sel ? "400px" : "20px", bottom: "20px", left: "20px" }} />
        </div>
        <div className="hud hud-tl hud-title"><span className="pt">Live map</span></div>
        <div className="hud hud-tr tools">
          <button type="button" className="btn" aria-pressed={flow} onClick={() => setFlow(!flow)}>Flow</button>
          <button type="button" className="btn mf-close" onClick={onClose} aria-label="Close the map"><Ms n="close" /></button>
        </div>
        {sel && (
          <aside className="drawer">
            {node && sel?.type === "node"
              ? <ControlPanel node={node} logsFirst={sel.logs} onClose={() => setSel(null)} onChanged={onChanged} onFullLog={onFullLog} />
              : <Inspector sel={sel} map={map} status={status} events={events} onSelect={setSel} />}
          </aside>
        )}
      </div>
    </div>
  );
}
