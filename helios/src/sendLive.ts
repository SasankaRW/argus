// Argus Send's live transfers in Helios: one hook (the pop-up, the Transfers page and the map all read it) and the
// small pure helpers they share. Argus keeps live transfers in memory (GET /send/live); a "send.live" event marks a
// transfer starting or finishing, so Helios looks then, reads twice a second while anything moves, and rests otherwise.
import { useCallback, useEffect, useState } from "react";
import { api, type ArgusEvent } from "./api";
import type { IconName } from "./icons";

export type SendLiveItem = {
  offer_id: string; leg: "send" | "hold"; from: string; to: string; label: string; files: number;
  total: number; done: number; state: "moving" | "verifying" | "done" | "failed" | "cancelled";
  speed: number; eta: number | null; device?: string; updated?: number; started?: number;
};
export type SendHistoryItem = { eid: string; device: string; kind: string; at: number; peer: string | null; label: string | null; bytes: number; detail: string | null };

const FINISHED = new Set(["done", "failed", "cancelled"]);
export const isMoving = (t: SendLiveItem) => !FINISHED.has(t.state);
export const fraction = (t: SendLiveItem) => (t.total > 0 ? Math.max(0, Math.min(1, t.done / t.total)) : t.state === "done" ? 1 : 0);

export function deviceIcon(id: string): IconName {
  if (id === "phone" || id.startsWith("phone")) return "smartphone";
  if (id === "pc" || id.includes("desktop")) return "desktop_windows";
  if (id === "calypso" || id.includes("laptop")) return "laptop_chromebook";
  return "devices";
}

export function size(n: number): string {
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(1)} GB`;
  if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KB`;
  return `${n} B`;
}

// "4.1 MB/s · 3 s left", "Checking", "Starting"
export function pace(t: SendLiveItem): string {
  if (t.state === "verifying") return "Checking";
  if (!t.speed) return "Starting";
  const left = t.eta == null ? "" : t.eta < 60 ? ` · ${Math.max(1, Math.round(t.eta))} s left` : ` · ${Math.round(t.eta / 60)} min left`;
  return `${size(t.speed)}/s${left}`;
}

// The finished line: "Saved to Calypso", "Couldn't send to my phone", "Cancelled"
export function result(t: SendLiveItem, name: (id: string) => string): string {
  if (t.state === "done") return `Saved to ${name(t.to)}`;
  if (t.state === "cancelled") return "Cancelled";
  return `Couldn't send to ${name(t.to)}`;
}

export function useSendLive(events: ArgusEvent[]) {
  const [items, setItems] = useState<SendLiveItem[]>([]);
  const [names, setNames] = useState<Record<string, string>>({});
  const mark = events.reduce((m, e) => (e.kind === "send.live" && e.seq > m ? e.seq : m), 0);
  const load = useCallback(async () => {
    try { setItems(await api<SendLiveItem[]>("/send/live")); } catch { /* Argus out of reach: keep what we had */ }
  }, []);
  useEffect(() => { load(); }, [mark, load]);  // a transfer started or finished
  const moving = items.some(isMoving);
  const any = items.length > 0;
  useEffect(() => {  // while something moves: twice a second; while only results show: until they're gone
    if (!any) return;
    const iv = window.setInterval(() => { if (!document.hidden) load(); }, moving ? 500 : 3000);
    return () => window.clearInterval(iv);
  }, [moving, any, load]);
  useEffect(() => {
    api<{ id: string; name: string }[]>("/send/devices")
      .then((l) => setNames(Object.fromEntries(l.map((d) => [d.id, d.name])))).catch(() => {});
  }, [mark]);
  const name = useCallback((id: string) => names[id] ?? (id ? id.charAt(0).toUpperCase() + id.slice(1) : "a device"), [names]);
  const cancel = useCallback((oid: string) => { api(`/send/live/${encodeURIComponent(oid)}/cancel`, { method: "POST" }).then(load).catch(() => {}); }, [load]);
  return { items, name, cancel };
}
export type SendLive = ReturnType<typeof useSendLive>;
