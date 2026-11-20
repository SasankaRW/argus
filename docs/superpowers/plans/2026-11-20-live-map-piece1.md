# Live map, piece 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Helios desktop live map visibly alive on real data: dots that travel the lines, boxes that flash, a breathing core, an opening, a news banner and a "what just happened" strip.

**Architecture:** Two new pure modules decide *what* to show (`mapFlow.ts`: which event becomes which dot on which route; `mapStrip.ts`: which event becomes which strip line), tested with vitest. One hook (`useMapFlow.ts`) feeds new events into them; one component (`FlowLayer.tsx`) draws and moves the dots with `requestAnimationFrame` and flashes boxes through the DOM, so React does not re-render per frame. `LevelCanvas` gets an optional flow layer; `MapView` adds the banner and strip. A small core change puts a message's tags on its `notify.queued` event so the map can tell a wishlist alert.

**Tech Stack:** React 19, TypeScript 5.9, Vite 7, vitest 3 (helios/); Python 3.12, pytest (core, plugins).

**Spec:** `docs/superpowers/specs/2026-11-20-live-map-and-stock-alerts-design.md` (part 1, and settled item 2). Approved look: `docs/superpowers/specs/2026-11-20-live-map-preview.html` (copy its CSS values and keyframes).

## Global Constraints

- Material 3 Expressive tokens from `m3.css` / `livemap.css`; no gradients (including masks), no hover lift (no box moves on hover), sentence case, no full stops on UI labels.
- All motion off under `prefers-reduced-motion: reduce`, and paused while the tab is hidden (`document.hidden`; CSS already pauses via `html[data-paused="1"]`).
- Emphasized decelerate `cubic-bezier(.05, .7, .1, 1)`; standard `cubic-bezier(.2, 0, 0, 1)`.
- Dots: at most 12 on screen, at most one per route every 600 ms, about 300 canvas px/s (news 420 px/s), eased in-out.
- Busy line under dots: stroke-opacity .42, width 3 (replaces the moving dash only when flow is on and motion allowed).
- Core: 9-lobe scalloped badge turning once per 24 s; ring breathing every 3.4 s (inset -2px → -16px, opacity .8 → 0).
- Opening: once per page load of the map, boxes rise 560 ms with 90 ms per column + 80 ms; solid lines draw in 700 ms starting at column × 90 + 260 ms.
- Banner ~5.2 s; strip keeps 3 chips, newest first.
- The Flow toggle in the header (`App.tsx` `flow`) still turns all of this off.
- Every Helios change: `.\scripts\dev.ps1 helios` (rebuild) and commit `core/argus/helios_dist` in the same PR; commits `type(scope): msg`, no attribution lines.

## Review Focus

1. **A burst of identical events** (6 `job.queued` in one second, seen live from `screenshot-renamer`) must give one dot, not six: test in Task 1.
2. **An event between boxes with no line** (worker → a hidden service, or `from` null) must give no dot and not throw: test in Task 1.
3. **Reconnect/backlog:** when the event list jumps (first load, or a reconnect replays hundreds of events) only events newer than the moment the map mounted make dots; nothing old animates: test in Task 3 (`useMapFlow` starts from the newest seq at mount).
4. **Layout changes under a dot** (a box appears or a machine goes offline) must drop dots whose route is gone instead of drawing at NaN: test in Task 1 (`liveRoutes` filter) and guard in Task 4.
5. **Unknown event kinds** in the strip must be skipped, and a strip line never shows raw ids like `worker-saspc`: tests in Task 2.

---

### Task 1: `mapFlow.ts`, which event makes which dot

**Files:**
- Create: `helios/src/mapFlow.ts`
- Test: `helios/src/mapFlow.test.ts`

