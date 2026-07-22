import os
import re
import json
import time
import hashlib
import secrets
import smtplib
import threading
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from functools import wraps

import requests
from flask import session, redirect, url_for, request, abort, jsonify, g, flash
from werkzeug.utils import secure_filename
from PIL import Image, UnidentifiedImageError

import config
import database as db

# Log a startup warning if Firebase is configured but google-auth is missing,
# so the failure isn't silent (Issue #20 from audit).
if config.FIREBASE_API_KEY:
    try:
        from google.auth.transport import requests as _google_requests  # noqa: F401
        from google.oauth2 import id_token as _google_id_token  # noqa: F401
    except ImportError:
        import logging as _logging
        _logging.getLogger("virtual_store").warning(
            "WARNING: FIREBASE_API_KEY is set but the 'google-auth' package is not "
            "installed. Firebase phone authentication will silently not work. "
            "Install it with: pip install google-auth"
        )


# ---------- Auth (admin) ----------

def has_permission(user_permissions, *required):
    """Check if a user's permission list satisfies any of the required perms.
    'master' has wildcard ['*'] which always passes."""
    if not user_permissions:
        return False
    if "*" in user_permissions:
        return True
    return any(p in user_permissions for p in required)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_id"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


# ---------- Auth (Android admin app — opaque bearer tokens) ----------
# Separate from the web admin's session-cookie auth above. The app logs in
# once via POST /api/admin/login and gets back an opaque token; every other
# call sends it as "Authorization: Bearer <token>". We only ever store a
# hash of the token (see hash_api_token) so a database leak can't be replayed.

def generate_api_token():
    """A new, random opaque token to hand to a freshly logged-in device."""
    return secrets.token_urlsafe(32)


def hash_api_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def api_admin_required(view):
    """Decorator for /api/admin/* routes. Looks up the bearer token's hash in
    api_tokens, rejects if missing/revoked/expired, and stashes the admin's
    id on flask.g for the view to use. Always returns JSON (never redirects —
    there's no login page to redirect a mobile app to)."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"success": False, "error": "Missing bearer token."}), 401
        token = auth_header[len("Bearer "):].strip()
        if not token:
            return jsonify({"success": False, "error": "Missing bearer token."}), 401

        conn = db.get_db()
        row = conn.execute(
            "SELECT * FROM api_tokens WHERE token_hash = ? AND revoked = 0",
            (hash_api_token(token),),
        ).fetchone()

        if not row:
            conn.close()
            return jsonify({"success": False, "error": "Invalid or expired token. Please log in again."}), 401

        # Token expiry is time-based (API_TOKEN_EXPIRY_DAYS from creation)
        try:
            created = datetime.fromisoformat(row["created_at"])
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - created > timedelta(days=config.API_TOKEN_EXPIRY_DAYS):
                conn.close()
                return jsonify({"success": False, "error": "Your session has expired. Please log in again."}), 401
        except (ValueError, TypeError):
            pass

        # Also verify the admin user still exists and is active
        admin = conn.execute(
            "SELECT id, role, permissions, is_active FROM admin_users WHERE id = ?",
            (row["admin_user_id"],),
        ).fetchone()
        if not admin or not admin["is_active"]:
            conn.execute(
                "UPDATE api_tokens SET revoked = 1 WHERE id = ?", (row["id"],)
            )
            conn.commit()
            conn.close()
            return jsonify({"success": False, "error": "Your account has been deactivated."}), 403

        conn.execute(
            "UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (db.now(), row["id"])
        )
        conn.commit()
        conn.close()

        g.admin_id = admin["id"]
        g.admin_role = admin["role"]
        g.admin_permissions = json.loads(admin["permissions"] or "[]") if admin["permissions"] else []
        if not g.admin_permissions and admin["role"] in ("master", "admin"):
            g.admin_permissions = ["*"]
        g.api_token_id = row["id"]
        return view(*args, **kwargs)
    return wrapped


def api_requires_permission(*perms):
    """Decorator that checks the API-authenticated admin has at least one of *perms.
    Reuses the token auth from api_admin_required (already sets g.admin_id, g.admin_role,
    g.admin_permissions). Master role (['*']) always passes. Returns JSON 403 on failure."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not g.get("admin_id"):
                return jsonify({"success": False, "error": "Authentication required."}), 401
            if not perms:
                return view(*args, **kwargs)
            if has_permission(g.get("admin_permissions", []), *perms):
                return view(*args, **kwargs)
            return jsonify({"success": False, "error": "Insufficient permissions."}), 403
        return wrapped
    return decorator


