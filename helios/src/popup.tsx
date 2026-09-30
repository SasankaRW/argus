// The Ari popup page: only the Ari pill, on a see-through page. The PC's popup window (python -m argus.ari_popup)
// shows it over every app; the window reads the page title to know when to appear:
//   "ari:open" = show, "ari:idle" = hide, "ari:helios" = you clicked it (open Helios on the Ari page).
import "@fontsource/geist-sans/latin-400.css";
import "@fontsource/geist-sans/latin-500.css";
import "@fontsource/geist-mono/latin-400.css";
import "@fontsource/geist-mono/latin-500.css";
import "./styles.css";

import { StrictMode, useEffect } from "react";
import { createRoot } from "react-dom/client";
import { adoptTokenFromUrl, api, EventStream, Status } from "./api";
import { AriPill } from "./AriPill";
import { ariFromEvents, setServerLook, useAri } from "./ariState";

adoptTokenFromUrl();

function Popup() {
  const s = useAri();
  useEffect(() => { document.title = s.phase === "idle" ? "ari:idle" : "ari:open"; }, [s.phase]);
  useEffect(() => {
    let es: EventStream | null = null;
    let stop = false;
    const ping = () => api("/ari/popup", { method: "POST" }).catch(() => {});
    ping();
    api<{ pill?: string }>("/ari-voice").then((v) => setServerLook(v.pill)).catch(() => {});
    const t = setInterval(ping, 30000);
    api<Status>("/status").then((st) => {
      if (stop) return;
      const from = Date.now() / 1000 - 5;  // only what happens from now on
      es = new EventStream(st.events.seq, { onEvents: (evs) => ariFromEvents(evs.filter((e) => e.at > from)), onState: () => {}, onReset: () => {} });
      es.start();
    }).catch(() => {});
    return () => { stop = true; clearInterval(t); es?.stop(); };
  }, []);
  const open = () => { document.title = "ari:helios"; setTimeout(() => { document.title = s.phase === "idle" ? "ari:idle" : "ari:open"; }, 300); };
  return <AriPill onOpen={open} />;
}

createRoot(document.getElementById("root")!).render(<StrictMode><Popup /></StrictMode>);
