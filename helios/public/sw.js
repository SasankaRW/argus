// Helios service worker: makes Argus an app on the phone.
// - The share menu: a share arrives as a POST to /helios/share-target; it is kept in a cache (no token needed for
//   that) and Helios opens its Share page to send it to Argus.
// - Opens instantly: the app itself (the page and its built files) is kept; the page is fetched fresh when Argus
//   answers and the kept one is used when it doesn't (then Helios says it can't reach Argus). Argus's data is never
//   kept here: every API call goes to Argus.
const SHARE = "helios-share";
const SHELL = "helios-shell-v3";

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(["/helios/", "/helios/manifest.webmanifest", "/helios/icon-192.png", "/helios/favicon.png"])).catch(() => {}));
  self.skipWaiting();
});
self.addEventListener("activate", (e) => e.waitUntil((async () => {
  for (const k of await caches.keys()) if (k.startsWith("helios-shell") && k !== SHELL) await caches.delete(k);
  await self.clients.claim();
})()));

async function share(req) {
  const form = await req.formData();
  const cache = await caches.open(SHARE);
  for (const k of await cache.keys()) await cache.delete(k);
  const files = form.getAll("files").filter((f) => f instanceof File && f.size > 0);
  const meta = {
    title: String(form.get("title") || ""),
    text: String(form.get("text") || ""),
    url: String(form.get("url") || ""),
    files: files.map((f, i) => ({ key: `/helios/__share/${i}`, name: f.name || `shared-${i + 1}`, type: f.type, size: f.size })),
    at: Date.now(),
  };
  await Promise.all(files.map((f, i) => cache.put(`/helios/__share/${i}`,
    new Response(f, { headers: { "Content-Type": f.type || "application/octet-stream" } }))));
  await cache.put("/helios/__share/meta", new Response(JSON.stringify(meta), { headers: { "Content-Type": "application/json" } }));
  return Response.redirect("/helios/#share", 303);
}

// The page: fresh from Argus, the kept copy when Argus doesn't answer within a few seconds.
async function page(req) {
  const cache = await caches.open(SHELL);
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 4000);
    const res = await fetch(req, { signal: ctl.signal });
    clearTimeout(t);
    if (res.ok) cache.put("/helios/", res.clone());
    return res;
  } catch {
    return (await cache.match("/helios/")) || new Response("Argus can't be reached.", { status: 503 });
  }
}

// Built files have their hash in the name: once kept, always right.
async function asset(req) {
  const cache = await caches.open(SHELL);
  const hit = await cache.match(req);
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok) cache.put(req, res.clone());
  return res;
}

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.origin !== self.location.origin) return;
  if (e.request.method === "POST" && url.pathname === "/helios/share-target") { e.respondWith(share(e.request)); return; }
  if (e.request.method !== "GET") return;
  if (e.request.mode === "navigate" && url.pathname.startsWith("/helios")) { e.respondWith(page(e.request)); return; }
  if (url.pathname.startsWith("/helios/assets/") || /^\/helios\/(icon|sc|favicon)[\w-]*\.png$/.test(url.pathname)) {
    e.respondWith(asset(e.request));
  }
});
