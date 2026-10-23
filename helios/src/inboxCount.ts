import { useEffect, useState } from "react";
import { api, ArgusEvent } from "./api";

// How many things wait for you (the nav badge), refreshed when approvals, lessons or Ari change.
export function useInboxCount(events: ArgusEvent[]): number {
  const [n, setN] = useState(0);
  const last = events.filter((e) => /^(approval|ari|lesson|guidance)\./.test(e.kind)).map((e) => e.seq).pop() ?? 0;
  useEffect(() => { api<{ items: unknown[] }>("/inbox").then((d) => setN(d.items.length)).catch(() => {}); }, [last]);
  return n;
}
