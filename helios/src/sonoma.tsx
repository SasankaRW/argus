// The phone view's Sonoma pieces: an icon (Material Symbols Rounded, cut to src/icons.ts), the hold-to-confirm
// button, and a context that tells shared pages they are on the phone (layout only: the desktop is Sonoma
// too, so labels, holds and icons are the same). Styles: sonoma.css (shared by the phone and the desktop).
import { createContext, KeyboardEvent, ReactNode, useContext, useEffect, useRef, useState } from "react";
import type { IconName } from "./icons";

export const PhoneCtx = createContext(false);
export const useIsPhone = () => useContext(PhoneCtx);

// Sentence case, as the other Sonoma apps write it: a capital first letter, and Ari's name capitalised.
export const sentence = (s: string) => s.replace(/\bari\b/g, "Ari").replace(/^[a-z]/, (c) => c.toUpperCase());
// Labels, placeholders and hints are written in sentence case, on the phone and the desktop alike.
export function useSc() {
  return sentence;
}

// A panel title. Some are written like paths ("ari/settings"); the path part is dropped.
export function Pt({ children }: { children: string }) {
  return <span className="pt">{children.replace(/^ari\//, "")}</span>;
}

// A small heading some pages write like a comment ("# tests"); the "# " is dropped.
export function SectI({ children }: { children: string }) {
  return <span className="sect-i">{children.replace(/^# /, "")}</span>;
}

export function Ms({ n, fill = false, size }: { n: IconName; fill?: boolean; size?: number }) {
  return <span className={`ms${fill ? " fill" : ""}`} aria-hidden="true" style={size ? { fontSize: size } : undefined}>{n}</span>;
}

export type Tone = "green" | "amber" | "blue" | "violet" | "coral" | "plain";

// A list row's leading icon: 36×36, the tone at 16% behind a filled glyph.
export function IconTile({ n, tone = "plain" }: { n: IconName; tone?: Tone }) {
  return <span className={`sn-tile t-${tone}`}><Ms n={n} fill /></span>;
}

const HOLD_MS = 1200;

// Press and hold about 1.2 s to confirm: an accent ring fills around a round coral button. Letting go early does
// nothing but show "Hold to <action>" in the accent for 1.6 s (the same hint as every Sonoma app; onTap can add more).
// Works with touch, mouse and the keyboard (hold Space or Enter). The timer, not the animation, confirms, so it still
// works with reduced motion.
export function HoldButton({ icon, label, caption, onConfirm, onTap, disabled = false, tone = "coral" }: {
  icon: IconName; label: string; caption?: ReactNode; onConfirm: () => void; onTap?: () => void; disabled?: boolean;
  tone?: "coral" | "accent";  // coral: destructive; accent: a confirmation that isn't (going live, running a fix)
}) {
  const [on, setOn] = useState(false);
  const [hint, setHint] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  const hintT = useRef<number | undefined>(undefined);
  const done = useRef(false);
  useEffect(() => () => { window.clearTimeout(timer.current); window.clearTimeout(hintT.current); }, []);
  const start = () => {
    if (disabled || timer.current !== undefined) return;
    done.current = false;
    setOn(true);
    timer.current = window.setTimeout(() => {
      timer.current = undefined; done.current = true; setOn(false);
      navigator.vibrate?.(30);
      onConfirm();
    }, HOLD_MS);
  };
  const stop = () => {
    if (timer.current === undefined) return;
    window.clearTimeout(timer.current); timer.current = undefined; setOn(false);
    if (!done.current) {
      setHint(true); window.clearTimeout(hintT.current); hintT.current = window.setTimeout(() => setHint(false), 1600);
      onTap?.();
    }
  };
  const key = (e: KeyboardEvent, down: boolean) => {
    if (e.key !== " " && e.key !== "Enter") return;
    e.preventDefault();
    if (down) { if (!e.repeat) start(); } else stop();
  };
  return (
    <span className="sn-holdwrap">
      <button type="button" className={`sn-hold${tone === "accent" ? " accent" : ""}${on ? " on" : ""}`} aria-label={`Hold to ${label.toLowerCase()}`} disabled={disabled}
        onPointerDown={(e) => { e.currentTarget.setPointerCapture?.(e.pointerId); start(); }}
        onPointerUp={stop} onPointerCancel={stop} onPointerLeave={stop} onContextMenu={(e) => e.preventDefault()}
        onKeyDown={(e) => key(e, true)} onKeyUp={(e) => key(e, false)}>
        <Ms n={icon} fill />
        <svg aria-hidden="true" viewBox="0 0 44 44">
          <circle className="trk" cx="22" cy="22" r="20.5" />
          <circle className="prg" cx="22" cy="22" r="20.5" pathLength={1} transform="rotate(-90 22 22)" />
        </svg>
      </button>
      {hint ? <span className="sn-holdcap hint" role="status">Hold to {label.toLowerCase()}</span>
        : caption !== undefined && <span className="sn-holdcap">{caption}</span>}
    </span>
  );
}

// The Argus mark: a ring around a dot, in the accent (the app icon is the same).
export function Logo({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" fill="none" aria-hidden="true" className="logo">
      <circle cx="16" cy="16" r="13" stroke="var(--ac)" strokeWidth="2.4" />
      <circle cx="16" cy="16" r="4.6" fill="var(--ac)" />
    </svg>
  );
}
