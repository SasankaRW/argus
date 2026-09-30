import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, Status } from "./api";
import { AriPhase, useAri } from "./ariState";

// Ari's island: a black shape grown out of the top edge of the screen (the PC's popup window).
//   idle     a slim lip under the edge; hover shows "ari"
//   active   it widens: listening / thinking / working / speaking, with what Ari is on
//   done     the answer, two lines, then it folds back
//   click    it opens down into your widgets (clock, status, inbox, next, shortcuts, last answer), set up in Helios
// The outline is one SVG path (concave shoulders into the edge, round bottom corners) driven by a spring, so it
// stays smooth at every size. The window's click area follows through the title: "ari:<mode>|<w>x<h>".

type Mode = "idle" | "active" | "done" | "details";
type Widget = { id: string; on: boolean };
type Shortcut = { label: string; action: string };
type IslandCfg = { widgets: Widget[]; shortcuts: Shortcut[] };
type Sched = { id: string; label: string | null; plugin: string; workflow: string; enabled: boolean; next_run_at: number | null };
type Size = { w: number; h: number };

const WORDS: Record<AriPhase, string> = { idle: "", listening: "listening", thinking: "thinking", working: "on it",
  speaking: "speaking", done: "" };
const SHADOW = 14;  // room around it in the window for the soft shadow

const shoulderOf = (h: number) => Math.min(13, h * 0.55);
const radiusOf = (h: number, sh: number, big: boolean) => Math.max(2, Math.min(big ? 26 : 20, h - sh, h / 2));

// The outline: top edge across both shoulders, concave curve down into each side, round bottom corners.
function outline(w: number, h: number, big: boolean): string {
  const sh = shoulderOf(h);
  const r = radiusOf(h, sh, big);
  const x0 = sh, x1 = sh + w, k = 0.55;
  return [
    `M0,0 H${x1 + sh}`,
    `C${x1 + sh * (1 - k)},0 ${x1},${sh * k} ${x1},${sh}`,
    `V${h - r}`,
    `C${x1},${h - r * (1 - k)} ${x1 - r * (1 - k)},${h} ${x1 - r},${h}`,
    `H${x0 + r}`,
    `C${x0 + r * (1 - k)},${h} ${x0},${h - r * (1 - k)} ${x0},${h - r}`,
    `V${sh}`,
    `C${x0},${sh * k} ${sh * (1 - k)},0 0,0 Z`,
  ].join(" ");
}

