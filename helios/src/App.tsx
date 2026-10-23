import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, setToken } from "./api";
import { currentConv } from "./conv";
import { AskBox } from "./AskBox";
import { EventsPanel } from "./EventsPanel";
import { uptime } from "./format";
import { Inspector } from "./Inspector";
import { useArgus } from "./live";
import { FitPad, mainParts, MapView, mergeEdges, Selection } from "./MapView";
import { MapFull } from "./MapFull";
import { CommandLayer, Layout, LayoutSwitch, loadLayout, saveLayout } from "./Desk";
import { PhoneHeader, PhoneHome, PhoneMore, PhoneOffline, PhoneTab, PhoneTabs } from "./Phone";
import { Sheet } from "./Sheet";
import { useInboxCount } from "./inboxCount";
import { pref, setPref, setVoiceStatus, VoiceStatus, WakeListener } from "./voice";
import { ariFromEvents, ariSet, ariTell, earsPaused, loadEars } from "./ariState";
import { PageBoundary, PageHead, pageMeta, Skeleton, usePauseWhenHidden, Vitals } from "./kit";
import { Palette, PaletteItem } from "./Palette";

// Pages load when first opened, so the first paint stays small.
const AriHome = lazy(() => import("./AriView").then((m) => ({ default: m.AriHome })));
const AriView = lazy(() => import("./AriView").then((m) => ({ default: m.AriView })));
const InboxView = lazy(() => import("./Pages").then((m) => ({ default: m.InboxView })));
const ModelsView = lazy(() => import("./Pages").then((m) => ({ default: m.ModelsView })));
const NotificationsView = lazy(() => import("./Pages").then((m) => ({ default: m.NotificationsView })));
const SettingsView = lazy(() => import("./Pages").then((m) => ({ default: m.SettingsView })));
const LogsView = lazy(() => import("./LogsView").then((m) => ({ default: m.LogsView })));
const PluginsView = lazy(() => import("./PluginsView").then((m) => ({ default: m.PluginsView })));
const PowerView = lazy(() => import("./PowerView").then((m) => ({ default: m.PowerView })));
const QueueView = lazy(() => import("./QueueView").then((m) => ({ default: m.QueueView })));
const RulesView = lazy(() => import("./RulesView").then((m) => ({ default: m.RulesView })));
const RunsView = lazy(() => import("./RunsView").then((m) => ({ default: m.RunsView })));
const ShareView = lazy(() => import("./ShareView").then((m) => ({ default: m.ShareView })));
const VoiceTrain = lazy(() => import("./VoiceTrain").then((m) => ({ default: m.VoiceTrain })));

const CMD_PAD: FitPad = { top: "340px", right: "40px", bottom: "150px", left: "40px" };  // below the corner cards, above the command bar

const NAV: { id: string; label: string; icon: string }[] = [
  { id: "map", label: "Live map", icon: "M3 6l6-3 6 3 6-3v15l-6 3-6-3-6 3zM9 3v15M15 6v15" },
  { id: "inbox", label: "Inbox", icon: "M4 13l2.5-8h11L20 13v6H4zM4 13h5l1 2h4l1-2h5" },
  { id: "notifications", label: "Notifications", icon: "M6 16V11a6 6 0 0 1 12 0v5l2 2H4zM10 20a2 2 0 0 0 4 0" },
  { id: "ari", label: "Ari", icon: "M4 5h16v11H9l-5 4zM8 10h.01M12 10h.01M16 10h.01" },
  { id: "plugins", label: "Plugins", icon: "M9 3v4M15 3v4M6 7h12v5a6 6 0 0 1-12 0zM12 18v3" },
  { id: "queue", label: "Queue", icon: "M4 6h10M4 12h10M4 18h10M18 6l2 2-2 2M18 14l2 2-2 2" },
  { id: "runs", label: "Runs", icon: "M4 6h16M4 12h11M4 18h14" },
  { id: "share", label: "Share", icon: "M12 15V3M7 8l5-5 5 5M5 13v6h14v-6" },
  { id: "power", label: "Power", icon: "M12 3v8M7.5 6.5a7 7 0 1 0 9 0" },
  { id: "models", label: "Models", icon: "M12 3l8 4.5v9L12 21l-8-4.5v-9zM12 12l8-4.5M12 12v9M12 12L4 7.5" },
  { id: "settings", label: "Settings", icon: "M4 7h10M18 7h2M4 17h4M12 17h8M14 5v4M8 15v4" },
  { id: "logs", label: "Logs", icon: "M5 4h14v16H5zM8 8h8M8 12h8M8 16h5" },
];

