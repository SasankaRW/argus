import { useEffect, useState } from "react";
import { setToken, Status } from "./api";
import { EventsPanel } from "./EventsPanel";
import { uptime } from "./format";
import { Inspector } from "./Inspector";
import { useArgus } from "./live";
import { MapView, Selection } from "./MapView";

const NAV: { id: string; label: string; icon: string; soon?: string }[] = [
  { id: "map", label: "Live map", icon: "M3 6l6-3 6 3 6-3v15l-6 3-6-3-6 3zM9 3v15M15 6v15" },
  { id: "runs", label: "Runs", icon: "M4 6h16M4 12h11M4 18h14", soon: "M2" },
  { id: "inbox", label: "Approvals", icon: "M4 13l2.5-8h11l2.5 8v6H4zM4 13h5l1.5 2h3l1.5-2h5", soon: "C8" },
  { id: "models", label: "Models", icon: "M12 3l8 4.5v9L12 21l-8-4.5v-9zM12 12l8-4.5M12 12v9M12 12L4 7.5", soon: "later" },
  { id: "power", label: "Power", icon: "M12 3v8M7.5 6.5a7 7 0 1 0 9 0", soon: "C10" },
  { id: "custom", label: "Customize", icon: "M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1M15 4v4M9 10v4M17 16v4", soon: "later" },
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

function Clock() {
  const [t, setT] = useState(new Date());
  useEffect(() => { const i = window.setInterval(() => setT(new Date()), 1000); return () => window.clearInterval(i); }, []);
  return <span className="mono hide-sm">{t.toLocaleTimeString([], { hour12: false })}</span>;
}

function Kpis({ status }: { status: Status | null }) {
  const j = status?.jobs ?? {};
  const running = (j.leased ?? 0) + (j.running ?? 0);
  const queued = (j.queued ?? 0) + (j.retry ?? 0);
  const online = status?.workers.filter((w) => w.state === "online").length ?? 0;
  const tiles: [string, string | number, string, string][] = [
    ["Running", running, queued ? `${queued} queued` : "queue empty", running ? "var(--flow)" : "var(--tx3)"],
    ["Waiting", j.waiting ?? 0, "for approval or a resume", (j.waiting ?? 0) ? "var(--amber)" : "var(--tx3)"],
    ["Succeeded", j.succeeded ?? 0, "all time", "var(--ok)"],
    ["Dead", j.dead ?? 0, "failed after retries", (j.dead ?? 0) ? "var(--bad)" : "var(--tx3)"],
    ["Workers", online, status ? `${status.workers.length - online} offline` : "", online ? "var(--ok)" : "var(--bad)"],
  ];
  return (
    <section className="kpis" aria-label="Jobs">
      {tiles.map(([l, v, s, c]) => (
        <div key={l} className="panel kpi">
          <div className="l"><span className="dot" style={{ background: c }} />{l}</div>
          <div className="v mono">{v}</div>
          <div className="s">{s}</div>
        </div>
      ))}
    </section>
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
  const [triedLogin, setTriedLogin] = useState(false);

  if (a.phase === "login") return <Login bad={triedLogin} onDone={() => { setTriedLogin(true); a.reconnect(); }} />;

  const st = a.status;
  const ok = st?.status === "ok" && a.conn === "live";
  return (
    <div className="shell">
      <header className="top">
        <div className="brand"><Logo /><b>Helios</b></div>
        <div className="status">
          <span className="inline">
            <span className="dot" style={{ background: a.phase === "down" ? "var(--bad)" : ok ? "var(--ok)" : "var(--amber)" }} />
            <b>{a.phase === "down" ? "Argus unreachable" : ok ? "Argus healthy" : a.conn === "live" ? "Argus degraded" : "Connecting"}</b>
          </span>
          {st && <span className="hide-sm">{st.instance} on <b>{st.host}</b></span>}
          {st && <span className="mono hide-sm">up <b>{uptime(st.uptime_seconds)}</b></span>}
          <span className="sep hide-sm" />
          {st && <span className="mono">v<b>{st.version}</b></span>}
          <Clock />
        </div>
      </header>

      <nav className="rail" aria-label="Helios sections">
        {NAV.map((n) => (
          <button key={n.id} type="button" className="nav" aria-current={n.id === "map" ? "page" : undefined} disabled={!!n.soon} title={n.soon ? `Arrives in ${n.soon}` : undefined}>
            <Icon d={n.icon} /><span>{n.label}</span>{n.soon && <span className="soon mono">{n.soon}</span>}
          </button>
        ))}
        <div className="foot">
          <b><span className="dot" style={{ background: ok ? "var(--ok)" : "var(--amber)" }} />{ok ? "All systems live" : "Checking…"}</b>
          {a.map ? `${a.map.nodes.length} boxes · ${a.map.edges.length} lines` : ""}
          <br />
          <a href="/lite">Lite page</a> · <a href="/docs">API</a>
        </div>
      </nav>

      <main>
        <Kpis status={st} />
        <div className="mid">
          <section className="panel mappanel" aria-label="Live map">
            <div className="ph">
              <span className="pt">Live map</span>
              <span className="muted">{a.map ? `${a.map.nodes.length} boxes · ${a.map.edges.length} lines` : "loading…"}</span>
              <div className="tools">
                <button type="button" className="btn" aria-pressed={flow} onClick={() => setFlow(!flow)}>Flow</button>
                <button type="button" className="btn" onClick={() => setRelayout((x) => x + 1)}>Auto layout</button>
              </div>
            </div>
            <div className="mapwrap">
              {a.map && a.map.nodes.length > 0 ? (
                <MapView map={a.map} pulses={a.pulses} active={a.active} selection={sel} onSelect={setSel} flow={flow} relayoutSignal={relayout} />
              ) : (
                <div className="empty">{a.phase === "down" ? "Waiting for Argus…" : "Loading the map…"}</div>
              )}
            </div>
            <div className="legend">
              <span><i style={{ background: "var(--flow)" }} />message</span>
              <span><i style={{ background: "var(--amber)" }} />escalation</span>
              <span><i style={{ background: "var(--bad)" }} />failure</span>
              <span><i className="thick" />busier line</span>
              <span className="right">Drag boxes to arrange · click to inspect</span>
            </div>
          </section>
          {a.map && <Inspector sel={sel} map={a.map} status={st} events={a.events} onSelect={setSel} />}
        </div>
        <EventsPanel events={a.events} conn={a.conn} onSelect={setSel} />
      </main>
    </div>
  );
}
