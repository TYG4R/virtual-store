/* Phone OTP Authentication via Firebase.
 * Flow: enter phone → Firebase sends SMS → user enters code →
 * Firebase verifies → we send the ID token to /auth/phone/verify →
 * backend creates/updates the session. */

let authPendingPhone = null;
let authTimerInterval = null;
let authResendSeconds = 0;
let authConfirmationResult = null;

function authGetFirebaseConfig() {
  if (window.FIREBASE_CONFIG) return window.FIREBASE_CONFIG;
  var el = document.getElementById("firebase-config");
  if (!el) return null;
  try {
    var parsed = JSON.parse(el.textContent || el.innerText || "{}");
    if (parsed && typeof parsed === "object") {
      window.FIREBASE_CONFIG = parsed;
      return parsed;
    }
  } catch (err) {}
  return null;
}

// ─── Lazy Firebase SDK loader — only loads when auth modal opens ────────
// Scripts are preload'd in base.html so the browser starts fetching them
// early. This function loads both scripts in PARALLEL (not serial) for the
// remaining speed gain, then initializes Firebase. Once loaded, future calls
// return the cached promise.
//
// NOTE: This stays on the Firebase v9+ compat layer (firebase-app-compat.js /
// firebase-auth-compat.js) rather than the true modular ES module SDK. The
// compat SDK re-exports under the global `firebase` namespace, which the rest
// of this file depends on extensively (firebase.auth(), firebase.initializeApp,
// new firebase.auth.GoogleAuthProvider(), etc.). A full modular v9 migration
// would require restructuring the entire file for ES module imports — a larger
// refactor beyond the scope of this change. The real performance win was
// already achieved by lazy-loading (item 22).
var firebaseLoadPromise = null;

function firebaseLoad() {
  if (firebaseLoadPromise) return firebaseLoadPromise;
  var firebaseConfig = authGetFirebaseConfig();
  if (!firebaseConfig) {
    firebaseLoadPromise = Promise.reject(new Error("No Firebase config"));
    return firebaseLoadPromise;
  }
  var SDK_BASE = "https://www.gstatic.com/firebasejs/10.14.1/";
  firebaseLoadPromise = new Promise(function(resolve, reject) {
    // Load both SDK scripts in PARALLEL instead of serial (the original
    // code loaded app, then waited, then loaded auth — wasted round-trip).
    // The preload links in base.html mean the browser may already have them
    // cached by the time this runs.
    var loaded = 0;
    var lastErr = null;
    function onLoad() {
      loaded++;
      if (loaded === 2) {
        window.FIREBASE_CONFIG = firebaseConfig;
        if (!firebase.apps.length) {
          firebase.initializeApp(window.FIREBASE_CONFIG);
        }
        resolve();
      }
    }
    function onError(err) {
      lastErr = err;
      loaded++;
      if (loaded === 2) {
        reject(lastErr || new Error("Firebase SDK failed to load"));
      }
    }
    var appScript = document.createElement("script");
    appScript.src = SDK_BASE + "firebase-app-compat.js";
    appScript.onload = onLoad;
    appScript.onerror = onError;
    document.head.appendChild(appScript);

    var authScript = document.createElement("script");
    authScript.src = SDK_BASE + "firebase-auth-compat.js";
    authScript.onload = onLoad;
    authScript.onerror = onError;
    document.head.appendChild(authScript);
  });
  return firebaseLoadPromise;
}

// ─── Sign out (intercept form submit, sign out of Google too) ─────────────
(function wireSignOut() {
  document.addEventListener("submit", function(e) {
    var form = e.target;
    if (form.method !== "POST" || !form.action) return;
    if (!form.action.match(/\/auth\/logout/)) return;
    sessionStorage.removeItem("__google_redirect_pending");
    sessionStorage.setItem("__google_signout", "1");
    // Attempt Firebase sign-out only if SDK is loaded
    firebaseLoad().then(function() {
      if (firebase.apps.length) {
        firebase.auth().signOut().catch(function(){});
      }
    }).catch(function(){});
    // GIS Google sign-out — if initialized, disconnect
    if (window.google && window.google.accounts && window.google.accounts.id) {
      try { google.accounts.id.disableAutoSelect(); } catch(e) {}
      try { google.accounts.id.cancel(); } catch(e) {}
    }
  });
})();

// ─── Wire bottom-nav Account link to the account page ────────────────────
(function wireAccountLink() {
  var links = document.querySelectorAll("[data-auth-signed-in].bottom-nav__item");
  for (var i = 0; i < links.length; i++) {
    links[i].addEventListener("click", function(e) {
      e.preventDefault();
      var href = this.getAttribute("href");
      if (href && href !== "#") { window.location.href = href; }
    });
  }
})();

