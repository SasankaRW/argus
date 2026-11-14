import { useEffect, useRef, useState } from "react";
import { api, ArgusEvent, Status } from "./api";
import { MiniLog, Pane, useHomeData } from "./Phone";
import { IconTile, Logo, Ms } from "./m3";

// The PC's home screen has two layouts, switched in the header and remembered per browser:
//   desk    - the live map as the main focus, panels beside it (DeskLayer.tsx)
//   command - the map fills the screen, small floating panels, one command bar at the bottom

export type Layout = "desk" | "command";

export function loadLayout(): Layout {
  try { return localStorage.getItem("helios.layout") === "command" ? "command" : "desk"; } catch { return "desk"; }
}

export function saveLayout(l: Layout) {
  try { localStorage.setItem("helios.layout", l); } catch { /* private */ }
}

export function LayoutSwitch({ layout, onChange }: { layout: Layout; onChange: (l: Layout) => void }) {
  return (
    <span className="seg-f lay-switch" role="group" aria-label="Layout">
      {(["desk", "command"] as const).map((l) => (
        <button key={l} type="button" className="btn" aria-pressed={layout === l} onClick={() => onChange(l)}
          title={l === "desk" ? "Map with panels beside it" : "Full-screen map with a command bar"}>{l === "desk" ? "Desk" : "Command"}</button>
      ))}
    </span>
  );
}

export const PAGES = ["ari", "plugins", "queue", "runs", "share", "power", "logs"];


// Layout C's floating cards and command bar, drawn over the full-screen map.
export function CommandLayer({ status, events, live, onView, onAsk, onInbox, onLayout, tools }: {
  status: Status | null; events: ArgusEvent[]; live: boolean; onView: (v: string) => void; onAsk: (text: string) => void;
  onInbox: () => void; onLayout: (l: Layout) => void; tools: React.ReactNode;
}) {
  const d = useHomeData(events);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {  // Ctrl+K or "/" jumps to the command bar
    const on = (e: KeyboardEvent) => {
      const typing = ["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName ?? "");
      if ((e.key === "k" && (e.ctrlKey || e.metaKey)) || (e.key === "/" && !typing)) {
        e.preventDefault(); input.current?.focus();
        if (e.key === "/") setQ("/");
      }
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, []);
  const run = (text: string) => {
    const t = text.trim();
    if (!t) return;
    if (t.startsWith("/")) {
      const page = t.slice(1).split(/\s+/)[0].toLowerCase();
      const hit = PAGES.find((p) => p.startsWith(page));
      if (hit) { onView(hit); setQ(""); }
      return;
    }
    onAsk(t);
    setQ("");
  };
  const suggest = q.startsWith("/") ? PAGES.filter((p) => p.startsWith(q.slice(1).toLowerCase())) : PAGES;
  const first = d.pending[0];
  const decide = async (id: string, answer: "approve" | "reject") => {
    setBusy(true);
    try { await api(`/approvals/${id}/decide`, { method: "POST", body: JSON.stringify({ answer, by: "helios" }) }); await d.reload(); }
    finally { setBusy(false); }
  };
  const ok = live && status?.status === "ok";
  return (
    <>
      <header className="cmd-top">
        <span className="cmd-id sn-pane"><Logo size={22} /><span className="who">Argus</span></span>
        <span className="cmd-tools">{tools}</span>
        <LayoutSwitch layout="command" onChange={onLayout} />
        <span className="badge sn-pane"><span className={`sn-dot ${ok ? "d-green" : live ? "d-amber" : "d-coral"}`} />{ok ? "Online" : live ? "Needs a look" : "Offline"}</span>
      </header>

      <div className="cmd-col left">
        {first && (
          <Pane label="Waiting for you" className="sn-waiting" meta={<button type="button" className="sn-link" onClick={onInbox}>{d.pending.length > 1 ? `All ${d.pending.length}` : "Open"}</button>}>
            <div className="sn-row">
              <IconTile n="folder_open" tone="amber" />
              <div className="sn-row-t"><b>{first.title}</b><span>{first.plugin}</span></div>
            </div>
            <div className="sn-acts">
              <button type="button" className="sn-btn primary" disabled={busy} onClick={() => decide(first.id, "approve")}><Ms n="check" />Approve</button>
              <button type="button" className="sn-btn quiet" disabled={busy} onClick={() => decide(first.id, "reject")}>Reject</button>
            </div>
          </Pane>
        )}
        <button type="button" className="sn-pane sn-tap" aria-label={`Today: ${d.today} jobs ran. Open runs`} onClick={() => onView("runs")}>
          <span className="sn-pane-h"><IconTile n="bolt" tone="blue" /><span className="sn-lbl">Today</span></span>
          <span className="sn-big">{d.today}</span>
          <span className="sn-sub">{d.today === 1 ? "job ran" : "jobs ran"}{d.failed ? <> · <span className="c-coral">{d.failed} failed</span></> : null}{d.running.length ? ` · ${d.running.length} running` : ""}</span>
        </button>
      </div>

      <div className="cmd-col right">
        <Pane label="Logs" icon="receipt_long" meta={<button type="button" className="sn-link" onClick={() => onView("logs")}>Open</button>}>
          <MiniLog n={12} empty="No logs yet" />
        </Pane>
      </div>

      <form className="cmd-bar" onSubmit={(e) => { e.preventDefault(); run(q); }}>
        <div className="cmd-chips">
          {suggest.map((p) => <button key={p} type="button" className="chip" onClick={() => onView(p)}>{p}</button>)}
        </div>
        <div className="cmd-in sn-pane">
          <Ms n="search" size={20} />
          <input ref={input} value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask Ari, or type / for a page" aria-label="Ask Ari or open a page" />
          <kbd>Ctrl K</kbd>
          <button type="button" className="sn-iconbtn accent breathe" aria-label="Talk to Ari"
            onClick={() => { try { sessionStorage.setItem("ari.mic", "1"); } catch { /* private */ } onView("ari"); }}><Ms n="mic" fill /></button>
        </div>
      </form>
    </>
  );
}
