// Minimal service worker — its presence is what makes Chrome on Android
// treat this site as a fully installable PWA (custom icon, standalone
// fullscreen mode). It doesn't need to do any offline caching for this
// app since everything here needs a live connection anyway.

self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', () => {
  // Pass-through — no offline caching, just needs to exist and respond.
});