// ─── Restore auth UI from server session on page load ─────────────────────
// Exported as window.doAuthRestore so authHandleGoogleRedirect can call it
// when re-processing a signed-in Google user on page navigation.
function doAuthRestore() {
  var b = document.body;
  if (!b || b.getAttribute('data-customer-logged-in') !== '1') return;
  var name = b.getAttribute('data-customer-name') || '';
  document.querySelectorAll("[data-auth-signed-out]").forEach(function(el) { el.style.display = "none"; });
  document.querySelectorAll("[data-auth-signed-in]").forEach(function(el) {
    el.style.display = "";
    var nameSpan = el.querySelector("[data-auth-name-placeholder]");
    if (nameSpan) nameSpan.textContent = (name || "Me").slice(0, 8);
    else if (el.tagName !== "FORM" && el.dataset.authStatic === undefined) el.textContent = name || "Account";
  });
}
(function authRestoreFromServer() {
  var body = document.body;
  if (!body) {
    document.addEventListener('DOMContentLoaded', doAuthRestore);
    return;
  }
  doAuthRestore();
})();

// ─── Modal open / close ───────────────────────────────────────────────────

function openAuthModal() {
  const modal = document.getElementById("authModal");
  if (!modal) return;
  modal.classList.add("auth-modal--open");
  modal.setAttribute("aria-hidden", "false");
  document.body.classList.add("auth-modal-open");
  // If the user is already signed in (server-side session), skip the phone
  // form and show the signed-in state immediately — prevents flash of the
  // sign-in form while Firebase auth state resolves.
  if (document.body.getAttribute("data-customer-logged-in") === "1") {
    var name = document.body.getAttribute("data-customer-name") || "Me";
    var el = document.getElementById("authSuccessName");
    if (el) el.textContent = name.slice(0, 12);
    authGoToStep("authStepSuccess");
  } else {
    authGoToStep("authStepPhone");
  }

  // Reset googly eyes tracking when modal opens (skip if no pupil elements)
  if (window.authEyesReset) window.authEyesReset();

  // Render the Google Identity Services button into its placeholder div.
  // (GIS button rendering only ever happened via the legacy Firebase
  // button's onclick, which isn't in the DOM when GIS is the active
  // provider — so the placeholder div stayed empty. Render it here,
  // every time the modal opens, so it's always populated.)
  if (window.GOOGLE_CLIENT_ID) {
    if (window.google && window.google.accounts && window.google.accounts.id) {
      authSignInGoogleGIS();
    } else {
      // GIS script may still be loading (it's an async <script> tag) — retry
      // briefly until it's available, then render.
      (function waitForGISThenRender(attempts) {
        if (window.google && window.google.accounts && window.google.accounts.id) {
          authSignInGoogleGIS();
          return;
        }
        if (attempts <= 0) return;
        setTimeout(function() { waitForGISThenRender(attempts - 1); }, 300);
      })(10);
    }
  }

  // Lazy-load Firebase SDK now — but only if Firebase is configured
  if (authGetFirebaseConfig()) {
    firebaseLoad().then(function() {
      if (!window.recaptchaVerifier) {
        window.recaptchaVerifier = new firebase.auth.RecaptchaVerifier("authSendCodeBtn", {
          size: "invisible",
          callback: function() {},
          "expired-callback": function() {
            authShowError("authPhoneError", "reCAPTCHA expired. Please try again.");
          }
        });
        window.recaptchaVerifier.render().catch(function(err) {
          console.error("reCAPTCHA render error:", err);
        });
      }
    }).catch(function() {});
  }

  setTimeout(() => {
    const phoneInput = document.getElementById("authPhoneInput");
    if (phoneInput) phoneInput.focus();
  }, 300);
}

function closeAuthModal() {
  const modal = document.getElementById("authModal");
  if (!modal) return;
  modal.classList.remove("auth-modal--open");
  modal.setAttribute("aria-hidden", "true");
  document.body.classList.remove("auth-modal-open");
  if (authTimerInterval) { clearInterval(authTimerInterval); authTimerInterval = null; }
}

