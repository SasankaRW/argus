import "@fontsource/geist-sans/latin-400.css";
import "@fontsource/geist-sans/latin-500.css";
import "@fontsource/geist-sans/latin-600.css";
import "@fontsource/geist-mono/latin-400.css";
import "@fontsource/geist-mono/latin-500.css";
import "@xyflow/react/dist/style.css";
import "./styles.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { adoptTokenFromUrl } from "./api";
import { App } from "./App";

adoptTokenFromUrl();
// Installable on the phone (Add to Home screen) and listed in its share menu; see public/sw.js.
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/helios/sw.js", { scope: "/helios/" }).catch(() => {});
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
