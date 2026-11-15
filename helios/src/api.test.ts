import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, AuthError, getToken, setToken } from "./api";

function memoryStorage() {
  const m = new Map<string, string>();
  return {
    getItem: (k: string) => m.get(k) ?? null,
    setItem: (k: string, v: string) => void m.set(k, v),
    removeItem: (k: string) => void m.delete(k),
  };
}

const reply = (status: number, body: unknown) =>
  ({ status, ok: status >= 200 && status < 300, json: async () => body }) as Response;

beforeEach(() => { vi.stubGlobal("localStorage", memoryStorage()); });
afterEach(() => { vi.unstubAllGlobals(); });

describe("token storage", () => {
  it("saves and clears the token", () => {
    expect(getToken()).toBeNull();
    setToken("abc");
    expect(getToken()).toBe("abc");
    setToken(null);
    expect(getToken()).toBeNull();
  });

  it("survives storage that throws", () => {
    vi.stubGlobal("localStorage", {
      getItem() { throw new Error("blocked"); },
      setItem() { throw new Error("blocked"); },
    });
    expect(getToken()).toBeNull();
    expect(() => setToken("x")).not.toThrow();
  });
});

describe("api", () => {
  it("sends the bearer token and a JSON content type only with a body", async () => {
    setToken("tok");
    const f = vi.fn().mockResolvedValue(reply(200, { ok: 1 }));
    vi.stubGlobal("fetch", f);
    expect(await api("/status")).toEqual({ ok: 1 });
    expect(f.mock.calls[0][1].headers).toEqual({ Accept: "application/json", Authorization: "Bearer tok" });
    await api("/jobs", { method: "POST", body: "{}" });
    expect(f.mock.calls[1][1].headers["Content-Type"]).toBe("application/json");
  });

  it("throws AuthError on 401", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(reply(401, {})));
    await expect(api("/status")).rejects.toBeInstanceOf(AuthError);
  });

  it("uses the server's detail text for other errors, else the status", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(reply(400, { detail: "bad input" })));
    await expect(api("/x")).rejects.toThrow("bad input");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(reply(500, {})));
    await expect(api("/y")).rejects.toThrow("/y: HTTP 500");
  });

  it("returns the body of a 503 instead of throwing", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(reply(503, { status: "starting" })));
    expect(await api("/status")).toEqual({ status: "starting" });
  });
});
