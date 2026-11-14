import { FormEvent, useState } from "react";
import type { ArgusEvent, ArgusMap, Job, MapNode, Status } from "./api";
import { useClaude } from "./chips";
import { Layout, LayoutSwitch, PAGES } from "./Desk";
import { startOfDay, useJobs } from "./jobsFeed";
import type { Pulse } from "./live";
import { Ms } from "./m3";
import { boxInfo, Ctx, mainParts, MapView, mergeEdges, PLUGINS, Selection, sub } from "./MapView";

// The phone's Map: its own screen between the greeting and the nav bar (design/material3/boards/Map.dc.html). The map
// fills the screen; a Live chip, the Desk/Command switch and a zoom column float over it, and a tapped box opens a
// bottom sheet with three numbers and a way into the full details.

type Tile = [string, string];

// Three numbers for the sheet, from the box's own counts, the jobs list and Claude's usage. Nothing invented.
function tiles(n: MapNode, ctx: Ctx, jobs: Job[], claude: { calls_today: number; calls_per_day: number } | null): Tile[] {
  const today = jobs.filter((j) => j.created_at >= startOfDay());
  const done = today.filter((j) => j.state === "succeeded").length, bad = today.filter((j) => j.state === "dead").length;
  const success: Tile = ["Success", done + bad ? `${Math.round((done / (done + bad)) * 100)}%` : "-"];
  if (n.kind === "core") return [["Running", String(ctx.queue?.running ?? 0)], ["Waiting", String(ctx.queue?.queued ?? 0)], success];
  if (n.kind === "plugin") { const j = n.jobs ?? { active: 0, queued: 0, waiting: 0 }; return [["Running", String(j.active)], ["Waiting", String(j.queued + (j.waiting ?? 0))], success]; }
  if (n.kind === "worker") return [["Running", String(ctx.byWorker[n.id] ?? 0)], ["State", n.state ?? "unknown"], ["Jobs today", String(today.filter((j) => j.lease_owner === n.id).length)]];
  if (n.kind === "model") {
    const calls = (n as MapNode & { calls?: number }).calls ?? 0;
    return [["Calls", String(calls)], ["State", n.state === "open" ? "paused" : n.state === "half_open" ? "retrying" : "ready"],
      ["Claude", claude ? `${claude.calls_today} / ${claude.calls_per_day}` : "-"]];
  }
  const r = boxInfo(n, ctx).row;
  return [r ? [r[0], r[1]] : ["State", sub(n)]];
}

export function PhoneMap({ map, pulses, active, status, events, layout, onLayout, onPlugins, onDetails, onAsk, onView }: {
  map: ArgusMap; pulses: Pulse[]; active: Record<string, number>; status: Status | null; events: ArgusEvent[];
  layout: Layout; onLayout: (l: Layout) => void; onPlugins: () => void; onDetails: (s: Selection) => void;
  onAsk: (text: string) => void; onView: (v: string) => void;
}) {
  const [sel, setSel] = useState<Selection>(null);
  const [q, setQ] = useState("");
  const jobs = useJobs(events);
  const claude = useClaude();
  const main = mainParts(map, [], {}).map;
  const j = status?.jobs ?? {};
  const queue = { running: (j.running ?? 0) + (j.leased ?? 0), queued: (j.queued ?? 0) + (j.retry ?? 0) };
  const byWorker: Record<string, number> = {};
  for (const x of jobs) if ((x.state === "running" || x.state === "leased") && x.lease_owner) byWorker[x.lease_owner] = (byWorker[x.lease_owner] ?? 0) + 1;
  const ctx: Ctx = { queue, byWorker };
  const node = sel?.type === "node" ? (main.nodes.find((n) => n.id === sel.id) ?? map.nodes.find((n) => n.id === sel.id)) : undefined;
  const pick = (s: Selection) => { if (s?.type === "edge" || s?.type === "job") onDetails(s); else setSel(s); };
  const run = (e: FormEvent) => {
    e.preventDefault();
    const t = q.trim();
    if (!t) return;
    if (t.startsWith("/")) { const hit = PAGES.find((p) => p.startsWith(t.slice(1).split(/\s+/)[0].toLowerCase())); if (hit) { onView(hit); setQ(""); } return; }
    onAsk(t); setQ("");
  };
  return (
    <div className="phonemap" role="region" aria-label="Live map">
      <div className="pm-canvas">
        <MapView map={map} pulses={pulses} active={active} selection={sel} onSelect={pick} flow relayoutSignal={0} onPlugins={onPlugins}
          events={events} queue={queue} byWorker={byWorker} vertical fitPad={{ top: "120px", right: "70px", bottom: sel ? "250px" : "110px", left: "20px" }} />
      </div>
      <div className="pm-top">
        <div className="float pm-live"><span className="sn-dot d-green live" /><b>Live</b><span className="ns">{main.nodes.length} parts · {mergeEdges(main.edges).length} lines</span></div>
        <span className="grow" />
        <LayoutSwitch layout={layout} onChange={onLayout} />
      </div>
      {layout === "command" && !node && (
        <form className="float pm-cmd" onSubmit={run}>
          <Ms n="search" />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask Ari, or type / for a page" aria-label="Ask Ari or open a page" enterKeyHint="send" />
          <button type="button" className="sn-fab" aria-label="Talk to Ari" onClick={() => { try { sessionStorage.setItem("ari.mic", "1"); } catch { /* private */ } onView("ari"); }}><Ms n="mic" fill /></button>
        </form>
      )}
      {node && (
        <section className="float pm-sheet" aria-label={`Selected: ${node.label}`}>
          <div className="pm-grip" aria-hidden="true" />
          <div className="pm-sh">
            <span className={`ni t-${boxInfo(node, ctx).tone}`}><Ms n={boxInfo(node, ctx).icon} fill /></span>
            <div className="grow"><div className="pm-t">{node.label}</div><div className="ns">{sub(node)}</div></div>
            <button type="button" className="ic-x" aria-label="Close details" onClick={() => setSel(null)}><Ms n="close" /></button>
          </div>
          <div className="pm-tiles">
            {tiles(node, ctx, jobs, claude).map(([k, v]) => <div key={k} className="pm-tile"><div className="ns">{k}</div><div className="num pm-v">{v}</div></div>)}
          </div>
          <button type="button" className="sn-link pm-more" onClick={() => onDetails(sel)}>Details</button>
        </section>
      )}
    </div>
  );
}
