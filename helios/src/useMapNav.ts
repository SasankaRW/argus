// Drag to move the live map, wheel or pinch to zoom, on the desktop and the phone. The map starts "fitted" (the view
// computed from the window); the first gesture takes over from there, and reset() hands it back to the fit.
// A drag that starts on a box still moves the map, and the click that ends it is swallowed so the box is not opened.

import { RefObject, useCallback, useEffect, useRef, useState } from "react";
import { keepInView, pinch, STEP, View, wheelFactor, zoomAbout } from "./navMath";

const DRAG_PX = 6;  // a finger that moves less than this is a tap

export function useMapNav(o: {
  ref: RefObject<HTMLElement | null>; fit: View; content: { w: number; h: number }; enabled: boolean;
  ignore: string;  // selector of the floating parts (the attention row, the buttons) that keep their own touches
}) {
  const [ov, setOv] = useState<View | null>(null);   // null: the fitted view
  const [smooth, setSmooth] = useState(false);       // buttons glide, gestures follow the finger exactly
  const [dragging, setDragging] = useState(false);
  const view = ov ?? o.fit;
  const live = useRef({ view, content: o.content, enabled: o.enabled, ignore: o.ignore });
  live.current = { view, content: o.content, enabled: o.enabled, ignore: o.ignore };

  const area = () => { const el = o.ref.current; return { w: el?.clientWidth ?? 0, h: el?.clientHeight ?? 0 }; };
  const commit = useCallback((v: View, glide = false) => {
    const a = area(), c = live.current.content;
    const next = keepInView(v, c.w, c.h, a.w, a.h);
    live.current.view = next;  // two events in one frame (both fingers of a pinch) must build on each other, not on the last render
    setSmooth(glide);
    setOv(next);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const el = o.ref.current;
    if (!el) return;
    const pts = new Map<number, { x: number; y: number }>();
    let start = { x: 0, y: 0 }, panning = false, swallow = false;
    const local = (e: { clientX: number; clientY: number }) => { const r = el.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
    const two = () => { const [a, b] = [...pts.values()]; return { cx: (a.x + b.x) / 2, cy: (a.y + b.y) / 2, d: Math.hypot(a.x - b.x, a.y - b.y) }; };

    const down = (e: PointerEvent) => {
      if (!live.current.enabled || (e.pointerType === "mouse" && e.button !== 0)) return;
      if ((e.target as Element).closest(live.current.ignore)) return;
      pts.set(e.pointerId, local(e));
      if (pts.size === 1) { start = local(e); panning = false; }
      else if (pts.size === 2) { panning = true; setDragging(true); }
    };
    const move = (e: PointerEvent) => {
      const prev = pts.get(e.pointerId);
      if (!prev) return;
      const now = local(e);
      if (pts.size === 2) {
        const before = two();
        pts.set(e.pointerId, now);
        commit(pinch(live.current.view, before, two()));
        return;
      }
      pts.set(e.pointerId, now);
      if (!panning && Math.hypot(now.x - start.x, now.y - start.y) > DRAG_PX) {
        panning = true; setDragging(true);
        try { el.setPointerCapture(e.pointerId); } catch { /* the pointer is already gone */ }
      }
      if (panning) { const v = live.current.view; commit({ ...v, x: v.x + now.x - prev.x, y: v.y + now.y - prev.y }); }
    };
    const up = (e: PointerEvent) => {
      if (!pts.delete(e.pointerId)) return;
      if (pts.size === 1) start = [...pts.values()][0];  // one finger lifted from a pinch: carry on dragging with the other
      if (pts.size === 0) {
        if (panning) { swallow = true; setTimeout(() => { swallow = false; }, 0); }
        panning = false; setDragging(false);
      }
    };
    const click = (e: MouseEvent) => { if (swallow) { e.stopPropagation(); e.preventDefault(); } };
    const wheel = (e: WheelEvent) => {
      if (!live.current.enabled || (e.target as Element).closest(live.current.ignore)) return;
      e.preventDefault();
      const p = local(e);
      commit(zoomAbout(live.current.view, p.x, p.y, wheelFactor(e.deltaY, e.ctrlKey, e.deltaMode)));
    };

    el.addEventListener("pointerdown", down);
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerup", up);
    el.addEventListener("pointercancel", up);
    el.addEventListener("click", click, true);
    el.addEventListener("wheel", wheel, { passive: false });
    return () => {
      el.removeEventListener("pointerdown", down);
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerup", up);
      el.removeEventListener("pointercancel", up);
      el.removeEventListener("click", click, true);
      el.removeEventListener("wheel", wheel);
    };
  }, [o.ref, commit]);

  const zoomBy = useCallback((f: number) => { const a = area(); commit(zoomAbout(live.current.view, a.w / 2, a.h / 2, f), true); }, [commit]);
  const reset = useCallback(() => { setSmooth(true); setOv(null); }, []);
  return { view, smooth, dragging, moved: ov !== null, zoomIn: () => zoomBy(STEP), zoomOut: () => zoomBy(1 / STEP), reset };
}