# ---------- Auth (customer, via Firebase phone/OTP) ----------

def customer_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("customer_id"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Please sign in first."}), 401
            return redirect(url_for("home"))
        # Check session token version — if it doesn't match the DB, the
        # session was revoked via "Sign out everywhere".
        stored_version = session.get("session_token_version", 0)
        if stored_version is not None:
            conn = None
            try:
                conn = db.get_db()
                row = conn.execute(
                    "SELECT session_token_version FROM customers WHERE id = ?",
                    (session["customer_id"],),
                ).fetchone()
                if row and row["session_token_version"] != stored_version:
                    # Session was revoked — clear and redirect
                    for key in ("customer_id", "customer_name", "customer_phone",
                                "customer_email", "session_token_version"):
                        session.pop(key, None)
                    conn.close()
                    flash("Your session was revoked. Please sign in again.", "info")
                    if request.is_json or request.path.startswith("/api/"):
                        return jsonify({"error": "Session revoked. Please sign in again."}), 401
                    return redirect(url_for("home"))
            except Exception:
                # If the column doesn't exist yet (pre-migration), skip the check
                pass
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
        return view(*args, **kwargs)
    return wrapped


def firebase_auth_enabled():
    return bool(config.FIREBASE_API_KEY and config.FIREBASE_PROJECT_ID)


def verify_firebase_id_token(id_token):
    """Verifies a Firebase Auth ID token sent up from the browser after a
    successful phone/OTP sign-in, using Google's public certs — no service
    account or firebase-admin dependency needed. Returns the decoded token
    dict (with at least 'uid'/'sub' and optionally 'phone_number') on
    success, or None if the token is missing/invalid/expired/wrong project.
    google-auth is an optional dependency: if it isn't installed, phone
    auth silently behaves as disabled rather than crashing the site.

    A short-lived cache (keyed by a hash of the token) avoids re-fetching
    Google's public certs and re-verifying the same token on every call —
    the cert fetch is the slow part on a cold worker, and the same ID token
    is often sent more than once (retries, page reloads, redirect recovery).
    """
    if not id_token or not firebase_auth_enabled():
        return None

    # ---- decoded-token cache (30 min TTL — Google ID tokens last ~1h,
    # and the same token is often re-sent on retries, page reloads, and
    # redirect recovery; re-verifying every 5 minutes was wasteful). ----
    import hashlib
    import time as _time
    cache_key = "tok:" + hashlib.sha256(id_token.encode("utf-8")).hexdigest()
    cached = _FIREBASE_TOKEN_CACHE.get(cache_key)
    now = _time.time()
    if cached and (now - cached["ts"]) < 1800:
        return cached["decoded"]

    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token as google_id_token
    except ImportError:
        return None
    try:
        import logging
        logger = logging.getLogger("virtual_store")
        request = google_requests.Request()
        decoded = google_id_token.verify_firebase_token(
            id_token, request, audience=config.FIREBASE_PROJECT_ID
        )
        logger.info(
            "Firebase token verified OK: uid=%s aud=%s iss=%s",
            decoded.get("uid") or decoded.get("sub", "?"),
            decoded.get("aud", "?"),
            decoded.get("iss", "?"),
        )
        _FIREBASE_TOKEN_CACHE[cache_key] = {"decoded": decoded, "ts": now}
        return decoded
    except Exception as exc:
        import logging
        logging.getLogger("virtual_store").error(
            "Firebase token verification failed: %s: %s",
            type(exc).__name__, exc,
        )
        return None


# Module-level cache for verified Firebase ID tokens (token-hash -> decoded).
_FIREBASE_TOKEN_CACHE: dict = {}


def prewarm_firebase_certs():
    """Fetch Google's Firebase public certs once at startup in a background
    thread so the first sign-in doesn't pay the network round-trip. The
    google-auth library keeps its own module-level cert cache, so this call
    warms it for every later verify_firebase_id_token() in this process."""
    if not firebase_auth_enabled():
        return
    import threading

    def _warm():
        try:
            from google.auth.transport import requests as google_requests
            from google.oauth2 import id_token as google_id_token
            request = google_requests.Request()
            # A dummy token triggers the cert fetch; verification fails but
            # the certs are now cached. We swallow the expected error.
            google_id_token.verify_firebase_token(
                "dummy.warmup.token", request,
                audience=config.FIREBASE_PROJECT_ID,
            )
        except Exception:
            pass

    threading.Thread(target=_warm, daemon=True).start()


# ---------- CSRF (lightweight, no extra dependency) ----------
# Two flavours: `check_csrf()` for normal HTML <form> POSTs (token comes in
# the form body), and `check_csrf_api()` for JSON fetch() calls, where the
# token travels in an `X-CSRF-Token` header instead (forms don't send custom
# headers, which is exactly why this split exists).

def get_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return session["csrf_token"]


def check_csrf():
    token = request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    if not token or not expected or not secrets.compare_digest(token, expected):
        abort(400, description="Your session expired — please refresh and try again.")


def check_csrf_api():
    """For requests made via fetch() rather than a plain form submit — covers
    JSON bodies (token in body or header) and FormData bodies sent via fetch
    (token travels as a normal form field there, same as check_csrf())."""
    token = request.headers.get("X-CSRF-Token", "")
    if not token:
        data = request.get_json(silent=True) or {}
        token = data.get("csrf_token", "")
    if not token:
        token = request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    if not token or not expected or not secrets.compare_digest(token, expected):
        response = jsonify({"error": "Your session expired — please refresh the page and try again."})
        response.status_code = 400
        abort(response)


# ---------- Rate limiting (simple, in-process — no Redis needed) ----------
# Good enough for a single small instance. Resets on restart, and only tracks
# this one worker process — not a substitute for a real service at scale, but
# stops casual scripted abuse of login/checkout/coupon/newsletter endpoints.

_rate_buckets = {}
_rate_lock = threading.Lock()


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def rate_limited(key_prefix, max_attempts, window_seconds):
    """Returns True if the current client has exceeded max_attempts within
    window_seconds for this key_prefix. Also records the current attempt."""
    key = f"{key_prefix}:{_client_ip()}"
    now = time.time()
    with _rate_lock:
        bucket = _rate_buckets.setdefault(key, [])
        # drop anything outside the window
        cutoff = now - window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        if len(bucket) >= max_attempts:
            return True
        bucket.append(now)
        # keep the whole structure from growing forever
        if len(_rate_buckets) > 5000:
            _rate_buckets.clear()
        return False


# ---------- CAPTCHA (Cloudflare Turnstile — free, no account limits) ----------

def turnstile_enabled():
    return bool(config.TURNSTILE_SITE_KEY and config.TURNSTILE_SECRET_KEY)


def verify_turnstile(token):
    """Returns True if Turnstile is not configured (so the site works before
    setup) or if the token is valid. Returns False only on a real failure."""
    if not turnstile_enabled():
        return True
    if not token:
        return False
    try:
        resp = requests.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={
                "secret": config.TURNSTILE_SECRET_KEY,
                "response": token,
                "remoteip": _client_ip(),
            },
            timeout=8,
        )
        return bool(resp.json().get("success"))
    except Exception:
        return False


