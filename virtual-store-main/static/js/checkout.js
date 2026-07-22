function showBuyError(msg) {
  const el = document.getElementById("buyError");
  el.textContent = msg;
  el.style.display = "block";
}

let appliedCoupon = null;

async function applyCoupon(productId) {
  const codeInput = document.getElementById("couponCode");
  const msgEl = document.getElementById("couponMessage");
  const code = codeInput.value.trim();
  if (!code) return;

  msgEl.style.display = "none";

  try {
    const res = await fetch("/api/apply-coupon", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": window.getCsrfToken() },
      body: JSON.stringify({ code, product_id: productId }),
    });
    const data = await res.json();

    if (!res.ok) {
      appliedCoupon = null;
      msgEl.textContent = data.error || "That coupon isn't valid.";
      msgEl.style.color = "#7a2222";
      msgEl.style.display = "block";
      resetPriceDisplay();
      return;
    }

    appliedCoupon = code.toUpperCase();
    msgEl.textContent = `Coupon applied — you save ${document.getElementById("buyBtn").dataset.currency}${data.discount_amount.toLocaleString()}!`;
    msgEl.style.color = "#234d23";
    msgEl.style.display = "block";
    updatePriceDisplay(data.final_price);
  } catch (err) {
    msgEl.textContent = "Could not check that coupon right now. Please try again.";
    msgEl.style.color = "#7a2222";
    msgEl.style.display = "block";
  }
}

function updatePriceDisplay(finalPrice) {
  const btn = document.getElementById("buyBtn");
  const currency = btn.dataset.currency;
  const originalEl = document.getElementById("priceOriginal");
  const discountedEl = document.getElementById("priceDiscounted");
  originalEl.style.textDecoration = "line-through";
  originalEl.style.color = "var(--grey-500)";
  originalEl.style.fontSize = "0.95rem";
  discountedEl.textContent = " " + currency + finalPrice.toLocaleString();
  discountedEl.style.display = "inline";
  btn.textContent = "Buy Now — " + currency + finalPrice.toLocaleString();
  btn.dataset.originalText = btn.textContent;
}

function resetPriceDisplay() {
  const btn = document.getElementById("buyBtn");
  const currency = btn.dataset.currency;
  const basePrice = btn.dataset.basePrice;
  const originalEl = document.getElementById("priceOriginal");
  const discountedEl = document.getElementById("priceDiscounted");
  originalEl.style.textDecoration = "none";
  originalEl.style.color = "";
  originalEl.style.fontSize = "";
  discountedEl.style.display = "none";
  btn.textContent = "Buy Now — " + currency + Number(basePrice).toLocaleString();
  btn.dataset.originalText = btn.textContent;
}

async function startCheckout(productId, productName) {
  if (window.storeSettings && window.storeSettings.disable_payments === "true") {
    showBuyError("Checkout is temporarily disabled.");
    return;
  }
  const name = document.getElementById("buyerName").value.trim();
  const email = document.getElementById("buyerEmail").value.trim();
  const phone = document.getElementById("buyerPhone").value.trim();
  const btn = document.getElementById("buyBtn");

  document.getElementById("buyError").style.display = "none";

  if (!name || !email) {
    showBuyError("Please enter your name and email to continue.");
    return;
  }

  btn.disabled = true;
  btn.textContent = "Please wait...";

  try {
    const res = await fetch("/api/create-order", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": window.getCsrfToken() },
      body: JSON.stringify({ product_id: productId, name, email, phone, coupon_code: appliedCoupon || "" }),
    });
    const data = await res.json();

    if (!res.ok) {
      showBuyError(data.error || "Something went wrong. Please try again.");
      resetBtn(btn, productName);
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
          showBuyError("We could not confirm your payment. Please contact us with your order reference.");
        }
      },
      modal: {
        ondismiss: function () {
          resetBtn(btn, productName);
        },
      },
    };

    const rzp = new Razorpay(options);
    rzp.on("payment.failed", function () {
      showBuyError("Payment failed. Please try again.");
      resetBtn(btn, productName);
    });
    rzp.open();
  } catch (err) {
    showBuyError("Network error. Please try again.");
    resetBtn(btn, productName);
  }
}

function resetBtn(btn, productName) {
  btn.disabled = false;
  btn.textContent = btn.dataset.originalText || btn.textContent;
}

document.addEventListener("DOMContentLoaded", function () {
  const btn = document.getElementById("buyBtn");
  if (btn) btn.dataset.originalText = btn.textContent;
});
