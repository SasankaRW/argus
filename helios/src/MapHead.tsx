// The strip above the map: what needs you, each with its action ("All fine" when nothing does), and Pause everything /
// Resume everything (only while something is paused). The items come from attention.ts.

import { forwardRef, useState } from "react";
import { api } from "./api";
import { Item, MAX_ITEMS } from "./attention";
import { PAUSE_ALL, RESUME_ALL, allPath, run } from "./controls";
import { Control } from "./ControlPanel";
import { Ms } from "./m3";

export const MapHead = forwardRef<HTMLDivElement, {
  items: Item[]; anyPaused: boolean; onReview: () => void; onLogs: (target: string) => void; onChanged: () => void;
  className?: string; style?: React.CSSProperties;
}>(function MapHead({ items, anyPaused, onReview, onLogs, onChanged, className, style }, ref) {
  const [said, setSaid] = useState<Record<string, string>>({});  // what a card says after its button was used
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  const shown = items.slice(0, MAX_ITEMS);
  const ring = async (id: string) => {
    try { await api("/phone/ring", { method: "POST" }); setSaid((s) => ({ ...s, [id]: "Ringing: three loud notifications, 20 s apart." })); }
    catch (e) { setSaid((s) => ({ ...s, [id]: e instanceof Error ? e.message : "Could not ring the phone." })); }
  };
  const all = async (b: typeof PAUSE_ALL) => {
    setBusy(true);
    const r = await run(allPath(b), b.action);
    setBusy(false);
    setNote({ ok: r.ok, text: r.ok ? r.note : r.error });
    onChanged();
  };
  return (
    <div ref={ref} className={`lm-head${className ? ` ${className}` : ""}`} style={style} role="region" aria-label="Needs attention">
      <div className="lm-att">
        {items.length === 0 && <span className="lm-pill ok"><Ms n="check" />All fine</span>}
        {shown.map((it) => (
          <div key={it.id} className={`lm-card t-${it.tone}`}>
            <span className="lm-ci"><Ms n={it.icon} fill /></span>
            <span className="lm-ct"><span className="lm-nm">{it.title}</span><span className="lm-st">{said[it.id] ?? it.detail ?? ""}</span></span>
            <button type="button" className="lm-go" onClick={() => (it.act.kind === "ring" ? ring(it.id) : it.act.kind === "review" ? onReview() : onLogs(it.act.target))}>{it.act.label}</button>
          </div>
        ))}
        {items.length > shown.length && <span className="lm-pill">+{items.length - shown.length} more</span>}
      </div>
      <div className="lm-all">
        <Control b={PAUSE_ALL} busy={busy} onFire={() => all(PAUSE_ALL)} />
        {anyPaused && <Control b={RESUME_ALL} busy={busy} onFire={() => all(RESUME_ALL)} />}
        {note && <span className={`lm-note${note.ok ? "" : " t-bad"}`} role="status">{note.text}</span>}
      </div>
    </div>
  );
});