**Interfaces:**
- Consumes: `ArgusEvent` (api.ts), `Routed` (mapGraph.ts: `{a, b, kind, d, mx, my}`, path drawn from a to b), `boxOf` (mapGraph.ts).
- Produces:
  - `export type Dot = { id: string; route: string; back: boolean; tone: "work" | "news"; at: number }` — `route` is `"a>b"` of a `Routed`.
  - `export const routeKey = (r: { a: string; b: string }) => \`${r.a}>${r.b}\``
  - `export function routeBetween(x: string, y: string, routes: Routed[]): { route: string; back: boolean } | null` — the route joining boxes x and y; `back` true when it is drawn from y to x.
  - `export function pickDots(events: ArgusEvent[], o: { afterSeq: number; routes: Routed[]; of: (id: string) => string | null; last: Map<string, number>; nowMs: number }): { dots: Dot[]; seq: number }` — dots for events with `seq > afterSeq`, oldest first; mutates `last` (route → ms of its last dot); returns the newest seq seen.
  - `export function newsRoute(e: ArgusEvent, routes: Routed[], of: (id: string) => string | null): string[][]` — for a `notify.queued` event with priority ≥ 4 in `data.priority` or a tag `wishlist` in `data.tags`: hops from the plugin's box home. Returns one or two chains of `"<a>b"`/`"a>b"` steps (see step 3).
  - `export const MAX_DOTS = 12, GAP_MS = 600`

- [ ] **Step 0: Branch** — `git switch -c feat/live-map` (from an up-to-date `main`).

- [ ] **Step 1: Write the failing tests** (`mapFlow.test.ts`, copying the `node`/`sample` helpers of `mapGraph.test.ts`; its own `ev(from, to, ago, seq)` sets `seq` and `at = NOW - ago`; `routes` from `layoutLevels(buildLevels(sample(), ctx).cards, ..., <the desktop options in MapView.tsx>)`, `of = boxOf(sample())`, and `opts()` = `{ afterSeq: 0, routes, of, last: new Map(), nowMs: 0 }`)

```ts
it("one dot per event along the line between its boxes", () => {
  const { dots } = pickDots([ev("worker-laptop", "advisor", 1, 10)], { afterSeq: 0, routes, of, last: new Map(), nowMs: 0 });
  expect(dots).toEqual([expect.objectContaining({ route: "worker-laptop>plugins", back: false, tone: "work" })]);
});
it("runs against the line when the event goes the other way", () => {
  const { dots } = pickDots([ev("advisor", "worker-laptop", 1, 10)], opts());
  expect(dots[0]).toMatchObject({ route: "worker-laptop>plugins", back: true });
});
it("a burst on one route makes one dot", () => {
  const six = [1, 2, 3, 4, 5, 6].map((s) => ev("worker-laptop", "advisor", 1, s));
  expect(pickDots(six, opts()).dots).toHaveLength(1);
});
it("again after the gap", () => {
  const last = new Map([["worker-laptop>plugins", 0]]);
  expect(pickDots([ev("worker-laptop", "advisor", 1, 7)], { ...opts(), last, nowMs: 599 }).dots).toHaveLength(0);
  expect(pickDots([ev("worker-laptop", "advisor", 1, 8)], { ...opts(), last, nowMs: 600 }).dots).toHaveLength(1);
});
it("no line, no dot, no throw", () => {
  expect(pickDots([ev("worker-laptop", "island", 1, 9), { ...ev("x", "y", 1, 10), from: null }], opts()).dots).toEqual([]);
});
it("only events after afterSeq, and returns the newest seq", () => {
  const r = pickDots([ev("helios", "argus", 1, 3), ev("helios", "argus", 1, 9)], { ...opts(), afterSeq: 5 });
  expect(r.dots).toHaveLength(1); expect(r.seq).toBe(9);
});
it("a wishlist alert runs home to the phone", () => {
  const e = { ...ev("watchers", "argus", 1, 11), kind: "notify.queued", data: { title: "Watcher: Wishlist: Dune book", priority: 4, tags: ["eyes", "wishlist"] } };
  const chains = newsRoute(e, routes, of);
  expect(chains[0]).toEqual(["<worker-laptop>plugins", "worker-laptop>argus", "<phone>argus"]);
});
it("ordinary messages are not news", () => {
  expect(newsRoute({ ...ev("advisor", "argus", 1, 12), kind: "notify.queued", data: { priority: 3 } }, routes, of)).toEqual([]);
});
```

