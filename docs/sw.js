// Service worker. Network-first for the page + data so updates show immediately
// when online; cache is only a fallback for offline. Static assets (icons,
// manifest) are cache-first since they rarely change.
const CACHE = "portfolio-v2";
const SHELL = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./icon-192.png",
  "./icon-512.png",
  "./apple-touch-icon.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  const isDoc =
    req.mode === "navigate" ||
    url.pathname.endsWith("/") ||
    url.pathname.endsWith("index.html");
  const isData =
    url.pathname.endsWith("data.json") ||
    url.pathname.includes("/api/live") ||
    url.pathname.includes("/reports/");

  if (isDoc || isData) {
    // Network-first: always try fresh, cache the result, fall back if offline.
    e.respondWith(
      fetch(req)
        .then((r) => {
          const clone = r.clone();
          caches.open(CACHE).then((c) => c.put(req, clone));
          return r;
        })
        .catch(async () => {
          const hit = await caches.match(req, { ignoreSearch: true });
          return hit || caches.match("./index.html");
        })
    );
    return;
  }

  // Cache-first for static assets.
  e.respondWith(caches.match(req).then((r) => r || fetch(req)));
});
