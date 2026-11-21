// The `calendar` plugin's card in Helios: what its saved status says, in plain words.
export type CalendarStatus = { state: "ok" | "needs_connect" | "error"; message: string; at: number };

export function calendarLine(s: CalendarStatus | null | undefined, now = Date.now() / 1000): { tone: "ok" | "warn" | "bad"; text: string } {
  if (!s) return { tone: "warn", text: "Not connected yet: press Connect Google Calendar." };
  if (s.state === "ok") {
    const m = Math.max(0, Math.round((now - s.at) / 60));
    return { tone: "ok", text: `Connected. Last read ${m < 1 ? "just now" : m < 60 ? `${m} min ago` : `${Math.round(m / 60)} h ago`}.` };
  }
  return { tone: s.state === "needs_connect" ? "warn" : "bad", text: s.message || "Google didn't answer." };
}