(Build the map for these tests with `sample({ nodes: [node("watchers", "plugin")] })` so the alert has a plugin to start from. Adjust the expected `route` keys to whatever `a>b` the real `buildLevels` lines produce: machine lines are `{a: machine, b: "argus"}` and `{a: machine, b: "plugins"}`, app lines `{a: app, b: "argus"}`.)

- [ ] **Step 2: Run to see them fail**

Run: `cd helios; npx vitest run src/mapFlow.test.ts`
Expected: FAIL, "Cannot find module './mapFlow'"

- [ ] **Step 3: Implement `mapFlow.ts`**

`pickDots`: for each event with `seq > afterSeq` and both ends mapped by `of` to different boxes, `routeBetween`; skip when `nowMs - last.get(route) < GAP_MS`; set `last`; `id = \`${e.seq}\``; `tone "work"`. `newsRoute`: the news starts at the event's `from` box (a plugin maps to `plugins`); hop to the machine that joins `plugins` (prefer Calypso: the first route `X>plugins` whose X `workerLabel` is "Calypso", else the first), then machine → `argus`, then `argus` → `phone`; written as steps with `<` when travelled against the drawn direction. When `data.tags` includes `wishlist` and a route joins a box `lifehub` (or `life-hub`) to `argus`, return a second chain `[that step]` that starts at Argus. Missing routes: return `[]`.

- [ ] **Step 4: Run to see them pass**

