import { describe, expect, it } from "vitest";
import { calendarLine } from "./calendarStatus";

describe("calendarLine", () => {
  it("asks to connect when nothing is saved yet", () => {
    expect(calendarLine(null)).toEqual({ tone: "warn", text: "Not connected yet: press Connect Google Calendar." });
  });
  it("says when it last read", () => {
    expect(calendarLine({ state: "ok", message: "Connected.", at: 1000 }, 1000 + 7 * 60).text).toBe("Connected. Last read 7 min ago.");
    expect(calendarLine({ state: "ok", message: "", at: 1000 }, 1010).text).toBe("Connected. Last read just now.");
  });
  it("gives Google's reason in plain words", () => {
    const why = "The Google Calendar API isn't switched on in your Google Cloud project.";
    expect(calendarLine({ state: "error", message: why, at: 1 })).toEqual({ tone: "bad", text: why });
    expect(calendarLine({ state: "needs_connect", message: "Google took the OK back.", at: 1 }).tone).toBe("warn");
  });
});
