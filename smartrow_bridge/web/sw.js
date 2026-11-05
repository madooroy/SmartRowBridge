// Minimal service worker: lets Chrome install the page as an app (where the page is a
// secure context: https, localhost, or an origin allowed by Chrome's
// "Insecure origins treated as secure" flag). It caches nothing: live rowing data must
// always come from the Pi.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (event) => event.respondWith(fetch(event.request)));
