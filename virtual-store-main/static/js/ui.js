
(function () {
  'use strict';

  function onReady(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn, { once: true });
    } else {
      fn();
    }
  }

  function postTimezoneOffset() {
    try {
      var offsetSeconds = -new Date().getTimezoneOffset() * 60;
      var cached = localStorage.getItem('vs_timezone_offset');
      if (cached && parseInt(cached, 10) === offsetSeconds) return;

      var xhr = new XMLHttpRequest();
      xhr.open('POST', '/set-timezone', true);
      xhr.setRequestHeader('Content-Type', 'application/json');
      xhr.send(JSON.stringify({ offset: offsetSeconds }));
      localStorage.setItem('vs_timezone_offset', String(offsetSeconds));
    } catch (e) {
      // Best effort only.
    }
  }

  function wireScrollProgress() {
    var p = document.getElementById('scrollProgress');
    if (!p) return;

    var html = document.documentElement;
    var body = document.body;
    var ticking = false;
    var current = 0;

    p.style.transition = 'transform 0.08s cubic-bezier(0.34,1.56,0.64,1)';

    function update() {
      var scrollHeight = Math.max(html.scrollHeight, body.scrollHeight);
      var viewport = Math.max(html.clientHeight, body.clientHeight, 1);
      var denominator = Math.max(scrollHeight - viewport, 1);
      var next = Math.min(Math.max(((body.scrollTop || html.scrollTop || 0) / denominator), 0), 1);

      if (Math.abs(next - current) > 0.01 || next === 1 || next === 0) {
        current = next;
        p.style.transform = 'scaleX(' + next + ')';
        p.style.opacity = next >= 1 ? '0' : '1';
      }
      ticking = false;
    }

    window.addEventListener('scroll', function () {
      if (!ticking) {
        ticking = true;
        requestAnimationFrame(update);
      }
    }, { passive: true });

    update();
  }

  function wireGreeting() {
    var g = document.getElementById('userGreeting');
    if (!g) return;

    var visible = document.body && document.body.dataset.greetingVisible === '1';

    document.addEventListener('click', function (e) {
      var btn = e.target.closest && e.target.closest('[data-logout-btn]');
      if (btn) sessionStorage.removeItem('greeting_shown');
    });

    function dismissGreeting() {
      g.classList.remove('greeting--visible');
      g.classList.add('greeting--hiding');
      setTimeout(function () { g.style.display = 'none'; }, 500);
    }

    window.dismissGreeting = dismissGreeting;

    if (visible && !sessionStorage.getItem('greeting_shown')) {
      setTimeout(function () {
        g.classList.add('greeting--visible');
        sessionStorage.setItem('greeting_shown', '1');
      }, 400);
    }

    var autoTimer = setTimeout(function () {
      if (g.classList.contains('greeting--visible')) dismissGreeting();
    }, 7000);

    g.addEventListener('click', function () {
      clearTimeout(autoTimer);
    });

    var closeBtn = g.querySelector('.greeting__close');
    if (closeBtn) {
      closeBtn.addEventListener('click', function (e) {
        e.preventDefault();
        dismissGreeting();
      });
    }
  }

  function wireScrollRing() {
    var ring = document.getElementById('scrollRing');
    if (!ring) return;

    var circ = 2 * Math.PI * 20;
    ring.style.strokeDasharray = circ;
    ring.style.strokeDashoffset = circ;

    window.addEventListener('scroll', function () {
      var docHeight = document.documentElement.scrollHeight - window.innerHeight;
      if (docHeight > 0) {
        ring.style.strokeDashoffset = circ - (window.scrollY / docHeight) * circ;
      }
    }, { passive: true });
  }

  function wirePageEndBadge() {
    var el = document.getElementById('pageEnd');
    if (!el) return;

    var ticking = false;
    var shown = false;
    var eeShown = parseInt(sessionStorage.getItem('ee_shown') || '0', 10);

    function checkScroll() {
      var scrollBottom = window.scrollY + window.innerHeight;
      var docHeight = document.documentElement.scrollHeight;
      if (scrollBottom >= docHeight - 2) {
        if (!shown) {
          shown = true;
          el.classList.add('page-end--show');
          setTimeout(function () { el.classList.remove('page-end--show'); }, 3000);
        }
        // Easter egg: 3rd scroll-to-bottom triggers the credit
        var count = parseInt(sessionStorage.getItem('ee_bottom') || '0', 10);
        count += 1;
        sessionStorage.setItem('ee_bottom', String(count));
        if (count >= 3 && !eeShown) {
          eeShown = 1;
          sessionStorage.setItem('ee_shown', '1');
          var egg = document.getElementById('easterEgg');
          if (egg) {
            egg.classList.add('easter-egg--show');
            setTimeout(function () { egg.classList.remove('easter-egg--show'); }, 4000);
          }
        }
      }
      ticking = false;
    }

    window.addEventListener('scroll', function () {
      if (!ticking) {
        ticking = true;
        requestAnimationFrame(checkScroll);
      }
    }, { passive: true });
  }

  function wireImageFallback() {
    document.addEventListener('error', function (e) {
      var target = e.target;
      if (!target || target.tagName !== 'IMG' || target.dataset.fallback) return;
      var placeholder = document.getElementById('placeholderImg');
      if (!placeholder || !placeholder.src) return;
      target.dataset.fallback = '1';
      target.src = placeholder.src;
    }, true);
  }

  function wireEasterEggs() {
    var seq = ['ArrowUp', 'ArrowUp', 'ArrowDown', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'ArrowLeft', 'ArrowRight', 'b', 'a'];
    var pos = 0;
    var egg = document.getElementById('easterEgg');
    var overlay = document.getElementById('easter-egg-overlay');

    document.addEventListener('keydown', function (e) {
      if (e.key === seq[pos]) {
        pos += 1;
      } else {
        pos = (e.key === seq[0]) ? 1 : 0;
      }
      if (pos === seq.length) {
        pos = 0;
        if (overlay) overlay.classList.add('ee-show');
        if (egg) {
          var bottomCount = parseInt(sessionStorage.getItem('ee_bottom') || '0', 10);
          sessionStorage.setItem('ee_shown', '1');
          sessionStorage.setItem('ee_bottom', String(bottomCount));
          egg.classList.add('easter-egg--show');
          setTimeout(function () { egg.classList.remove('easter-egg--show'); }, 4000);
        }
      }
    });
  }

  /* ── Gallery image swap (product detail page) ── */
  window.swapGalleryImage = function (src, thumb) {
    var main = document.getElementById('mainImage');
    if (!main) return;
    main.src = src;
    /* Toggle active class on all thumbnails in the same gallery */
    var container = thumb && thumb.parentNode;
    if (container) {
      var thumbs = container.querySelectorAll('img');
      thumbs.forEach(function (t) { t.classList.remove('active'); });
      thumb.classList.add('active');
    }
  };

  onReady(function () {
    wireScrollProgress();
    wireGreeting();
    wireScrollRing();
    wirePageEndBadge();
    wireImageFallback();
    wireEasterEggs();
    postTimezoneOffset();
  });
})();
