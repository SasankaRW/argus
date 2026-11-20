import { describe, expect, it } from "vitest";
import { agoShort, pushLine } from "./MapNews";
import type { StripLine } from "./mapStrip";

const line = (key: string, text = `line ${key}`): StripLine & { at: number } => ({ key, icon: "task_alt", text, tone: "ok", at: 0 });

describe("pushLine", () => {
  it("newest first, three at most", () => {
    let l: ReturnType<typeof pushLine> = [];
    for (const k of ["1", "2", "3", "4"]) l = pushLine(l, line(k));
    expect(l.map((x) => x.key)).toEqual(["4", "3", "2"]);
  });
  it("ignores nothing and repeats of the newest", () => {
    const l = pushLine([line("1", "Calypso came online")], line("2", "Calypso came online"));
    expect(l).toHaveLength(1);
    expect(pushLine(l, null)).toBe(l);
  });
});

describe("agoShort", () => {
  it("reads like the preview", () => {
    expect(agoShort(2)).toBe("now");
    expect(agoShort(4)).toBe("4 s ago");
    expect(agoShort(75)).toBe("1 min ago");
    expect(agoShort(7300)).toBe("2 h ago");
  });
});
