(function () {
  function showToast(message, isError) {
    var stack = document.getElementById("toastStack");
    if (!stack) return;
    var toast = document.createElement("div");
    toast.className = "toast" + (isError ? " toast--error" : "");
    toast.textContent = message;
    stack.appendChild(toast);
    requestAnimationFrame(function () { toast.classList.add("toast--in"); });
    setTimeout(function () {
      toast.classList.remove("toast--in");
      setTimeout(function () { toast.remove(); }, 250);
    }, 2600);
  }
  window.showToast = function (message, kind) {
    showToast(message, kind === "error" || kind === true);
  };

  function updateCartBadges(count) {
    document.querySelectorAll(".nav__cart-badge").forEach(function (el) {
      el.textContent = count;
      el.style.display = count > 0 ? "" : "none";
    });
    var cartLinks = document.querySelectorAll(".nav__cart");
    cartLinks.forEach(function (el) {
      el.setAttribute("aria-label", "View cart, " + count + " item" + (count === 1 ? "" : "s"));
      if (count > 0 && !el.querySelector(".nav__cart-badge")) {
        var span = document.createElement("span");
        span.className = "nav__cart-badge";
        span.textContent = count;
        el.appendChild(span);
      }
    });
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form.classList || !form.classList.contains("add-to-cart-form")) return;
    e.preventDefault();
    var formData = new FormData(form);
    var btn = form.querySelector("button[type=submit]");
    var originalText = btn ? btn.textContent : null;
    if (btn) { btn.disabled = true; btn.textContent = "Adding…"; }

    fetch(form.action, {
      method: "POST",
      body: formData,
      headers: { "X-Requested-With": "fetch" },
    })
      .then(function (r) { return r.json().then(function (data) { return { ok: r.ok, data: data }; }); })
      .then(function (res) {
        if (res.ok && res.data.success) {
          updateCartBadges(res.data.cart_count);
          showToast('Added "' + res.data.product_name + '" to your cart.');

          // Confetti burst from the button position
          if (window.fireConfetti && btn) {
            var rect = btn.getBoundingClientRect();
            window.fireConfetti(rect.left + rect.width / 2, rect.top + rect.height / 2, { count: 20 });
          }

          // Cart badge bounce
          document.querySelectorAll(".nav__cart-badge").forEach(function (badge) {
            badge.classList.remove("cart-bounce");
            void badge.offsetWidth;
            badge.classList.add("cart-bounce");
          });
          var bottomBadge = document.querySelector(".bottom-nav__badge");
          if (bottomBadge) {
            bottomBadge.classList.remove("cart-bounce");
            void bottomBadge.offsetWidth;
            bottomBadge.classList.add("cart-bounce");
          }

          // Cart icon jiggle
          document.querySelectorAll(".nav__cart").forEach(function (cart) {
            cart.classList.remove("cart-jiggle");
            void cart.offsetWidth;
            cart.classList.add("cart-jiggle");
            setTimeout(function () { cart.classList.remove("cart-jiggle"); }, 600);
          });
        } else {
          showToast(res.data.error || "Could not add to cart.", true);
        }
      })
      .catch(function () { showToast("Could not add to cart. Please try again.", true); })
      .finally(function () {
        if (btn) { btn.disabled = false; btn.textContent = originalText; }
      });
  });
})();
