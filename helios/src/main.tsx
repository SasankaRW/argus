import "@fontsource/geist-sans/latin-400.css";
import "@fontsource/geist-sans/latin-500.css";
import "@fontsource/geist-sans/latin-600.css";
import "@fontsource/geist-mono/latin-400.css";
import "@fontsource/geist-mono/latin-500.css";
import "@xyflow/react/dist/style.css";
import "./styles.css";
import "./kit.css";
// The phone view (Sonoma): its styles (all under .phone2), with Inter 4.1 (@font-face in sonoma-phone.css). Browsers
// fetch a font only when a page uses it, so the desktop never downloads Inter.
import "./sonoma-phone.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { adoptTokenFromUrl } from "./api";
import { App } from "./App";
import { currentConv } from "./conv";

adoptTokenFromUrl();
// Opened from the phone app's shortcuts: /helios/?app=1&mic=1#ari = Talk to Ari (the mic starts in the current chat).
{
  const q = new URLSearchParams(location.search);
  if (q.has("app") || q.has("mic")) {
    // Once, in the phone app: forget a "full dashboard" choice saved before the map's Plugins link stopped making it
    // (that link left the app stuck in the desktop layout). A real choice is one tap away again under More.
    try {
      if (!localStorage.getItem("helios.full.reset")) { localStorage.removeItem("helios.full"); localStorage.setItem("helios.full.reset", "1"); }
    } catch { /* private */ }
    let hash = location.hash;
    if (q.get("mic") === "1") {
      try { sessionStorage.setItem("ari.mic", "1"); } catch { /* private */ }
      hash = `#ari/${currentConv()}`;
    }
    history.replaceState(null, "", location.pathname + hash);
  }
}
// The phone can install Helios as an app ("Install app" under More); Chrome offers it through this event.
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  (window as unknown as { heliosInstall?: Event }).heliosInstall = e;
  window.dispatchEvent(new CustomEvent("helios-installable"));
});
// Installable on the phone (Add to Home screen) and listed in its share menu; see public/sw.js.
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/helios/sw.js", { scope: "/helios/" }).catch(() => {});
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
