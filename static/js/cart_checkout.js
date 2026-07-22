function showCartError(msg) {
  const el = document.getElementById("cartError");
  if (!el) return;
  el.textContent = msg;
  el.style.display = "block";
}

let cartAppliedCoupon = null;

async function applyCartCoupon() {
  const codeInput = document.getElementById("cartCouponCode");
  const msgEl = document.getElementById("cartCouponMessage");
  const code = codeInput.value.trim();
  if (!code) return;

  msgEl.style.display = "none";

  try {
    const res = await fetch("/api/cart/apply-coupon", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": window.getCsrfToken() },
      body: JSON.stringify({ code }),
    });
    const data = await res.json();

    if (!res.ok) {
      cartAppliedCoupon = null;
      msgEl.textContent = data.error || "That coupon isn't valid.";
      msgEl.style.color = "#7a2222";
      msgEl.style.display = "block";
      resetCartPriceDisplay();
      return;
    }

    cartAppliedCoupon = code.toUpperCase();
    const btn = document.getElementById("cartCheckoutBtn");
    const currency = btn.dataset.currency;
    msgEl.textContent = `Coupon applied — you save ${currency}${data.discount_amount.toLocaleString()}!`;
    msgEl.style.color = "#234d23";
    msgEl.style.display = "block";
    updateCartPriceDisplay(data.final_price);
  } catch (err) {
    msgEl.textContent = "Could not check that coupon right now. Please try again.";
    msgEl.style.color = "#7a2222";
    msgEl.style.display = "block";
  }
}

function updateCartPriceDisplay(finalPrice) {
  const btn = document.getElementById("cartCheckoutBtn");
  const currency = btn.dataset.currency;
  const originalEl = document.getElementById("cartSubtotalDisplay");
  const discountedEl = document.getElementById("cartDiscountedDisplay");
  if (originalEl) {
    originalEl.style.textDecoration = "line-through";
    originalEl.style.color = "var(--grey-500)";
  }
  if (discountedEl) {
    discountedEl.textContent = currency + finalPrice.toLocaleString();
    discountedEl.style.display = "inline";
  }
  btn.textContent = "Checkout — " + currency + finalPrice.toLocaleString();
  btn.dataset.originalText = btn.textContent;
}

function resetCartPriceDisplay() {
  const btn = document.getElementById("cartCheckoutBtn");
  const currency = btn.dataset.currency;
  const basePrice = btn.dataset.basePrice;
  const originalEl = document.getElementById("cartSubtotalDisplay");
  const discountedEl = document.getElementById("cartDiscountedDisplay");
  if (originalEl) { originalEl.style.textDecoration = "none"; originalEl.style.color = ""; }
  if (discountedEl) { discountedEl.style.display = "none"; }
  btn.textContent = "Checkout — " + currency + Number(basePrice).toLocaleString();
  btn.dataset.originalText = btn.textContent;
}

async function startCartCheckout() {
  if (window.storeSettings && window.storeSettings.disable_payments === "true") {
    const errEl = document.getElementById("cartCheckoutError");
    if (errEl) { errEl.textContent = "Checkout is temporarily disabled."; errEl.style.display = "block"; }
    return;
  }
  const name = document.getElementById("cartBuyerName").value.trim();
  const email = document.getElementById("cartBuyerEmail").value.trim();
  const phone = document.getElementById("cartBuyerPhone").value.trim();
  const btn = document.getElementById("cartCheckoutBtn");

  document.getElementById("cartError").style.display = "none";

  if (!name || !email) {
    showCartError("Please enter your name and email to continue.");
    return;
  }

  btn.disabled = true;
  btn.textContent = "Please wait...";

  try {
    const res = await fetch("/api/cart/create-order", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": window.getCsrfToken() },
      body: JSON.stringify({ name, email, phone, coupon_code: cartAppliedCoupon || "" }),
    });
    const data = await res.json();

    if (!res.ok) {
      showCartError(data.error || "Something went wrong. Please try again.");
      resetCartBtn(btn);
      return;
    }

    if (data.test_mode) {
      window.location.href = data.redirect_url || ("/track?order_ref=" + encodeURIComponent(data.order_ref) + "&email=" + encodeURIComponent(email));
      return;
    }

    const options = {
      key: data.razorpay_key,
      amount: data.amount,
      currency: data.currency,
      name: document.querySelector(".nav__brand").textContent,
      description: data.product_name,
      order_id: data.razorpay_order_id,
      prefill: {
        name: data.customer_name,
        email: data.customer_email,
        contact: data.customer_phone,
      },
      theme: { color: "#0a0a0a" },
      handler: async function (response) {
        const verifyRes = await fetch("/api/verify-payment", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": window.getCsrfToken() },
          body: JSON.stringify({
            razorpay_order_id: response.razorpay_order_id,
            razorpay_payment_id: response.razorpay_payment_id,
            razorpay_signature: response.razorpay_signature,
          }),
        });
        const verifyData = await verifyRes.json();
        if (verifyRes.ok) {
          window.location.href = "/track?order_ref=" + encodeURIComponent(verifyData.order_ref) +
            "&email=" + encodeURIComponent(email);
        } else {
          showCartError("We could not confirm your payment. Please contact us with your order reference.");
        }
      },
      modal: {
        ondismiss: function () {
          resetCartBtn(btn);
        },
      },
    };

    const rzp = new Razorpay(options);
    rzp.on("payment.failed", function () {
      showCartError("Payment failed. Please try again.");
      resetCartBtn(btn);
    });
    rzp.open();
  } catch (err) {
    showCartError("Network error. Please try again.");
    resetCartBtn(btn);
  }
}

function resetCartBtn(btn) {
  btn.disabled = false;
  btn.textContent = btn.dataset.originalText || btn.textContent;
}

document.addEventListener("DOMContentLoaded", function () {
  const btn = document.getElementById("cartCheckoutBtn");
  if (btn) btn.dataset.originalText = btn.textContent;
});
