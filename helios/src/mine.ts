// "My" transfers: what the device Helios is running on sent and received, and opening those files there.
// Asked straight from this device, never through Argus, so it works while Argus is down:
// - in the phone app: the app itself (window.ArgusPhone, a bridge the Argus app adds to its web view);
// - on a computer with Argus Send: its Send agent on this same machine (http://127.0.0.1:8610, with the worker token).
// Anywhere else (another computer, no agent) there is no "me", and nothing offers Open.
import { useCallback, useEffect, useState } from "react";
import { getToken } from "./api";

export type MineFile = { n: number; name: string; size: number; here: boolean };
export type MineItem = { kind: "sent" | "received"; offer_id: string; at: number; label: string; peer: string; files: MineFile[] };
export type Mine = {
  device: string | null;  // this device's Send id ("pc", "phone"), or null when Helios can't ask it
  items: MineItem[];
  open: (it: MineItem, n: number) => Promise<string | null>;  // null when it opened, else why not
  reveal: ((it: MineItem, n: number) => Promise<string | null>) | null;  // "Show in folder" (computers only)
};

type PhoneBridge = { mine(): string; open(kind: string, offerId: string, n: number): string; clipSync?(): boolean; setClipSync?(on: boolean): void };
declare global { interface Window { ArgusPhone?: PhoneBridge } }

const AGENT = "http://127.0.0.1:8610";

export async function agent<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const t = getToken();
  const headers: Record<string, string> = t ? { Authorization: `Bearer ${t}` } : {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const r = await fetch(AGENT + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(2500) });
  if (!r.ok) {
    const why = await r.json().then((b) => (typeof b?.detail === "string" ? b.detail : null)).catch(() => null);
    throw new Error(why ?? `It said ${r.status}`);
  }
  return r.json() as Promise<T>;
}

export const mineKey = (kind: string, offerId: string) => `${kind}:${offerId}`;

export function useMine(refresh: unknown): Mine {
  const [state, setState] = useState<{ device: string | null; items: MineItem[] }>({ device: null, items: [] });
  useEffect(() => {
    let live = true;
    const phone = window.ArgusPhone;
    if (phone) {
      try { const got = JSON.parse(phone.mine()); if (live) setState({ device: got.device ?? "phone", items: got.items ?? [] }); } catch { /* an older app */ }
      return () => { live = false; };
    }
    agent<{ device: string; items: MineItem[] }>("/send/mine")
      .then((got) => { if (live) setState({ device: got.device, items: got.items }); })
      .catch(() => { if (live) setState({ device: null, items: [] }); });
    return () => { live = false; };
  }, [refresh]);

  const open = useCallback(async (it: MineItem, n: number) => {
    const phone = window.ArgusPhone;
    if (phone) { const why = phone.open(it.kind, it.offer_id, n); return why || null; }
    try { await agent(`/send/mine/${it.kind}/${it.offer_id}/${n}/open`, "POST"); return null; } catch (e) { return e instanceof Error ? e.message : "Couldn't open it"; }
  }, []);
  const reveal = useCallback(async (it: MineItem, n: number) => {
    try { await agent(`/send/mine/${it.kind}/${it.offer_id}/${n}/reveal`, "POST"); return null; } catch (e) { return e instanceof Error ? e.message : "Couldn't show it"; }
  }, []);
  return { ...state, open, reveal: window.ArgusPhone ? null : reveal };
}