function authGoToStep(stepId) {
  document.querySelectorAll(".auth-step").forEach(el => el.classList.remove("auth-step--active"));
  const step = document.getElementById(stepId);
  if (step) step.classList.add("auth-step--active");

  // Keep the progress dots in sync — filled up to and including the active
  // step, hidden entirely once we reach the success screen.
  const progress = document.getElementById("authProgress");
  if (progress) {
    const order = ["authStepPhone", "authStepCode", "authStepDetails"];
    const idx = order.indexOf(stepId);
    progress.style.display = idx === -1 ? "none" : "";
    progress.querySelectorAll(".auth-progress__dot").forEach(function(dot, i) {
      dot.classList.toggle("is-active", i === idx);
      dot.classList.toggle("is-done", idx > -1 && i < idx);
    });
  }
}

function authShowError(elId, msg) {
  const el = document.getElementById(elId);
  if (!el) return;
  el.textContent = msg;
  el.style.display = msg ? "block" : "none";
}

// ─── Resend timer ─────────────────────────────────────────────────────────

function authStartResendTimer() {
  authResendSeconds = 30;
  const resendBtn = document.querySelector('.auth-modal__link[onclick="authResendCode()"]');
  if (!resendBtn) return;
  if (authTimerInterval) clearInterval(authTimerInterval);
  authTimerInterval = setInterval(() => {
    if (authResendSeconds > 0) {
      authResendSeconds--;
      resendBtn.textContent = `Resend in ${authResendSeconds}s`;
      resendBtn.style.opacity = "0.5";
      resendBtn.style.pointerEvents = "none";
    } else {
      clearInterval(authTimerInterval);
      authTimerInterval = null;
      resendBtn.textContent = "Resend code";
      resendBtn.style.opacity = "";
      resendBtn.style.pointerEvents = "";
    }
  }, 1000);
}

// ─── Google Sign-In via Google Identity Services (GIS) ──────────────────
// No Firebase SDK needed — direct OAuth using Google's ~14KB gsi/client
// library. Supports one-tap auto sign-in (hidden iframe, no popup) and
// the standard Google button. One-tap shows a small dialog instantly if
// the user has an active Google session — the key speed feature.

var _gisInitialized = false;
var _gisPromptShown = false;

function authSignInGoogleGIS() {
  if (!window.GOOGLE_CLIENT_ID) {
    authShowError("authPhoneError", "Google sign-in is not available.");
    return;
  }
  if (!window.google || !window.google.accounts || !window.google.accounts.id) {
    authShowError("authPhoneError", "Google sign-in is loading. Please try again.");
    return;
  }
  // Initialize GIS once
  if (!_gisInitialized) {
    google.accounts.id.initialize({
      client_id: window.GOOGLE_CLIENT_ID,
      callback: handleGISCredential,
    });
    _gisInitialized = true;
  }
  // Render the GIS Google button into the placeholder div
  var btnWrap = document.getElementById("authGoogleBtn");
  if (btnWrap && btnWrap.tagName === "DIV") {
    google.accounts.id.renderButton(btnWrap, {
      theme: "filled_black",
      size: "large",
      text: "signin_with",
      shape: "pill",
      logo_alignment: "center",
      width: Math.min(btnWrap.clientWidth || 320, 400),
    });
  }
  // Trigger one-tap only once per page load — initOneTap already does this
  // on page load, and re-prompting every time the modal opens caused an
  // unwanted repeat popup/flicker each time the user clicked Sign In.
  if (!_gisPromptShown) {
    _gisPromptShown = true;
    google.accounts.id.prompt(function(notification) {
      // notification.isNotDisplayed() — user has no eligible session
      // notification.isSkippedMoment() — user dismissed / browser refused
      // notification.isDismissedMoment() — user explicitly dismissed
      // We just log; the button is already visible as fallback.
    });
  }
}

function handleGISCredential(response) {
  if (!response || !response.credential) {
    authShowError("authPhoneError", "Google sign-in failed. Please try again.");
    return;
  }
  // Make sure the modal is visible (needed for the one-tap case, where the
  // user never clicked "Sign In" and the modal may not be open yet) WITHOUT
  // calling openAuthModal() — that function resets the view back to the
  // phone step (since the server hasn't confirmed login yet) and re-runs
  // GIS init/render/prompt, which was fighting the credential we just got.
  const modal = document.getElementById("authModal");
  if (modal && !modal.classList.contains("auth-modal--open")) {
    modal.classList.add("auth-modal--open");
    modal.setAttribute("aria-hidden", "false");
    document.body.classList.add("auth-modal-open");
  }
  authGoToStep("authStepSuccess");
  var nameEl = document.getElementById("authSuccessName");
  if (nameEl) nameEl.textContent = "Signing in with Google…";

  sendGISCredentialToServer(response.credential);
}

