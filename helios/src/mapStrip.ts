// "What just happened": the strip along the bottom of the map turns an event into one plain line, or into nothing when
// the event is not something a person would recognise. Pure; MapNews.tsx draws the lines.

import type { ArgusEvent } from "./api";
import type { IconName } from "./icons";

export type StripLine = { key: string; icon: IconName; text: string; tone: "ok" | "warn" | "bad" };

type Rule = (e: ArgusEvent, name: (id: string | null) => string | null) => Omit<StripLine, "key"> | null;

const by = (id: string | null, name: (id: string) => string | null) => (id ? name(id) : null);
const title = (e: ArgusEvent) => String(e.data?.title ?? "").replace(/^Watcher: /, "").replace(/^Wishlist: /, "").trim();
const wishlist = (e: ArgusEvent) => Array.isArray(e.data?.tags) && (e.data.tags as string[]).includes("wishlist");

const RULES: Record<string, Rule> = {
  "job.succeeded": (e, n) => { const p = n(e.to); return p ? { icon: "task_alt", text: `${p} finished`, tone: "ok" } : null; },
  "job.dead": (e, n) => { const p = n(e.to); return p ? { icon: "error", text: `${p} failed`, tone: "bad" } : null; },
  "approval.requested": (e, n) => { const p = n(e.to); return p ? { icon: "pan_tool", text: `${p} is waiting for you`, tone: "warn" } : null; },
  "notify.queued": (e) => {
    const t = title(e);
    if (!t) return null;
    return wishlist(e) ? { icon: "sell", text: `Told your phone: ${t}`, tone: "warn" } : { icon: "notifications", text: `Told your phone: ${t}`, tone: "ok" };
  },
  "model.escalated": (e, n) => { const m = n(e.to); return m ? { icon: "neurology", text: `Asked ${m} for help`, tone: "ok" } : null; },
  "worker.online": (e, n) => { const w = n(e.from); return w ? { icon: "computer", text: `${w} came online`, tone: "ok" } : null; },
  "worker.offline": (e, n) => { const w = n(e.from); return w ? { icon: "computer", text: `${w} went offline`, tone: "bad" } : null; },
  "phone.online": () => ({ icon: "smartphone", text: "Phone is back", tone: "ok" }),
  "phone.offline": () => ({ icon: "smartphone", text: "Phone went offline", tone: "bad" }),
  "check.failed": (e, n) => { const w = n(e.from); return w ? { icon: "error", text: `A check failed on ${w}`, tone: "bad" } : null; },
};

export function stripLine(e: ArgusEvent, name: (id: string) => string | null): StripLine | null {
  const rule = RULES[e.kind];
  const line = rule ? rule(e, (id) => by(id, name)) : null;
  return line ? { key: `${e.seq}`, ...line } : null;
}
