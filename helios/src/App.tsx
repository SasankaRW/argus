import { useCallback, useEffect, useRef, useState } from "react";
import { api, setToken, Status, useTimeSaved } from "./api";
import { AriView } from "./AriView";
import { AskBox } from "./AskBox";
import { EventsPanel } from "./EventsPanel";
import { uptime } from "./format";
import { Inspector } from "./Inspector";
import { useArgus } from "./live";
import { mainParts, MapView, mergeEdges, Selection } from "./MapView";
import { LogsView } from "./LogsView";
import { MapFull } from "./MapFull";
import { PhoneHeader, PhoneHome, PhoneInbox, PhoneMore, PhoneTab, PhoneTabs } from "./Phone";
import { Sheet } from "./Sheet";
import { PluginsView } from "./PluginsView";
import { PowerView } from "./PowerView";
import { QueueView } from "./QueueView";
import { RulesView } from "./RulesView";
import { RunsView } from "./RunsView";
import { ShareView } from "./ShareView";
import { pref, setPref, setVoiceStatus, voiceStatus, VoiceStatus, WakeListener } from "./voice";
import { AriPill } from "./AriPill";
import { ariFromEvents, ariSet, ariTell } from "./ariState";

const NAV: { id: string; label: string; icon: string }[] = [
  { id: "map", label: "Live map", icon: "M3 6l6-3 6 3 6-3v15l-6 3-6-3-6 3zM9 3v15M15 6v15" },
  { id: "ari", label: "Ari", icon: "M4 5h16v11H9l-5 4zM8 10h.01M12 10h.01M16 10h.01" },
  { id: "plugins", label: "Plugins", icon: "M9 3v4M15 3v4M6 7h12v5a6 6 0 0 1-12 0zM12 18v3" },
  { id: "queue", label: "Queue", icon: "M4 6h10M4 12h10M4 18h10M18 6l2 2-2 2M18 14l2 2-2 2" },
  { id: "runs", label: "Runs", icon: "M4 6h16M4 12h11M4 18h14" },
  { id: "share", label: "Share", icon: "M12 15V3M7 8l5-5 5 5M5 13v6h14v-6" },
  { id: "power", label: "Power", icon: "M12 3v8M7.5 6.5a7 7 0 1 0 9 0" },
  { id: "logs", label: "Logs", icon: "M5 4h14v16H5zM8 8h8M8 12h8M8 16h5" },
];

function Icon({ d }: { d: string }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={d} />
    </svg>
  );
}