async function sendGISCredentialToServer(credential) {
  try {
    var csrfToken = (typeof window.getCsrfToken === "function") ? window.getCsrfToken() : "";
    var res = await fetch("/auth/google", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken
      },
      body: JSON.stringify({ credential: credential }),
    });

    var contentType = res.headers.get("content-type") || "";
    if (!contentType.includes("application/json")) {
      authShowError("authPhoneError", "Server error. Please refresh and try again.");
      return;
    }
    var data = await res.json();

    if (!res.ok) {
      authShowError("authPhoneError", data.error || "Google sign-in failed.");
      return;
    }

    authShowSuccess(data.name || "", "", data.email || "");
  } catch (err) {
    authShowError("authPhoneError", err.message || "Google sign-in failed. Please try again.");
  }
}

// Legacy alias so the old onclick handler still works for Firebase fallback
function authSignInGoogle() {
  // If GIS is configured, use GIS path; otherwise use the Firebase path
  if (window.GOOGLE_CLIENT_ID) {
    authSignInGoogleGIS();
    return;
  }
  // Legacy Firebase Google sign-in (kept for backward compat when GIS is not set)
  if (!authGetFirebaseConfig()) {
    setTimeout(function(){ authShowError("authPhoneError", "Google sign-in is not available."); }, 0);
    return;
  }
  const btn = document.getElementById("authGoogleBtn");
  if (!btn) return;
  btn.disabled = true;
  btn.classList.add("btn--loading");
  firebaseLoad().then(function() {
    const provider = new firebase.auth.GoogleAuthProvider();
    provider.addScope("profile");
    provider.addScope("email");
    firebase.auth().signInWithPopup(provider).then(function(result) {
      _handleFirebaseGoogleSignIn(result);
    }).catch(function(err) {
      btn.disabled = false;
      btn.classList.remove("btn--loading");
      if (err.code === "auth/popup-blocked") {
        sessionStorage.setItem('__google_redirect_pending', '1');
        firebase.auth().signInWithRedirect(provider);
      } else if (err.code !== "auth/popup-closed-by-user") {
        authShowError("authPhoneError", friendlyFirebaseError(err));
      }
    });
  }).catch(function(err) {
    btn.disabled = false;
    btn.classList.remove("btn--loading");
    authShowError("authPhoneError", friendlyFirebaseError(err));
  });
}

// Minimal Firebase Google sign-in handler (fallback only)
function _handleFirebaseGoogleSignIn(result) {
  if (!result || !result.user) return;
  result.user.getIdToken().then(function(idToken) {
    var name = result.user.displayName || "";
    var email = result.user.email || "";
    return fetch("/auth/phone/verify", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": (typeof window.getCsrfToken === "function") ? window.getCsrfToken() : ""
      },
      body: JSON.stringify({ id_token: idToken, name, email }),
    });
  }).then(function(res) {
    return res.json().then(function(data) {
      if (!res.ok) {
        authShowError("authPhoneError", data.error || "Sign-in failed.");
        var btn = document.getElementById("authGoogleBtn");
        if (btn) { btn.disabled = false; btn.classList.remove("btn--loading"); }
        return;
      }
      // Use the name from the response or the Google profile
      var successName = data.name || result.user.displayName || "";
      authShowSuccess(successName, "", data.email || "");
    });
  }).catch(function(err) {
    authShowError("authPhoneError", err.message || "Google sign-in failed.");
    var btn = document.getElementById("authGoogleBtn");
    if (btn) { btn.disabled = false; btn.classList.remove("btn--loading"); }
  });
}

// ─── One-tap auto sign-in on page load (if eligible) ────────────────────
// This is the KEY feature: if the user has an active Google session, a small
// one-tap dialog appears instantly (hidden iframe, no popup). Only fires
// when GIS is configured and the user isn't already logged in.
(function initOneTap() {
  if (!window.GOOGLE_CLIENT_ID) return;
  // Skip if user is already logged in server-side
  if (document.body && document.body.getAttribute('data-customer-logged-in') === '1') return;
  // Skip if user just signed out in this session
  if (sessionStorage.getItem('__google_signout') === '1') return;

  function tryOneTap() {
    if (!window.google || !window.google.accounts || !window.google.accounts.id) {
      // GIS script may not be loaded yet — retry
      setTimeout(tryOneTap, 500);
      return;
    }
    google.accounts.id.initialize({
      client_id: window.GOOGLE_CLIENT_ID,
      callback: handleGISCredential,
      cancel_on_tap_outside: false,
    });
    _gisInitialized = true;
    _gisPromptShown = true;
    google.accounts.id.prompt();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', tryOneTap);
  } else {
    tryOneTap();
  }
})();

