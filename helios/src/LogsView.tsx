import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";

type Entry = { source: string; ts: string | null; level: string; logger: string | null; msg: string; extra: Record<string, unknown> };
type Source = { name: string; size: number; modified: number };

const KEEP = 3000; // lines kept in the view
const LEVELS: [string, string, (l: string) => boolean][] = [
  ["all", "All", () => true],
  ["warn", "Warnings", (l) => /warn|error|critical|fatal/.test(l)],
  ["error", "Errors", (l) => /error|critical|fatal/.test(l)],
];
const LABEL: Record<string, string> = { argus: "argusd", worker: "worker", ollama: "ollama" };
const label = (s: string) => LABEL[s] ?? s;

function time(ts: string | null): string {
  if (!ts) return "";
  const d = new Date(ts);
  return isNaN(d.getTime()) ? ts.slice(11, 19) : d.toLocaleTimeString([], { hour12: false });
}

function extraText(e: Entry): string {
  return Object.entries(e.extra).filter(([k]) => k !== "exc")
    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`).join("  ");
}

// Every process's log in one place: argusd, the worker, Ollama (and a crash log if one of them died on start).
export function LogsView() {
  const [sources, setSources] = useState<Source[]>([]);
  const [pick, setPick] = useState("all");
  const [level, setLevel] = useState("all");
  const [q, setQ] = useState("");
  const [paused, setPaused] = useState(false);
  const [lines, setLines] = useState<Entry[]>([]);
  const [open, setOpen] = useState<number | null>(null);
  const offsets = useRef<Record<string, number>>({});
  const box = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const list = await api<Source[]>("/logs");
        if (stop) return;
        setSources(list);
        if (pausedRef.current) return;
        const fresh: Entry[] = [];
        for (const s of list) {
          const after = offsets.current[s.name];
          const r = await api<{ entries: Entry[]; offset: number }>(
            `/logs/${encodeURIComponent(s.name)}${after === undefined ? "?lines=300" : `?after=${after}`}`);
          offsets.current[s.name] = r.offset;
          fresh.push(...r.entries);
        }
        if (!stop && fresh.length) {
          setLines((old) => {
            const all = [...old, ...fresh];
            // first load: interleave the sources by time; later reads are already newer
            if (!old.length) all.sort((a, b) => (a.ts ? Date.parse(a.ts) : 0) - (b.ts ? Date.parse(b.ts) : 0));
            return all.slice(-KEEP);
          });
        }
      } catch { /* argusd restarting: try again next tick */ }
    };
    tick();
    const i = window.setInterval(tick, 1500);
    return () => { stop = true; window.clearInterval(i); };
  }, []);

  const shown = useMemo(() => {
    const lv = LEVELS.find((x) => x[0] === level)![2];
    const needle = q.trim().toLowerCase();
    return lines.map((e, i) => ({ e, i })).filter(({ e }) => (pick === "all" || e.source === pick) && lv(e.level)
      && (!needle || e.msg.toLowerCase().includes(needle) || extraText(e).toLowerCase().includes(needle)));
  }, [lines, pick, level, q]);

  useEffect(() => { if (stick.current && box.current) box.current.scrollTop = box.current.scrollHeight; }, [shown]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const e of lines) if (/error|critical|fatal/.test(e.level)) c[e.source] = (c[e.source] ?? 0) + 1;
    return c;
  }, [lines]);

  return (
    <section className="panel logs" aria-label="Logs">
      <div className="ph">
        <span className="pt">Logs</span>
        <div className="seg" role="group" aria-label="Source">
          <button type="button" aria-pressed={pick === "all"} onClick={() => setPick("all")}>All</button>
          {sources.map((s) => (
            <button key={s.name} type="button" aria-pressed={pick === s.name} onClick={() => setPick(s.name)}>
              {label(s.name)}{counts[s.name] ? <span className="errn">{counts[s.name]}</span> : null}
            </button>
          ))}
        </div>
        <div className="seg" role="group" aria-label="Level">
          {LEVELS.map(([id, l]) => <button key={id} type="button" aria-pressed={level === id} onClick={() => setLevel(id)}>{l}</button>)}
        </div>
        <input className="search" type="search" placeholder="Search" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search logs" />
        <div className="tools">
          <button type="button" className="btn" aria-pressed={paused} onClick={() => setPaused(!paused)}>{paused ? "Resume" : "Pause"}</button>
          <button type="button" className="btn" onClick={() => setLines([])}>Clear</button>
        </div>
      </div>
      <div className="logbox mono" ref={box} onScroll={(e) => {
        const el = e.currentTarget;
        stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      }}>
        {shown.length === 0 && <div className="tip" style={{ padding: "8px 10px" }}>{sources.length ? "Nothing matches." : "No logs yet. Start Argus with dev.ps1 up."}</div>}
        {shown.map(({ e, i }) => {
          const ex = extraText(e);
          const exc = e.extra.exc ? String(e.extra.exc) : null;
          return (
            <div key={i} className={`ll lv-${e.level}`} onClick={() => setOpen(open === i ? null : i)}>
              <span className="t">{time(e.ts)}</span>
              <span className={`s src-${e.source.replace(/[^a-z]/g, "")}`}>{label(e.source)}</span>
              <span className="l">{(e.level === "warning" ? "warn" : e.level).slice(0, 5).toUpperCase()}</span>
              <span className="m">{e.msg}{ex && <span className="x">  {ex}</span>}</span>
              {(open === i || e.level === "error") && exc && <pre className="exc">{exc}</pre>}
            </div>
          );
        })}
      </div>
    </section>
  );
}
