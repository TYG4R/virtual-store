/* Auto-coupon system — fetches auto-applicable coupons and applies the best
 * one automatically. Real-time recalculation: if the cart changes (items
 * added/removed/qty changed), the page reloads and auto-coupons are
 * re-evaluated. Also handles URL-driven coupons stored in session.
 *
 * Used on both the cart page and the product detail page. On the cart page
 * it checks the full cart; on the product page it checks a single product. */

(function () {
  "use strict";

  // ---- Cart page: auto-apply the best coupon on load ----
  var cartCheckoutBtn = document.getElementById("cartCheckoutBtn");
  if (!cartCheckoutBtn) return; // not on cart page

  var autoCouponBanner = document.getElementById("autoCouponBanner");
  var cartError = document.getElementById("cartError");

  function initAutoCoupons() {
    fetch("/api/auto-coupons")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.best && data.best.auto_apply) {
          applyAutoCoupon(data.best);
        }
      })
      .catch(function () {});
  }

  function applyAutoCoupon(coupon) {
    // Only apply if the user hasn't already manually applied a coupon
    if (cartAppliedCoupon) return;

    var btn = cartCheckoutBtn;
    var currency = btn.dataset.currency;
    var originalEl = document.getElementById("cartSubtotalDisplay");
    var discountedEl = document.getElementById("cartDiscountedDisplay");
    var msgEl = document.getElementById("cartCouponMessage");

    // Set the applied coupon so checkout uses it
    cartAppliedCoupon = coupon.code;

    // Update the price display
    if (originalEl) {
      originalEl.style.textDecoration = "line-through";
      originalEl.style.color = "var(--grey-500)";
    }
    if (discountedEl) {
      discountedEl.textContent = currency + coupon.final_price.toLocaleString();
      discountedEl.style.display = "inline";
    }
    btn.textContent = "Checkout — " + currency + coupon.final_price.toLocaleString();
    btn.dataset.originalText = btn.textContent;

    // Show the auto-coupon banner
    if (autoCouponBanner) {
      autoCouponBanner.innerHTML =
        '<div class="auto-coupon-badge">🎁 Auto-applied</div>' +
        '<div class="auto-coupon-info">' +
          '<strong>' + coupon.code + '</strong> — ' + coupon.description +
          '. You save ' + currency + coupon.discount_amount.toLocaleString() + '!' +
        '</div>' +
        '<button type="button" class="auto-coupon-remove" onclick="removeAutoCoupon()">Remove</button>';
      autoCouponBanner.style.display = "flex";
    }

    // Also show in the coupon message area
    if (msgEl) {
      msgEl.textContent = "Auto-applied: " + coupon.code + " — you save " + currency + coupon.discount_amount.toLocaleString() + "!";
      msgEl.style.color = "#234d23";
      msgEl.style.display = "block";
    }
  }

  window.removeAutoCoupon = function () {
    cartAppliedCoupon = null;
    resetCartPriceDisplay();
    if (autoCouponBanner) {
      autoCouponBanner.style.display = "none";
      autoCouponBanner.innerHTML = "";
    }
    var msgEl = document.getElementById("cartCouponMessage");
    if (msgEl) msgEl.style.display = "none";
  };

  initAutoCoupons();
})();

/* ---- Product page: show auto-coupon badge ---- */
(function () {
  "use strict";
  var buyBtn = document.getElementById("buyBtn");
  if (!buyBtn) return; // not on product page

  var productId = null;
  // Get product id from the buy button's onclick attribute
  var onclickAttr = buyBtn.getAttribute("onclick") || "";
  var match = onclickAttr.match(/startCheckout\((\d+)/);
  if (!match) return;
  productId = parseInt(match[1], 10);

  var autoCouponBox = document.getElementById("autoCouponBox");
  if (!autoCouponBox) return;

  fetch("/api/auto-coupons?product_id=" + productId)
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (data.best && data.best.auto_apply) {
        var c = data.best;
        var currency = buyBtn.dataset.currency;
        autoCouponBox.innerHTML =
          '<div class="auto-coupon-badge">🎁 Auto-applied</div>' +
          '<div class="auto-coupon-info">' +
            '<strong>' + c.code + '</strong> — ' + c.description +
            '. You pay ' + currency + c.final_price.toLocaleString() +
            ' (save ' + currency + c.discount_amount.toLocaleString() + ').' +
          '</div>';
        autoCouponBox.style.display = "flex";

        // Auto-apply the coupon so Buy Now uses the discounted price
        appliedCoupon = c.code;
        updatePriceDisplay(c.final_price);

        var msgEl = document.getElementById("couponMessage");
        if (msgEl) {
          msgEl.textContent = "Auto-applied: " + c.code + " — you save " + currency + c.discount_amount.toLocaleString() + "!";
          msgEl.style.color = "#234d23";
          msgEl.style.display = "block";
        }
      }
    })
    .catch(function () {});
})();