Run: `cd helios; npx vitest run src/mapFlow.test.ts`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add helios/src/mapFlow.ts helios/src/mapFlow.test.ts
git commit -m "feat(helios): decide which events travel the map's lines"
```

---

### Task 2: `mapStrip.ts`, what just happened, in plain words

**Files:**
- Create: `helios/src/mapStrip.ts`
- Test: `helios/src/mapStrip.test.ts`

**Interfaces:**
- Consumes: `ArgusEvent`, `IconName` (icons.ts).
- Produces: `export type StripLine = { key: string; icon: IconName; text: string; tone: "ok" | "warn" | "bad" }` and `export function stripLine(e: ArgusEvent, name: (id: string) => string | null): StripLine | null` — `name` gives a box's label (`labelOf`), or null for ids not on the map.

The table (event kind → line); `{p}` is `name(e.to)` for job events (the plugin), `{w}` `name(e.from)` for worker events:

| kind | text | icon | tone |
|---|---|---|---|
| `job.succeeded` | `{p} finished` | `task_alt` | ok |
| `job.dead` | `{p} failed` | `error` | bad |
| `approval.requested` | `{p} is waiting for you` | `front_hand` | warn |
| `notify.queued` | `Told your phone: {data.title without "Watcher: " and "Wishlist: "}` | `notifications` (`sell` when tagged wishlist) | ok (warn when wishlist) |
| `model.escalated` | `Asked {name(e.to)} for help` | `neurology` | ok |
| `worker.online` / `worker.offline` | `{w} came online` / `{w} went offline` | `computer` | ok / bad |
| `phone.online` / `phone.offline` | `Phone is back` / `Phone went offline` | `smartphone` | ok / bad |
| `check.failed` | `A check failed on {w}` | `error` | bad |

Anything else, or a name that is null: `null`. Only icons already in `icons.ts`; add any missing name to `icons.ts` and run `python scripts/icons.py` (Material Symbols subset) in the same task.

- [ ] **Step 1: Write the failing tests**

```ts
it("a finished job reads as the plugin's name", () => {
  expect(stripLine({ ...ev("worker-laptop", "screenshot-renamer"), kind: "job.succeeded" }, names)).toEqual({ key: "1", icon: "task_alt", text: "Screenshot renamer finished", tone: "ok" });
});
it("a wishlist alert reads as the news, without the prefixes", () => {
  const e = { ...ev("watchers", "argus"), kind: "notify.queued", data: { title: "Watcher: Wishlist: Dune book", tags: ["wishlist"] } };
  expect(stripLine(e, names)).toMatchObject({ icon: "sell", text: "Told your phone: Dune book", tone: "warn" });
});
it("never shows a raw id", () => {
  expect(stripLine({ ...ev("worker-zz", "x"), kind: "worker.online" }, () => null)).toBeNull();
});
it("skips kinds it doesn't know", () => {
  expect(stripLine({ ...ev("a", "b"), kind: "edge.added" }, names)).toBeNull();
});
```

- [ ] **Step 2: Run to see them fail** — `cd helios; npx vitest run src/mapStrip.test.ts` → FAIL (module missing)
- [ ] **Step 3: Implement `stripLine`** as a lookup table keyed by kind.
- [ ] **Step 4: Run to see them pass** — PASS (4 tests)
- [ ] **Step 5: Commit** — `git commit -m "feat(helios): say what just happened on the map in plain words"`

---

### Task 3: tags on `notify.queued`, and Watchers marks wishlist alerts

**Files:**
- Modify: `core/argus/context.py` (`notify_from_job`, the `insert_event(... "notify.queued" ...)` call)
- Modify: `plugins/watchers/plugin.py` (`check`, the `ctx.notify(...)` call)
- Test: `tests/test_watchers.py`, and the existing notify test file that covers `notify_from_job` (find with `grep -ln notify_from_job tests`)

**Interfaces:**
- Produces: `notify.queued` event `data = {"title": str, "priority": int, "tags": list[str]}`. Watchers' messages for watches named `Wishlist: …` have tags `["eyes", "wishlist"]`. Used by Task 1 (`newsRoute`), Task 2, and pieces 3–5.

- [ ] **Step 1: Failing tests**: `test_notify_queued_event_carries_priority_and_tags` (queue a message with `priority="high", tags=["eyes", "wishlist"]`; the event's data is `{"title": ..., "priority": 4, "tags": ["eyes", "wishlist"]}`); `test_wishlist_watch_alert_is_tagged` (a `Wishlist: Dune book` price watch going out → in: the notify call's tags are `["eyes", "wishlist"]`; a watch named `Dune book on x.lk` keeps `["eyes"]`).
- [ ] **Step 2: Run** — `.venv\Scripts\python -m pytest tests/test_watchers.py -k tagged -q` → FAIL
- [ ] **Step 3: Implement**: add `"priority": message.get("priority"), "tags": message.get("tags") or []` to the event data; in Watchers, `tags=["eyes", "wishlist"] if w["name"].startswith("Wishlist: ") else ["eyes"]`.
- [ ] **Step 4: Run** — the two tests and the whole `tests/test_watchers.py` PASS
- [ ] **Step 5: Commit** — `git commit -m "feat(watchers): tag wishlist alerts and put tags on notify events"`

---

### Task 4: the flow layer, dots that move and boxes that flash

**Files:**
- Create: `helios/src/useMapFlow.ts`, `helios/src/FlowLayer.tsx`
- Modify: `helios/src/LevelCanvas.tsx` (optional `flow` children: renders `<FlowLayer>` inside `.lm-cv`, after the svg), `helios/src/MapView.tsx` (main map canvases only, not the plugin view), `helios/src/livemap.css`

**Interfaces:**
- Consumes: Task 1 (`pickDots`, `newsRoute`, `routeKey`, `MAX_DOTS`), `Layout`.
- Produces:
  - `export function useMapFlow(events: ArgusEvent[], layout: Layout, of: (id: string) => string | null, on: boolean): { dots: Dot[]; news: ArgusEvent[]; done: (id: string) => void }` — starts from the newest `seq` present at mount (no backlog), keeps at most `MAX_DOTS` (drops oldest), drops dots whose route is no longer in `layout.routes`, returns `news` (wishlist or priority ≥ 4 `notify.queued` events since mount) for Task 6.
  - `export function FlowLayer(p: { layout: Layout; dots: Dot[]; chains: string[][][]; onDone: (id: string) => void; onArrive: (box: string, tone: "work" | "news") => void })` — an absolutely placed `<svg class="lm-dots">` the size of the canvas. One `requestAnimationFrame` loop for all dots; positions from `SVGPathElement.getPointAtLength` on hidden `<path d>` copies keyed by route; a head circle (r 4.2, `#e8fff0` for work / `#fff4d0` for news, `drop-shadow(0 0 5px <colour>)`) plus three trail circles at -0.035 progress steps (r 2.6 / 2.0 / 1.4, opacity .38 / .26 / .14). Work colour `var(--lm-busy)`, news `var(--lm-slow)`. On arrival: `onArrive(boxId)`; for news chains the next hop starts at arrival.
  - Flashing: `onArrive` adds class `hit` (or `hit-warn` for news) to `.lm-n[data-card="<box>"]`, removing and re-adding it (force reflow) to restart; CSS keyframes `hit` / `hitw` and the icon `pop` copied from the preview.
