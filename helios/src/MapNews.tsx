// The map's two news surfaces (desktop): the banner that says what just reached your phone (a wishlist alert, an
// important message), and the strip along the bottom that keeps the last three things Argus did, newest first.
// Older chips glide over when a new one comes in (FLIP), and the one that leaves is lifted out of the row first, so
// the row never jumps.

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ArgusEvent } from "./api";
import { Ms } from "./m3";
import { freshEvents } from "./mapFlow";
import { stripLine, type StripLine } from "./mapStrip";
import { reducedMotion } from "./useMapFlow";

export type TimedLine = StripLine & { at: number };  // at: the event's time, seconds

const KEEP = 3;
const EASE_OUT = "cubic-bezier(.05, .7, .1, 1)";

// The strip after `next`: newest first, at most three, no repeat of the newest line.
export function pushLine(prev: TimedLine[], next: TimedLine | null): TimedLine[] {
  if (!next || prev[0]?.text === next.text) return prev;
  return [next, ...prev].slice(0, KEEP);
}

export function agoShort(sec: number): string {
  if (sec < 3) return "now";
  if (sec < 60) return `${Math.round(sec)} s ago`;
  if (sec < 3600) return `${Math.round(sec / 60)} min ago`;
  return `${Math.round(sec / 3600)} h ago`;
}

// Strip lines for the events after the map opened (none from before: a reload doesn't replay old news).
export function useStrip(events: ArgusEvent[], name: (id: string) => string | null): TimedLine[] {
  const seq = useRef<number | null>(null);
  const [lines, setLines] = useState<TimedLine[]>([]);
  useEffect(() => {
    const newest = events.reduce((m, e) => Math.max(m, e.seq), 0);
    if (seq.current === null) { seq.current = newest; return; }
    const after = seq.current;
    if (newest <= after) return;
    seq.current = newest;
    let next = lines;
    for (const e of freshEvents(events, after, Date.now() / 1000)) { const l = stripLine(e, name); next = pushLine(next, l ? { ...l, at: e.at } : null); }
    if (next !== lines) setLines(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events, name]);
  return lines;
}

const cleanTitle = (t: unknown) => String(t ?? "").replace(/^Watcher: /, "").replace(/^Wishlist: /, "").trim();

export function MapBanner({ news, style }: { news: ArgusEvent | null; style?: React.CSSProperties }) {
  if (!news) return null;
  const wish = Array.isArray(news.data?.tags) && (news.data.tags as string[]).includes("wishlist");
  const text = String(news.data?.text ?? "").trim();
  const title = cleanTitle(news.data?.title);
  return (
    <div key={news.seq} className="lm-news" role="status" style={style}>
      <span className="lm-news-i"><Ms n={wish ? "sell" : "notifications"} fill /></span>
      <span className="lm-news-t">
        <b>{text && wish ? text.replace(/^Wishlist: /, "").replace(/\.$/, "") : title}</b>
        <span>{!wish && text && text !== title ? text : "Told your phone"}</span>
      </span>
    </div>
  );
}

export function MapStrip({ lines, now, style }: { lines: TimedLine[]; now: number; style?: React.CSSProperties }) {
  const ref = useRef<HTMLDivElement>(null);
  const pos = useRef(new Map<string, number>());
  const prev = useRef<TimedLine[]>([]);
  const [leaving, setLeaving] = useState<(TimedLine & { left: number })[]>([]);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const gone = prev.current.filter((l) => !lines.some((x) => x.key === l.key));
    let t = 0;
    if (gone.length) {
      setLeaving((g) => [...g, ...gone.map((l) => ({ ...l, left: pos.current.get(l.key) ?? 0 }))]);
      t = window.setTimeout(() => setLeaving((g) => g.filter((x) => !gone.some((y) => y.key === x.key))), 320);
    }
    const chips = [...el.querySelectorAll<HTMLElement>(".lm-ev[data-k]")];
    if (!reducedMotion()) {
      for (const c of chips) {
        const old = pos.current.get(c.dataset.k!);
        if (old === undefined) c.animate([{ transform: "translateX(-24px)", opacity: 0 }, { transform: "none", opacity: 1 }], { duration: 460, easing: EASE_OUT });
        else if (old !== c.offsetLeft) c.animate([{ transform: `translateX(${old - c.offsetLeft}px)` }, { transform: "none" }], { duration: 460, easing: EASE_OUT });
      }
    }
    pos.current = new Map(chips.map((c) => [c.dataset.k!, c.offsetLeft]));
    prev.current = lines;
    return () => window.clearTimeout(t);
  }, [lines]);
  if (!lines.length && !leaving.length) return null;
  return (
    <div ref={ref} className="lm-strip" aria-live="polite" style={style}>
      {lines.map((l, i) => (
        <span key={l.key} data-k={l.key} className={`lm-ev t-${l.tone}${i === 0 ? " new" : ""}`}>
          <Ms n={l.icon} fill /><span>{l.text}</span><span className="lm-ago">{agoShort(now - l.at)}</span>
        </span>
      ))}
      {leaving.map((l) => (
        <span key={`out${l.key}`} className={`lm-ev t-${l.tone} out`} style={{ left: l.left }} aria-hidden="true">
          <Ms n={l.icon} fill /><span>{l.text}</span>
        </span>
      ))}
    </div>
  );
}
