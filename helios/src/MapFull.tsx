import { useEffect, useState } from "react";
import type { ArgusEvent, ArgusMap, Status } from "./api";
import { Inspector } from "./Inspector";
import { MapView } from "./MapView";
import type { Selection } from "./mapInfo";
import { Ms } from "./m3";

// The map as a full-screen overlay, for a narrow window showing the full dashboard (the phone has its own Map screen,
// PhoneMap.tsx). Held in landscape it fills the screen.
export function MapFull({ map, status, events, onClose, onPlugins }: {
  map: ArgusMap; status: Status | null; events: ArgusEvent[];
  onClose: () => void; onPlugins: () => void;
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
  return (
    <div className="mapfull" role="dialog" aria-label="Live map">
      <div className="mapfull-in">
        <div className="canvas">
          <MapView map={map} events={events} selection={sel} onSelect={setSel} flow={flow}
            onPlugins={onPlugins} />
        </div>
        <div className="hud hud-tl hud-title"><span className="pt">Live map</span></div>
        <div className="hud hud-tr tools">
          <button type="button" className="btn" aria-pressed={flow} onClick={() => setFlow(!flow)}>Flow</button>
          <button type="button" className="btn mf-close" onClick={onClose} aria-label="Close the map"><Ms n="close" /></button>
        </div>
        {sel && (
          <aside className="drawer">
            <Inspector sel={sel} map={map} status={status} events={events} onSelect={setSel} />
          </aside>
        )}
      </div>
    </div>
  );
}
