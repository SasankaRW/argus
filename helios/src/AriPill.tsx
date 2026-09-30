import { useLayoutEffect, useRef, useState } from "react";
import { ariSet, PillLook, useAri, usePillLook } from "./ariState";
import { stopSpeaking } from "./voice";

const LABEL: Record<string, string> = { listening: "listening", thinking: "thinking", working: "working", speaking: "speaking", done: "done" };
const PAD_X = 9, PAD_Y = 8;  // the capsule's padding around its content (keep in step with .ap-body)

// The Ari pill: a smooth black capsule at the top while Ari listens, thinks, works or talks, from anywhere in Helios
// (and over the whole PC screen through the popup). Its edge is lit by a soft light, never a hard line, in one of
// two looks: "pulse" (the light breathes, with the voice while listening or speaking) or "comet" (the light travels
// round the edge). It sizes itself to what it says and glides between sizes. Tap it to open Ari.
export function AriPill({ onOpen, look: forced }: { onOpen: () => void; look?: PillLook }) {
  const s = useAri();
  const chosen = usePillLook();
  const look = forced ?? chosen;
  const body = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);
  useLayoutEffect(() => {
    const el = body.current;
    if (!el) return;
    const measure = () => setSize({ w: Math.ceil(el.offsetWidth) + PAD_X * 2, h: Math.ceil(el.offsetHeight) + PAD_Y * 2 });
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const open = s.phase !== "idle";
  const voice = s.phase === "listening" || s.phase === "speaking";
  const busy = s.phase === "thinking" || s.phase === "working";
  return (
    <div className={`aripill-wrap${open ? " open" : ""}`} aria-live="polite">
      <div className={`aripill look-${look} ph-${s.phase}${voice ? " voice" : ""}${busy ? " busy" : ""}`} role="status"
        style={size ? { width: size.w, height: size.h } : undefined}>
        <span className="ap-light" aria-hidden="true" />
        <span className="ap-clip">
          <span className="ap-inner" aria-hidden="true" />
          <div ref={body} className="ap-body">
            <button type="button" className="ap-main" onClick={onOpen} title="Open Ari">
              <span className="ap-orb" aria-hidden="true">
                {voice ? <span className="ap-bars">{[0, 1, 2, 3, 4].map((k) => <i key={k} />)}</span>
                  : busy ? <span className="ap-dots"><i /><i /><i /></span> : null}
              </span>
              <span className="ap-text">
                <span className="ap-who mono">ari · {LABEL[s.phase] ?? ""}</span>
                {s.text && <span className="ap-said">{s.text}</span>}
              </span>
            </button>
            {(s.phase === "speaking" || s.phase === "done") && (
              <button type="button" className="ap-x" aria-label="Stop" onClick={() => { stopSpeaking(); ariSet("idle"); }}>
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>
              </button>
            )}
          </div>
        </span>
      </div>
    </div>
  );
}
