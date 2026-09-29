// Helios service worker: only for the phone's share menu. A share arrives as a POST to /helios/share-target;
// it is kept in a cache (no token is needed for that) and Helios opens its Share page to send it to Argus.
const CACHE = "helios-share";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "POST" || url.pathname !== "/helios/share-target") return;
  e.respondWith((async () => {
    const form = await e.request.formData();
    const cache = await caches.open(CACHE);
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
  })());
});
