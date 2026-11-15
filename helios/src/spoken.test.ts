import { describe, expect, it } from "vitest";
import { plain } from "./spoken";

describe("plain", () => {
  it("removes mood and sound tags and tidies the spacing", () => {
    expect(plain("[cheerful] Oh nice! [laugh]")).toBe("Oh nice!");
    expect(plain("Well [sigh] , okay.")).toBe("Well, okay.");
  });

  it("leaves other bracketed text alone", () => {
    expect(plain("see [1] for details")).toBe("see [1] for details");
  });
});