- When `flow` is false, motion is reduced, or the tab is hidden: `useMapFlow` returns no dots and the loop stops.
- Busy lines: `.lm-cv.dots .lm-e.busy { stroke-dasharray: none; animation: none; stroke-opacity: .42; stroke-width: 3; }`, with `dots` on the canvas only when the layer is active.
- Pointing at a box: dots whose route touches the box stay, others get `.dim` (opacity .25), using the `focus` state `LevelCanvas` already keeps (pass the near set to `FlowLayer`).

- [ ] **Step 1: Failing test** for the hook's pure core: export `nextDots(prev: Dot[], picked: Dot[], liveRoutes: Set<string>): Dot[]` from `useMapFlow.ts` and test: keeps at most 12 (newest kept); drops dots whose route is not in `liveRoutes`. (`useMapFlow.test.ts`)
- [ ] **Step 2: Run** — `npx vitest run src/useMapFlow.test.ts` → FAIL
- [ ] **Step 3: Implement** `nextDots`, `useMapFlow`, `FlowLayer`, the CSS, and the wiring in `LevelCanvas`/`MapView` (the desktop map, the command layout and the sideways phone; the upright phone map gets dots and flashes too).
- [ ] **Step 4: Run** — `npx vitest run` (all Helios tests) and `npm run typecheck` → PASS
- [ ] **Step 5: Look at it**: `.\scripts\dev.ps1 up`, open http://127.0.0.1:8600/helios/, run `.\scripts\dev.ps1 demo` a few times: dots travel, boxes flash, nothing moves with reduced motion turned on (Windows: Settings > Accessibility > Visual effects > Animation effects off). Then `.\scripts\dev.ps1 down`.
- [ ] **Step 6: Commit** — `git commit -m "feat(helios): jobs travel the map as dots, boxes flash when work arrives"`

---

### Task 5: the living core and the opening

**Files:**
- Modify: `helios/src/LevelCanvas.tsx`, `helios/src/livemap.css`, `helios/src/m3.tsx` (a `Cookie` badge if one doesn't already exist; check `m3.tsx` and `m3.css` for the existing rotating scalloped badge first and reuse it)
- Test: `helios/src/mapFlow.test.ts` (one test for `cookiePath`)

**Interfaces:**
- Produces: `export function cookiePath(size: number, lobes = 9, depth = 0.05): string` (in `mapFlow.ts` only if no badge path exists to reuse) — test: starts with `M`, ends with `Z`, every point within `size/2 * (1 ± depth) ± 0.5` of the centre.
- The core box (`skin "core"` / `big`, id `argus`): its icon tile becomes transparent with the cookie svg behind the icon (`#ffffff26`), turning 24 s linear; `.lm-n.big.core::after` ring, keyframes `ring` from the preview. Digits in its status get `<span class="num">` that re-mount (key = the value) so they play `tick` (520 ms) when they change.
- The opening: `LevelCanvas` takes `opening?: boolean`; MapView passes true on the first render after the map's first data load of this page view (a module-level flag so switching pages and back doesn't replay). Boxes get `style={{ "--col": c.level }}` and `.lm-cv.opening .lm-n { animation: lm-rise 560ms var(--lm-ease-out) backwards; animation-delay: calc(var(--col) * 90ms + 80ms) }`; solid route paths (`quiet`, `busy`) draw with a `stroke-dasharray`/`stroke-dashoffset` transition set from their `getTotalLength()` in a layout effect, cleared after 1.5 s; dashed lines and badges fade in at 620 ms.
- All of it inside `@media (prefers-reduced-motion: no-preference)`.

