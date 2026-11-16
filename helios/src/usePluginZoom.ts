// Opening and closing the plugin view. One progress value p (a spring, zoomMath.ts) drives the whole zoom, and it is
// written straight to the DOM on every frame (no React render per frame). Because p is one number with a velocity, a
// press of Back during opening, or of the Plugins box during closing, simply changes where the spring is headed: the
// motion turns round from wherever it is. This file owns the spring loop, history (one entry per open, so the browser's
// back button closes the view), Escape, focus and the reduced-motion path.

import { RefObject, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Box, camera, cardPose, Canvas, Kind, settled, Spring, springStep, zoomFrame } from "./zoomMath";

const MARK = "lmPlugins";  // in history.state while the plugin view is open

export type ZoomRefs = {
  clip: RefObject<HTMLDivElement | null>; cam: RefObject<HTMLDivElement | null>; view: RefObject<HTMLDivElement | null>;
  pbg: RefObject<HTMLDivElement | null>; idle: RefObject<HTMLDivElement | null>; head: RefObject<HTMLElement | null>;
  bar: RefObject<HTMLDivElement | null>; back: RefObject<HTMLButtonElement | null>;
};
export type ZoomIn = {
  enabled: boolean;
  box: Box | null;  // the Plugins box, in canvas coordinates
  canvas: Canvas;
  plug?: Canvas;  // the plugin view's own canvas, when it differs from the map's (the phone held sideways)
  groups: Record<Kind, string[]>;  // the plugin view's cards by role, in order
  at: Record<string, { x: number; y: number }>;  // where each of them sits
  fitScreen?: boolean;  // the plugin view is fitted to the whole screen (the phone held sideways): the map's clip lets go once it is in
  root: RefObject<HTMLElement | null>;
  hasSelection: boolean;  // a side panel is open: Escape closes it first
  clearSelection: () => void;
};
// A hook for tests and screenshots: freeze the zoom at any progress. Lives on the map's root element, not on window.
export type ZoomHook = { set: (p: number) => void; get: () => number };

const reducedMotion = () => typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