// A spring towards the target size (a little overshoot, then it settles), one frame at a time.
function useSpring(target: Size): Size {
  const [v, setV] = useState(target);
  const st = useRef({ w: target.w, h: target.h, vw: 0, vh: 0 });
  useEffect(() => {
    let raf = 0;
    let last = performance.now();
    const K = 260, C = 26;  // stiffness, damping (a bit under critical: a small overshoot)
    const tick = (now: number) => {
      const dt = Math.min(0.032, (now - last) / 1000);
      last = now;
      const s = st.current;
      for (let i = 0; i < 2; i++) {  // two half steps: steadier at low frame rates
        const d = dt / 2;
        s.vw += (K * (target.w - s.w) - C * s.vw) * d; s.w += s.vw * d;
        s.vh += (K * (target.h - s.h) - C * s.vh) * d; s.h += s.vh * d;
      }
      const done = Math.abs(target.w - s.w) < 0.3 && Math.abs(target.h - s.h) < 0.3 && Math.abs(s.vw) + Math.abs(s.vh) < 2;
      if (done) { s.w = target.w; s.h = target.h; s.vw = s.vh = 0; }
      setV({ w: s.w, h: s.h });
      if (!done) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target.w, target.h]);
  return v;
}

export function Island() {
  const s = useAri();
  const [details, setDetails] = useState(false);
  const [hover, setHover] = useState(false);
  const [cfg, setCfg] = useState<IslandCfg | null>(null);
  const [info, setInfo] = useState<{ st: Status | null; waiting: number; next: Sched | null; last: string | null }>({ st: null, waiting: 0, next: null, last: null });
  const [flash, setFlash] = useState<string | null>(null);
  const [now, setNow] = useState(() => new Date());
  const body = useRef<HTMLDivElement>(null);
  const [bodyH, setBodyH] = useState(0);
  const leave = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const mode: Mode = details ? "details" : s.phase === "idle" ? "idle" : s.phase === "done" ? "done" : "active";

  useEffect(() => {  // open: your set-up and the numbers, refreshed while it stays open
    if (!details) return;
    const load = () => {
      api<IslandCfg>("/island").then(setCfg).catch(() => {});
      api<Status>("/status").then((st) => setInfo((x) => ({ ...x, st }))).catch(() => {});
      api<{ items: unknown[] }>("/inbox").then((d) => setInfo((x) => ({ ...x, waiting: d.items.length }))).catch(() => {});
      api<Sched[]>("/schedules").then((l) => {
        const next = l.filter((x) => x.enabled && x.next_run_at).sort((a, b) => a.next_run_at! - b.next_run_at!)[0] ?? null;
        setInfo((x) => ({ ...x, next }));
      }).catch(() => {});
      api<{ conv: string }[]>("/ari-chats").then(async (c) => {
        if (!c[0]) return;
        const r = await api<{ turns: { role: string; text: string | null }[] }>(`/ari/${c[0].conv}`);
        const a = [...r.turns].reverse().find((t) => t.role === "ari" && t.text);
        setInfo((x) => ({ ...x, last: a?.text?.trim() ?? null }));
      }).catch(() => {});
    };
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [details]);

  useEffect(() => {  // the clock
    if (!details) return;
    setNow(new Date());
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, [details]);

  useLayoutEffect(() => {  // the details' own height, kept up to date while the island opens around it
    const el = body.current;
    if (!el) { setBodyH(0); return; }
    const measure = () => setBodyH(Math.ceil(el.offsetHeight));
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [details]);

  const target: Size = mode === "details" ? { w: 440, h: Math.min(340, Math.max(96, bodyH)) }
    : mode === "done" ? { w: 460, h: 68 }
    : mode === "active" ? { w: Math.round(Math.min(440, Math.max(260, 150 + s.text.length * 6.4))), h: 40 }
    : hover ? { w: 150, h: 24 } : { w: 104, h: 9 };
  const cur = useSpring(target);
  const big = mode === "details";
  const sh = shoulderOf(cur.h);
  const r = radiusOf(cur.h, sh, big);

  const area = (m: Mode) => `ari:${m}|${Math.ceil(Math.max(target.w, cur.w) + 2 * 13 + 2 * SHADOW)}x${Math.ceil(Math.max(target.h, cur.h) + SHADOW)}`;
  useEffect(() => { if (!document.title.startsWith("ari:go|")) document.title = area(mode); },
    [mode, target.w, target.h]); // eslint-disable-line react-hooks/exhaustive-deps

  const go = (hash: string) => {  // open Helios (or a website) through the window
    document.title = `ari:go|${hash}`;
    setDetails(false);
    setTimeout(() => { document.title = area("idle"); }, 400);
  };
  const run = async (sc: Shortcut) => {
    if (sc.action.startsWith("show:")) { go(sc.action.slice(5) === "map" ? "" : sc.action.slice(5)); return; }
    if (sc.action.startsWith("url:")) { go(sc.action); return; }
    setFlash(`${sc.label}…`);
    try {
      await api("/island/run", { method: "POST", body: JSON.stringify({ action: sc.action }) });
      setFlash(`${sc.label} ✓`);
    } catch (e) { setFlash(`${sc.label}: ${e instanceof Error ? e.message : "failed"}`); }
    setTimeout(() => setFlash(null), 2600);
  };

  const tone = s.phase === "listening" || s.phase === "done" ? "tg" : s.phase === "working" ? "flow" : "amber";
  const j = info.st?.jobs ?? {};
  const running = (j.running ?? 0) + (j.leased ?? 0);
  const queued = (j.queued ?? 0) + (j.retry ?? 0);
  const workersOn = info.st?.workers.filter((w) => w.state === "online").length ?? 0;
  const healthy = info.st?.status === "ok";
  const nextAt = info.next?.next_run_at ? new Date(info.next.next_run_at * 1000) : null;

  const widget = (id: string) => {
    switch (id) {
      case "clock":
        return (
          <div key={id} className="iw-clock">
            <b>{now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}</b>
            <span>{now.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" })}</span>
          </div>
        );
      case "status":
        return (
          <div key={id} className="iw-line">
            <i className={`iw-dot ${info.st ? (healthy ? "ok" : "amber") : "dim"}`} />
            <span>{info.st ? (healthy ? "all good" : "needs a look") : "…"}</span>
            <span className="iw-dim">{running} running · {queued} queued · {workersOn} worker{workersOn === 1 ? "" : "s"} on</span>
          </div>
        );
      case "inbox":
        return (
          <button key={id} type="button" className="iw-line iw-link" onClick={() => go("inbox")}>
            <i className={`iw-dot ${info.waiting ? "amber" : "dim"}`} />
            <span>{info.waiting ? `${info.waiting} waiting for you` : "nothing waiting"}</span>
            <span className="iw-dim">inbox ›</span>
          </button>
        );
      case "next":
        return info.next && nextAt ? (
          <div key={id} className="iw-line">
            <i className="iw-dot flow" />
            <span>{info.next.label ?? `${info.next.plugin} · ${info.next.workflow}`}</span>
            <span className="iw-dim">{nextAt.toDateString() === now.toDateString() ? "" : `${nextAt.toLocaleDateString(undefined, { weekday: "short" })} `}{nextAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}</span>
          </div>
        ) : null;
      case "shortcuts":
        return cfg && cfg.shortcuts.length > 0 ? (
          <div key={id} className="iw-shortcuts">
            {cfg.shortcuts.map((sc) => <button key={sc.label + sc.action} type="button" onClick={() => run(sc)}>{sc.label}</button>)}
          </div>
        ) : null;
      case "last":
        return info.last ? <p key={id} className="iw-last"><span>ari ›</span> {info.last}</p> : null;
      default:
        return null;
    }
  };

  return (
    <div className="island-wrap">
      <div
        className={`island m-${mode} t-${tone}`}
        style={{ width: cur.w + 2 * sh, height: cur.h }}
        role="button"
        tabIndex={0}
        aria-label={mode === "details" ? "Ari: close" : `Ari${s.phase !== "idle" ? `: ${WORDS[s.phase] || "done"}` : ""}. Open`}
        onClick={(e) => { if ((e.target as HTMLElement).closest("button")) return; setDetails(!details); }}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") setDetails(!details); if (e.key === "Escape") setDetails(false); }}
        onMouseEnter={() => { clearTimeout(leave.current); setHover(true); }}
        onMouseLeave={() => { setHover(false); if (details) leave.current = setTimeout(() => setDetails(false), 3500); }}
      >
        <svg className="isl-shape" width={cur.w + 2 * sh} height={cur.h} aria-hidden="true">
          <defs>
            <linearGradient id="islfill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stopColor="#000" /><stop offset=".6" stopColor="#030303" /><stop offset="1" stopColor="#0b0b0c" />
            </linearGradient>
          </defs>
          <path d={outline(cur.w, cur.h, big)} fill="url(#islfill)" />
        </svg>
        <div className="isl-body" style={{ left: sh, width: cur.w, height: cur.h, borderRadius: `0 0 ${r}px ${r}px` }}>
          {mode === "idle" && hover && <span className="isl-idle">ari</span>}
          {(mode === "active" || mode === "done") && (
            <div className="isl-row">
              <Indicator phase={s.phase} />
              <div className="isl-text">
                {mode === "active" && <b>{WORDS[s.phase]}</b>}
                <span>{s.text || (s.phase === "listening" ? "say what you need" : "")}</span>
              </div>
            </div>
          )}
          {mode === "details" && (
            <div ref={body} className="isl-details">
              {(cfg?.widgets ?? []).filter((w) => w.on).map((w) => widget(w.id))}
              {!cfg && <div className="iw-dim">…</div>}
              <div className="iw-foot">
                {flash ? <span className="iw-flash">{flash}</span> : s.phase !== "idle" ? <span className="iw-dim">ari is {WORDS[s.phase] || "done"}</span> : <span />}
                <button type="button" className="iw-edit" onClick={() => go("ari")}>customize ›</button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function Indicator({ phase }: { phase: AriPhase }) {
  if (phase === "listening" || phase === "speaking") {
    return <span className={`isl-ind bars ${phase}`} aria-hidden="true"><i /><i /><i /><i /><i /></span>;
  }
  if (phase === "thinking" || phase === "working") return <span className={`isl-ind ring ${phase}`} aria-hidden="true"><i /></span>;
  return <span className="isl-ind dot" aria-hidden="true" />;
}
