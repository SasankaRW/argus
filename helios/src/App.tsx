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
import { PhoneHeader, PhoneMore, PhoneOffline, PhoneTab, PhoneTabs, PhoneHome } from "./Phone";
import { PhoneMap } from "./PhoneMap";
import { DeskLayer } from "./DeskLayer";
import { InspectCard } from "./InspectCard";
import { ClaudeChip, Clock } from "./chips";
import { useJobs } from "./jobsFeed";
import { Logo, Ms, PhoneCtx } from "./m3";
import type { IconName } from "./icons";
import { Sheet } from "./Sheet";
import { useInboxCount } from "./inboxCount";
import { pref, setPref, setVoiceStatus, VoiceStatus, WakeListener } from "./voice";
import { ariFromEvents, ariSet, ariTell, earsPaused, loadEars } from "./ariState";
import { PageBoundary, PageHead, pageMeta, PAGES, Skeleton, usePauseWhenHidden, Vitals } from "./kit";
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

const DESK_PAD: FitPad = { top: "130px", right: "330px", bottom: "150px", left: "90px" };  // between the floating bars, tools, inspector and timeline
const CMD_PAD: FitPad = { top: "340px", right: "40px", bottom: "150px", left: "40px" };  // below the corner cards, above the command bar

const NAV: { id: string; label: string; icon: IconName }[] = [
  { id: "map", label: "Live map", icon: "hub" },
  { id: "inbox", label: "Inbox", icon: "inbox" },
  { id: "notifications", label: "Notifications", icon: "notifications" },
  { id: "ari", label: "Ari", icon: "graphic_eq" },
  { id: "plugins", label: "Plugins", icon: "extension" },
  { id: "queue", label: "Queue", icon: "queue" },
  { id: "runs", label: "Runs", icon: "history" },
  { id: "share", label: "Share", icon: "ios_share" },
  { id: "power", label: "Power", icon: "power_settings_new" },
  { id: "models", label: "Models", icon: "neurology" },
  { id: "logs", label: "Logs", icon: "terminal" },
  { id: "settings", label: "Settings", icon: "settings" },
];
// The desktop rail, as on the board (design/material3/boards/Desktop.dc.html). The palette (Ctrl K) goes to every page in NAV.
const RAIL_TOP = ["map", "inbox", "plugins", "runs", "power", "models"];
const RAIL_BOTTOM = ["logs", "settings"];  // after the spacer
const RAIL_LABEL: Record<string, string> = { map: "Map" };

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

