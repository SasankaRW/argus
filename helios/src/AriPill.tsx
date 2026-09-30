import { useEffect, useState } from "react";
import { ariSet, useAri } from "./ariState";
import { stopSpeaking } from "./voice";

const SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏";
const LABEL: Record<string, string> = { listening: "listening", thinking: "thinking", working: "working", speaking: "speaking", done: "ari" };

// The Ari pill: a floating capsule at the top while Ari listens, thinks, works or talks, from anywhere in Helios.
// Glowing edge in the terminal colours; bars for sound, a spinner for thinking; tap it to open Ari.
export function AriPill({ onOpen }: { onOpen: () => void }) {
  const s = useAri();
  const [i, setI] = useState(0);
  const busy = s.phase === "thinking" || s.phase === "working";
  useEffect(() => {
    if (!busy) return;
    const t = setInterval(() => setI((x) => (x + 1) % SPIN.length), 80);
    return () => clearInterval(t);
  }, [busy]);
  const open = s.phase !== "idle";
  const sound = s.phase === "listening" || s.phase === "speaking";
  return (
    <div className={`aripill-wrap${open ? " open" : ""}`} aria-live="polite">
      <div className={`aripill ph-${s.phase}`} role="status">
        <span className="ap-edge" aria-hidden="true" />
        <button type="button" className="ap-main" onClick={onOpen} title="Open Ari">
          <span className="ap-orb" aria-hidden="true">
            {sound ? <span className="ap-bars">{[0, 1, 2, 3, 4].map((k) => <i key={k} />)}</span>
              : busy ? <span className="ap-spin mono">{SPIN[i]}</span>
                : <span className="ap-dot" />}
          </span>
          <span className="ap-text mono">
            <span className="ap-who">ari <b>›</b> {LABEL[s.phase] ?? ""}</span>
            {s.text && <span className="ap-said">{s.text}</span>}
            {busy && !s.text && <span className="ap-cursor" />}
          </span>
        </button>
        {(s.phase === "speaking" || s.phase === "done") && (
          <button type="button" className="ap-x" aria-label="Stop" onClick={() => { stopSpeaking(); ariSet("idle"); }}>×</button>
        )}
      </div>
    </div>
  );
}
