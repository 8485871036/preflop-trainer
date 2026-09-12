/* Preflop Bible Trainer — offline shell.
   Bump CACHE whenever index.html changes so clients pick the new build up. */
const CACHE = "preflop-trainer-v2";

const SHELL = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./icons/icon-192.png",
  "./icons/icon-512.png",
  "./icons/icon-maskable-512.png",
  "./icons/favicon-64.png"
];

self.addEventListener("install", e => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => Promise.allSettled(SHELL.map(u => c.add(u))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

const isFont = url =>
  url.hostname === "fonts.googleapis.com" || url.hostname === "fonts.gstatic.com";

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  /* Google Fonts: cache the first successful fetch, then serve it forever. */
  if (isFont(url)) {
    e.respondWith(
      caches.open(CACHE).then(c =>
        c.match(req).then(hit =>
          hit || fetch(req).then(res => { if (res.ok || res.type === "opaque") c.put(req, res.clone()); return res; })
                           .catch(() => hit)
        )
      )
    );
    return;
  }

  /* App shell: cache first, fall back to the network, then to index.html. */
  e.respondWith(
    caches.match(req).then(hit =>
      hit || fetch(req)
        .then(res => {
          if (res.ok && url.origin === self.location.origin) {
            const copy = res.clone();
            caches.open(CACHE).then(c => c.put(req, copy));
          }
          return res;
        })
        .catch(() => (req.mode === "navigate" ? caches.match("./index.html") : Response.error()))
    )
  );
});
