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
    rollupOptions: { input: { main: "index.html" } },
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
      "/schedules": argus,
      "/plugins": argus,
      "/power": argus,
      "/ari": argus,
      "/ari-voice": argus,
      "/ari-chats": argus,
      "/ari-memory": argus,
      "/ask": argus,
      "/argus-settings": argus,
      "/guidance": argus,
      "/inbox": argus,
      "/island": argus,
      "/lessons": argus,
      "/logs": argus,
      "/phone": argus,
      "/problems": argus,
      "/queue": argus,
      "/samples": argus,
      "/share": argus,
      "/shares": argus,
      "/time-saved": argus,
      "/tools": argus,
      "/voice-train": argus,
      "/ws": { target: argus.replace("http", "ws"), ws: true },
    },
  },
});