// ─── Country code selector ────────────────────────────────────────────────

function initCountrySelector() {
  const trigger = document.getElementById("countrySelector");
  const dropdown = document.getElementById("countryDropdown");
  const list = document.getElementById("countryList");
  const search = document.getElementById("countrySearch");
  const flagEl = document.getElementById("selectedFlag");
  const codeEl = document.getElementById("selectedCode");
  const phoneInput = document.getElementById("authPhoneInput");

  if (!list) return;

  function render(filter) {
    const q = (filter || "").toLowerCase();
    list.innerHTML = "";
    const matched = q
      ? window.COUNTRIES.filter(c => c.name.toLowerCase().includes(q) || c.code.includes(q))
      : window.COUNTRIES;
    matched.forEach(c => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "country-selector__item" + (c.code === codeEl.textContent && c.flag === flagEl.textContent ? " selected" : "");
      item.innerHTML = `<span class="country-selector__item-flag">${c.flag}</span><span class="country-selector__item-name">${c.name}</span><span class="country-selector__item-code">${c.code}</span>`;
      item.addEventListener("click", () => selectCountry(c));
      list.appendChild(item);
    });
  }

  function selectCountry(c) {
    flagEl.textContent = c.flag;
    codeEl.textContent = c.code;
    phoneInput.maxLength = c.maxDigits;
    phoneInput.placeholder = c.example;
    document.getElementById("phoneHint").textContent = `Enter your ${c.maxDigits}-digit number after ${c.code}.`;
    closeDropdown();
    validatePhoneInput();
  }

  function openDropdown() {
    dropdown.classList.add("open");
    render(search.value);
    search.value = "";
    search.focus();
    trigger.setAttribute("aria-expanded", "true");
  }

  function closeDropdown() {
    dropdown.classList.remove("open");
    trigger.setAttribute("aria-expanded", "false");
  }

  trigger.addEventListener("click", (e) => {
    if (!dropdown.classList.contains("open")) openDropdown();
    else closeDropdown();
  });

  search.addEventListener("input", () => render(search.value));

  document.addEventListener("click", (e) => {
    if (!trigger.contains(e.target) && !dropdown.contains(e.target)) closeDropdown();
  });

  trigger.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeDropdown();
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDropdown(); }
  });

  phoneInput.addEventListener("input", validatePhoneInput);
  phoneInput.addEventListener("blur", formatPhoneDisplay);

  const india = window.COUNTRY_MAP["+91"];
  if (india) selectCountry(india);

  render("");
}

function validatePhoneInput() {
  const input = document.getElementById("authPhoneInput");
  const code = document.getElementById("selectedCode").textContent;
  const country = window.COUNTRY_MAP[code];
  const max = country ? country.maxDigits : 15;
  let digits = input.value.replace(/\D/g, "");
  if (digits.length > max) digits = digits.slice(0, max);
  input.value = digits;
  return digits;
}

function formatPhoneDisplay() {
  const input = document.getElementById("authPhoneInput");
  const code = document.getElementById("selectedCode").textContent;
  const digits = input.value.replace(/\D/g, "");
  if (code === "+91" && digits.length === 10) {
    input.value = digits.slice(0, 5) + " " + digits.slice(5);
  } else if (code === "+1" && digits.length === 10) {
    input.value = "(" + digits.slice(0, 3) + ") " + digits.slice(3, 6) + "-" + digits.slice(6);
  } else if (digits.length > 0) {
    const parts = [];
    for (let i = 0; i < digits.length; i += 4) parts.push(digits.slice(i, i + 4));
    input.value = parts.join(" ");
  }
}

function getFullPhone() {
  const code = document.getElementById("selectedCode").textContent;
  const digits = document.getElementById("authPhoneInput").value.replace(/\D/g, "");
  return code + digits;
}

// ─── Step 1: Send OTP ──────────────────────────────────────────────────────
// Prefers Firebase when configured; falls back to the backend's own
// /auth/send-otp endpoint (no Firebase dependency).

