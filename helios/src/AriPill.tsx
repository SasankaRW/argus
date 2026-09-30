import { useLayoutEffect, useRef, useState } from "react";
import { ariSet, useAri } from "./ariState";
import { stopSpeaking } from "./voice";

const LABEL: Record<string, string> = { listening: "listening", thinking: "thinking", working: "working", speaking: "speaking", done: "done" };
const PAD_X = 8, PAD_Y = 7;  // the capsule's padding around its content (keep in step with .ap-body)

// The Ari pill: a black capsule at the top while Ari listens, thinks, works or talks, from anywhere in Helios (and
// over the whole PC screen through the popup). Two lights on its edge: a ring that pulses with the voice
// (listening, speaking) and a comet that runs round it while Ari thinks or works. It sizes itself to what it says,
// growing and shrinking smoothly; long answers take two lines. Tap it to open Ari.
export function AriPill({ onOpen }: { onOpen: () => void }) {
  const s = useAri();
  const body = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);
  useLayoutEffect(() => {  // follow the content's own size, so the capsule can glide between sizes
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
  const two = (size?.h ?? 0) > 60;
  return (
    <div className={`aripill-wrap${open ? " open" : ""}`} aria-live="polite">
      <div className={`aripill ph-${s.phase}${voice ? " voice" : ""}${busy ? " busy" : ""}${two ? " tall" : ""}`} role="status"
        style={size ? { width: size.w, height: size.h } : undefined}>
        <span className="ap-glow" aria-hidden="true" />
        <span className="ap-ring ap-base" aria-hidden="true" />
        <span className="ap-ring ap-pulse" aria-hidden="true" />
        <span className="ap-ring ap-comet" aria-hidden="true" />
        <span className="ap-ring ap-comet ap-blur" aria-hidden="true" />
        <div className="ap-clip">
        <div ref={body} className="ap-body">
          <button type="button" className="ap-main" onClick={onOpen} title="Open Ari">
            <span className="ap-orb" aria-hidden="true">
              <span className="ap-core" />
              {busy && <span className="ap-arc" />}
              {voice && <span className="ap-bars">{[0, 1, 2, 3, 4].map((k) => <i key={k} />)}</span>}
            </span>
            <span className="ap-text">
              <span className="ap-who mono">ari <b>›</b> {LABEL[s.phase] ?? ""}</span>
              {s.text && <span className="ap-said">{s.text}</span>}
            </span>
          </button>
          {(s.phase === "speaking" || s.phase === "done") && (
            <button type="button" className="ap-x" aria-label="Stop" onClick={() => { stopSpeaking(); ariSet("idle"); }}>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>
            </button>
          )}
        </div>
        </div>
      </div>
    </div>
  );
}