function Login({ onDone, bad }: { onDone: () => void; bad: boolean }) {
  const [v, setV] = useState("");
  return (
    <div className="login">
      <form className="panel card" onSubmit={(e) => { e.preventDefault(); setToken(v.trim()); onDone(); }}>
        <div className="brand"><Logo size={30} /></div>
        <h1 className="sn-h1">Helios</h1>
        <p>Argus needs its worker token (ARGUS_WORKER_TOKEN in <code>.env</code>). It is kept in this browser only.</p>
        {bad && <p className="bad" role="alert">That token did not work.</p>}
        <span className={`sn-field${bad ? " is-error" : ""}`}><input autoFocus type="password" value={v} onChange={(e) => setV(e.target.value)} placeholder="Token" aria-label="Token" /></span>
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
  const [triedLogin, setTriedLogin] = useState(false);
  const parse = (h: string) => (["livemap", "queue", "runs", "logs", "share", "power", "ari", "plugins", "more", "inbox", "notifications", "models", "settings", "voice"].includes(h) || h.startsWith("rules/") || h.startsWith("plugins/") || h.startsWith("ari/") ? h : "map");
  const [rawView, setView] = useState<string>(() => parse(location.hash.slice(1)));
  const [details, setDetails] = useState(false);  // the desktop inspector card opened into the full details
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
  const view = rawView === "livemap" && !phone ? "map" : rawView;  // the phone's Map screen is the desktop's map
  const jobs = useJobs(a.events);
  const qj = a.status?.jobs ?? {};
  const queue = { running: (qj.running ?? 0) + (qj.leased ?? 0), queued: (qj.queued ?? 0) + (qj.retry ?? 0) };
  const byWorker: Record<string, number> = {};
  for (const j of jobs) if ((j.state === "running" || j.state === "leased") && j.lease_owner) byWorker[j.lease_owner] = (byWorker[j.lease_owner] ?? 0) + 1;
  useEffect(() => { setDetails(false); }, [sel]);
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
    ...NAV.map((n) => ({ id: `go-${n.id}`, group: "Go to", label: n.label, hint: n.id === "map" ? "Home" : `#${n.id}`, run: () => go(n.id) })),
    { id: "go-voice", group: "Go to", label: "Voice training", hint: "#voice", run: () => go("voice") },
    { id: "lay-tiles", group: "Layout", label: "Home: map with panels", run: () => { setLayout("desk"); go("map"); } },
    { id: "lay-command", group: "Layout", label: "Home: command bar", run: () => { setLayout("command" as Layout); go("map"); } },
    { id: "ask", group: "Ari", label: "Talk to Ari", hint: "Open the current chat", run: () => go("ari") },
    { id: "phone", group: "View", label: "Phone view", run: () => setFullView(false) },
    { id: "lite", group: "View", label: "Lite view", hint: "/lite", run: () => { location.href = "/lite"; } },
    { id: "api", group: "View", label: "API docs", hint: "/docs", run: () => { location.href = "/docs"; } },
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
  const mapFull = bigMap && a.map && !phone ? (
    <MapFull map={a.map} pulses={a.pulses} active={a.active} status={a.status} events={a.events} onClose={closeMap}
      // On the phone the map's Plugins link opens the phone's Plugins page. It used to switch to the full dashboard
      // (and remember it), which left the phone app stuck in the desktop layout. Only More's "Open the full
      // dashboard" switches views.
      onPlugins={() => { setBigMap(false); if (!phone) setFullView(true); go("plugins"); }} />
  ) : null;

  if (a.phase === "login") return <Login bad={triedLogin} onDone={() => { setTriedLogin(true); a.reconnect(); }} />;

  const st = a.status;
  const ok = st?.status === "ok" && a.conn === "live";
  if (phone) {  // the phone: M3 cards (Phone.tsx), the standard navigation bar, details in a bottom sheet
    const proot = view.split("/")[0];
    const tab: PhoneTab = proot === "map" ? "home" : proot === "livemap" ? "map" : proot === "ari" ? "ari" : proot === "inbox" ? "inbox" : "more";
    const open = (v: string) => go(v);
    const title = proot === "map" || proot === "livemap" ? null : proot === "more" ? "More" : PAGES[proot]?.title ?? null;
    const onTab = (t: PhoneTab) => go(t === "home" ? "map" : t === "map" ? "livemap" : t);
    return (
      <PhoneCtx.Provider value={true}>
      <div className={`phone2${proot === "livemap" ? " pm-on" : ""}`}>
        {proot === "livemap" ? (
          a.map && a.map.nodes.length > 0
            ? <PhoneMap map={a.map} pulses={a.pulses} active={a.active} status={st} events={a.events} layout={layout} onLayout={setLayout}
                onPlugins={() => go("plugins")} onDetails={setSel} onAsk={askAri} onView={go} />
            : <div className="empty pm-empty">{a.phase === "down" ? "Waiting for Argus…" : "Loading the map…"}</div>
        ) : (<>
        <PhoneHeader status={st} live={a.conn === "live"} title={title} down={a.phase === "down"} onBell={() => go("notifications")} />
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
          : proot === "more" ? <PhoneMore onOpen={open} onFull={() => setFullView(true)} waiting={waiting} />
          : <PhoneHome status={st} events={a.events} live={a.conn === "live"} down={a.phase === "down"} onSelect={setSel} onOpen={open} onInbox={() => go("inbox")} onAsk={askAri}
              onAri={(mic) => { if (mic) { try { sessionStorage.setItem("ari.mic", "1"); } catch { /* private */ } } go("ari"); }} />}
          </Suspense>
        </main>
        </>)}
        <PhoneTabs tab={tab} onTab={onTab} waiting={waiting} />
        {sel && a.map && (
          <Sheet onClose={() => setSel(null)}>
            <Inspector sel={sel} map={a.map} status={st} events={a.events} onSelect={setSel} />
          </Sheet>
        )}
        {heardBadge}
      </div>
      </PhoneCtx.Provider>
    );
  }
  const navItem = (id: string) => {
    const n = NAV.find((x) => x.id === id)!;
    const cur = id === view.split("/")[0];
    const label = RAIL_LABEL[id] ?? n.label;
    return (
      <button key={id} type="button" className="side-i" title={n.label} aria-label={id === "inbox" && waiting > 0 ? `${label}, ${waiting} waiting` : label}
        aria-current={cur ? "page" : undefined} onClick={() => go(id)}>
        <span className="ind"><Ms n={n.icon} fill={cur} />{id === "inbox" && waiting > 0 && <span className="sn-badge" aria-hidden="true">{waiting}</span>}</span>{label}
      </button>
    );
  };
  const root = view.split("/")[0];
  const meta = pageMeta(view);
  const cmd = root === "map" && layout === "command";
  const tools = (
    <>
      <button type="button" className="btn" aria-pressed={flow} onClick={() => setFlow(!flow)}>Flow</button>
      <button type="button" className="btn" onClick={() => setRelayout((x) => x + 1)}>Auto layout</button>
      {narrow && <button type="button" className="btn" onClick={() => setBigMap(true)}>Full screen</button>}
      <button type="button" className="btn" aria-pressed={info || !!sel} onClick={() => (sel || info ? (setSel(null), setInfo(false)) : setInfo(true))}>Inspector</button>
    </>
  );
  const deskTools = (
    <>
      <button type="button" className="tb" aria-label="Flow" title="Flow" aria-pressed={flow} onClick={() => setFlow(!flow)}><Ms n="route" fill={flow} size={20} /></button>
      <button type="button" className="tb" aria-label="Auto layout" title="Auto layout" onClick={() => setRelayout((x) => x + 1)}><Ms n="auto_awesome_mosaic" size={20} /></button>
      <button type="button" className="tb" aria-label="Inspector" title="Inspector" aria-pressed={info || !!sel} onClick={() => (sel || info ? (setSel(null), setInfo(false)) : setInfo(true))}><Ms n="info" fill={info || !!sel} size={20} /></button>
      {narrow && <button type="button" className="tb" aria-label="Full screen" title="Full screen" onClick={() => setBigMap(true)}><Ms n="fit_screen" size={20} /></button>}
    </>
  );
  const search = (
    <div className="float dl-search">
      <AskBox onSelect={setSel} onView={(v) => go(v)} />
      <button type="button" className="cmdk" onClick={() => setPal(true)} title="Command palette"><kbd>Ctrl K</kbd></button>
    </div>
  );
  const nodeSel = sel?.type === "node" ? sel.id : null;
  const inspector = a.map && (sel || info) ? (
    <aside className={`drawer${nodeSel && !details ? " card-ic" : ""}`} aria-label="Inspector">
      {nodeSel && !details
        ? <InspectCard id={nodeSel} map={mainParts(a.map, [], {}).map} jobs={jobs} queue={queue} byWorker={byWorker} onClose={() => { setSel(null); setInfo(false); }} onDetails={() => setDetails(true)} />
        : <>
            {nodeSel && <button type="button" className="sn-link ic-back" onClick={() => setDetails(false)}>Summary</button>}
            <Inspector sel={sel} map={a.map} status={st} events={a.events} onSelect={setSel} onClose={() => { setSel(null); setInfo(false); }} />
          </>}
    </aside>
  ) : null;
  const stage = (
    <section className={`stage${cmd ? " cmd" : ""}`} aria-label="Live map">
      <div className="canvas">
        {a.map && a.map.nodes.length > 0 ? (
          <MapView map={a.map} pulses={a.pulses} active={a.active} selection={sel} onSelect={setSel} flow={flow} relayoutSignal={relayout} onPlugins={() => go("plugins")}
            events={a.events} queue={queue} byWorker={byWorker} fitPad={cmd ? CMD_PAD : DESK_PAD} />
        ) : (
          <div className="empty">{a.phase === "down" ? "Waiting for Argus…" : "Loading the map…"}</div>
        )}
      </div>
      {cmd ? (
        <CommandLayer status={st} events={a.events} live={a.conn === "live"} onView={go} onAsk={askAri}
          onInbox={() => go("inbox")} onLayout={setLayout} tools={tools} />
      ) : (
        <DeskLayer status={st} events={a.events} live={a.conn === "live"} down={a.phase === "down"} onInbox={() => go("inbox")}
          layoutSwitch={<LayoutSwitch layout={layout} onChange={setLayout} />} search={search} tools={deskTools} />
      )}
      {inspector}
      {!cmd && <EventsPanel events={a.events} conn={a.conn} onSelect={setSel} lanes={{ jobs, workers: st?.workers ?? [] }} />}
    </section>
  );
  const health = a.phase === "down" ? "Can't reach Argus" : ok ? "All good" : a.conn === "live" ? "Needs a look" : "Connecting";
  const hTone = a.phase === "down" ? "d-coral" : ok ? "d-green" : "d-amber";
  return (
    <div className={`shell${root === "map" ? " onmap" : ""}`}>
      <nav className="side" aria-label="Helios sections">
        <button type="button" className="side-ask" aria-label="Ask Ari" title="Ask Ari" onClick={() => go("ari")}><Ms n="graphic_eq" fill /></button>
        {RAIL_TOP.map(navItem)}
        <div className="side-grow" />
        {RAIL_BOTTOM.map(navItem)}
      </nav>

      {root !== "map" && <header className="top">
        <div className="brand" title={st ? `${st.instance}@${st.host}` : undefined}>
          <span className="who">Argus</span>
        </div>
        <AskBox onSelect={setSel} onView={(v) => go(v)} />
        <button type="button" className="cmdk" onClick={() => setPal(true)} title="Command palette"><span className="lbl">Jump to</span><kbd>Ctrl K</kbd></button>
        {narrow && <button type="button" className="btn phone-back" onClick={() => setFullView(false)}>Phone view</button>}
        <div className="status">
          <span className={`seg-s ${a.phase === "down" ? "bad" : ok ? "ok" : "warn"}`}>
            <span className={`sn-dot ${hTone}`} />{health}
          </span>
          {st && <span className="seg-s hide-sm faint" title={`Argus ${st.version}`}>Up {uptime(st.uptime_seconds)}</span>}
          <ClaudeChip />
          <span className="seg-s hide-sm"><Clock /></span>
        </div>
      </header>}

      <main>
        {root !== "map" && <Vitals status={st} />}
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
          stage
        ) : (
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
        {root !== "map" && <EventsPanel events={a.events} conn={a.conn} onSelect={setSel} />}
      </main>
      <Palette items={items} open={pal} onClose={() => setPal(false)} />
      {mapFull}
      {heardBadge}
    </div>
  );
}