export function usePluginZoom(o: ZoomIn) {
  const [phase, setPhase] = useState<"map" | "view">("map");
  const live = useRef(o);
  live.current = o;
  const refs: ZoomRefs = {
    clip: useRef(null), cam: useRef(null), view: useRef(null), pbg: useRef(null), idle: useRef(null), head: useRef(null), bar: useRef(null), back: useRef(null),
  };
  const S = useRef<Spring & { to: number; raf: number; last: number }>({ x: 0, v: 0, to: 0, raf: 0, last: 0 });
  const pushed = useRef(false);  // the history entry of an open view is there
  const refocus = useRef(false);  // give focus back to the Plugins box once the map is live again

  const apply = useCallback((p: number) => {
    const { box, canvas, groups, at } = live.current;
    const pc = live.current.plug ?? canvas;
    const r = refs.cam.current, view = refs.view.current;
    if (!box || !r || !view) return;
    const reduced = reducedMotion();
    const f = zoomFrame(p, box, canvas, reduced);
    const set = (el: HTMLElement | null, opacity: number, on: boolean) => {
      if (!el) return;
      el.style.opacity = String(opacity);
      el.style.pointerEvents = on ? "auto" : "none";
      el.inert = !on;
    };
    r.style.transform = reduced ? "" : camera(f, box);
    set(r, f.map, f.live.map);
    set(view, f.view, f.live.view);
    set(refs.head.current, f.head, f.live.head);
    set(refs.bar.current, f.bar, f.live.bar);
    refs.clip.current?.classList.toggle("zooming", p > 0.001 && !reduced && !(live.current.fitScreen && p > 0.8));
    if (refs.pbg.current) refs.pbg.current.style.background = `color-mix(in srgb, var(--c-sec) ${f.mix}%, var(--map, #111114))`;
    for (const kind of ["mid", "src", "snk"] as const) {
      const ids = groups[kind];
      ids.forEach((id, i) => {
        const el = view.querySelector<HTMLElement>(`[data-card="${CSS.escape(id)}"]`);
        const a = at[id];
        if (!el || !a) return;
        const c = cardPose(kind, i, ids.length, p, a, pc, reduced);
        el.style.transform = `translate(${c.tx}px, ${c.ty}px) scale(${c.scale})`;
        el.style.setProperty("--q", String(c.opacity));
      });
    }
    view.style.setProperty("--ql", String(f.lines));
    const idle = refs.idle.current;
    if (idle) { idle.style.opacity = String(f.idle); idle.style.transform = `translateY(${30 * (1 - f.idle)}px)`; }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const settle = useCallback(() => {
    const s = S.current;
    if (s.to === 1) refs.back.current?.focus({ preventScroll: true });
    else if (refocus.current) { refocus.current = false; refs.cam.current?.querySelector<HTMLElement>('[data-card="plugins"]')?.focus({ preventScroll: true }); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const frame = useCallback((t: number) => {
    const s = S.current;
    const next = springStep(s, s.to, (t - s.last) / 1000);
    s.last = t; s.x = next.x; s.v = next.v;
    if (settled(s, s.to)) { s.x = s.to; s.v = 0; s.raf = 0; apply(s.to); settle(); return; }
    apply(Math.max(0, Math.min(1, s.x)));
    s.raf = requestAnimationFrame(frame);
  }, [apply, settle]);

  const go = useCallback((to: 0 | 1) => {
    const s = S.current;
    if (!live.current.enabled) return;
    s.to = to;
    setPhase(to === 1 ? "view" : "map");
    if (reducedMotion()) { cancelAnimationFrame(s.raf); s.raf = 0; s.x = to; s.v = 0; apply(to); settle(); return; }
    if (!s.raf) { s.last = performance.now(); s.raf = requestAnimationFrame(frame); }
  }, [apply, frame, settle]);

  const open = useCallback(() => {
    if (!live.current.enabled) return;
    if (!pushed.current) { try { history.pushState({ ...(history.state ?? {}), [MARK]: true }, ""); pushed.current = true; } catch { /* no history */ } }
    refocus.current = true;
    go(1);
  }, [go]);

  // Back: the browser's back button does the work when this view added the history entry (popstate closes it)
  const back = useCallback(() => {
    if (pushed.current) history.back();
    else go(0);
  }, [go]);

  useEffect(() => {
    const pop = () => { if (pushed.current && !(history.state && history.state[MARK])) { pushed.current = false; go(0); } };
    // Escape: the side panel first, then the plugin view; the full-screen map under it stays open
    const key = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || S.current.to !== 1) return;
      e.preventDefault(); e.stopImmediatePropagation();
      if (live.current.hasSelection) live.current.clearSelection(); else back();
    };
    window.addEventListener("popstate", pop);
    window.addEventListener("keydown", key, true);
    return () => { window.removeEventListener("popstate", pop); window.removeEventListener("keydown", key, true); cancelAnimationFrame(S.current.raf); S.current.raf = 0; };
  }, [back, go]);

  // every render: put the current progress back on whatever React just drew (new cards, new positions)
  useLayoutEffect(() => { apply(Math.max(0, Math.min(1, S.current.x))); });

  useEffect(() => {  // the test hook
    const el = live.current.root.current as (HTMLElement & { __lmZoom?: ZoomHook }) | null;
    if (!el) return;
    el.__lmZoom = {
      set: (p) => { const s = S.current; cancelAnimationFrame(s.raf); s.raf = 0; s.x = s.to = p; s.v = 0; setPhase(p > 0.5 ? "view" : "map"); apply(p); },
      get: () => S.current.x,
    };
    return () => { delete el.__lmZoom; };
  }, [apply]);

  useEffect(() => {  // /helios/?zoom=plugins opens the plugin view straight away
    if (o.enabled && o.box && new URLSearchParams(location.search).get("zoom") === "plugins") open();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [o.enabled, !!o.box]);

  return { refs, phase, open, back };
}
