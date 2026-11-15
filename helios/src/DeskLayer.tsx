import type { ReactNode } from "react";
import type { ArgusEvent, Status } from "./api";
import { Clock, useClaude } from "./chips";
import { avgRun, lastHour } from "./jobsFeed";
import { headline, useHomeData } from "./Phone";
import { Ms } from "./m3";
import type { IconName } from "./icons";

// The desktop's home layout ("Desk"), from design/material3/boards/Desktop.dc.html: the live map fills the window and
// everything floats over it: a status bar with number chips, the Desk/Command switch, search, the clock
// and a tool column at the left. What needs you (approvals included) is the map's own attention row. The inspector, zoom and timeline are placed by App and MapView. The numbers are
// the ones Argus has: jobs started in the last hour and the average run today (from the jobs list), Claude's calls.

export function KpiChip({ icon, children }: { icon: IconName; children: ReactNode }) {
  return <span className="kpi"><Ms n={icon} size={18} />{children}</span>;
}

export function DeskLayer({ status, events, live, down, layoutSwitch, search, tools }: {
  status: Status | null; events: ArgusEvent[]; live: boolean; down: boolean;
  layoutSwitch: ReactNode; search: ReactNode; tools: ReactNode;
}) {
  const d = useHomeData(events);
  const claude = useClaude();
  const hour = lastHour(d.jobs);
  const avg = avgRun(d.jobs);
  const ok = live && !down && status?.status === "ok";
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
      <div className="float dl-tools" role="toolbar" aria-label="Map tools">{tools}</div>
      </div>
    </>
  );
}
