import { ReactNode, useState } from "react";
import { api, ArgusEvent, Status } from "./api";
import { Clock, useClaude } from "./chips";
import { avgRun, lastHour } from "./jobsFeed";
import { headline, useHomeData } from "./Phone";
import { Ms } from "./m3";
import type { IconName } from "./icons";

// The desktop's home layout ("Desk"), from design/material3/boards/Desktop.dc.html: the live map fills the window and
// everything floats over it: a status bar with number chips, the Desk/Command switch, search, the clock, the waiting card
// at the top left and a tool column. The inspector, zoom and timeline are placed by App and MapView. The numbers are
// the ones Argus has: jobs started in the last hour and the average run today (from the jobs list), Claude's calls.

export function KpiChip({ icon, children }: { icon: IconName; children: ReactNode }) {
  return <span className="kpi"><Ms n={icon} size={18} />{children}</span>;
}

export function DeskLayer({ status, events, live, down, onInbox, layoutSwitch, search, tools }: {
  status: Status | null; events: ArgusEvent[]; live: boolean; down: boolean; onInbox: () => void;
  layoutSwitch: ReactNode; search: ReactNode; tools: ReactNode;
}) {
  const d = useHomeData(events);
  const claude = useClaude();
  const [busy, setBusy] = useState<string | null>(null);
  const [later, setLater] = useState<string | null>(null);  // "Not now": hides this card until another approval is first
  const hour = lastHour(d.jobs);
  const avg = avgRun(d.jobs);
  const ok = live && !down && status?.status === "ok";
  const pending = d.pending.filter((a) => a.id !== later);
  const first = pending[0];
  const decide = async (id: string, answer: "approve" | "reject") => {
    setBusy(id);
    try { await api(`/approvals/${id}/decide`, { method: "POST", body: JSON.stringify({ answer, by: "helios" }) }); await d.reload(); }
    finally { setBusy(null); }
  };
  return (
    <>
      <div className="dl-top">
        <div className="float dl-status">
          <span className={`sn-dot ${ok ? "d-green live" : down || !live ? "d-coral" : "d-amber"}`} />
          <span className="dl-head">{headline(status, live, down)}</span>
          <KpiChip icon="bolt"><b className="num">{hour.count}</b> jobs / hr</KpiChip>
          {avg !== null && <KpiChip icon="timer"><b className="num">{avg < 10 ? avg.toFixed(1) : Math.round(avg)} s</b> avg run</KpiChip>}
          {claude && claude.logged_in !== false && <KpiChip icon="token">Claude <b className="num">{claude.calls_today} / {claude.calls_per_day}</b></KpiChip>}
        </div>
        <span className="grow" />
        {layoutSwitch}
        {search}
        <Clock />
      </div>

      <div className="dl-left">
      {first && (
        <section className="float dl-wait" aria-label="Waiting for you">
          <div className="dl-wait-h">
            <span className="dl-wait-tile"><Ms n={/delete|remove|clean/i.test(first.title) ? "folder_delete" : "pending_actions"} fill /></span>
            <div className="grow"><div className="dl-wait-l">Needs you{pending.length > 1 ? ` · 1 of ${pending.length}` : ""}</div><div className="dl-wait-t">{first.title}</div></div>
          </div>
          <div className="dl-wait-a">
            <button type="button" className="dl-yes" disabled={busy === first.id} onClick={() => decide(first.id, "approve")}>Approve</button>
            <button type="button" className="dl-later" onClick={() => setLater(first.id)}>Not now</button>
            <button type="button" className="dl-later" onClick={onInbox}>{pending.length > 1 ? `All ${pending.length}` : "Details"}</button>
          </div>
        </section>
      )}

      <div className="float dl-tools" role="toolbar" aria-label="Map tools">{tools}</div>
      </div>
    </>
  );
}
