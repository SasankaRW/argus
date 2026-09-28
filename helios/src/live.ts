// useArgus: the one place Helios keeps live state. Snapshot from /map, then the event stream keeps it current.

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ArgusEvent, ArgusMap, AuthError, EventStream, Status } from "./api";

export type Tone = "flow" | "bad" | "warn";
export type Pulse = { id: number; src: string; dst: string; tone: Tone; at: number };
export type Conn = "connecting" | "live" | "offline";
export type Phase = "loading" | "login" | "ready" | "down";

const MAX_EVENTS = 400;
const PULSE_MS = 1400;
const MAP_KINDS = /^(component\.added|edge\.added|worker\.(online|offline)|model\.breaker_\w+|phone\.(online|offline))$/;
const BAD = /(dead|failed|offline|timeout|error)/;
const WARN = /(escalated|skipped|retry|requested|rejected|expired)/;
const pulseTone = (k: string): Tone => (BAD.test(k) ? "bad" : WARN.test(k) ? "warn" : "flow");

export function useArgus() {
  const [phase, setPhase] = useState<Phase>("loading");
  const [authError, setAuthError] = useState<string | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [map, setMap] = useState<ArgusMap | null>(null);
  const [events, setEvents] = useState<ArgusEvent[]>([]);
  const [pulses, setPulses] = useState<Pulse[]>([]);
  const [active, setActive] = useState<Record<string, number>>({}); // component -> last activity (ms)
  const [conn, setConn] = useState<Conn>("connecting");
  const stream = useRef<EventStream | null>(null);
  const timers = useRef<{ map?: number; status?: number }>({});
  const pulseId = useRef(0);

  const loadStatus = useCallback(async () => {
    try { setStatus(await api<Status>("/status")); } catch { /* shown as offline by the stream */ }
  }, []);

  const loadMap = useCallback(async () => {
    const m = await api<ArgusMap>("/map");
    setMap(m);
    return m;
  }, []);

  // Batch refreshes: a burst of events causes one fetch, not fifty.
  const soon = useCallback((what: "map" | "status", ms: number) => {
    const t = timers.current;
    if (t[what]) return;
    t[what] = window.setTimeout(() => {
      t[what] = undefined;
      (what === "map" ? loadMap() : loadStatus()).catch(() => {});
    }, ms);
  }, [loadMap, loadStatus]);

  const onEvents = useCallback((evs: ArgusEvent[], replay: boolean) => {
    setEvents((old) => {
      const merged = old.concat(evs);
      return merged.length > MAX_EVENTS ? merged.slice(merged.length - MAX_EVENTS) : merged;
    });
    let mapChanged = false;
    let jobsChanged = false;
    const now = Date.now();
    const newPulses: Pulse[] = [];
    const touched: Record<string, number> = {};
    for (const e of evs) {
      if (MAP_KINDS.test(e.kind)) mapChanged = true;
      if (/^(job|worker|model|approval|outbox)\./.test(e.kind)) jobsChanged = true;
      if (!replay && e.from && e.to && e.kind !== "edge.added") {
        newPulses.push({ id: ++pulseId.current, src: e.from, dst: e.to, tone: pulseTone(e.kind), at: now });
      }
      if (!replay) {
        if (e.from) touched[e.from] = now;
        if (e.to) touched[e.to] = now;
      }
    }
    if (!replay && evs.length) {
      // keep line thickness live without refetching the map
      setMap((m) => {
        if (!m) return m;
        let edges = m.edges;
        for (const e of evs) {
          if (!e.from || !e.to || e.kind === "edge.added") continue;
          edges = edges.map((x) => (x.src === e.from && x.dst === e.to ? { ...x, count: x.count + 1, last_kind: e.kind, last_seen: e.at } : x));
        }
        return edges === m.edges ? m : { ...m, edges };
      });
    }
    if (newPulses.length) {
      setPulses((p) => p.filter((x) => now - x.at < PULSE_MS).concat(newPulses.slice(-40)));
      window.setTimeout(() => setPulses((p) => p.filter((x) => Date.now() - x.at < PULSE_MS)), PULSE_MS + 50);
    }
    if (Object.keys(touched).length) setActive((a) => ({ ...a, ...touched }));
    if (mapChanged) soon("map", 250);
    else if (jobsChanged) soon("map", 600); // plugin job counts live on the map
    if (jobsChanged || mapChanged) soon("status", 400);
  }, [soon]);

  const connect = useCallback(async () => {
    stream.current?.stop();
    try {
      const m = await loadMap();
      setAuthError(null);
      setPhase("ready");
      const s = new EventStream(m.seq, { onEvents, onState: setConn, onReset: () => { loadMap().catch(() => {}); } });
      stream.current = s;
      s.start();
      // show some history on first load
      api<{ events: ArgusEvent[] }>("/events?newest=true&limit=150")
        .then((r) => setEvents((old) => {
          const hist = r.events.filter((e) => e.seq <= m.seq);
          const seen = new Set(old.map((e) => e.seq));
          return hist.filter((e) => !seen.has(e.seq)).concat(old).sort((a, b) => a.seq - b.seq).slice(-MAX_EVENTS);
        }))
        .catch(() => {});
    } catch (e) {
      if (e instanceof AuthError) {
        setPhase("login");
      } else {
        setPhase("down");
        window.setTimeout(() => connect(), 3000);
      }
    }
  }, [loadMap, onEvents]);

  useEffect(() => {
    loadStatus();
    connect();
    const iv = window.setInterval(loadStatus, 5000);
    return () => { window.clearInterval(iv); stream.current?.stop(); };
  }, [connect, loadStatus]);

  return { phase, authError, setAuthError, status, map, events, pulses, active, conn, reconnect: connect };
}
