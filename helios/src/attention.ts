// The "Needs attention" row above the map: one card per thing that needs you, each with its action. Built from the
// map's own state (the cards' problems, the approvals that are waiting), nothing invented. Pure.

import type { IconName } from "./icons";
import { CORE, type Card } from "./mapGraph";

export type Act =
  | { kind: "ring"; label: "Ring phone" }  // POST /phone/ring
  | { kind: "review"; label: "Review" }  // the Approvals page (Inbox)
  | { kind: "logs"; label: "Open logs"; target: string };  // the side panel of `target`, on its logs
export type Item = { id: string; tone: "bad" | "warn" | "plain"; icon: IconName; title: string; detail?: string; act: Act };

export const MAX_ITEMS = 4;

export function waited(sec: number): string {
  const m = Math.max(1, Math.round(sec / 60));
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  return m % 60 && h < 10 ? `${h} h ${m % 60} m` : `${h} h`;
}

// Which box's logs say why a box is in trouble: a plugin or a worker has its own; everything else is in the core's.
export function logsOf(card: Card, nodeKind: (id: string) => string | undefined): string {
  const k = nodeKind(card.select);
  return k === "worker" || k === "plugin" ? card.select : CORE;
}

export function attentionItems(
  cards: Card[],
  o: { now: number; approvals: { count: number; oldest: number | null }; nodeKind: (id: string) => string | undefined },
): Item[] {
  const items: Item[] = [];
  for (const c of cards) {
    if (!c.problem || (c.tone !== "bad" && c.tone !== "warn")) continue;
    const tone = c.tone;
    if (c.id === "phone") items.push({ id: c.id, tone, icon: "smartphone", title: c.problem, detail: c.detail, act: { kind: "ring", label: "Ring phone" } });
    else {
      items.push({ id: c.id, tone, icon: c.icon, title: c.problem, detail: c.detail, act: { kind: "logs", label: "Open logs", target: logsOf(c, o.nodeKind) } });
    }
  }
  if (o.approvals.count > 0) {
    const n = o.approvals.count;
    items.push({ id: "approvals", tone: "plain", icon: "pending_actions", title: `${n} approval${n === 1 ? "" : "s"} waiting`,
      detail: o.approvals.oldest ? `oldest ${waited(o.now - o.approvals.oldest)}` : undefined, act: { kind: "review", label: "Review" } });
  }
  const rank = { bad: 0, plain: 1, warn: 2 } as const;
  return items.sort((a, b) => rank[a.tone] - rank[b.tone]);
}
