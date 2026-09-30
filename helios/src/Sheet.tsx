import { ReactNode, useEffect, useRef, useState } from "react";

// A bottom sheet for the phone: slides up over the page; drag the handle down (or tap outside, or Esc) to close.
export function Sheet({ onClose, children, label = "Details" }: { onClose: () => void; children: ReactNode; label?: string }) {
  const [dy, setDy] = useState(0);
  const [closing, setClosing] = useState(false);
  const start = useRef<number | null>(null);
  const close = () => { setClosing(true); setTimeout(onClose, 220); };
  useEffect(() => {
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return (
    <div className={`sheet-wrap${closing ? " closing" : ""}`} role="dialog" aria-label={label}>
      <button type="button" className="sheet-back" aria-label="Close" onClick={close} />
      <div className="sheet" style={dy ? { transform: `translateY(${dy}px)`, transition: "none" } : undefined}>
        <div className="sheet-grip"
          onPointerDown={(e) => { start.current = e.clientY; (e.target as HTMLElement).setPointerCapture(e.pointerId); }}
          onPointerMove={(e) => { if (start.current !== null) setDy(Math.max(0, e.clientY - start.current)); }}
          onPointerUp={() => { if (dy > 90) close(); setDy(0); start.current = null; }}
          onPointerCancel={() => { setDy(0); start.current = null; }}>
          <span />
        </div>
        <div className="sheet-body">{children}</div>
      </div>
    </div>
  );
}
