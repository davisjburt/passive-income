// Service worker: cache the app shell for instant/offline load, but always try
// the network first for live data so numbers stay fresh.
const CACHE = "portfolio-v1";
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
  const isData =
    url.pathname.endsWith("data.json") ||
    url.pathname.includes("/api/live") ||
    url.pathname.includes("/reports/");

  if (isData) {
    // Network-first; on failure fall back to the last cached copy.
    e.respondWith(
      fetch(req)
        .then((r) => {
          const clone = r.clone();
          caches.open(CACHE).then((c) => c.put(req, clone));
          return r;
        })
        .catch(() => caches.match(req, { ignoreSearch: true }))
    );
    return;
  }

  // Cache-first for the static shell.
  e.respondWith(caches.match(req).then((r) => r || fetch(req)));
});
