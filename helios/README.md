# Helios

The Argus dashboard: React + Vite + React Flow + ELK. `argusd` serves the built copy at `/helios`
(and `/` redirects there), so using Helios needs no Node.js.

- `src/api.ts`: REST calls and the event stream (reconnects with `since=<seq>`, never loses an event)
- `src/live.ts`: `useArgus()`: map snapshot + live events, pulses, job counts
- `src/layout.ts`: ELK layout (left to right), pinned positions kept in the browser
- `src/MapView.tsx`: the live map (boxes, lines, a dot per message)
- `src/Inspector.tsx`: details for a box, a line or a job
- `src/EventsPanel.tsx`: the live event list with filters

Change it:

```powershell
cd helios
npm install
npm run dev        # http://localhost:5173/helios/ with live reload, talking to argusd on :8600
npm run build      # writes core/argus/helios_dist (commit that folder)
```

or `.\scripts\dev.ps1 helios` from the repo root to rebuild.

Token: if `ARGUS_WORKER_TOKEN` is set, Helios asks for it once and keeps it in the browser. A proper
password login arrives in C11.
