import { describe, expect, it } from "vitest";
import { buildPluginView, PL } from "./pluginGraph";
import { deviceIcon, fraction, isMoving, pace, result, size, type SendLiveItem } from "./sendLive";

const item = (over: Partial<SendLiveItem> = {}): SendLiveItem => ({
  offer_id: "o1", leg: "send", from: "phone", to: "pc", label: "Home Lab review.pdf", files: 1,
  total: 100 * 1024 * 1024, done: 62 * 1024 * 1024, state: "moving", speed: 4.3 * 1024 * 1024, eta: 3.2, ...over,
});
const name = (id: string) => ({ phone: "my phone", pc: "PC", calypso: "Calypso" }[id] ?? id);

describe("Argus Send in Helios", () => {
  it("says how far and how fast, in plain words", () => {
    expect(fraction(item())).toBeCloseTo(0.62);
    expect(pace(item())).toBe("4.3 MB/s · 3 s left");
    expect(pace(item({ eta: null }))).toBe("4.3 MB/s");
    expect(pace(item({ state: "verifying" }))).toBe("Checking");
    expect(size(1536)).toBe("2 KB");
    expect(result(item({ state: "done" }), name)).toBe("Saved to PC");
    expect(result(item({ state: "cancelled" }), name)).toBe("Cancelled");
    expect(isMoving(item({ state: "failed" }))).toBe(false);
    expect([deviceIcon("phone"), deviceIcon("pc"), deviceIcon("calypso"), deviceIcon("tv")])
      .toEqual(["smartphone", "desktop_windows", "laptop_chromebook", "devices"]);
  });

  it("shows Send on the plugin view while a file moves, and with the idle plugins otherwise", () => {
    const map = { seq: 1, nodes: [], edges: [] };
    const idle = buildPluginView(map, { now: 0, events: [] });
    expect(idle.idle).toContain("Send");
    const busy = buildPluginView(map, { now: 0, events: [], transfers: [item()], name });
    const send = busy.cards.find((c) => c.id === "send")!;
    expect(send.status).toBe("my phone → PC · 62%");
    expect(send.meter).toBeCloseTo(0.62);
    expect(busy.cards.find((c) => c.id === "dev:phone")!.level).toBe(PL.inputs);
    expect(busy.cards.find((c) => c.id === "dev:pc")!.level).toBe(PL.outputs);
    expect(busy.lines).toEqual([{ a: "dev:phone", b: "send", kind: "busy" }, { a: "send", b: "dev:pc", kind: "busy" }]);
    expect(busy.idle).not.toContain("Send");
    expect(busy.running).toBe(1);
  });
});
