/* Service Worker for /admin/* — offline shell caching + Web Push */
const CACHE = 'admin-v3';
const SHELL_URLS = [
  '/admin/',
  '/admin/dashboard',
  '/admin/orders',
  '/admin/products',
  '/admin/settings',
  '/admin/account',
  '/admin/coupons',
  '/admin/testimonials',
  '/admin/faqs',
  '/admin/sections',
];

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(CACHE).then(function (cache) {
      return cache.addAll(SHELL_URLS).catch(function () {
        // Individual failures are OK — shell still works for cached items
      });
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(
        keys.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); })
      );
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', function (event) {
  var url = new URL(event.request.url);

  // Only intercept /admin/ requests + static assets
  if (
    !url.pathname.startsWith('/admin/') &&
    !url.pathname.startsWith('/static/') &&
    url.pathname !== '/admin'
  ) {
    return;
  }

  // For API calls and form POSTs, try network first, fall back to stale cache
  if (url.pathname.startsWith('/api/') || event.request.method !== 'GET') {
    event.respondWith(
      fetch(event.request).catch(function () {
        return caches.match(event.request);
      })
    );
    return;
  }

  // For static assets (CSS, JS, images, fonts): cache-first, update cache in background
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(event.request).then(function (cached) {
        var fetchPromise = fetch(event.request).then(function (response) {
          return caches.open(CACHE).then(function (cache) {
            cache.put(event.request, response.clone());
            return response;
          });
        });
        return cached || fetchPromise;
      })
    );
    return;
  }

  // For admin HTML pages: network-first with cache fallback
  event.respondWith(
    fetch(event.request)
      .then(function (response) {
        var clone = response.clone();
        caches.open(CACHE).then(function (cache) {
          cache.put(event.request, clone);
        });
        return response;
      })
      .catch(function () {
        return caches.match(event.request).then(function (cached) {
          return cached || new Response('Offline', { status: 503 });
        });
      })
  );
});

/* ── Web Push ── */
self.addEventListener('push', function (event) {
  var data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { title: 'New Order', body: event.data ? event.data.text() : '' };
  }

  var title = data.title || 'Store Update';
  var options = {
    body: data.body || '',
    icon: data.icon || '/static/favicon.ico',
    badge: '/static/favicon.ico',
    data: data.data || {},
    tag: data.tag || 'admin-notification',
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  var target = '/admin/orders';
  if (event.notification.data && event.notification.data.url) {
    target = event.notification.data.url;
  }
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (clientList) {
      for (var i = 0; i < clientList.length; i++) {
        var client = clientList[i];
        if (client.url.indexOf('/admin/') !== -1 && 'focus' in client) {
          client.focus();
          client.navigate(target);
          return;
        }
      }
      if (clients.openWindow) {
        clients.openWindow(target);
      }
    })
  );
});