# ---------- Slugs ----------

def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or secrets.token_hex(4)


# ---------- Images ----------

def allowed_image(filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in config.ALLOWED_IMAGE_EXTENSIONS


def save_product_image(file_storage):
    """
    Saves an uploaded image, resized so the longest side is at most
    MAX_IMAGE_DIMENSION px, re-encoded efficiently. Keeps the storefront
    fast regardless of what the admin uploads from their phone/camera.
    Returns the stored filename.
    """
    if not file_storage or file_storage.filename == "":
        return None
    if not allowed_image(file_storage.filename):
        raise ValueError("Please upload a PNG, JPG or WEBP image.")

    # Verify this is actually a valid, safe-to-decode image before touching it —
    # Image.open() alone doesn't fully parse the file, so a corrupt or
    # disguised upload could otherwise slip through.
    try:
        file_storage.stream.seek(0)
        probe = Image.open(file_storage.stream)
        probe.verify()
    except (UnidentifiedImageError, OSError, ValueError):
        raise ValueError("That file doesn't look like a valid image. Please try a different file.")
    finally:
        file_storage.stream.seek(0)

    os.makedirs(config.UPLOAD_FOLDER, exist_ok=True)
    ext = file_storage.filename.rsplit(".", 1)[-1].lower()
    filename = secure_filename(f"{secrets.token_hex(8)}.{ext}")
    path = os.path.join(config.UPLOAD_FOLDER, filename)

    image = Image.open(file_storage)
    image.load()

    # JPEG has no alpha channel — flatten transparency onto white instead of
    # letting Pillow error out (or silently corrupt colours) on save.
    if ext in ("jpg", "jpeg"):
        if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
            background = Image.new("RGB", image.size, (255, 255, 255))
            rgba = image.convert("RGBA")
            background.paste(rgba, mask=rgba.split()[-1])
            image = background
        elif image.mode != "RGB":
            image = image.convert("RGB")
    elif image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")

    w, h = image.size
    longest = max(w, h)
    if longest > config.MAX_IMAGE_DIMENSION:
        scale = config.MAX_IMAGE_DIMENSION / longest
        image = image.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    save_kwargs = {"quality": 85, "optimize": True} if ext in ("jpg", "jpeg") else {"optimize": True}
    image.save(path, **save_kwargs)
    return filename


def delete_file_quietly(filename):
    try:
        # Handle both relative filenames (uploads/) and full relative paths (static/product_files/)
        if os.path.sep in filename:
            os.remove(os.path.join(os.getcwd(), filename))
        else:
            os.remove(os.path.join(config.UPLOAD_FOLDER, filename))
    except OSError:
        pass


# ---------- OTP (self-contained, no external service needed) ----------

def generate_otp_code():
    """Random 6-digit numeric code."""
    return f"{secrets.randbelow(1000000):06d}"


def store_otp(conn, phone, code, name="", email=""):
    """Store an OTP in the database with an expiry. Invalidates any previous
    unused OTPs for the same phone first, so only the latest one works."""
    from datetime import timedelta
    now_str = datetime.now(timezone.utc).isoformat()
    expires = (datetime.now(timezone.utc) + timedelta(minutes=config.OTP_EXPIRY_MINUTES)).isoformat()
    conn.execute("UPDATE otps SET used = 1 WHERE phone = ? AND used = 0", (phone,))
    conn.execute(
        "INSERT INTO otps (phone, code, name, email, created_at, expires_at, used) VALUES (?, ?, ?, ?, ?, ?, 0)",
        (phone, code, name, email, now_str, expires),
    )


def verify_otp_code(conn, phone, code):
    """Check whether the given code matches the latest unused, unexpired OTP
    for this phone. Returns (True, stored_name, stored_email) on success,
    (False, "", "") on failure. Marks the OTP as used on success."""
    row = conn.execute(
        "SELECT * FROM otps WHERE phone = ? AND code = ? AND used = 0 ORDER BY id DESC LIMIT 1",
        (phone, code),
    ).fetchone()
    if not row:
        return False, "", ""
    # Check expiry
    now_str = datetime.now(timezone.utc).isoformat()
    if row["expires_at"] < now_str:
        return False, "", ""
    conn.execute("UPDATE otps SET used = 1 WHERE id = ?", (row["id"],))
    return True, row["name"], row["email"]


# ---------- Email (optional — Resend or SendGrid preferred, SMTP fallback) ----------
# Three interchangeable ways to send mail, tried in this order: Resend (HTTP
# API, no SMTP setup needed), SendGrid (HTTP API), then classic SMTP. Any one
# of them being configured is enough — the site works the same either way,
# callers just call send_email() and don't need to know which is active.

def email_enabled():
    return bool(
        config.RESEND_API_KEY or config.SENDGRID_API_KEY
        or (config.SMTP_HOST and config.SMTP_USERNAME and config.SMTP_PASSWORD)
    )


def _from_header():
    name = config.EMAIL_FROM_NAME.strip()
    addr = config.EMAIL_FROM or config.SMTP_FROM
    return f"{name} <{addr}>" if name else addr


def _send_via_resend(to_address, subject, body):
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
        json={
            "from": _from_header(),
            "to": [to_address],
            "subject": subject,
            "text": body,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return True


def _send_via_sendgrid(to_address, subject, body):
    from_addr = config.EMAIL_FROM or config.SMTP_FROM
    resp = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={
            "Authorization": f"Bearer {config.SENDGRID_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "personalizations": [{"to": [{"email": to_address}]}],
            "from": {
                "email": from_addr,
                **({"name": config.EMAIL_FROM_NAME} if config.EMAIL_FROM_NAME else {}),
            },
            "subject": subject,
            "content": [{"type": "text/plain", "value": body}],
        },
        timeout=15,
    )
    resp.raise_for_status()
    return True


def _send_via_smtp(to_address, subject, body):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = _from_header()
    msg["To"] = to_address
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as server:
        server.starttls()
        server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
        server.sendmail(config.EMAIL_FROM or config.SMTP_FROM, [to_address], msg.as_string())
    return True


def send_email(to_address, subject, body):
    """Best-effort — tries providers in priority order and returns True the
    moment one succeeds. Never raises: a broken email provider should never
    take down checkout or delivery, it should just mean the customer doesn't
    get the automated email (the order/delivery itself still goes through)."""
    if not email_enabled():
        return False
    if config.RESEND_API_KEY:
        try:
            return _send_via_resend(to_address, subject, body)
        except Exception:
            pass
    if config.SENDGRID_API_KEY:
        try:
            return _send_via_sendgrid(to_address, subject, body)
        except Exception:
            pass
    if config.SMTP_HOST and config.SMTP_USERNAME and config.SMTP_PASSWORD:
        try:
            return _send_via_smtp(to_address, subject, body)
        except Exception:
            pass
    return False


# ---------- Push notifications (Firebase Cloud Messaging, Android admin app) ----------
# Optional — only active once FIREBASE_SERVICE_ACCOUNT_JSON or
# FIREBASE_SERVICE_ACCOUNT_FILE is set. Everything here is best-effort and
# never raises: a Firebase hiccup must never break checkout or the payment
# webhook, it should just mean the admin doesn't get a phone alert this time.

_firebase_app = None
_firebase_init_lock = threading.Lock()
_firebase_init_failed = False


def push_notifications_enabled():
    return bool(config.FIREBASE_SERVICE_ACCOUNT_JSON or config.FIREBASE_SERVICE_ACCOUNT_FILE)


def _get_firebase_app():
    """Lazily initializes the firebase-admin SDK exactly once per process and
    caches the result (including failures, so a broken credential doesn't
    retry — and log a warning — on every single order)."""
    global _firebase_app, _firebase_init_failed
    if _firebase_app is not None:
        return _firebase_app
    if _firebase_init_failed or not push_notifications_enabled():
        return None

    with _firebase_init_lock:
        if _firebase_app is not None or _firebase_init_failed:
            return _firebase_app
        import logging as _logging
        logger = _logging.getLogger("virtual_store")
        try:
            import firebase_admin
            from firebase_admin import credentials
        except ImportError:
            logger.warning(
                "WARNING: Firebase push credentials are set but the 'firebase-admin' "
                "package is not installed. Push notifications will silently not work. "
                "Install it with: pip install firebase-admin"
            )
            _firebase_init_failed = True
            return None

        try:
            if config.FIREBASE_SERVICE_ACCOUNT_JSON:
                cred_info = json.loads(config.FIREBASE_SERVICE_ACCOUNT_JSON)
                cred = credentials.Certificate(cred_info)
            else:
                cred = credentials.Certificate(config.FIREBASE_SERVICE_ACCOUNT_FILE)
            _firebase_app = firebase_admin.initialize_app(cred)
        except Exception as e:
            logger.warning("WARNING: Failed to initialize Firebase Admin SDK: %s", e)
            _firebase_init_failed = True
            return None

    return _firebase_app


def _forget_admin_device(fcm_token):
    """Removes a device row whose token Firebase reports as dead (app
    uninstalled, token rotated, etc.) so we stop wasting a send on it."""
    try:
        conn = db.get_db()
        conn.execute("DELETE FROM admin_devices WHERE fcm_token = ?", (fcm_token,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _notify_admins_new_order_sync(order_id):
    fb_app = _get_firebase_app()
    if fb_app is None:
        return
    try:
        from firebase_admin import messaging
    except ImportError:
        return

    try:
        conn = db.get_db()
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not order:
            conn.close()
            return
        devices = conn.execute("SELECT fcm_token FROM admin_devices").fetchall()
        conn.close()
    except Exception:
        return

    if not devices:
        return

    amount_display = "{:,}".format(order["amount"]) if order["amount"] is not None else "0"
    title = f"🛒 New order — ₹{amount_display}"
    body = f"{order['product_name']} · {order['order_ref']}"
    data = {
        "type": "new_order",
        "order_id": str(order["id"]),
        "order_ref": order["order_ref"],
    }

    dead_tokens = []
    for device in devices:
        token = device["fcm_token"]
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data=data,
            token=token,
        )
        try:
            messaging.send(message, app=fb_app)
        except Exception:
            # Covers Firebase's "unregistered"/"invalid-argument" errors for a
            # token as well as any transient send failure — either way, drop
            # it rather than retrying a possibly-dead device on every order.
            dead_tokens.append(token)

    for token in dead_tokens:
        _forget_admin_device(token)


def notify_admins_new_order(order_id):
    """Pushes a notification to every registered admin device the moment an
    order becomes 'paid'. Fire-and-forget in a background thread so a slow
    or unreachable Firebase never adds latency to the checkout/webhook
    response the caller is about to send back."""
    if not push_notifications_enabled():
        return
    threading.Thread(
        target=_notify_admins_new_order_sync, args=(order_id,), daemon=True
    ).start()


# ================================================================
# Twilio SMS + WhatsApp notifications
# ================================================================
def twilio_enabled():
    return bool(config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN)


def send_sms(to_phone, message):
    """Send a plain SMS via Twilio. to_phone should be E.164."""
    if not twilio_enabled():
        return False
    try:
        from twilio.rest import Client as TwilioClient
        client = TwilioClient(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
        client.messages.create(
            body=message,
            from_=config.TWILIO_PHONE,
            to=to_phone,
        )
        return True
    except Exception:
        return False


def whatsapp_enabled():
    return bool(config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN)


def send_whatsapp(to_phone, message):
    """Send a WhatsApp message via Twilio. to_phone should be E.164 without
    prefix — we add whatsapp: internally."""
    if not whatsapp_enabled():
        return False
    try:
        from twilio.rest import Client as TwilioClient
        client = TwilioClient(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
        client.messages.create(
            body=message,
            from_=config.TWILIO_WHATSAPP_FROM,
            to=f"whatsapp:{to_phone}",
        )
        return True
    except Exception:
        return False


# ================================================================
# Product file upload / download system
# ================================================================
def allowed_product_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in config.ALLOWED_PRODUCT_EXTENSIONS


def save_product_file(file_storage):
    """Save an uploaded product file to the product_files directory.
    Returns the relative path (e.g. 'static/product_files/abc123.pdf')."""
    import uuid
    ext = file_storage.filename.rsplit(".", 1)[1].lower() if "." in file_storage.filename else ""
    safe_name = f"{uuid.uuid4().hex}.{ext}" if ext else uuid.uuid4().hex
    rel_path = os.path.join(config.PRODUCT_UPLOAD_FOLDER, safe_name)
    abspath = os.path.join(os.getcwd(), rel_path)
    os.makedirs(os.path.dirname(abspath), exist_ok=True)
    file_storage.save(abspath)
    return rel_path


def generate_download_tokens(conn, order_id, product_ids, expiry_hours=72):
    """Generate secure single-use download tokens for each product file
    associated with the given product IDs. Returns a list of dicts with
    token / filename / original_name."""
    import secrets
    from datetime import datetime, timedelta, timezone
    tokens = []
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=expiry_hours)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    from config import MAX_DOWNLOADS
    for pid in product_ids:
        files = conn.execute(
            "SELECT id, filename, original_name FROM product_files WHERE product_id = ?",
            (pid,),
        ).fetchall()
        for pf in files:
            token_str = secrets.token_urlsafe(32)
            conn.execute(
                """INSERT INTO download_tokens (order_id, product_id, token, file_path,
                   filename, expires_at, created_at, downloads_remaining)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, pid, token_str, pf["filename"], pf["original_name"], expires_at, now, MAX_DOWNLOADS),
            )
            tokens.append({
                "token": token_str,
                "filename": pf["original_name"],
                "file_path": pf["filename"],
            })
    return tokens


# ================================================================
# Abandoned cart tracking
# ================================================================
def track_cart_add(session_key, product_id, product_name, product_price, quantity=1):
    """Log or update an abandoned cart entry. Called when an item is added
    to cart and the visitor hasn't checked out yet."""
    import json
    from datetime import datetime, timezone
    conn = db.get_db()
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT id, product_data FROM abandoned_carts WHERE session_key = ? AND status = 'active'",
        (session_key,),
    ).fetchone()
    if existing:
        items = json.loads(existing["product_data"])
        # update qty if already in cart
        found = False
        for item in items:
            if item["product_id"] == product_id:
                item["quantity"] = item.get("quantity", 1) + quantity
                found = True
                break
        if not found:
            items.append({"product_id": product_id, "name": product_name, "price": product_price, "quantity": quantity})
        conn.execute(
            "UPDATE abandoned_carts SET product_data = ?, updated_at = ? WHERE id = ?",
            (json.dumps(items), now, existing["id"]),
        )
    else:
        items = json.dumps([{"product_id": product_id, "name": product_name, "price": product_price, "quantity": quantity}])
        conn.execute(
            """INSERT INTO abandoned_carts (session_key, product_data, created_at, updated_at)
               VALUES (?, ?, ?, ?)""",
            (session_key, items, now, now),
        )
    conn.commit()
    conn.close()


def track_cart_contact(session_key, name="", email="", phone=""):
    """Attach contact info to an abandoned cart entry. Called when the
    visitor fills in their details on the checkout form."""
    from datetime import datetime, timezone
    conn = db.get_db()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE abandoned_carts SET name = COALESCE(NULLIF(?, ''), name),
           email = COALESCE(NULLIF(?, ''), email), phone = COALESCE(NULLIF(?, ''), phone),
           updated_at = ? WHERE session_key = ? AND status = 'active'""",
        (name, email, phone, now, session_key),
    )
    conn.commit()
    conn.close()


def resolve_abandoned_cart(session_key):
    """Mark an abandoned cart as resolved (checked out)."""
    from datetime import datetime, timezone
    conn = db.get_db()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE abandoned_carts SET status = 'resolved', updated_at = ? WHERE session_key = ? AND status = 'active'",
        (now, session_key),
    )
    conn.commit()
    conn.close()


# ════════════════════════════════════════════════════════════════
# Web Push (browser push notifications for admin panel)
# ════════════════════════════════════════════════════════════════

_VAPID_PRIVATE_KEY_CACHED = None


def _get_vapid_private_key():
    global _VAPID_PRIVATE_KEY_CACHED
    if _VAPID_PRIVATE_KEY_CACHED is not None:
        return _VAPID_PRIVATE_KEY_CACHED
    raw = config.VAPID_PRIVATE_KEY
    if not raw:
        return None
    _VAPID_PRIVATE_KEY_CACHED = raw
    return raw


def web_push_enabled():
    return bool(config.VAPID_PUBLIC_KEY and _get_vapid_private_key())


def _send_web_push(subscription_info, title, body, data=None):
    """Send a Web Push notification to a single browser subscription."""
    if not web_push_enabled():
        return False

    import base64, struct, time, http.client as http_client
    from urllib.parse import urlparse
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    endpoint = subscription_info.get("endpoint")
    p256dh = subscription_info.get("keys", {}).get("p256dh", "")
    auth = subscription_info.get("keys", {}).get("auth", "")
    if not endpoint or not p256dh or not auth:
        return False

    def _b64decode(s):
        return base64.urlsafe_b64decode(s + "==")

    client_pub = _b64decode(p256dh)
    client_auth = _b64decode(auth)

    # Server ephemeral key
    server_key = ec.generate_private_key(ec.SECP256R1())
    server_pub_bytes = server_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint
    )
    client_public_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), client_pub)
    shared_secret = server_key.exchange(ec.ECDH(), client_public_key)

    # HKDF
    salt = secrets.token_bytes(16)
    prk = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"Content-Encoding: auth\0"
    ).derive(shared_secret)

    context = (b"P-256\0" +
               struct.pack(">H", len(client_pub)) + client_pub +
               struct.pack(">H", len(server_pub_bytes)) + server_pub_bytes)

    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=client_auth,
               info=b"Content-Encoding: aes128gcm\0" + context).derive(prk)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=client_auth,
                 info=b"Content-Encoding: nonce\0" + context).derive(prk)

    # Encrypt
    aesgcm = AESGCM(cek)
    payload_bytes = json.dumps({
        "title": title, "body": body, "data": data or {},
    }).encode("utf-8")
    padded = struct.pack(">H", 0) + payload_bytes
    ciphertext = aesgcm.encrypt(nonce, padded, b"")

    record_size = 4096
    record = (salt +
              struct.pack(">I", record_size) +
              bytes([len(server_pub_bytes)]) +
              server_pub_bytes +
              ciphertext)

    # VAPID JWT
    vapid_priv_pem = "-----BEGIN PRIVATE KEY-----\n" + _get_vapid_private_key() + "\n-----END PRIVATE KEY-----"
    vapid_private = serialization.load_pem_private_key(vapid_priv_pem.encode(), password=None)
    vapid_pub_bytes = vapid_private.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint
    )

    now_ts = int(time.time())
    jwt_header = _b64encode_str(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    jwt_payload = _b64encode_str(json.dumps({
        "aud": urlparse(endpoint).netloc,
        "exp": now_ts + 86400,
        "sub": config.VAPID_CLAIM_EMAIL or "mailto:admin@virtualstore.local",
    }).encode())
    sign_input = (jwt_header + "." + jwt_payload).encode()
    der_sig = vapid_private.sign(sign_input, ec.ECDSA(hashes.SHA256()))

    # Parse DER signature to raw r||s
    r_start = 4 if der_sig[2] == 0x02 else 3
    r_len = der_sig[r_start - 1]
    if der_sig[r_start] == 0x00:
        r_start += 1
        r_len -= 1
    r_bytes = der_sig[r_start:r_start + r_len]
    s_start = r_start + r_len + 2
    s_len = der_sig[s_start - 1]
    if der_sig[s_start] == 0x00:
        s_start += 1
        s_len -= 1
    s_bytes = der_sig[s_start:s_start + s_len]
    raw_sig = r_bytes.rjust(32, b'\0') + s_bytes.rjust(32, b'\0')
    jwt_sig = _b64encode_str(raw_sig)
    vapid_jwt = jwt_header + "." + jwt_payload + "." + jwt_sig

    headers = {
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": "86400",
        "Authorization": "vapid t=" + vapid_jwt + ", k=" + _b64encode_str(vapid_pub_bytes),
    }

    parsed = urlparse(endpoint)
    conn = http_client.HTTPSConnection(parsed.netloc, timeout=10)
    try:
        conn.request("POST", parsed.path, body=record, headers=headers)
        resp = conn.getresponse()
        resp.read()
        return resp.status
    except Exception:
        return False
    finally:
        conn.close()


def _b64encode_str(data):
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def webpush_notify_admins_new_order(order_id):
    """Send Web Push notifications to all web-push-registered admin devices."""
    try:
        conn = db.get_db()
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not order:
            conn.close()
            return
        devices = conn.execute(
            "SELECT fcm_token, id FROM admin_devices WHERE platform = 'web'"
        ).fetchall()
        conn.close()
    except Exception:
        return

    if not devices:
        return

    amount_display = "{:,}".format(order["amount"]) if order["amount"] is not None else "0"
    title = "🛒 New order — ₹" + amount_display
    body = order["product_name"] + " · " + order["order_ref"]

    dead_ids = []
    for dev in devices:
        try:
            sub = json.loads(dev["fcm_token"])
            ok = _send_web_push(sub, title, body, {
                "type": "new_order",
                "order_id": str(order["id"]),
                "order_ref": order["order_ref"],
                "url": "/admin/orders",
            })
            if not ok:
                dead_ids.append(dev["id"])
        except Exception:
            dead_ids.append(dev["id"])

    for did in dead_ids:
        try:
            conn2 = db.get_db()
            conn2.execute("DELETE FROM admin_devices WHERE id = ?", (did,))
            conn2.commit()
            conn2.close()
        except Exception:
            pass
