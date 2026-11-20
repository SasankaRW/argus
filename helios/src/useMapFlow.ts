// Feeds the map's flow layer: turns the events that arrive after the map opened into dots (mapFlow.ts decides which),
// and collects the news (important phone messages, wishlist alerts) for the banner. Nothing from before the map
// opened animates, so a first load or a reconnect replaying hundreds of events stays still.

import { useEffect, useRef, useState } from "react";
import type { ArgusEvent } from "./api";
import { isNews, MAX_DOTS, newsRoute, pickDots, stepDot, type Dot } from "./mapFlow";
import type { Routed } from "./mapGraph";

const KEEP = 30;  // new dots handed to the layer, kept so it can tell which it has already started

// The dots on screen after `picked` arrive: no repeats, none on a line that is gone, at most MAX_DOTS (newest kept).
export function nextDots(prev: Dot[], picked: Dot[], live: Set<string>): Dot[] {
  const have = new Set(prev.map((d) => d.id));
  return [...prev, ...picked.filter((d) => !have.has(d.id))].filter((d) => live.has(d.route)).slice(-MAX_DOTS);
}

export const reducedMotion = () => typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

export function useMapFlow(events: ArgusEvent[], routes: Routed[], of: (id: string) => string | null, on: boolean):
  { incoming: Dot[]; news: ArgusEvent[] } {
  const seq = useRef<number | null>(null);
  const last = useRef(new Map<string, number>());
  const [incoming, setIncoming] = useState<Dot[]>([]);
  const [news, setNews] = useState<ArgusEvent[]>([]);
  useEffect(() => {
    const newest = events.reduce((m, e) => Math.max(m, e.seq), 0);
    if (seq.current === null) { seq.current = newest; return; }  // the map just opened: start from here
    const after = seq.current;
    if (newest <= after) return;
    seq.current = newest;
    const fresh = events.filter((e) => e.seq > after);
    const got = fresh.filter(isNews);
    if (got.length) setNews((n) => [...n, ...got].slice(-5));
    if (!on || (typeof document !== "undefined" && document.hidden)) return;
    const now = performance.now();
    const { dots } = pickDots(fresh, { afterSeq: after, routes, of, last: last.current, nowMs: now });
    for (const e of got) {
      newsRoute(e, routes, of).forEach((chain, i) => {
        if (chain.length) dots.push(stepDot(`n${e.seq}-${i}`, chain[0], "news", now, chain.slice(1)));
      });
    }
    if (dots.length) setIncoming((prev) => [...prev, ...dots].slice(-KEEP));
  }, [events, routes, of, on]);
  return { incoming, news };
}