function Logo() {
  return (
    <svg width="22" height="22" viewBox="0 0 26 26" fill="none" aria-hidden="true">
      <circle cx="13" cy="13" r="5" fill="#F5A524" />
      <path d="M13 2v3M13 21v3M2 13h3M21 13h3M5.2 5.2l2.1 2.1M18.7 18.7l2.1 2.1M5.2 20.8l2.1-2.1M18.7 7.3l2.1-2.1" stroke="#F5A524" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}

// Phones (narrow screens) get the simple view; "Open the full dashboard" switches, remembered per browser.
function useNarrow(): boolean {
  const q = "(max-width: 720px)";
  const [m, setM] = useState(() => window.matchMedia(q).matches);
  useEffect(() => {
    const mm = window.matchMedia(q);
    const on = () => setM(mm.matches);
    mm.addEventListener("change", on);
    return () => mm.removeEventListener("change", on);
  }, []);
  return m;
}

function Clock() {
  const [t, setT] = useState(new Date());
  useEffect(() => { const i = window.setInterval(() => setT(new Date()), 1000); return () => window.clearInterval(i); }, []);
  return <b>{t.toLocaleTimeString([], { hour12: false })}</b>;
}

function Kpis({ status, hud = false }: { status: Status | null; hud?: boolean }) {
  const saved = useTimeSaved();
  const j = status?.jobs ?? {};
  const running = (j.leased ?? 0) + (j.running ?? 0);
  const queued = (j.queued ?? 0) + (j.retry ?? 0);
  const online = status?.workers.filter((w) => w.state === "online").length ?? 0;
  const tiles: [string, string | number, string, string][] = [
    ["run", running, queued ? `${queued} queued` : "queue empty", running ? "var(--flow)" : "var(--tx3)"],
    ["wait", j.waiting ?? 0, "waiting for approval or a resume", (j.waiting ?? 0) ? "var(--amber)" : "var(--tx3)"],
    ["ok", j.succeeded ?? 0, "succeeded, all time", "var(--ok)"],
    ["dead", j.dead ?? 0, "failed after retries", (j.dead ?? 0) ? "var(--bad)" : "var(--tx3)"],
    ["workers", status ? `${online}/${status.workers.length}` : "…", status ? `${status.workers.length - online} offline` : "", online ? "var(--ok)" : "var(--bad)"],
    ["saved", saved ? (saved.seconds ? saved.text : "—") : "…",
      saved?.plugins[0] ? `this week · most by ${saved.plugins[0].plugin}` : "time saved this week", saved?.seconds ? "var(--ok)" : "var(--tx3)"],
  ];
  return (
    <section className={`kstrip${hud ? " float" : ""}`} aria-label="Jobs">
      {tiles.map(([l, v, s, c]) => (
        <div key={l} className="kc" title={s}>
          <span className="kl"><span className="dot" style={{ background: c }} />{l}</span>
          <Num v={v} color={c === "var(--tx3)" ? undefined : c} />
          {!hud && <span className="ks">{s}</span>}
        </div>
      ))}
    </section>
  );
}

// A KPI value that flashes once when it changes.
function Num({ v, color }: { v: string | number; color?: string }) {
  const ref = useRef<HTMLElement>(null);
  const prev = useRef(v);
  useEffect(() => {
    if (prev.current === v) return;
    prev.current = v;
    const el = ref.current;
    if (el) { el.classList.remove("bump"); void el.offsetWidth; el.classList.add("bump"); }
  }, [v]);
  return <b ref={ref} className="mono" style={{ color }}>{v}</b>;
}

// Claude today: calls used of the daily cap, and whether the CLI is logged in (checked by the workers).
function ClaudeChip() {
  const [c, setC] = useState<{ calls_today: number; calls_per_day: number; logged_in: boolean | null } | null>(null);
  useEffect(() => {
    const load = () => api<{ claude: NonNullable<typeof c> }>("/models").then((m) => setC(m.claude)).catch(() => {});
    load();
    const id = setInterval(load, 60000);
    return () => clearInterval(id);
  }, []);
  if (!c) return null;
  if (c.logged_in === false) {
    return <span className="seg-s claude-chip bad" title="Run `claude` on the PC and log in (/login)"><span className="dot" style={{ background: "var(--bad)" }} />claude logged out</span>;
  }
  const left = c.calls_per_day - c.calls_today;
  return (
    <span className="seg-s claude-chip hide-sm" title={`Claude calls today: ${c.calls_today} of ${c.calls_per_day}`}>
      claude <b>{c.calls_today}/{c.calls_per_day}</b>
      {left <= 3 && <span className={left <= 0 ? "bad" : "warn"}>{left <= 0 ? " · none left" : ` · ${left} left`}</span>}
    </span>
  );
}

function Login({ onDone, bad }: { onDone: () => void; bad: boolean }) {
  const [v, setV] = useState("");
  return (
    <div className="login">
      <form className="panel card" onSubmit={(e) => { e.preventDefault(); setToken(v.trim()); onDone(); }}>
        <div className="brand"><Logo /><b>Helios</b></div>
        <p>Argus needs its worker token (ARGUS_WORKER_TOKEN in <code>.env</code>). It is kept in this browser only.</p>
        {bad && <p className="bad">That token did not work.</p>}
        <input autoFocus type="password" value={v} onChange={(e) => setV(e.target.value)} placeholder="Token" aria-label="Token" />
        <button type="submit" className="primary" disabled={!v.trim()}>Open Helios</button>
      </form>
    </div>
  );
}

export function App() {
  const a = useArgus();
  const [sel, setSel] = useState<Selection>(null);
  const [flow, setFlow] = useState(true);
  const [relayout, setRelayout] = useState(0);
  const [info, setInfo] = useState(false);
  const [bigMap, setBigMap] = useState(false);
  const [inbox, setInbox] = useState(false);  // the phone's "waiting for you" sheet  // the phone's sideways full-screen map  // the map's inspector drawer with nothing selected (the overview)
  const [wide, setWide] = useState(() => { try { return localStorage.getItem("helios.rail") === "wide"; } catch { return false; } });
  const setWideRail = (v: boolean) => { setWide(v); try { localStorage.setItem("helios.rail", v ? "wide" : "icons"); } catch { /* private */ } };
  const [triedLogin, setTriedLogin] = useState(false);
  const parse = (h: string) => (["queue", "runs", "logs", "share", "power", "ari", "plugins", "more"].includes(h) || h.startsWith("rules/") || h.startsWith("plugins/") ? h : "map");
  const [view, setView] = useState<string>(() => parse(location.hash.slice(1)));
  useEffect(() => {  // the share menu opens /helios/#share on a page that may already be open
    const on = () => setView(parse(location.hash.slice(1)));
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const narrow = useNarrow();
  const [full, setFull] = useState(() => { try { return localStorage.getItem("helios.full") === "1"; } catch { return false; } });
  const setFullView = (v: boolean) => { setFull(v); try { localStorage.setItem("helios.full", v ? "1" : "0"); } catch { /* private */ } };
  const phone = narrow && !full;
  const go = (v: string) => { setView(v); history.replaceState(null, "", v === "map" ? location.pathname : `#${v}`); };
  const viewRef = useRef(view);
  viewRef.current = view;
  const [heard, setHeard] = useState<string | null>(null);
  useEffect(() => {  // "Hey Ari" while Helios is open (Ari > "Hey Ari" turns it on)
    let wl: WakeListener | null = null;
    let hide: ReturnType<typeof setTimeout> | undefined;
    const badge = (msg: string | null, ms = 8000) => {
      clearTimeout(hide); setHeard(msg); if (msg) hide = setTimeout(() => setHeard(null), ms);
    };
    const sync = () => {
      const want = pref("wake", false);
      if (want && !wl) {
        wl = new WakeListener({
          onCommand: (text) => {
            badge(null);
            ariSet("thinking", text);
            if (viewRef.current === "ari") window.dispatchEvent(new CustomEvent("ari-command", { detail: text }));
            else { try { sessionStorage.setItem("ari.command", text); } catch { /* private */ } setView("ari"); history.replaceState(null, "", "#ari"); }
          },
          onWake: () => ariTell("listening"),
          onError: (msg) => { badge(msg, 15000); setPref("wake", false); },
        });
        wl.start();
      } else if (!want && wl) { wl.stop(); wl = null; }
    };
    const restart = () => { if (wl) { wl.stop(); wl = null; } sync(); };  // Whisper became (un)available
    const arm = () => wl?.arm();
    api<VoiceStatus>("/ari-voice").then(setVoiceStatus).catch(() => {}).finally(sync);
    window.addEventListener("ari-pref", sync);
    window.addEventListener("ari-status", restart);
    window.addEventListener("ari-arm", arm);
    return () => {
      window.removeEventListener("ari-pref", sync); window.removeEventListener("ari-status", restart);
      window.removeEventListener("ari-arm", arm); wl?.stop(); clearTimeout(hide);
    };
  }, []);
  // The Ari pill follows Ari from the event stream (answers from any screen, the PC's "Hey Ari"). On the PC that
  // runs the Ari popup, the popup shows it over everything instead.
  const seen = useRef(0);
  useEffect(() => {
    const fresh = a.events.filter((e) => e.seq > seen.current);
    if (!fresh.length) return;
    const first = seen.current === 0;
    seen.current = fresh[fresh.length - 1].seq;
    if (!first) ariFromEvents(fresh);  // not the replay of old events on load
  }, [a.events]);
  const [popupHere, setPopupHere] = useState(false);
  useEffect(() => {
    const on = () => setPopupHere(!!voiceStatus().popup_here);
    const t = setInterval(() => api<VoiceStatus>("/ari-voice").then((v) => setPopupHere(!!v.popup_here)).catch(() => {}), 60000);
    window.addEventListener("ari-status", on);
    return () => { clearInterval(t); window.removeEventListener("ari-status", on); };
  }, []);
  const heardBadge = <>
    {heard && <div className="ari-heard" role="status">{heard}</div>}
    {!popupHere && <AriPill onOpen={() => go("ari")} />}
  </>;
  const closeMap = useCallback(() => setBigMap(false), []);
  const mapFull = bigMap && a.map ? (
    <MapFull map={a.map} pulses={a.pulses} active={a.active} status={a.status} events={a.events} onClose={closeMap}
      onPlugins={() => { setBigMap(false); setFullView(true); go("plugins"); }} />
  ) : null;

  if (a.phase === "login") return <Login bad={triedLogin} onDone={() => { setTriedLogin(true); a.reconnect(); }} />;

  const st = a.status;
  const ok = st?.status === "ok" && a.conn === "live";
  if (phone) {  // the phone: terminal widgets (Phone.tsx), a floating tab bar, details in a bottom sheet
    const proot = view.split("/")[0];
    const tab: PhoneTab = proot === "map" ? "home" : proot === "ari" ? "ari" : proot === "plugins" ? "plug" : "more";
    const open = (v: string) => go(v);
    return (
      <div className="phone2">
        <div className="glow g1" aria-hidden="true" /><div className="glow g2" aria-hidden="true" />
        <PhoneHeader status={st} live={a.conn === "live"} />
        <main key={view} className="pmain2">
          {proot === "ari" ? <AriView onSelect={setSel} onView={open} />
          : proot === "plugins" ? (
            <PluginsView phone selected={view.includes("/") ? decodeURIComponent(view.split("/")[1]) : null}
              onPick={(id) => go(id ? `plugins/${encodeURIComponent(id)}` : "plugins")} onSelect={setSel} />
          )
          : proot === "share" ? <ShareView onJob={(id) => setSel({ type: "job", id })} onDone={() => go("map")} />
          : proot === "queue" ? <QueueView events={a.events} onSelect={setSel} />
          : proot === "runs" ? <RunsView events={a.events} selected={null} onSelect={setSel} />
          : proot === "power" ? <PowerView events={a.events} onSelect={setSel} />
          : proot === "logs" ? <LogsView />
          : proot === "more" ? <PhoneMore onOpen={open} onFull={() => setFullView(true)} />
          : <PhoneHome status={st} events={a.events} onSelect={setSel} onOpen={open} onInbox={() => setInbox(true)}
              onAri={(mic) => { if (mic) { try { sessionStorage.setItem("ari.mic", "1"); } catch { /* private */ } } go("ari"); }} />}
        </main>
        <PhoneTabs tab={tab} onTab={(t) => (t === "map" ? setBigMap(true) : go(t === "home" ? "map" : t === "plug" ? "plugins" : t))} />
        {sel && a.map && (
          <Sheet onClose={() => setSel(null)}>
            <Inspector sel={sel} map={a.map} status={st} events={a.events} onSelect={setSel} />
          </Sheet>
        )}
        {inbox && <Sheet label="Waiting for you" onClose={() => setInbox(false)}><PhoneInbox onDone={() => setInbox(false)} /></Sheet>}
        {mapFull}
        {heardBadge}
      </div>
    );
  }
  const root = view.split("/")[0];
  const main = a.map ? mainParts(a.map, [], {}).map : null;
  const withKpis = ["queue", "runs", "power"].includes(root);
  const withDock = !["logs", "share", "plugins", "rules"].includes(root);
  const health = a.phase === "down" ? "unreachable" : ok ? "healthy" : a.conn === "live" ? "degraded" : "connecting";
  return (
    <div className={`shell${wide ? " wide" : ""}`}>
      <header className="top">
        <div className="brand"><Logo /><b>helios</b>{st && <span className="crumb hide-sm">▸ {st.instance}@{st.host}</span>}</div>
        <AskBox onSelect={setSel} onView={(v) => go(v)} />
        {narrow && <button type="button" className="btn phone-back" onClick={() => setFullView(false)}>phone view</button>}
        <div className="status mono">
          <span className={`seg-s ${a.phase === "down" ? "bad" : ok ? "ok" : "warn"}`}>
            <span className={`dot${ok ? " pulse" : ""}`} style={{ background: a.phase === "down" ? "var(--bad)" : ok ? "var(--ok)" : "var(--amber)" }} />{health}
          </span>
          {st && <span className="seg-s hide-sm">up <b>{uptime(st.uptime_seconds)}</b></span>}
          <ClaudeChip />
          {st && <span className="seg-s hide-sm">v<b>{st.version}</b></span>}
          <span className="seg-s hide-sm"><Clock /></span>
        </div>
      </header>

      <nav className="rail" aria-label="Helios sections">
        {NAV.map((n) => (
          <button key={n.id} type="button" className="nav" title={n.label} aria-current={n.id === root ? "page" : undefined} onClick={() => go(n.id)}>
            <Icon d={n.icon} /><span className="nl">{n.label}</span>
          </button>
        ))}
        <div className="foot">
          <span className="nl">
            <a href="/lite">lite</a> · <a href="/docs">api</a>
            {narrow && <> · <button type="button" className="linkbtn" onClick={() => setFullView(false)}>phone</button></>}
          </span>
          <button type="button" className="nav" title={wide ? "Collapse the menu" : "Show labels"} onClick={() => setWideRail(!wide)} aria-pressed={wide}>
            <Icon d={wide ? "M15 6l-6 6 6 6" : "M9 6l6 6-6 6"} /><span className="nl">Collapse</span>
          </button>
        </div>
      </nav>

      <main>
        <div key={root} className={`page${root === "map" ? " stagepage" : ""}`}>
        {withKpis && <Kpis status={st} />}
        {view.startsWith("rules/") ? <RulesView plugin={decodeURIComponent(view.slice(6))} onBack={() => history.back()} />
        : root === "plugins" ? (
          <PluginsView selected={view.includes("/") ? decodeURIComponent(view.split("/")[1]) : null}
            onPick={(id) => go(id ? `plugins/${encodeURIComponent(id)}` : "plugins")}
            onSelect={(s) => { setSel(s); go("runs"); }} />
        )
        : view === "share" ? <ShareView onJob={(id) => { setSel({ type: "job", id }); go("runs"); }} onDone={() => go("map")} />
        : view === "logs" ? <LogsView />
        : root === "map" ? (
          <section className="stage" aria-label="Live map">
            <div className="canvas">
              {a.map && a.map.nodes.length > 0 ? (
                <MapView map={a.map} pulses={a.pulses} active={a.active} selection={sel} onSelect={setSel} flow={flow} relayoutSignal={relayout} onPlugins={() => go("plugins")} />
              ) : (
                <div className="empty mono">{a.phase === "down" ? "$ waiting for argus…" : "$ loading the map…"}</div>
              )}
            </div>
            <div className="hud hud-tl">
              <div className="hud-title mono"><span className="pt">live map</span><span className="dim">{main ? `${main.nodes.length} parts · ${mergeEdges(main.edges).length} lines` : "loading…"}</span></div>
              <Kpis status={st} hud />
            </div>
            <div className="hud hud-tr tools">
              <button type="button" className="btn" aria-pressed={flow} onClick={() => setFlow(!flow)}>flow</button>
              <button type="button" className="btn" onClick={() => setRelayout((x) => x + 1)}>auto layout</button>
              {narrow && <button type="button" className="btn" onClick={() => setBigMap(true)}>full screen</button>}
              <button type="button" className="btn" aria-pressed={info || !!sel} onClick={() => (sel || info ? (setSel(null), setInfo(false)) : setInfo(true))}>inspector</button>
            </div>
            <div className="hud hud-bl legend mono">
              <span><i style={{ background: "var(--flow)" }} />message</span>
              <span><i style={{ background: "var(--amber)" }} />escalation</span>
              <span><i style={{ background: "var(--bad)" }} />failure</span>
              <span><i className="thick" />busier line</span>
              <span className="dim hide-sm">drag to arrange · click to inspect</span>
            </div>
            {a.map && (sel || info) && (
              <aside className="drawer">
                <Inspector sel={sel} map={a.map} status={st} events={a.events} onSelect={setSel} onClose={() => { setSel(null); setInfo(false); }} />
              </aside>
            )}
          </section>
        ) : (
        <div className="mid">
          {view === "ari" ? (
            <AriView onSelect={setSel} onView={(v) => go(v)} />
          ) : view === "power" ? (
            <PowerView events={a.events} onSelect={setSel} />
          ) : view === "queue" ? (
            <QueueView events={a.events} onSelect={setSel} />
          ) : (
            <RunsView events={a.events} selected={sel?.type === "job" ? sel.id : null} onSelect={setSel} />
          )}
          {a.map && <Inspector sel={sel} map={a.map} status={st} events={a.events} onSelect={setSel} />}
        </div>
        )}
        </div>
        {withDock && <EventsPanel events={a.events} conn={a.conn} onSelect={setSel} />}
      </main>
      {mapFull}
      {heardBadge}
    </div>
  );
}
