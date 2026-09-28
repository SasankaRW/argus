export function clock(t: number): string {
  const d = new Date(t * 1000);
  const p = (n: number, w = 2) => String(n).padStart(w, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
}

export function ago(t: number): string {
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 5) return "just now";
  if (s < 60) return `${Math.round(s)} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

export function uptime(s: number): string {
  if (s < 120) return `${Math.round(s)} s`;
  if (s < 7200) return `${Math.round(s / 60)} min`;
  if (s < 172800) return `${(s / 3600).toFixed(1)} h`;
  return `${Math.round(s / 86400)} d`;
}

export function shortId(id: string | null | undefined): string {
  return id ? id.slice(-6) : "";
}

// Colour for an event kind: green good, red bad, amber attention, blue flow, grey noise.
export function tone(kind: string): "ok" | "bad" | "warn" | "flow" | "dim" {
  if (/succeeded|online|added/.test(kind)) return "ok";
  if (/dead|failed|offline/.test(kind)) return "bad";
  if (/retry|waiting|deduped|cancelled/.test(kind)) return "warn";
  if (/^step\./.test(kind)) return "dim";
  return "flow";
}

export function detail(data: Record<string, unknown> | null): string {
  if (!data) return "";
  const keys = ["workflow", "from", "attempt", "worker", "reason", "idx", "tier", "error", "host", "kind", "first_kind"];
  const bits: string[] = [];
  for (const k of keys) {
    const v = data[k];
    if (v === undefined || v === null || v === "") continue;
    bits.push(`${k}=${typeof v === "object" ? JSON.stringify(v) : v}`);
  }
  return bits.join("  ");
}

export function pretty(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "string") return v;
  return JSON.stringify(v, null, 2);
}
