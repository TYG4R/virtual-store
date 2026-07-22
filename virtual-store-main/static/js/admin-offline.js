/* Admin offline support: data caching, action queue, connection status */
(function () {
  'use strict';

  var CACHE_KEY = 'admin_data_cache';
  var QUEUE_KEY = 'admin_action_queue';
  var CACHE_MAX_AGE = 48 * 60 * 60 * 1000; // 48 hours

  /* ── Cache admin data views ── */
  function cacheData(key, data) {
    try {
      var entry = { data: data, ts: Date.now() };
      localStorage.setItem(CACHE_KEY + ':' + key, JSON.stringify(entry));
    } catch (e) {
      /* storage full — ignore */
    }
  }

  function getCachedData(key) {
    try {
      var raw = localStorage.getItem(CACHE_KEY + ':' + key);
      if (!raw) return null;
      var entry = JSON.parse(raw);
      if (Date.now() - entry.ts > CACHE_MAX_AGE) {
        localStorage.removeItem(CACHE_KEY + ':' + key);
        return null;
      }
      return entry.data;
    } catch (e) {
      return null;
    }
  }

  /* Cache helpers for common admin views */
  window.__adminCache = {
    set: cacheData,
    get: getCachedData,
  };

  /* ── Offline banner ── */
  function showOfflineBanner() {
    var existing = document.getElementById('admin-offline-banner');
    if (existing) return;
    var banner = document.createElement('div');
    banner.id = 'admin-offline-banner';
    banner.style.cssText =
      'position:fixed;top:0;left:0;right:0;z-index:9999;background:#7a2222;color:#fff;' +
      'text-align:center;padding:0.5rem 1rem;font-size:0.85rem;font-weight:500;';
    banner.textContent = 'You\u2019re offline. Showing cached data. Changes will sync when you reconnect.';
    document.body.prepend(banner);
  }

  function hideOfflineBanner() {
    var banner = document.getElementById('admin-offline-banner');
    if (banner) banner.remove();
  }

  /* Show "showing cached data from [time]" notice */
  function showCachedNotice(key) {
    var existing = document.getElementById('admin-cached-notice');
    if (!existing) {
      var notice = document.createElement('div');
      notice.id = 'admin-cached-notice';
      notice.style.cssText =
        'margin-bottom:1rem;padding:0.6rem 1rem;background:#fff8e1;border:1px solid #ffe082;' +
        'border-radius:4px;font-size:0.82rem;color:#6d5200;';
      var main = document.querySelector('.admin-main');
      if (main) main.prepend(notice);
    }
    var el = document.getElementById('admin-cached-notice');
    if (el) {
      try {
        var raw = localStorage.getItem(CACHE_KEY + ':' + key);
        if (raw) {
          var entry = JSON.parse(raw);
          var d = new Date(entry.ts);
          el.textContent = 'Showing cached data from ' + d.toLocaleString() + '. Data may not be current.';
        } else {
          el.textContent = 'Showing cached data. Data may not be current.';
        }
      } catch (e) {
        el.textContent = 'Showing cached data. Data may not be current.';
      }
    }
  }

  /* ── Connection monitoring ── */
  function updateOnlineStatus() {
    if (navigator.onLine) {
      hideOfflineBanner();
      processQueue();
    } else {
      showOfflineBanner();
    }
  }

  window.addEventListener('online', updateOnlineStatus);
  window.addEventListener('offline', updateOnlineStatus);

  /* ── Action queue ── */
  function getQueue() {
    try {
      return JSON.parse(localStorage.getItem(QUEUE_KEY) || '[]');
    } catch (e) {
      return [];
    }
  }

  function saveQueue(queue) {
    localStorage.setItem(QUEUE_KEY, JSON.stringify(queue));
  }

  function enqueueAction(action) {
    var queue = getQueue();
    action.id = Date.now() + '_' + Math.random().toString(36).slice(2, 8);
    action.status = 'pending';
    queue.push(action);
    saveQueue(queue);
    showPendingSyncIndicator();
  }

  function processQueue() {
    var queue = getQueue();
    var pending = queue.filter(function (a) { return a.status === 'pending'; });
    if (pending.length === 0) return;

    pending.forEach(function (action) {
      if (action.type === 'mark_delivered') {
        var formData = new FormData();
        formData.append('csrf_token', action.csrfToken);
        formData.append('delivery_message', action.deliveryMessage);
        formData.append('_queued_action', '1');
        formData.append('_queued_order_id', action.orderId);

        fetch('/admin/orders/' + action.orderId + '/deliver', {
          method: 'POST',
          body: formData,
        }).then(function (r) {
          if (r.ok || r.status === 409) {
            // 409 = already delivered — likely means it went through
            action.status = 'completed';
          } else {
            action.status = 'failed';
          }
          saveQueue(getQueue());
          updatePendingSyncDisplay();
        }).catch(function () {
          action.status = 'pending';
          saveQueue(getQueue());
        });
      }
    });
  }

  function showPendingSyncIndicator() {
    var el = document.getElementById('admin-sync-indicator');
    if (!el) {
      el = document.createElement('div');
      el.id = 'admin-sync-indicator';
      el.style.cssText =
        'position:fixed;bottom:70px;right:1rem;z-index:9999;background:var(--accent);color:#fff;' +
        'padding:0.4rem 0.8rem;border-radius:4px;font-size:0.78rem;font-weight:500;' +
        'box-shadow:0 2px 8px rgba(0,0,0,0.2);';
      document.body.appendChild(el);
    }
    var count = getQueue().filter(function (a) { return a.status === 'pending'; }).length;
    el.textContent = count + ' action' + (count !== 1 ? 's' : '') + ' pending sync';
    el.style.display = 'block';
  }

  function updatePendingSyncDisplay() {
    var count = getQueue().filter(function (a) { return a.status === 'pending'; }).length;
    var el = document.getElementById('admin-sync-indicator');
    if (count === 0) {
      if (el) el.style.display = 'none';
      return;
    }
    showPendingSyncIndicator();
  }

  /* ── Intercept "Mark as Delivered" form submissions ── */
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form.action || form.action.indexOf('/admin/orders/') === -1) return;
    if (!form.action || form.action.indexOf('/deliver') === -1) return;
    if (!navigator.onLine) {
      e.preventDefault();
      var msg = form.querySelector('[name="delivery_message"]');
      var csrf = form.querySelector('[name="csrf_token"]');
      var orderId = form.action.split('/')[5]; // /admin/orders/<id>/deliver

      enqueueAction({
        type: 'mark_delivered',
        orderId: orderId,
        deliveryMessage: msg ? msg.value : '',
        csrfToken: csrf ? csrf.value : '',
      });

      /* Show a flash-style confirmation */
      var flash = document.createElement('div');
      flash.style.cssText =
        'padding:0.7rem 1rem;margin-bottom:1rem;background:#fff8e1;border:1px solid #ffe082;' +
        'border-radius:4px;font-size:0.85rem;color:#6d5200;';
      flash.textContent = 'Your action has been queued and will be submitted once you\u2019re back online.';
      form.parentNode.insertBefore(flash, form);

      var btn = form.querySelector('[type="submit"]');
      if (btn) btn.disabled = true;
    }
  });

  /* ── Initial state ── */
  if (!navigator.onLine) {
    showOfflineBanner();
  }

  /* ── Auto-cache page content on every admin page load ── */
  (function autoCache() {
    var path = window.location.pathname;
    if (path.indexOf('/admin/') === -1) return;
    var main = document.querySelector('.admin-main');
    if (main) {
      cacheData('html:' + path, main.innerHTML);
    }
  })();

  /* ── On error, try to serve cached content ── */
  /* Listen for failed image loads and replace with cached if available */
  document.addEventListener('DOMContentLoaded', function () {
    if (!navigator.onLine) {
      var path = window.location.pathname;
      var cached = getCachedData('html:' + path);
      if (cached) {
        var main = document.querySelector('.admin-main');
        if (main) {
          main.innerHTML = cached;
        }
        showCachedNotice(path);
      }
    }
  });

  /* ── Toast notification (used by admin templates) ── */
  window.showToast = function (message, kind) {
    var stack = document.getElementById("toastStack");
    if (!stack) {
      /* Create a toast stack if it doesn't exist */
      stack = document.createElement("div");
      stack.id = "toastStack";
      stack.style.cssText = "position:fixed;top:1rem;right:1rem;z-index:9999;display:flex;flex-direction:column;gap:0.5rem;";
      document.body.appendChild(stack);
    }
    var toast = document.createElement("div");
    toast.style.cssText =
      "padding:0.7rem 1.2rem;border-radius:6px;font-size:0.85rem;color:#fff;" +
      "background:" + (kind === "success" ? "#2e7d32" : "#c62828") + ";" +
      "box-shadow:0 2px 8px rgba(0,0,0,0.15);opacity:0;transition:opacity 0.25s;";
    toast.textContent = message;
    stack.appendChild(toast);
    requestAnimationFrame(function () { toast.style.opacity = "1"; });
    setTimeout(function () {
      toast.style.opacity = "0";
      setTimeout(function () { toast.remove(); }, 250);
    }, 2600);
  };

  /* Expose for use from templates */
  window.__adminOffline = {
    cacheData: cacheData,
    getCachedData: getCachedData,
    showCachedNotice: showCachedNotice,
    showOfflineBanner: showOfflineBanner,
    hideOfflineBanner: hideOfflineBanner,
    enqueueAction: enqueueAction,
  };

})();
