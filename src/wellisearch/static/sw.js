// wellisearch service worker: app-shell caching only.
// The dashboard polls /api/* and /health every five seconds, so those
// requests always go straight to the network and are never cached.
const VERSION = "v2";
const SHELL_CACHE = `wellisearch-shell-${VERSION}`;
const SHELL = [
  "/",
  "/search",
  "/manifest.webmanifest",
  "/search.css",
  "/search.js",
  "/markdown.js",
  "/icon-180.png",
  "/icon-192.png",
  "/icon-512.png",
  "/icon-512-maskable.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL_CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  const path = url.pathname;
  // API + health probes: never cache, always network.
  if (path.startsWith("/api/") || path === "/health") return;
  if (request.method !== "GET") return;
  event.respondWith(cacheFirst(request));
});

async function cacheFirst(request) {
  const cached = await caches.match(request, { ignoreSearch: true });
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok) {
      const copy = response.clone();
      const cache = await caches.open(SHELL_CACHE);
      await cache.put(request, copy);
    }
    return response;
  } catch (err) {
    // Offline navigation: fall back to the cached dashboard shell.
    if (request.mode === "navigate") {
      return (await caches.match("/")) || Response.error();
    }
    throw err;
  }
}