- [ ] **Step 1: Failing test** for `cookiePath` (or skip if reusing an existing, tested badge; say so in the commit)
- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** `npx vitest run` and `npm run typecheck` → PASS; look at it as in Task 4 step 5 (reload: the opening plays once; open Ari and come back: it doesn't replay)
- [ ] **Step 5: Commit** — `git commit -m "feat(helios): a living core and an opening for the map"`

---

### Task 6: the news banner and the strip

**Files:**
- Create: `helios/src/MapNews.tsx` (`MapBanner`, `MapStrip`)
- Modify: `helios/src/MapView.tsx` (desktop and command layouts; not the phone), `helios/src/livemap.css`

**Interfaces:**
- Consumes: Task 2 `stripLine`, Task 4 `useMapFlow(...).news`, `labelOf`.
- Produces:
  - `MapBanner({ news }: { news: ArgusEvent | null })` — top right under the attention row (`top: headTop + headSize.h + 8`, `right: 20`); amber container (`--lm-slowc` background, `--lm-slow` icon tile, radius 24); title = the message title without `Watcher: ` / `Wishlist: `; second line = `data.text` if present, else nothing (the event carries the title only; add `"text": message["message"][:200]` to the `notify.queued` data in Task 3's change if the builder wants the second line — then extend Task 3's test). Shows when the news dot reaches the Phone box (`onArrive("phone", "news")`), hides after 5.2 s; `role="status"`.
  - `MapStrip({ lines }: { lines: StripLine[] })` — bottom left (`left: pad.l + 18`, `bottom: pad.b + 14`, clear of the zoom buttons on the right); three chips, newest first; relative time in Roboto Mono (`now`, `4 s ago`, `1 min ago` from each line's event `at`, refreshed with the map's 5 s clock); FLIP on insert (measure `offsetLeft`, prepend, `element.animate` translateX from the old place, 460 ms emphasized decelerate); the leaving chip gets `position: absolute` at its old `left` and fades in 300 ms, then is removed. `aria-live="polite"`.
- Strip lines come from all new events (since mount) through `stripLine`, deduped by consecutive identical text.

- [ ] **Step 1: Failing test**: export `pushLine(prev: StripLine[], next: StripLine | null): StripLine[]` from `MapNews.tsx` — test: newest first, keeps 3, ignores null, ignores a line whose text equals the newest's.
- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** `npx vitest run`, `npm run typecheck` → PASS. Look at it: `dev.ps1 up`, `dev.ps1 notify` with a high-priority test message shows the amber dot and the banner; the strip fills as `demo` jobs finish; the fourth chip leaves without the row jumping. `dev.ps1 down`.
- [ ] **Step 5: Commit** — `git commit -m "feat(helios): a news banner and a what-just-happened strip on the map"`

---

### Task 7: ship

- [ ] **Step 1:** `.\scripts\dev.ps1 helios` (rebuilds `core/argus/helios_dist`); check the new `main-*.js` contains `lm-dots` (`Select-String -Path core\argus\helios_dist\assets\main-*.js -Pattern lm-dots`).
- [ ] **Step 2:** `.\scripts\dev.ps1 test` → all pass; `cd helios; npm run typecheck; npx vitest run` → pass.
- [ ] **Step 3:** CHANGELOG entry under the next version: "Helios: the live map shows work travelling its lines, boxes react, the core breathes, news runs home to your phone, and a strip says what just happened."
- [ ] **Step 4:** `git add core/argus/helios_dist CHANGELOG.md; git commit -m "build(helios): rebuild for the livelier map"`; `.\scripts\dev.ps1 pr`.
- [ ] **Step 5:** Stop anything this task started (`dev.ps1 down`), and say so.
