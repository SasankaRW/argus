// Talking to argusd: REST for snapshots, one WebSocket for everything live.

export type ArgusEvent = {
  seq: number;
  id: string;
  kind: string;
  job_id: string | null;
  step: string | null;
  from: string | null;
  to: string | null;
  data: Record<string, unknown> | null;
  at: number;
};

export type MapNode = {
  id: string;
  kind: "core" | "plugin" | "model" | "app" | "worker" | "service";
  label: string;
  group: string | null;
  meta: Record<string, unknown>;
  first_seen: number;
  state?: string;
  jobs?: { active: number; queued: number; waiting?: number };
  pending?: number;
  unsent?: number;
  failed?: number;
  implicit?: boolean;
};

export type Approval = {
  id: string;
  job_id: string | null;
  plugin: string;
  step: string | null;
  type: "entry" | "batch" | "draft";
  title: string;
  state: "pending" | "approved" | "rejected" | "expired";
  payload: {
    fields: Record<string, unknown>;
    items: Record<string, unknown>[];
    summary: string[];
    link: string | null;
    count?: number;
    total?: string;
    amount?: string;
  };
  answer: Record<string, unknown> | null;
  decided_by: string | null;
  decided_at: number | null;
  created_at: number;
  expires_at: number;
};

export type MapEdge = { src: string; dst: string; count: number; last_kind: string; first_seen: number; last_seen: number };
export type ArgusMap = { seq: number; nodes: MapNode[]; edges: MapEdge[] };

export type Status = {
  status: string;
  version: string;
  instance: string;
  host: string;
  uptime_seconds: number;
  jobs: Record<string, number>;
  workers: { id: string; host: string; state: string }[];
  map: { nodes: number; edges: number };
  events: { alive: boolean; seq: number; subscribers: number };
  token_required: boolean;
};

export type Step = { idx: number; name: string; state: string; tier_used: string | null; output: unknown; error: string | null };
export type Job = {
  id: string; plugin: string; workflow: string; state: string; attempt: number; max_attempts: number;
  lease_owner: string | null; wait_reason: string | null; input: unknown; result: unknown; error: string | null;
  created_at: number; updated_at: number; finished_at: number | null; steps?: Step[];
};

const TOKEN_KEY = "helios.token";

export function getToken(): string | null {
  try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
}
export function setToken(t: string | null) {
  try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch { /* private mode */ }
}

// A token passed as ?token= (e.g. from the old home page) is saved and removed from the address bar.
export function adoptTokenFromUrl() {
  const u = new URL(location.href);
  const t = u.searchParams.get("token");
  if (t) {
    setToken(t);
    u.searchParams.delete("token");
    history.replaceState(null, "", u.pathname + (u.search ? u.search : "") + u.hash);
  }
}

export class AuthError extends Error {}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const t = getToken();
  const headers: Record<string, string> = { Accept: "application/json" };
  if (t) headers.Authorization = `Bearer ${t}`;
  if (init?.body) headers["Content-Type"] = "application/json";
  const r = await fetch(path, { ...init, headers: { ...headers, ...(init?.headers as Record<string, string>) } });
  if (r.status === 401) throw new AuthError("token");
  if (!r.ok && r.status !== 503) {
    const detail = await r.json().then((b) => (typeof b?.detail === "string" ? b.detail : null)).catch(() => null);
    throw new Error(detail ?? `${path}: HTTP ${r.status}`);
  }
  return (await r.json()) as T;
}

type StreamHandlers = {
  onEvents: (evs: ArgusEvent[], replay: boolean) => void;
  onState: (s: "connecting" | "live" | "offline") => void;
  onReset: () => void;
  onRefused?: () => void; // closed before it ever opened (e.g. a wrong token): the caller checks why
};

// One WebSocket that never loses an event: on reconnect it asks for everything after the last seq it saw.
export class EventStream {
  private ws: WebSocket | null = null;
  private retry = 800;
  private timer: number | undefined;
  private closed = false;
  constructor(private since: number, private h: StreamHandlers) {}

  start() { this.closed = false; this.open(); }

  stop() {
    this.closed = true;
    window.clearTimeout(this.timer);
    this.ws?.close(1000);
  }

  private open() {
    this.h.onState("connecting");
    const q = new URLSearchParams({ since: String(this.since) });
    const t = getToken();
    if (t) q.set("token", t);
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/events?${q}`);
    this.ws = ws;
    let opened = false;
    ws.onopen = () => { opened = true; this.retry = 800; this.h.onState("live"); };
    ws.onmessage = (m) => {
      const msg = JSON.parse(m.data);
      if (msg.type === "events") {
        const evs = msg.events as ArgusEvent[];
        if (evs.length) this.since = evs[evs.length - 1].seq;
        this.h.onEvents(evs, !!msg.replay);
      } else if (msg.type === "reset") {
        this.h.onReset();
      }
    };
    ws.onclose = () => {
      if (this.closed) return;
      if (!opened) this.h.onRefused?.();
      this.h.onState("offline");
      this.timer = window.setTimeout(() => this.open(), this.retry);
      this.retry = Math.min(this.retry * 2, 10_000);
    };
  }
}