function Icon({ d }: { d: string }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={d} />
    </svg>
  );
}

// The Argus mark in small: a cyan-to-violet ring, a hexagonal iris and a bright pupil (the app icon is the same).
export function Logo({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" fill="none" aria-hidden="true" className="logo">
      <defs>
        <linearGradient id="lg-ring" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#4cc2ff" /><stop offset=".55" stopColor="#7f9cff" /><stop offset="1" stopColor="#a78bfa" />
        </linearGradient>
        <radialGradient id="lg-pupil" cx="35%" cy="30%" r="75%">
          <stop offset="0" stopColor="#ffffff" /><stop offset=".45" stopColor="#bfeaff" /><stop offset="1" stopColor="#4cc2ff" />
        </radialGradient>
      </defs>
      <circle cx="16" cy="16" r="12" stroke="url(#lg-ring)" strokeWidth="2.6" />
      <path d="M16 9.4l5.7 3.3v6.6L16 22.6l-5.7-3.3v-6.6z" fill="#141d33" stroke="rgba(160,200,255,.45)" strokeWidth=".8" />
      <circle cx="16" cy="16" r="3.6" fill="#05080d" />
      <circle cx="16.8" cy="15.2" r="2.2" fill="url(#lg-pupil)" />
      <circle cx="25.2" cy="8.3" r="1.4" fill="#e9f6ff" />
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
  const [layout, setLayoutState] = useState<Layout>(loadLayout);  // the PC's home: map + tiles, or map + command bar
  const setLayout = (l: Layout) => { setLayoutState(l); saveLayout(l); };  // the phone's "waiting for you" sheet  // the phone's sideways full-screen map  // the map's inspector drawer with nothing selected (the overview)
  const [wide, setWide] = useState(() => { try { return localStorage.getItem("helios.rail") === "wide"; } catch { return false; } });
  const setWideRail = (v: boolean) => { setWide(v); try { localStorage.setItem("helios.rail", v ? "wide" : "icons"); } catch { /* private */ } };
  const [triedLogin, setTriedLogin] = useState(false);
  const parse = (h: string) => (["queue", "runs", "logs", "share", "power", "ari", "plugins", "more", "inbox", "notifications", "models", "settings", "voice"].includes(h) || h.startsWith("rules/") || h.startsWith("plugins/") || h.startsWith("ari/") ? h : "map");
  const [view, setView] = useState<string>(() => parse(location.hash.slice(1)));
  useEffect(() => {  // the share menu opens /helios/#share on a page that may already be open
    const on = () => setView(parse(location.hash.slice(1)));
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const narrow = useNarrow();
  usePauseWhenHidden();
  const [pal, setPal] = useState(false);
  useEffect(() => {  // Ctrl/Cmd+K opens the palette from anywhere
    const on = (e: KeyboardEvent) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setPal((v) => !v); } };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, []);
  const waiting = useInboxCount(a.events);
  const [full, setFull] = useState(() => { try { return localStorage.getItem("helios.full") === "1"; } catch { return false; } });
  const setFullView = (v: boolean) => { setFull(v); try { localStorage.setItem("helios.full", v ? "1" : "0"); } catch { /* private */ } };
  const phone = narrow && !full;
  const go = (to: string) => {
    let v = to;
    if (v === "ari") {  // a question or the mic waiting for Ari: straight into the current chat, not the chat list
      let queued = false;
      try { queued = !!(sessionStorage.getItem("ari.command") || sessionStorage.getItem("ari.mic")); } catch { /* private */ }
      if (queued) v = `ari/${currentConv()}`;
    }
    setView(v); history.replaceState(null, "", v === "map" ? location.pathname : `#${v}`);
  };
  const askAri = (text: string) => { try { sessionStorage.setItem("ari.command", text); } catch { /* private */ } go("ari"); };
  const items = useMemo<PaletteItem[]>(() => [
    ...NAV.map((n) => ({ id: `go-${n.id}`, group: "go to", label: n.label, hint: n.id === "map" ? "home" : `#${n.id}`, run: () => go(n.id) })),
    { id: "go-voice", group: "go to", label: "Voice training", hint: "#voice", run: () => go("voice") },
    { id: "lay-tiles", group: "layout", label: "Home: map + tiles", run: () => { setLayout("desk"); go("map"); } },
    { id: "lay-command", group: "layout", label: "Home: command bar", run: () => { setLayout("command" as Layout); go("map"); } },
    { id: "ask", group: "ari", label: "Talk to Ari", hint: "open the current chat", run: () => go("ari") },
    { id: "phone", group: "view", label: "Phone view", run: () => setFullView(false) },
  // eslint-disable-next-line react-hooks/exhaustive-deps
  ], []);
  const viewRef = useRef(view);
  viewRef.current = view;
  const [heard, setHeard] = useState<string | null>(null);
  useEffect(() => {  // "Hey Ari" while Helios is open (Ari > "Hey Ari" turns it on)
    let wl: WakeListener | null = null;
    let hide: ReturnType<typeof setTimeout> | undefined;
    const badge = (msg: string | null, ms = 8000) => {
      clearTimeout(hide); setHeard(msg); if (msg) hide = setTimeout(() => setHeard(null), ms);
    };
    // Only ONE "Hey Ari" may listen, or several Aris answer at once: not here when the PC's own listener runs on this
    // machine, and only in one Helios tab per browser (a Web Lock; another tab takes over when this one closes).
    let pcListens = false;
    let waiting: AbortController | null = null;
    let release: (() => void) | null = null;
    const local = ["127.0.0.1", "localhost", "[::1]"].includes(location.hostname);
    const checkPc = () => {
      if (!local) return;
      api<{ listener: boolean }>("/ari/listener").then((r) => {
        if (r.listener !== pcListens) { pcListens = r.listener; sync(); }
      }).catch(() => {});
    };
    const stopWl = () => { wl?.stop(); wl = null; release?.(); release = null; waiting?.abort(); waiting = null; };
    const sync = () => {
      const want = pref("wake", false) && !pcListens && !earsPaused();
      if (want && !wl && !waiting) {
        const locks = (navigator as Navigator & { locks?: LockManager }).locks;
        if (!locks) { startWl(); return; }
        waiting = new AbortController();
        locks.request("helios-hey-ari", { signal: waiting.signal }, () => new Promise<void>((done) => {
          waiting = null; release = done; startWl();
        })).catch(() => {});
      } else if (!want && (wl || waiting)) stopWl();
    };
    const startWl = () => {
      if (wl) return;
      {
        wl = new WakeListener({
          onCommand: (text) => {
            badge(null);
            ariSet("thinking", text);
            if (viewRef.current.startsWith("ari/")) window.dispatchEvent(new CustomEvent("ari-command", { detail: text }));
            else {
              const v = `ari/${currentConv()}`;
              try { sessionStorage.setItem("ari.command", text); } catch { /* private */ }
              setView(v); history.replaceState(null, "", `#${v}`);
            }
          },
          onWake: () => ariTell("listening"),
          onError: (msg) => { badge(msg, 15000); setPref("wake", false); },
        });
        wl.start();
      }
    };
    const restart = () => { stopWl(); sync(); };  // Whisper became (un)available
    checkPc();
    loadEars();
    const pcTimer = setInterval(checkPc, 30000);
    const arm = () => wl?.arm();
    api<VoiceStatus>("/ari-voice").then(setVoiceStatus).catch(() => {}).finally(sync);
    window.addEventListener("ari-pref", sync);
    window.addEventListener("ari-status", restart);
    window.addEventListener("ari-arm", arm);
    return () => {
      window.removeEventListener("ari-pref", sync); window.removeEventListener("ari-status", restart);
      window.removeEventListener("ari-arm", arm); stopWl(); clearTimeout(hide); clearInterval(pcTimer);
    };
  }, []);
  // Ari's state follows the event stream (answers from any screen, the PC's "Hey Ari"): the chat page shows it; on
  // the PC the island is Ari's face.
  const seen = useRef(0);
  useEffect(() => {
    const fresh = a.events.filter((e) => e.seq > seen.current);
    if (!fresh.length) return;
    const first = seen.current === 0;
    seen.current = fresh[fresh.length - 1].seq;
    if (!first) ariFromEvents(fresh);  // not the replay of old events on load
  }, [a.events]);
  const heardBadge = <>
    {heard && <div className="ari-heard" role="status">{heard}</div>}
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
        {a.phase === "down" && <PhoneOffline onRetry={() => a.reconnect()} />}
        <main key={view} className="pmain2">
          <Suspense fallback={<Skeleton rows={4} />}>
          {proot === "ari" ? (view.includes("/")
            ? <AriView conv={view.split("/")[1]} onBack={() => go("ari")} onNew={(c) => go(`ari/${c}`)} onSelect={setSel} onView={open} />
            : <AriHome onOpen={(c) => go(`ari/${c}`)} />)
          : proot === "plugins" ? (
            <PluginsView phone selected={view.includes("/") ? decodeURIComponent(view.split("/")[1]) : null}
              onPick={(id) => go(id ? `plugins/${encodeURIComponent(id)}` : "plugins")} onSelect={setSel} />
          )
          : proot === "share" ? <ShareView onJob={(id) => setSel({ type: "job", id })} onDone={() => go("map")} />
          : proot === "queue" ? <QueueView events={a.events} onSelect={setSel} />
          : proot === "runs" ? <RunsView events={a.events} selected={null} onSelect={setSel} />
          : proot === "power" ? <PowerView events={a.events} onSelect={setSel} />
          : proot === "logs" ? <LogsView />
          : proot === "inbox" ? <InboxView events={a.events} onOpenChat={(c) => go(`ari/${c}`)} />
          : proot === "notifications" ? <NotificationsView events={a.events} />
          : proot === "settings" ? <SettingsView />
          : proot === "voice" ? <VoiceTrain />
          : proot === "models" ? <ModelsView events={a.events} />
          : proot === "more" ? <PhoneMore onOpen={open} onFull={() => setFullView(true)} />
          : <PhoneHome status={st} events={a.events} onSelect={setSel} onOpen={open} onInbox={() => go("inbox")}
              onAri={(mic) => { if (mic) { try { sessionStorage.setItem("ari.mic", "1"); } catch { /* private */ } } go("ari"); }} />}
          </Suspense>
        </main>
        <PhoneTabs tab={tab} onTab={(t) => (t === "map" ? setBigMap(true) : go(t === "home" ? "map" : t === "plug" ? "plugins" : t))} />
        {sel && a.map && (
          <Sheet onClose={() => setSel(null)}>
            <Inspector sel={sel} map={a.map} status={st} events={a.events} onSelect={setSel} />
          </Sheet>
        )}
        {mapFull}
        {heardBadge}
      </div>
    );
  }
  const root = view.split("/")[0];
  const main = a.map ? mainParts(a.map, [], {}).map : null;
  const meta = pageMeta(view);
  const withDock = root !== "map";
  const cmd = root === "map" && layout === "command";
  const tools = (
    <>
      <button type="button" className="btn" aria-pressed={flow} onClick={() => setFlow(!flow)}>flow</button>
      <button type="button" className="btn" onClick={() => setRelayout((x) => x + 1)}>auto layout</button>
      {narrow && <button type="button" className="btn" onClick={() => setBigMap(true)}>full screen</button>}
      <button type="button" className="btn" aria-pressed={info || !!sel} onClick={() => (sel || info ? (setSel(null), setInfo(false)) : setInfo(true))}>inspector</button>
    </>
  );
  const stage = (
    <section className="stage" aria-label="Live map">
      <div className="canvas">
        {a.map && a.map.nodes.length > 0 ? (
          <MapView map={a.map} pulses={a.pulses} active={a.active} selection={sel} onSelect={setSel} flow={flow} relayoutSignal={relayout} onPlugins={() => go("plugins")}
            fitPad={cmd ? CMD_PAD : undefined} />
        ) : (
          <div className="empty mono">{a.phase === "down" ? "$ waiting for argus…" : "$ loading the map…"}</div>
        )}
      </div>
      {cmd ? (
        <CommandLayer status={st} events={a.events} live={a.conn === "live"} onView={go} onAsk={askAri}
          onInbox={() => go("inbox")} onLayout={setLayout} tools={tools} />
      ) : (
        <>
          <div className="hud hud-tl">
            <div className="hud-title mono"><span className="pt">live map</span><span className="dim">{main ? `${main.nodes.length} parts · ${mergeEdges(main.edges).length} lines` : "loading…"}</span></div>
          </div>
          <div className="hud hud-tr tools">{tools}</div>
        </>
      )}
      {!cmd && <div className="hud hud-bl legend mono">
        <span><i style={{ background: "var(--flow)" }} />message</span>
        <span><i style={{ background: "var(--amber)" }} />escalation</span>
        <span><i style={{ background: "var(--bad)" }} />failure</span>
        <span><i className="thick" />busier line</span>
        <span className="dim hide-sm">drag to arrange · click to inspect</span>
      </div>}
      {a.map && (sel || info) && (
        <aside className="drawer">
          <Inspector sel={sel} map={a.map} status={st} events={a.events} onSelect={setSel} onClose={() => { setSel(null); setInfo(false); }} />
        </aside>
      )}
    </section>
  );
  const health = a.phase === "down" ? "unreachable" : ok ? "healthy" : a.conn === "live" ? "degraded" : "connecting";
  return (
    <div className={`shell${wide ? " wide" : ""}${cmd ? " cmdmode" : ""}`}>
      {!cmd && <header className="top">
        <div className="brand" title={st ? `${st.instance}@${st.host}` : undefined}>
          <Logo /><span className="who mono"><span className="tg">sas@argus</span><span className="dim">:~{root === "map" ? "" : `/${root}`}$</span></span>
        </div>
        <AskBox onSelect={setSel} onView={(v) => go(v)} />
        <button type="button" className="cmdk" onClick={() => setPal(true)} title="Command palette"><span className="lbl">jump to</span><kbd>Ctrl K</kbd></button>
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
        {root === "map" && <LayoutSwitch layout={layout} onChange={setLayout} />}
      </header>}

      {!cmd && <nav className="rail" aria-label="Helios sections">
        {NAV.map((n) => (
          <button key={n.id} type="button" className="nav" title={n.label} aria-current={n.id === root ? "page" : undefined} onClick={() => go(n.id)}>
            <Icon d={n.icon} /><span className="nl">{n.label}</span>
            {n.id === "inbox" && waiting > 0 && <span className="navbadge" aria-label={`${waiting} waiting`}>{waiting}</span>}
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
      </nav>}

      <main>
        <Vitals status={st} />
        <div key={root === "ari" ? view : root} className={`page${root === "map" ? " stagepage" : ""}${["runs", "logs", "queue"].includes(root) ? " fill" : ""}`}>
        {meta && <PageHead meta={meta} live={a.conn === "live"} />}
        <PageBoundary name={root}>
        <Suspense fallback={<Skeleton />}>
        {view.startsWith("rules/") ? <RulesView plugin={decodeURIComponent(view.slice(6))} onBack={() => history.back()} />
        : root === "plugins" ? (
          <PluginsView selected={view.includes("/") ? decodeURIComponent(view.split("/")[1]) : null}
            onPick={(id) => go(id ? `plugins/${encodeURIComponent(id)}` : "plugins")}
            onSelect={(s) => { setSel(s); go("runs"); }} />
        )
        : view === "share" ? <ShareView onJob={(id) => { setSel({ type: "job", id }); go("runs"); }} onDone={() => go("map")} />
        : view === "logs" ? <LogsView />
        : view === "inbox" ? <InboxView events={a.events} onOpenChat={(c) => go(`ari/${c}`)} />
        : view === "notifications" ? <NotificationsView events={a.events} />
        : view === "settings" ? <SettingsView />
        : view === "voice" ? <VoiceTrain />
        : view === "models" ? <ModelsView events={a.events} />
        : root === "map" ? (
          cmd ? stage : (
            <div className="deskhome">
              {stage}
              <aside className="desk-side" aria-label="At a glance">
                <PhoneHome status={st} events={a.events} onSelect={setSel} onOpen={go} onInbox={() => go("inbox")} onAsk={askAri}
                  onAri={(mic) => { if (mic) { try { sessionStorage.setItem("ari.mic", "1"); } catch { /* private */ } } go("ari"); }} />
              </aside>
            </div>
          )        ) : (
        <div className="mid one">
          {view === "ari" ? (
            <AriHome onOpen={(c) => go(`ari/${c}`)} />
          ) : root === "ari" ? (
            <AriView conv={view.split("/")[1]} onBack={() => go("ari")} onNew={(c) => go(`ari/${c}`)} onSelect={setSel} onView={(v) => go(v)} />
          ) : view === "power" ? (
            <PowerView events={a.events} onSelect={setSel} />
          ) : view === "queue" ? (
            <QueueView events={a.events} onSelect={setSel} />
          ) : (
            <RunsView events={a.events} selected={sel?.type === "job" ? sel.id : null} onSelect={setSel} />
          )}
          {a.map && sel && (
            <aside className="drawer page-drawer" aria-label="Details">
              <Inspector sel={sel} map={a.map} status={st} events={a.events} onSelect={setSel} onClose={() => setSel(null)} />
            </aside>
          )}
        </div>
        )}
        </Suspense>
        </PageBoundary>
        </div>
        {withDock && <EventsPanel events={a.events} conn={a.conn} onSelect={setSel} />}
      </main>
      <Palette items={items} open={pal} onClose={() => setPal(false)} />
      {mapFull}
      {heardBadge}
    </div>
  );
}
