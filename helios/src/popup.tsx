// The Ari popup page: Ari's island (Island.tsx) on a see-through page. The PC's popup window
// (python -m argus.ari_popup) keeps it at the top edge of the screen over every app and reads the page title:
//   "ari:<mode>|<w>x<h>" = where the island is (the window's click area), "ari:go|<hash>" = open Helios there.
import "@fontsource/geist-sans/latin-400.css";
import "@fontsource/geist-sans/latin-500.css";
import "@fontsource/geist-mono/latin-400.css";
import "@fontsource/geist-mono/latin-500.css";
import "./styles.css";

import { StrictMode, useEffect } from "react";
import { createRoot } from "react-dom/client";
import { adoptTokenFromUrl, api, EventStream, Status } from "./api";
import { ariFromEvents, setServerLook } from "./ariState";
import { Island } from "./Island";

adoptTokenFromUrl();

function Popup() {
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
  return <Island />;
}

createRoot(document.getElementById("root")!).render(<StrictMode><Popup /></StrictMode>);
