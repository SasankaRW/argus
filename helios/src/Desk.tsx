import { useEffect, useRef, useState } from "react";
import { api, ArgusEvent, Status } from "./api";
import { MiniLog, Tile, useHomeData } from "./Phone";

// The PC's home screen has two layouts, switched in the header and remembered per browser:
//   desk    - the live map as the main focus, terminal tiles in a column beside it (Phone.tsx's tiles)
//   command - the map fills the screen, small floating cards, one command bar at the bottom

export type Layout = "desk" | "command";

export function loadLayout(): Layout {
  try { return localStorage.getItem("helios.layout") === "command" ? "command" : "desk"; } catch { return "desk"; }
}

export function saveLayout(l: Layout) {
  try { localStorage.setItem("helios.layout", l); } catch { /* private */ }
}

export function LayoutSwitch({ layout, onChange }: { layout: Layout; onChange: (l: Layout) => void }) {
  return (
    <span className="lay-switch mono" role="group" aria-label="Layout">
      {(["desk", "command"] as const).map((l) => (
        <button key={l} type="button" aria-pressed={layout === l} onClick={() => onChange(l)}
          title={l === "desk" ? "Map with tiles beside it" : "Full-screen map with a command bar"}>{l}</button>
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
        <span className="who mono"><span className="tg">sas@argus</span><span className="dim">:~$</span> map</span>
        <span className="cmd-tools">{tools}</span>
        <LayoutSwitch layout="command" onChange={onLayout} />
        <span className={`badge mono ${ok ? "ok" : "warn"}`}><i />{ok ? "ONLINE" : live ? "DEGRADED" : "OFFLINE"}</span>
      </header>

      <div className="cmd-col left">
        {first && (
          <Tile path={`! awaiting input${d.pending.length > 1 ? ` · ${d.pending.length}` : ""}`} dot="var(--amber)" alert>
            <div className="await-t"><b>{first.title}</b><span>{first.plugin}</span></div>
            <div className="cmd-yn">
              <button type="button" className="k-yes" disabled={busy} onClick={() => decide(first.id, "approve")}>[Y] ok</button>
              <button type="button" className="k-no" disabled={busy} onClick={() => decide(first.id, "reject")}>[n]</button>
              {d.pending.length > 1 && <button type="button" className="tt-link" onClick={onInbox}>all ›</button>}
            </div>
          </Tile>
        )}
        <Tile path="~/today" dot="var(--tg)" onOpen={() => onView("runs")}>
          <div className="big ok">{d.today}</div>
          <div className="sub">jobs · {d.ok} ok{d.failed ? ` · ${d.failed} failed` : ""}{d.running.length ? ` · ${d.running.length} run` : ""}</div>
        </Tile>
      </div>

      <div className="cmd-col right">
        <Tile path="~/logs" dot="var(--flow)" onOpen={() => onView("logs")}>
          <MiniLog />
        </Tile>
      </div>

      <form className="cmd-bar" onSubmit={(e) => { e.preventDefault(); run(q); }}>
        <div className="cmd-chips">
          {suggest.map((p) => <button key={p} type="button" className="chip" onClick={() => onView(p)}>/{p}</button>)}
        </div>
        <div className="cmd-in">
          <span className="caret">›</span>
          <input ref={input} value={q} onChange={(e) => setQ(e.target.value)} placeholder="ask ari, or / for a page…" aria-label="Ask Ari or open a page" />
          <kbd>ctrl k</kbd>
          <button type="button" className="prompt-mic" aria-label="Talk to Ari"
            onClick={() => { try { sessionStorage.setItem("ari.mic", "1"); } catch { /* private */ } onView("ari"); }}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v3" /></svg>
          </button>
        </div>
      </form>
    </>
  );
}
