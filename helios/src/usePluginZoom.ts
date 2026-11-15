// Opening and closing the plugin view: a shared-element expansion built from Web Animations (element.animate), played
// forward to open and backward to close, so closing is the exact reverse and an interrupted move turns around from
// where it is. The what-moves-when is zoomPlan.ts; this file owns the state machine, history, focus and clean-up.

import { RefObject, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Layout } from "./mapGraph";
import { EASE, Plan, zoomPlan } from "./zoomPlan";

export type Mode = "map" | "opening" | "plugins" | "closing";
const MARK = "lmPlugins";  // in history.state while the plugin view is open: the browser's back button closes it

export function usePluginZoom(o: { enabled: boolean; main: Layout; plug: Layout; wrap: RefObject<HTMLElement | null> }) {
  const [mode, setModeState] = useState<Mode>("map");
  const modeRef = useRef<Mode>("map");
  const setMode = (m: Mode) => { modeRef.current = m; setModeState(m); };
  const frozen = useRef<{ main: Layout; plug: Layout } | null>(null);  // the drawing as it was when the move began
  const anims = useRef<{ list: Animation[]; master: Animation } | null>(null);
  const pushed = useRef(false);
  const refocus = useRef(false);  // give focus back to the Plugins box once the map is live again
  const popping = useRef(false);  // history.back() is on its way: a second press must not go back twice
  const live = useRef(o);
  live.current = o;
  const cam = useRef<HTMLDivElement>(null);
  const main = useRef<HTMLDivElement>(null);
  const plug = useRef<HTMLDivElement>(null);
  const bar = useRef<HTMLDivElement>(null);
  const idle = useRef<HTMLDivElement>(null);
  const status = useRef<HTMLDivElement>(null);
  const backBtn = useRef<HTMLButtonElement>(null);

  const targets = (t: string): Element[] => {
    const all = (root: HTMLElement | null, sel: string) => (root ? [...root.querySelectorAll(sel)] : []);
    if (t === "cam") return cam.current ? [cam.current] : [];
    if (t === "layer") return plug.current ? [plug.current] : [];
    if (t === "bar") return bar.current ? [bar.current] : [];
    if (t === "idle") return idle.current ? [idle.current] : [];
    if (t === "fade") return [...all(main.current, ".lm-n:not([data-card='plugins']), .lm-l, .lm-badge, .lm-ch"), ...(status.current ? [status.current] : [])];
    if (t === "plugbox") return all(main.current, "[data-card='plugins']");
    if (t === "lines") return all(plug.current, ".lm-l, .lm-badge");
    if (t.startsWith("card:")) return all(plug.current, `[data-card="${CSS.escape(t.slice(5))}"]`);
    return [];
  };

  const cancel = useCallback(() => {
    anims.current?.list.forEach((a) => a.cancel());
    anims.current?.master.cancel();
    anims.current = null;
    if (cam.current) cam.current.style.transformOrigin = "";
  }, []);

  const commit = useCallback((m: "map" | "plugins") => {
    if (m === "map") {
      cancel();
      frozen.current = null;
      refocus.current = true;
      setMode("map");
    } else {
      setMode("plugins");
      requestAnimationFrame(() => backBtn.current?.focus());
    }
  }, [cancel]);

  const play = useCallback((plan: Plan, origin: { x: number; y: number }) => {
    cancel();
    if (cam.current) cam.current.style.transformOrigin = `${origin.x}px ${origin.y}px`;
    const list: Animation[] = [];
    for (const s of plan.specs) {
      for (const el of targets(s.target)) {
        list.push(el.animate(s.keyframes, { duration: s.duration, delay: s.delay, endDelay: plan.total - s.delay - s.duration, easing: EASE, fill: "both" }));
      }
    }
    // one animation as long as the whole move tells us when it is over, in either direction
    const master = (cam.current ?? document.body).animate([{ outlineWidth: "0px" }, { outlineWidth: "0px" }], { duration: plan.total, fill: "both" });
    master.onfinish = () => commit(master.playbackRate >= 0 ? "plugins" : "map");
    anims.current = { list, master };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cancel, commit]);

  const turn = () => {  // play the move the other way from where it is
    const a = anims.current;
    if (!a) return false;
    a.list.forEach((x) => x.reverse());
    a.master.reverse();
    return true;
  };

  const open = useCallback(() => {
    const m = modeRef.current;
    if (!live.current.enabled || m === "opening" || m === "plugins") return;
    const push = () => { try { history.pushState({ [MARK]: 1 }, ""); pushed.current = true; } catch { /* no history */ } };
    if (m === "closing") { if (turn()) { push(); setMode("opening"); return; } }
    frozen.current = { main: live.current.main, plug: live.current.plug };
    push();
    setMode("opening");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const close = useCallback(() => {
    const m = modeRef.current;
    if (m === "map" || m === "closing") return;
    frozen.current = frozen.current ?? { main: live.current.main, plug: live.current.plug };
    if (turn()) setMode("closing"); else commit("map");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [commit]);

  // the animations are built once the plugin layer is in the page (before it is painted, so nothing flashes)
  useLayoutEffect(() => {
    if (mode !== "opening" || anims.current) return;
    const f = frozen.current;
    if (!f) return;
    const at = (l: Layout, level: number) => Object.values(l.placed).filter((c) => c.level === level).map((c) => ({ id: c.id, x: c.x, y: c.y }));
    const box = f.main.placed.plugins;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    play(zoomPlan({ origin: box ? { x: box.x, y: box.y } : { x: f.main.width / 2, y: f.main.height / 2 }, running: at(f.plug, 1), inputs: at(f.plug, 0), outputs: at(f.plug, 2), reduced }), box ?? { x: f.main.width / 2, y: f.main.height / 2 });
  }, [mode, play]);

  useEffect(() => {
    if (mode !== "map" || !refocus.current) return;
    refocus.current = false;
    (live.current.wrap.current?.querySelector("[data-card='plugins']") as HTMLElement | null)?.focus();
  }, [mode]);

  // Back button, Escape and the browser's back all close just the plugin view
  const back = useCallback(() => {
    const m = modeRef.current;
    if (m === "map" || m === "closing") return;
    if (!pushed.current) close();
    else if (!popping.current) { popping.current = true; history.back(); }
  }, [close]);
  useEffect(() => {
    if (mode === "map") return;
    const esc = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();  // the full-screen map closes on Escape too: this one only closes the plugin view
      e.preventDefault();
      back();
    };
    window.addEventListener("keydown", esc, true);
    return () => window.removeEventListener("keydown", esc, true);
  }, [mode, back]);
  useEffect(() => {
    const pop = () => {
      popping.current = false;
      if (!pushed.current || (history.state && history.state[MARK])) return;
      pushed.current = false;
      close();
    };
    window.addEventListener("popstate", pop);
    return () => window.removeEventListener("popstate", pop);
  }, [close]);
  useEffect(() => () => { cancel(); }, [cancel]);

  const f = frozen.current;
  return {
    mode, open, back, refs: { cam, main, plug, bar, idle, status, backBtn },
    mainShown: mode === "map" || !f ? o.main : f.main,
    plugShown: mode === "plugins" || !f ? o.plug : f.plug,
  };
}
