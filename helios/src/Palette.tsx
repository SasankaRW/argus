// Ctrl+K: jump to any page or run an action from the keyboard.
import { useEffect, useMemo, useRef, useState } from "react";

export type PaletteItem = { id: string; label: string; hint?: string; group: string; run: () => void };

function score(q: string, s: string): number {
  if (!q) return 1;
  const t = s.toLowerCase();
  if (t.includes(q)) return 100 - t.indexOf(q);
  let i = 0;
  for (const ch of t) if (ch === q[i]) i++;
  return i === q.length ? 1 : 0;
}

export function Palette({ items, open, onClose }: { items: PaletteItem[]; open: boolean; onClose: () => void }) {
  const [q, setQ] = useState("");
  const [i, setI] = useState(0);
  const ref = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);
  useEffect(() => { if (open) { setQ(""); setI(0); setTimeout(() => ref.current?.focus(), 0); } }, [open]);
  const shown = useMemo(() => {
    const n = q.trim().toLowerCase();
    return items.map((x) => ({ x, s: score(n, `${x.label} ${x.group} ${x.hint ?? ""}`) })).filter((r) => r.s > 0)
      .sort((a, b) => b.s - a.s).map((r) => r.x).slice(0, 40);
  }, [items, q]);
  useEffect(() => { setI(0); }, [q]);
  useEffect(() => { list.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" }); }, [i]);
  if (!open) return null;
  const run = (x?: PaletteItem) => { if (!x) return; onClose(); x.run(); };
  return (
    <div className="pal-back" onMouseDown={onClose} role="presentation">
      <div className="pal" role="dialog" aria-label="Command palette" onMouseDown={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === "Escape") onClose();
          else if (e.key === "ArrowDown") { e.preventDefault(); setI((v) => Math.min(shown.length - 1, v + 1)); }
          else if (e.key === "ArrowUp") { e.preventDefault(); setI((v) => Math.max(0, v - 1)); }
          else if (e.key === "Enter") { e.preventDefault(); run(shown[i]); }
        }}>
        <div className="pal-in"><span className="prompt">&gt;</span>
          <input ref={ref} value={q} onChange={(e) => setQ(e.target.value)} placeholder="Go to a page or run an action" aria-label="Command" />
          <kbd>esc</kbd>
        </div>
        <div className="pal-list" ref={list} role="listbox">
          {shown.length === 0 && <div className="pal-none">Nothing matches “{q}”.</div>}
          {shown.map((x, n) => (
            <button key={x.id} type="button" role="option" aria-selected={n === i} className="pal-row" onMouseMove={() => setI(n)} onClick={() => run(x)}>
              <span className="pal-g">{x.group}</span><span className="pal-l">{x.label}</span>{x.hint && <span className="pal-h">{x.hint}</span>}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
