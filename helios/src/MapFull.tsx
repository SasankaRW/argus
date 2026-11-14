import { useEffect, useState } from "react";
import type { ArgusEvent, ArgusMap, Status } from "./api";
import { Inspector } from "./Inspector";
import type { Pulse } from "./live";
import { MapView, Selection } from "./MapView";
import { Ms } from "./sonoma";

// The phone's live map: its own full-screen page, the normal left-to-right map turned sideways so it uses the
// phone's long side (turn the phone to read it). Held in landscape, it just fills the screen.
export function MapFull({ map, pulses, active, status, events, onClose, onPlugins, phone = false }: {
  map: ArgusMap; pulses: Pulse[]; active: Record<string, number>; status: Status | null; events: ArgusEvent[];
  onClose: () => void; onPlugins: () => void; phone?: boolean;  // phone: the Sonoma controls (icon buttons)
}) {
  const [sel, setSel] = useState<Selection>(null);
  const [flow, setFlow] = useState(true);
  const [relayout, setRelayout] = useState(0);
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
          <MapView map={map} pulses={pulses} active={active} selection={sel} onSelect={setSel} flow={flow}
            relayoutSignal={relayout} onPlugins={onPlugins} />
        </div>
        {phone ? (<>
          <div className="hud hud-tl sn-mf-title">Live map<span className="ac">.</span></div>
          <div className="hud hud-tr sn-mf-tools">
            <button type="button" className="sn-chip" aria-pressed={flow} onClick={() => setFlow(!flow)}>{flow && <Ms n="check" />}Flow</button>
            <button type="button" className="sn-iconbtn" aria-label="Lay out again" onClick={() => setRelayout((x) => x + 1)}><Ms n="account_tree" /></button>
            <button type="button" className="sn-iconbtn" onClick={onClose} aria-label="Close the map"><Ms n="close" /></button>
          </div>
        </>) : (<>
          <div className="hud hud-tl hud-title mono"><span className="pt">live map</span></div>
          <div className="hud hud-tr tools">
            <button type="button" className="btn" aria-pressed={flow} onClick={() => setFlow(!flow)}>flow</button>
            <button type="button" className="btn" onClick={() => setRelayout((x) => x + 1)}>layout</button>
            <button type="button" className="btn mf-close" onClick={onClose} aria-label="Close the map">×</button>
          </div>
        </>)}
        {sel && (
          <aside className="drawer">
            <Inspector sel={sel} map={map} status={status} events={events} onSelect={setSel} />
          </aside>
        )}
      </div>
    </div>
  );
}