async function authSendCode() {
  const phone = getFullPhone();
  authShowError("authPhoneError", "");

  const country = window.COUNTRY_MAP[document.getElementById("selectedCode").textContent];
  const digits = document.getElementById("authPhoneInput").value.replace(/\D/g, "");
  if (!digits || digits.length < (country ? country.maxDigits - 2 : 6)) {
    authShowError("authPhoneError", "Please enter a complete phone number.");
    return;
  }

  const btn = document.getElementById("authSendCodeBtn");
  btn.disabled = true;
  btn.classList.add("btn--loading");

  // If Firebase SDK config exists, use the Firebase phone-auth path.
  if (authGetFirebaseConfig()) {
    try { await firebaseLoad(); } catch(e) {
      btn.disabled = false; btn.classList.remove("btn--loading");
      authShowError("authPhoneError", "Sign-in service unavailable."); return;
    }
    try {
      authConfirmationResult = await firebase.auth().signInWithPhoneNumber(phone, window.recaptchaVerifier);
      authPendingPhone = phone;
      document.getElementById("authPhoneDisplay").textContent = phone;
      authGoToStep("authStepCode");
      authStartResendTimer();
      setTimeout(() => {
        const firstInput = document.querySelector('#authOtpInputs input[data-idx="0"]');
        if (firstInput) firstInput.focus();
      }, 300);
    } catch (err) {
      console.error("Firebase sendCode error:", err);
      window.recaptchaVerifier.render().then(function(widgetId) { grecaptcha.reset(widgetId); }).catch(function(){});
      authShowError("authPhoneError", friendlyFirebaseError(err));
    } finally {
      btn.disabled = false;
      btn.classList.remove("btn--loading");
    }
    return;
  }

  // Fallback: backend self-contained OTP flow (no Firebase needed)
  try {
    var csrf = (typeof window.getCsrfToken === "function") ? window.getCsrfToken() : "";
    var res = await fetch("/auth/send-otp", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify({ phone: phone }),
    });
    var data = await res.json();
    if (!res.ok) {
      authShowError("authPhoneError", data.error || "Failed to send code.");
      return;
    }
    authPendingPhone = phone;
    document.getElementById("authPhoneDisplay").textContent = phone;
    authGoToStep("authStepCode");
    authStartResendTimer();
    // In dev mode, show the OTP code directly
    if (data.dev_code) {
      var hint = document.getElementById("authDevHint");
      if (hint) { hint.textContent = "Dev code: " + data.dev_code; hint.style.display = "block"; }
    }
    setTimeout(function() {
      var firstInput = document.querySelector('#authOtpInputs input[data-idx="0"]');
      if (firstInput) firstInput.focus();
    }, 300);
  } catch (err) {
    authShowError("authPhoneError", err.message || "Network error. Please try again.");
  } finally {
    btn.disabled = false;
    btn.classList.remove("btn--loading");
  }
}

// ─── Resend ───────────────────────────────────────────────────────────────
// If Firebase is configured, resends via Firebase; otherwise calls the
// backend's /auth/send-otp again.

async function authResendCode() {
  if (!authPendingPhone || authResendSeconds > 0) return;
  authShowError("authCodeError", "");

  // If Firebase is available, use Firebase path.
  if (authGetFirebaseConfig()) {
    try { await firebaseLoad(); } catch(e) { authShowError("authCodeError", "Sign-in service unavailable."); return; }
    try {
      authConfirmationResult = await firebase.auth().signInWithPhoneNumber(authPendingPhone, window.recaptchaVerifier);
      authStartResendTimer();
    } catch (err) {
      console.error("Firebase resend error:", err);
      authShowError("authCodeError", friendlyFirebaseError(err));
    }
    return;
  }

  // Fallback: send again via backend OTP endpoint
  try {
    var csrf = (typeof window.getCsrfToken === "function") ? window.getCsrfToken() : "";
    var res = await fetch("/auth/send-otp", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify({ phone: authPendingPhone }),
    });
    var data = await res.json();
    if (!res.ok) {
      authShowError("authCodeError", data.error || "Failed to resend code.");
      return;
    }
    authStartResendTimer();
    if (data.dev_code) {
      var hint = document.getElementById("authDevHint");
      if (hint) { hint.textContent = "Dev code: " + data.dev_code; hint.style.display = "block"; }
    }
  } catch (err) {
    authShowError("authCodeError", err.message || "Network error. Please try again.");
  }
}

// ─── Step 2: Verify OTP code ──────────────────────────────────────────────
// Prefers Firebase when configured; falls back to the backend's own
// /auth/verify-otp endpoint.

