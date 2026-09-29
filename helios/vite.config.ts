import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` serves Helios on :5173 and forwards API calls to argusd on :8600.
const argus = "http://127.0.0.1:8600";

export default defineConfig({
  base: "/helios/",
  plugins: [react()],
  build: {
    outDir: "../core/argus/helios_dist",
    emptyOutDir: true,
    chunkSizeWarningLimit: 2000,
  },
  server: {
    proxy: {
      "/status": argus,
      "/map": argus,
      "/events": argus,
      "/jobs": argus,
      "/workers": argus,
      "/registry": argus,
      "/models": argus,
      "/approvals": argus,
      "/outbox": argus,
      "/ws": { target: argus.replace("http", "ws"), ws: true },
    },
  },
});
