import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api } from "./api";
import { AriPhase, useAri } from "./ariState";

// Ari's island: a black shape that grows out of the top edge of the screen (the PC's popup window), like a notch.
//   idle     a slim lip under the edge; hover shows "ari"
//   active   it widens: listening / thinking / working / speaking, with what Ari is on
//   done     the answer, two lines, then it folds back
//   click    it opens down into details: the last question and answer, what waits for you, shortcuts
// The shoulders curve into the edge so it reads as part of the screen, not a floating pill. The window sizes its
// click area to the island (see argus/ari_popup.py) through the page title: "ari:<mode>|<w>x<h>".

type Mode = "idle" | "active" | "done" | "details";
type Turn = { role: "you" | "ari"; text: string | null };

const WORDS: Record<AriPhase, string> = { idle: "", listening: "listening", thinking: "thinking", working: "on it",
  speaking: "speaking", done: "" };
const SHOULDER = 14;  // the curve into the screen's edge, each side
const SHADOW = 12;    // room under it for the soft shadow

export function Island() {
  const s = useAri();
  const [details, setDetails] = useState(false);
  const [hover, setHover] = useState(false);
  const [last, setLast] = useState<{ conv: string; you: string | null; ari: string | null } | null>(null);
  const [waiting, setWaiting] = useState(0);
  const body = useRef<HTMLDivElement>(null);
  const [bodyH, setBodyH] = useState(0);
  const leave = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const mode: Mode = details ? "details" : s.phase === "idle" ? "idle" : s.phase === "done" ? "done" : "active";

  useEffect(() => {  // opening the details: fetch the last exchange and what waits
    if (!details) return;
    api<{ conv: string }[]>("/ari-chats").then(async (chats) => {
      if (!chats[0]) { setLast(null); return; }
      const r = await api<{ turns: Turn[] }>(`/ari/${chats[0].conv}`);
      const said = [...r.turns].reverse();
      setLast({ conv: chats[0].conv, you: said.find((t) => t.role === "you")?.text ?? null,
        ari: said.find((t) => t.role === "ari" && t.text)?.text ?? null });
    }).catch(() => setLast(null));
    api<{ items: unknown[] }>("/inbox").then((d) => setWaiting(d.items.length)).catch(() => {});
  }, [details, s.phase]);

  useLayoutEffect(() => {  // the details' own height, kept up to date while the island widens around it
    const el = body.current;
    if (!el) { setBodyH(0); return; }
    const measure = () => setBodyH(Math.ceil(el.offsetHeight));
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [details]);

  const size = (() => {
    if (mode === "details") return { w: 520, h: Math.min(320, Math.max(110, bodyH)) };
    if (mode === "done") return { w: 460, h: 68 };
    if (mode === "active") return { w: Math.min(440, Math.max(260, 150 + s.text.length * 6.4)), h: 40 };
    return hover ? { w: 150, h: 24 } : { w: 104, h: 7 };
  })();

  useEffect(() => {  // tell the window where the island is (its click area), and whether it's "open" at all
    const w = size.w + 2 * SHOULDER + 2 * SHADOW;
    const h = size.h + SHADOW;
    const t = `ari:${mode}|${w}x${h}`;
    if (!document.title.startsWith("ari:go|")) document.title = t;
  }, [mode, size.w, size.h]);

  const go = (hash: string) => {
    document.title = `ari:go|${hash}`;
    setDetails(false);
    setTimeout(() => { document.title = `ari:${mode}|${size.w + 2 * SHOULDER + 2 * SHADOW}x${size.h + SHADOW}`; }, 400);
  };

  const tone = s.phase === "listening" ? "tg" : s.phase === "working" ? "flow" : s.phase === "done" ? "tg" : "amber";
  const r = Math.min(size.h / 2, mode === "details" ? 26 : 22);
  return (
    <div className="island-wrap">
      <div
        className={`island m-${mode} t-${tone}`}
        style={{ width: size.w, height: size.h, borderRadius: `0 0 ${r}px ${r}px`,
          ["--sh" as string]: `${Math.min(SHOULDER, size.h)}px` }}
        role="button"
        tabIndex={0}
        aria-label={mode === "details" ? "Ari: close" : `Ari${s.phase !== "idle" ? `: ${WORDS[s.phase] || "done"}` : ""}. Open details`}
        onClick={(e) => { if ((e.target as HTMLElement).closest("button")) return; setDetails(!details); }}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") setDetails(!details); if (e.key === "Escape") setDetails(false); }}
        onMouseEnter={() => { clearTimeout(leave.current); setHover(true); }}
        onMouseLeave={() => { setHover(false); if (details) leave.current = setTimeout(() => setDetails(false), 4000); }}
      >
        <i className="shoulder l" aria-hidden="true" /><i className="shoulder r" aria-hidden="true" />
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
            <div className="isl-head">
              <Indicator phase={s.phase === "idle" ? "done" : s.phase} still={s.phase === "idle"} />
              <b>ari</b>
              <span className="isl-dim">{s.phase === "idle" ? "ready" : WORDS[s.phase] || "done"}</span>
              <button type="button" className="isl-x" aria-label="Close" onClick={() => setDetails(false)}>×</button>
            </div>
            {last ? (
              <div className="isl-chat">
                {last.you && <p className="you"><span>you ›</span> {last.you.trim()}</p>}
                {last.ari && <p className="ari"><span>ari ›</span> {last.ari.trim()}</p>}
              </div>
            ) : <p className="isl-dim">No chats yet. Say "Hey Ari".</p>}
            <div className="isl-actions">
              {last && <button type="button" onClick={() => go(`ari/${last.conv}`)}>open chat</button>}
              <button type="button" onClick={() => go("ari")}>new chat</button>
              <button type="button" className={waiting ? "warn" : ""} onClick={() => go("inbox")}>inbox{waiting ? ` · ${waiting}` : ""}</button>
              <button type="button" onClick={() => go("")}>helios ›</button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function Indicator({ phase, still = false }: { phase: AriPhase; still?: boolean }) {
  if (phase === "listening" || phase === "speaking") {
    return <span className={`isl-ind bars ${phase}`} aria-hidden="true"><i /><i /><i /><i /><i /></span>;
  }
  if (phase === "thinking" || phase === "working") return <span className={`isl-ind ring ${phase}`} aria-hidden="true"><i /></span>;
  return <span className={`isl-ind dot${still ? " still" : ""}`} aria-hidden="true" />;
}