async function authVerifyCode() {
  const inputs = document.querySelectorAll("#authOtpInputs input");
  const code = Array.from(inputs).map(i => i.value).join("");
  authShowError("authCodeError", "");

  if (code.length !== 6 || !/^\d{6}$/.test(code)) {
    authShowError("authCodeError", "Please enter all 6 digits.");
    return;
  }

  const btn = document.getElementById("authVerifyBtn");
  btn.disabled = true;
  btn.classList.add("btn--loading");

  // Firebase path: need authConfirmationResult from Firebase
  if (authGetFirebaseConfig()) {
    if (!authConfirmationResult) {
      btn.disabled = false;
      btn.classList.remove("btn--loading");
      authShowError("authCodeError", "Session expired. Please go back and request a new code.");
      return;
    }
    try {
      const result = await authConfirmationResult.confirm(code);
      const idToken = await result.user.getIdToken();

      const res = await fetch("/auth/phone/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": window.getCsrfToken() },
        body: JSON.stringify({ id_token: idToken }),
      });
      const data = await res.json();

      if (!res.ok) {
        authShowError("authCodeError", data.error || "Verification failed. Please try again.");
        return;
      }

      if (data.name) {
        authShowSuccess(data.name, data.phone, data.email);
      } else {
        authGoToStep("authStepDetails");
        setTimeout(function() {
          var nameInput = document.getElementById("authNameInput");
          if (nameInput) nameInput.focus();
        }, 300);
      }
    } catch (err) {
      console.error("Firebase verifyCode error:", err);
      var otpContainer = document.getElementById("authOtpInputs");
      otpContainer.classList.add("auth-otp-shake");
      setTimeout(function() { otpContainer.classList.remove("auth-otp-shake"); }, 400);
      authShowError("authCodeError", friendlyFirebaseError(err));
    } finally {
      btn.disabled = false;
      btn.classList.remove("btn--loading");
    }
    return;
  }

  // Fallback: verify via backend OTP endpoint (no Firebase)
  try {
    var csrf = (typeof window.getCsrfToken === "function") ? window.getCsrfToken() : "";
    var res = await fetch("/auth/verify-otp", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify({
        phone: authPendingPhone || "",
        code: code,
      }),
    });
    var data = await res.json();

    if (!res.ok) {
      authShowError("authCodeError", data.error || "Verification failed. Please try again.");
      return;
    }

    if (data.name) {
      authShowSuccess(data.name, data.phone, data.email);
    } else {
      authGoToStep("authStepDetails");
      setTimeout(function() {
        var nameInput = document.getElementById("authNameInput");
        if (nameInput) nameInput.focus();
      }, 300);
    }
  } catch (err) {
    console.error("OTP verify error:", err);
    var otpContainer = document.getElementById("authOtpInputs");
    if (otpContainer) { otpContainer.classList.add("auth-otp-shake"); setTimeout(function() { otpContainer.classList.remove("auth-otp-shake"); }, 400); }
    authShowError("authCodeError", err.message || "Verification failed. Please try again.");
  } finally {
    btn.disabled = false;
    btn.classList.remove("btn--loading");
  }
}

// ─── Step 3: Save name/email for new users ────────────────────────────────

async function authFinish() {
  const name = document.getElementById("authNameInput").value.trim();
  const email = document.getElementById("authEmailInput").value.trim();
  authShowError("authDetailsError", "");

  if (!name) {
    authShowError("authDetailsError", "Please enter your name so we can personalize your experience.");
    return;
  }

  const btn = document.getElementById("authFinishBtn");
  btn.disabled = true;
  btn.classList.add("btn--loading");

  try {
    const res = await fetch("/auth/update-profile", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": window.getCsrfToken()
      },
      body: JSON.stringify({ name, email }),
    });
    const data = await res.json();

    if (!res.ok) {
      authShowError("authDetailsError", data.error || "Something went wrong. Please try again.");
      return;
    }

    authShowSuccess(data.name || name, data.phone || authPendingPhone, data.email || email);
  } catch (err) {
    authShowError("authDetailsError", "Network error. Please try again.");
  } finally {
    btn.disabled = false;
    btn.classList.remove("btn--loading");
  }
}

// ─── Back to phone step ───────────────────────────────────────────────────

function authBackToPhone() {
  authGoToStep("authStepPhone");
  authShowError("authCodeError", "");
  authShowError("authPhoneError", "");
  if (authTimerInterval) { clearInterval(authTimerInterval); authTimerInterval = null; }
  document.querySelectorAll("#authOtpInputs input").forEach(i => i.value = "");
  authConfirmationResult = null;
}

// ─── Success ──────────────────────────────────────────────────────────────

