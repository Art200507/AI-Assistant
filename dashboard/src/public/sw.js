// Minimal service worker — exists mainly so "Add to Home Screen" on iOS
// registers this as a real installable PWA. No offline mode: every real
// screen here (tasks, routing, chat, the hologram's live state) is
// inherently live data from the API, so caching it would just show stale
// readings with no way to tell they're stale — worse than a clear "no
// connection" failure.
//
// Network-first, not cache-first — caught live: a cache-first strategy
// served a stale pre-feature mobile.html even after a fresh deploy (the
// new Remote Chat panel was simply missing from the DOM, no error, no
// clue why). This app wants freshness over speed: always try the real
// network first, and only fall back to whatever's cached if the network
// genuinely fails (actually offline), not as the default path.
const CACHE_NAME = "jarvis-static-v2";

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  // Never cache API calls — those are the live data this whole app exists
  // to show accurately.
  if (url.pathname.startsWith("/api/")) return;
  if (event.request.method !== "GET") return;

  event.respondWith(
    fetch(event.request)
      .then((resp) => {
        if (resp.ok) {
          const copy = resp.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        }
        return resp;
      })
      .catch(() => caches.match(event.request))
  );
});
