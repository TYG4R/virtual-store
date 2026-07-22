// Delivery feedback UI — reveal, copy, download actions with motion
(function () {
"use strict";

// ---- Copy to clipboard with confirmation ----
window.deliveryCopy = function (btn, text) {
  if (!navigator.clipboard) {
    // Fallback for older browsers
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) {}
    document.body.removeChild(ta);
  } else {
    navigator.clipboard.writeText(text).catch(function () {});
  }
  btn.classList.add("btn--copied");
  btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" width="16" height="16"><path d="M20 6L9 17l-5-5"></path></svg> Copied!';
  setTimeout(function () {
    btn.classList.remove("btn--copied");
    btn.innerHTML = btn.getAttribute("data-original-html") || "Copy";
  }, 2000);
};

// ---- Reveal masked content with animation ----
window.deliveryReveal = function (btn) {
  var container = btn.closest(".delivery-key") || btn.parentElement;
  var masked = container.querySelector(".delivery-key__masked");
  var full = container.querySelector(".delivery-key__full");
  if (!masked || !full) return;
  // Animate the masked portion out
  masked.style.transition = "opacity 0.2s ease, transform 0.25s cubic-bezier(0.16,1,0.3,1)";
  masked.style.opacity = "0";
  masked.style.transform = "translateY(-6px) scale(0.96)";
  // Reveal the full key
  full.style.display = "inline";
  full.style.opacity = "0";
  full.style.transform = "translateY(4px)";
  // Force layout then animate in
  void full.offsetWidth;
  full.style.transition = "opacity 0.3s ease 0.12s, transform 0.35s cubic-bezier(0.16,1,0.3,1) 0.12s";
  full.style.opacity = "1";
  full.style.transform = "translateY(0)";
  btn.style.display = "none";
};

// ---- Download with progress feedback ----
window.deliveryDownload = function (btn, url) {
  btn.classList.add("btn--loading");
  btn.disabled = true;
  var origHtml = btn.innerHTML;
  btn.innerHTML = '<span class="btn__spinner"></span> Downloading…';
  // Create a hidden anchor for the actual download
  var a = document.createElement("a");
  a.href = url;
  a.download = "";
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // Reset button after a short delay
  setTimeout(function () {
    btn.classList.remove("btn--loading");
    btn.disabled = false;
    btn.innerHTML = origHtml;
    // Brief success flash
    btn.classList.add("btn--downloaded");
    setTimeout(function () { btn.classList.remove("btn--downloaded"); }, 1200);
  }, 800);
};

// Auto-init: find copy/reveal/download buttons and wire them
document.addEventListener("DOMContentLoaded", function () {
  // Copy buttons
  document.querySelectorAll("[data-delivery-copy]").forEach(function (btn) {
    btn.setAttribute("data-original-html", btn.innerHTML);
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      deliveryCopy(btn, btn.getAttribute("data-delivery-copy"));
    });
  });
  // Reveal buttons
  document.querySelectorAll("[data-delivery-reveal]").forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      deliveryReveal(btn);
    });
  });
  // Download buttons
  document.querySelectorAll("[data-delivery-download]").forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      deliveryDownload(btn, btn.getAttribute("data-delivery-download"));
    });
  });
  // Activate buttons — open link in new tab, optionally record activation
  document.querySelectorAll("[data-delivery-activate]").forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      var url = btn.getAttribute("data-delivery-activate");
      var recordUrl = btn.getAttribute("data-record-activation");
      window.open(url, "_blank");
      if (recordUrl) {
        fetch(recordUrl, { method: "POST", headers: { "X-Requested-With": "XMLHttpRequest" } })
          .then(function () { btn.textContent = "Activated"; btn.disabled = true; })
          .catch(function () {});
      }
    });
  });
});

})();