function authShowSuccess(name, phone, email) {
  authGoToStep("authStepSuccess");
  const nameEl = document.getElementById("authSuccessName");
  nameEl.textContent = name ? `Hey, ${name}!` : "Welcome!";

  document.querySelectorAll("[data-auth-signed-out]").forEach(el => el.style.display = "none");
  document.querySelectorAll("[data-auth-signed-in]").forEach(el => {
    el.style.display = "";
    const nameSpan = el.querySelector("[data-auth-name-placeholder]");
    if (nameSpan) {
      nameSpan.textContent = (name || "Me").slice(0, 8);
    } else if (el.tagName !== "FORM" && el.dataset.authStatic === undefined) {
      el.textContent = name || phone || "Account";
    }
  });

  const nameField  = document.getElementById("buyerName")
  const emailField = document.getElementById("buyerEmail") || document.getElementById("cartBuyerEmail");
  const phoneField = document.getElementById("buyerPhone") || document.getElementById("cartBuyerPhone");
  if (nameField  && name  && !nameField.value)  nameField.value  = name;
  if (emailField && email && !emailField.value) emailField.value = email;
  if (phoneField && phone && !phoneField.value) phoneField.value = phone;

  if (typeof authEyesWink === "function") authEyesWink();

  document.body.setAttribute("data-customer-logged-in", "1");
  document.body.setAttribute("data-customer-name", name || "");

  const greeting = document.getElementById("userGreeting");
  if (greeting) {
    const greetingName = greeting.querySelector(".greeting__name");
    if (greetingName) greetingName.textContent = name || "there";
    greeting.style.display = "";
    setTimeout(() => greeting.classList.add("greeting--visible"), 100);
    setTimeout(() => {
      if (greeting.classList.contains("greeting--visible") && window.dismissGreeting) window.dismissGreeting();
    }, 6000);
  }

  if (window.showToast) window.showToast(`Signed in as ${name || phone}`, "success");
  setTimeout(() => closeAuthModal(), 1200);
}

// ─── Friendly Firebase error messages ────────────────────────────────────

function friendlyFirebaseError(err) {
  const code = err.code || "";
  const map = {
    "auth/invalid-phone-number":      "That phone number isn't valid. Please include your country code, e.g. +91.",
    "auth/too-many-requests":         "Too many attempts. Please wait a few minutes and try again.",
    "auth/invalid-verification-code": "That code is wrong. Please check and try again.",
    "auth/code-expired":              "That code has expired. Please request a new one.",
    "auth/quota-exceeded":            "SMS quota exceeded. Please try again later.",
    "auth/captcha-check-failed":      "reCAPTCHA check failed. Please refresh and try again.",
    "auth/network-request-failed":    "Network error. Please check your connection and try again.",
    "auth/missing-phone-number":      "Please enter your phone number.",
  };
  return map[code] || "Something went wrong. Please try again.";
}

// ─── OTP input UX + reCAPTCHA setup ─────────────────────────────────────

document.addEventListener("DOMContentLoaded", function () {
  const inputs = document.querySelectorAll("#authOtpInputs input");
  if (!inputs.length) return;

  inputs.forEach((input, idx) => {
    input.addEventListener("input", function () {
      this.value = this.value.replace(/\D/g, "");
      if (this.value && idx < inputs.length - 1) inputs[idx + 1].focus();
      const code = Array.from(inputs).map(i => i.value).join("");
      if (code.length === 6) setTimeout(() => authVerifyCode(), 200);
    });

    input.addEventListener("keydown", function (e) {
      if (e.key === "Backspace" && !this.value && idx > 0) {
        inputs[idx - 1].focus();
        inputs[idx - 1].value = "";
      }
      if (e.key === "ArrowLeft"  && idx > 0)               inputs[idx - 1].focus();
      if (e.key === "ArrowRight" && idx < inputs.length - 1) inputs[idx + 1].focus();
    });

    input.addEventListener("paste", function (e) {
      e.preventDefault();
      const pasted = (e.clipboardData || window.clipboardData).getData("text").replace(/\D/g, "");
      pasted.split("").forEach((d, i) => { if (inputs[i]) inputs[i].value = d; });
      const code = Array.from(inputs).map(i => i.value).join("");
      if (code.length === 6) {
        setTimeout(() => authVerifyCode(), 200);
      } else if (inputs[Math.min(pasted.length, 5)]) {
        inputs[Math.min(pasted.length, 5)].focus();
      }
    });
  });

  document.addEventListener("keydown", e => { if (e.key === "Escape") closeAuthModal(); });

// ─── Enter-key submits the active step, same as clicking its button ──────
(function wireEnterToSubmit() {
  function onEnter(id, fn) {
    var el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("keydown", function(e) {
      if (e.key === "Enter") { e.preventDefault(); fn(); }
    });
  }
  onEnter("authPhoneInput", authSendCode);
  onEnter("authNameInput", authFinish);
  onEnter("authEmailInput", authFinish);
})();

  initCountrySelector();
});
