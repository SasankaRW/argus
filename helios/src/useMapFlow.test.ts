import { describe, expect, it } from "vitest";
import type { Dot } from "./mapFlow";
import { nextDots } from "./useMapFlow";

const dot = (id: number, route = "a>b"): Dot => ({ id: `${id}`, route, back: false, tone: "work", at: id });

describe("nextDots", () => {
  it("keeps at most 12, the newest", () => {
    const out = nextDots(Array.from({ length: 10 }, (_, i) => dot(i)), [dot(10), dot(11), dot(12), dot(13)], new Set(["a>b"]));
    expect(out).toHaveLength(12);
    expect(out[0].id).toBe("2");
    expect(out[11].id).toBe("13");
  });
  it("drops dots whose line is gone", () => {
    const out = nextDots([dot(1, "a>b"), dot(2, "x>y")], [dot(3, "x>y")], new Set(["a>b"]));
    expect(out.map((d) => d.id)).toEqual(["1"]);
  });
  it("ignores a dot it already has", () => {
    expect(nextDots([dot(1)], [dot(1)], new Set(["a>b"]))).toHaveLength(1);
  });
});
