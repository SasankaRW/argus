// The "Clipboard sync" switch, asked straight from this device (like mine.ts), never through Argus:
// - in the phone app: window.ArgusPhone.clipSync() / setClipSync(on);
// - on a computer with Argus Send: its Send agent on this machine, GET /send/clip and POST /send/clip/enabled.
// Anywhere else the switch is not reachable and Helios shows no row.
import { useCallback, useEffect, useRef, useState } from "react";
import { agent } from "./mine";

export type ClipSwitch = {
  where: "phone" | "pc";
  on: boolean;
  busy: boolean;
  failed: boolean;
  toggle: () => void;
};

export function useClipSwitch(): ClipSwitch | null {
  const [state, setState] = useState<{ where: "phone" | "pc"; on: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    const phone = window.ArgusPhone;
    if (phone) {
      if (phone.clipSync && phone.setClipSync) {
        try { setState({ where: "phone", on: !!phone.clipSync() }); } catch { /* an older app */ }
      }
    } else {
      agent<{ enabled: boolean }>("/send/clip")
        .then((got) => { if (live.current) setState({ where: "pc", on: !!got.enabled }); })
        .catch(() => { /* no agent here: no row */ });
    }
    return () => { live.current = false; };
  }, []);

  const toggle = useCallback(() => {
    if (!state || busy) return;
    const was = state.on;
    const next = !was;
    setFailed(false); setBusy(true);
    setState({ ...state, on: next });  // optimistic
    const revert = () => { if (live.current) { setState((s) => (s ? { ...s, on: was } : s)); setFailed(true); } };
    const done = () => { if (live.current) setBusy(false); };
    if (state.where === "phone") {
      try {
        window.ArgusPhone!.setClipSync!(next);
        if (!!window.ArgusPhone!.clipSync!() !== next) revert();
      } catch { revert(); }
      done();
      return;
    }
    agent<{ enabled: boolean }>("/send/clip/enabled", "POST", { enabled: next })
      .then((got) => { if (live.current && typeof got.enabled === "boolean") setState((s) => (s ? { ...s, on: got.enabled } : s)); })
      .catch(revert)
      .finally(done);
  }, [state, busy]);

  return state ? { ...state, busy, failed, toggle } : null;
}

export const clipHint = (where: "phone" | "pc") =>
  where === "pc" ? "Copies on this PC appear on your phone" : "Send clipboard tile and in-app copies go to your PC";
