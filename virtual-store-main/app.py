import os
import secrets
import uuid
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pyotp
from markupsafe import Markup

from flask import (
    Flask, g, render_template, request, redirect, url_for, session,
    flash, jsonify, abort, send_file, Response, send_from_directory,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from functools import wraps

import config
import database as db
from helpers import (
    login_required, get_csrf_token, check_csrf, check_csrf_api, slugify,
    save_product_image, delete_file_quietly, send_email, email_enabled,
    rate_limited, turnstile_enabled, verify_turnstile,
    firebase_auth_enabled, verify_firebase_id_token, prewarm_firebase_certs,
    generate_otp_code, store_otp, verify_otp_code,
    notify_admins_new_order, webpush_notify_admins_new_order,
    whatsapp_enabled, send_whatsapp, twilio_enabled, send_sms,
    allowed_product_file, save_product_file,
    customer_login_required,
    track_cart_add, track_cart_contact,
    has_permission,
)
import razorpay_client as rzp
import invoicing
from admin_api import admin_api
from storefront_service import get_primary_image_map

# ---------------------------------------------------------------------------
# Calendarific holiday helper — fetches & caches authentic holidays for the
# configured country so the smart greeting shows real festival names.
# ---------------------------------------------------------------------------
import urllib.request as _ur
import json as _j

_CALENDARIFIC_CACHE_KEY = "calendarific_last_fetch"


def _get_webp_path(path):
    """If a WebP version of the given image path exists, return the WebP path.
    Otherwise return the original path unchanged.  The WebP file must be in
    the same directory with `.webp` appended (e.g. `photo.jpg.webp`)."""
    if not path:
        return path
    webp_path = path + ".webp"
    full = os.path.join(config.UPLOAD_FOLDER, webp_path)
    if os.path.exists(full):
        return webp_path
    return path


def _track_product_view(product_id):
    """Track a recently viewed product.  Stores up to 10 product IDs in the
    session, most recent first.  Uses strings for JSON-serializable storage."""
    pid_str = str(product_id)
    viewed = session.get("recently_viewed", [])
    if pid_str in viewed:
        viewed.remove(pid_str)
    viewed.insert(0, pid_str)
    session["recently_viewed"] = viewed[:10]
    session.modified = True


def fetch_calendarific_holidays(year=None, force=False):
    """Fetch holidays from Calendarific for the configured country+year and
    cache them in the settings table as holiday_<mmdd> keys.  Returns a
    dict of {mmdd: festival_name} (the cached set even on a re-fetch)."""
    if not config.CALENDARIFIC_API_KEY:
        return {}
    if year is None:
        year = datetime.now(timezone.utc).year
    conn = db.get_db()
    # Check if we already fetched this year (unless forced)
    if not force:
        cached = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (_CALENDARIFIC_CACHE_KEY,)
        ).fetchone()
        if cached and cached["value"] == str(year):
            rows = conn.execute(
                "SELECT key, value FROM settings WHERE key LIKE 'holiday_%'"
            ).fetchall()
            conn.close()
            return {r["key"].replace("holiday_", ""): r["value"] for r in rows}
    # Fetch from Calendarific
    url = (
        f"https://calendarific.com/api/v2/holidays?"
        f"api_key={config.CALENDARIFIC_API_KEY}&"
        f"country={config.CALENDARIFIC_COUNTRY}&"
        f"year={year}"
    )
    try:
        req = _ur.Request(url, headers={"User-Agent": "virtual-store/1.0"})
        resp = _ur.urlopen(req, timeout=10)
        data = _j.loads(resp.read())
    except Exception as exc:
        _startup_logger.warning("Calendarific fetch failed: %s", exc)
        conn.close()
        return {}
    holidays = data.get("response", {}).get("holidays", [])
    # Clear old holiday cache
    conn.execute("DELETE FROM settings WHERE key LIKE 'holiday_%'")
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_CALENDARIFIC_CACHE_KEY, str(year)),
    )
    result = {}
    for h in holidays:
        date_str = h.get("date", {}).get("iso")
        if not date_str:
            continue
        mmdd = date_str[5:7] + date_str[8:10]
        name = h.get("name", "").strip()
        if mmdd and name:
            # Only take the first holiday for each date (highest-level one)
            if mmdd not in result:
                # Skip "working day" / "observance" type holidays
                holiday_type = h.get("type", [])
                if not any(t in str(holiday_type) for t in ("National", "Common", "Observance")):
                    continue
                # Skip generic observances like "Day off for ..."
                if "Day off for" in name:
                    continue
                result[mmdd] = name
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (f"holiday_{mmdd}", name),
                )
    conn.commit()
    conn.close()
    return result

app = Flask(__name__)
# Trust Render's proxy headers for correct scheme (HSTS), remote_addr (rate
# limiting), and host detection. x_for=1 trusts the leftmost X-Forwarded-For,
# x_proto=1 trusts the leftmost X-Forwarded-Proto (https flag from Render's LB).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config["SECRET_KEY"] = config.SECRET_KEY
MAX_IMAGES_PER_PRODUCT = 6
# Max request body: 6 images at 5 MB each + up to 5 product files at 100 MB each.
app.config["MAX_CONTENT_LENGTH"] = max(
    config.MAX_IMAGE_SIZE_MB * 1024 * 1024 * MAX_IMAGES_PER_PRODUCT,
    config.MAX_PRODUCT_FILE_MB * 1024 * 1024 * 5,
)

# Session cookie hardening — not readable by JS, not sent cross-site, and
# only sent over HTTPS once deployed behind TLS (off under DEBUG so local
# http:// testing still works).
app.config["SESSION_COOKIE_HTTPONLY"] = True

app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = not config.DEBUG
app.config["SESSION_REFRESH_EACH_REQUEST"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

# Warm Google's Firebase public certs in the background so the first Google
# sign-in after a worker starts isn't slow waiting on the cert fetch.
prewarm_firebase_certs()

app.register_blueprint(admin_api)

# ---------------------------------------------------------------------------
# Permission-check decorator and audit-log helper for admin roles.
# ---------------------------------------------------------------------------

PRESET_PERMISSIONS = {
    "order_manager": ["orders.view", "orders.edit", "orders.refund", "orders.export", "tickets.create"],
    "catalog_manager": ["products.edit", "tickets.create"],
    "support_agent": ["orders.view", "tickets.create"],
    "admin_manager": ["admin.manage", "audit.view", "audit.export", "tickets.create"],
    "content_manager": ["testimonials.manage", "faqs.manage", "newsletter.view", "tickets.create"],
}

def requires_permission(*perms):
    """Decorator that checks the logged-in admin has at least one of *perms.
    Checks admin_users.permissions (JSON list of strings).  Master role
    (permissions ['*']) always passes.  On failure, flashes a message and
    redirects to admin_dashboard, or returns a 403 JSON for API calls."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            admin_id = session.get("admin_id")
            if not admin_id:
                return redirect(url_for("admin_login", next=request.path))
            # Grab permissions from the DB every time (cached in session
            # would be fine too, but DB is precise if an admin's role is
            # edited while they are logged in).
            conn = db.get_db()
            user = conn.execute(
                "SELECT role, permissions, is_active FROM admin_users WHERE id = ?",
                (admin_id,),
            ).fetchone()
            if not user:
                conn.close()
                session.clear()
                return redirect(url_for("admin_login"))
            if not user["is_active"]:
                conn.close()
                session.clear()
                flash("Your admin account has been deactivated.", "error")
                return redirect(url_for("admin_login"))

            # Auto-patch: backfill missing permissions.
            # When new permissions are added to PRESET_PERMISSIONS, existing
            # sub-admins with that preset pick them up automatically.
            # For custom roles, we ensure tickets.create is always present
            # for any non-master admin (since every sub-admin needs tickets).
            if user["role"] != "master":
                try:
                    stored_perms = json.loads(user["permissions"]) if user["permissions"] else []
                except (ValueError, TypeError):
                    stored_perms = []
                patched = False
                # Case 1: preset role — fill in missing preset perms
                if user["role"] in PRESET_PERMISSIONS:
                    preset_perms = list(PRESET_PERMISSIONS[user["role"]])
                    needed = [p for p in preset_perms if p not in stored_perms]
                    if needed:
                        stored_perms.extend(needed)
                        patched = True
                # Case 2: custom role — ensure tickets.create is present
                if "tickets.create" not in stored_perms:
                    stored_perms.append("tickets.create")
                    patched = True
                if patched:
                    conn.execute(
                        "UPDATE admin_users SET permissions = ? WHERE id = ?",
                        (json.dumps(stored_perms), admin_id),
                    )
                    conn.commit()
                    # Update the local variable so session cache picks it up
                    user_perms = stored_perms
                else:
                    user_perms = stored_perms
            else:
                user_perms = ["*"]

            conn.close()

            # Cache permissions in session for nav rendering
            session["admin_permissions"] = user_perms
            session["admin_role"] = user["role"]

            if not perms:
                # No specific permission required — just being logged in is enough
                return view(*args, **kwargs)
            if has_permission(user_perms, *perms):
                return view(*args, **kwargs)
            # Insufficient permissions
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Insufficient permissions."}), 403
            flash("You don't have permission to access that page.", "error")
            return redirect(url_for("admin_dashboard"))
        return wrapped
    return decorator


def log_admin_action(action, target="", details=""):
    """Insert an audit log entry for the current admin (from session)."""
    admin_id = session.get("admin_id")
    if not admin_id:
        return
    conn = db.get_db()
    try:
        conn.execute(
            "INSERT INTO admin_audit_log (admin_id, action, target, details, created_at) VALUES (?, ?, ?, ?, ?)",
            (admin_id, action, target, details, db.now()),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


# --- Simple markdown filter for delivery messages ---
@app.template_filter("simple_markdown")
def simple_markdown_filter(text):
    """Convert basic markdown to safe HTML: **bold**, *italic*, `code`, newlines."""
    if not text:
        return ""
    import re
    html = str(Markup.escape(text or ""))
    html = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html)
    html = re.sub(r'\*(.+?)\*', r'<em>\1</em>', html)
    html = re.sub(r'`([^`]+)`', r'<code>\1</code>', html)
    html = re.sub(r'(https?://[^\s<]+)', r'<a href="\1" target="_blank" rel="noopener">\1</a>', html)
    html = html.replace('\n', '<br>')
    return Markup(html)


# --- Startup check: warn if the database directory might be on ephemeral storage ---
# On platforms like Render, the default filesystem is wiped on every deploy/restart.
# If instance/ (where the SQLite DB lives) isn't on a persistent disk, all data is
# silently lost. This check writes a sentinel file, reads it back, and logs a loud
# warning if anything looks off — it can't *prevent* data loss, but it makes the
# problem visible instead of silent.
import logging as _logging
_startup_logger = _logging.getLogger("virtual_store")
try:
    _db_dir = os.path.dirname(config.DB_PATH) or "."
    _sentinel = os.path.join(_db_dir, ".persistence_check")
    os.makedirs(_db_dir, exist_ok=True)
    _sentinel_value = f"persistent-since-{db.now()}"
    with open(_sentinel, "w") as _f:
        _f.write(_sentinel_value)
    with open(_sentinel, "r") as _f:
        _read_back = _f.read().strip()
    if _read_back != _sentinel_value:
        _startup_logger.warning(
            "WARNING: Database directory '%s' failed a persistence read-back check. "
            "Data may be on ephemeral storage and could be lost on redeploy.",
            _db_dir,
        )
except Exception as _e:
    _startup_logger.warning(
        "WARNING: Could not verify persistence of database directory '%s': %s. "
        "If you're deploying on Render or similar, ensure a Persistent Disk is attached "
        "to the instance/ and static/uploads/ directories.",
        os.path.dirname(config.DB_PATH) or ".",
        _e,
    )

# Warn if OTP_DEV_MODE is true in a non-debug (production-like) deployment
if config.OTP_DEV_MODE and not config.DEBUG:
    _startup_logger.warning(
        "WARNING: OTP_DEV_MODE is true but DEBUG is false. OTP codes are being "
        "returned in API responses — this is insecure for production. Set "
        "OTP_DEV_MODE=false in your environment."
    )

# Warn if SECRET_KEY was auto-generated (will invalidate sessions on restart)
if config.SECRET_KEY_WAS_GENERATED:
    _startup_logger.warning(
        "WARNING: SECRET_KEY was auto-generated — sessions will be invalidated "
        "on every server restart. Set SECRET_KEY in your environment to a fixed value."
    )

# Warn if ADMIN_PASSWORD was not set (no default admin can be created)
if not config.DEFAULT_ADMIN_PASSWORD:
    _startup_logger.warning(
        "WARNING: ADMIN_PASSWORD is not set — the default admin account cannot be "
        "created on first run. Set ADMIN_PASSWORD in your environment."
    )


@app.before_request
def make_session_permanent():
    if session:
        session.permanent = True


@app.before_request
def _ensure_db_initialized():
    """Initialize the database on the first request (not at import time).
    This lets gunicorn boot and open the port immediately even if the
    Turso/libsql connection is slow — the DB connects on first traffic.
    Health checks are excluded so Render can verify the worker is up
    without depending on the database."""
    if request.path == "/healthz":
        return
    db.init_db_if_needed()


@app.before_request
def capture_url_coupon():
    """Check for ?coupon=CODE in the URL on any GET request and store it in
    the session so it can be auto-applied at checkout. This enables
    URL-driven coupons from marketing campaigns, social media links, etc."""
    if request.method == "GET" and request.args.get("coupon"):
        code = request.args.get("coupon", "").strip().upper()
        if code:
            session["url_coupon_code"] = code
            session.modified = True


@app.before_request
def stamp_request_context():
    g.request_started_at = _time.time()
    g.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]



@app.teardown_appcontext
def _close_request_db(exc):
    conn = g.pop("_sqlite_db_conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def is_safe_redirect_target(target):
    """Only allow redirecting to a same-site relative path — blocks
    open-redirect attacks via a crafted ?next= value."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return False
    return True


@app.after_request
def set_security_headers(response):
    # Invalidate catalog/settings caches after any admin POST (product,
    # section, testimonial, FAQ, settings saves) so changes show up
    # immediately instead of waiting for TTL expiry.
    if request.method == "POST" and request.path.startswith("/admin/") and request.path not in {"/admin/login", "/admin/logout"}:
        try:
            invalidate_frontend_caches()
        except Exception as exc:
            _startup_logger.warning("Cache invalidation skipped: %s", exc)

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    # Cache static assets aggressively
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
    )
    rid = getattr(g, "request_id", None)
    if rid:
        response.headers.setdefault("X-Request-ID", rid)
    started_at = getattr(g, "request_started_at", None)
    if started_at is not None:
        elapsed_ms = int((_time.time() - started_at) * 1000)
        response.headers.setdefault("X-Response-Time-ms", str(elapsed_ms))
        if elapsed_ms >= 1000:
            _startup_logger.info("slow request rid=%s %s %s -> %sms", rid, request.method, request.path, elapsed_ms)
    # IMPORTANT: Never add 'nonce-...' alongside 'unsafe-inline'.
    # The CSP spec says 'unsafe-inline' is ignored when a nonce
    # or hash is present in script-src.  Every HTML onclick/on
    # submit attribute handler would be silently blocked because
    # attribute handlers can't carry a nonce.
    script_src = (
        "'self' 'unsafe-inline' https://challenges.cloudflare.com https://checkout.razorpay.com "
        "https://www.gstatic.com https://www.google.com https://apis.google.com "
        "https://cdn.jsdelivr.net https://unpkg.com https://accounts.google.com"
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        f"default-src 'self'; "
        f"style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        f"font-src 'self' data: https://fonts.gstatic.com; "
        f"img-src 'self' data:; "
        f"script-src {script_src}; "
        f"frame-src https://challenges.cloudflare.com https://api.razorpay.com "
          "https://www.google.com https://accounts.google.com https://*.firebaseapp.com; "
          f"connect-src 'self' https://api.razorpay.com https://lumberjack.razorpay.com "
          "https://identitytoolkit.googleapis.com https://securetoken.googleapis.com "
          "https://www.googleapis.com https://accounts.google.com "
          "https://www.gstatic.com; "
        f"base-uri 'self'; "
        f"object-src 'none'; "
        f"report-uri /csp-report; "
        f"report-to csp-endpoint",
    )

    # HSTS — tell browsers to always use HTTPS for this site
    if request.is_secure:
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )

    # Long-cache static files — Flask defaults to no-cache which forces
    # re-downloading CSS/JS/fonts/images on every page load. Static files
    # served from /static/ are content-addressed by the browser via ETag,
    # so a 1-year cache with immutable is safe and massively cuts repeat
    # page-load time.
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"

    return response


@app.context_processor
def inject_globals():
    cart = session.get("cart", {})
    cart_count = sum(cart.values()) if cart else 0
    _settings = get_settings()

    # --- smart greeting ---
    now_utc = datetime.now(timezone.utc)
    tz_offset_sec = session.get("timezone_offset", 19800)  # default +5:30 IST in seconds
    local_hour = (now_utc.hour + tz_offset_sec // 3600) % 24
    tod = "evening"
    if local_hour < 12:
        tod = "morning"
    elif local_hour < 17:
        tod = "afternoon"

    cust_id = session.get("customer_id")
    cust_name = session.get("customer_name", "")
    is_first_visit = False
    order_count = 0
    pending_count = 0
    festival = None
    today_str = now_utc.strftime("%m%d")
    greeting_msg = None

    conn = None
    try:
        # Only hit the database when we actually need live stats.
        if session.get("admin_id") or cust_id:
            conn = db.get_db()
        # pending orders count is only relevant to logged-in admin sessions
        if conn is not None and session.get("admin_id"):
            pending_count = conn.execute(
                "SELECT COUNT(*) c FROM orders WHERE status = 'paid'"
            ).fetchone()["c"]
        # customer stats (if logged in)
        if conn is not None and cust_id:
            row = conn.execute(
                "SELECT last_login_at, (SELECT COUNT(*) FROM orders WHERE customer_id = ?) as oc FROM customers WHERE id = ?",
                (cust_id, cust_id)
            ).fetchone()
            if row:
                is_first_visit = row["last_login_at"] is None
                order_count = row["oc"]
        # seasonal greetings now come from cached settings, not direct DB reads
        admin_g = (_settings.get(f"greeting_{today_str}") or "").strip()
        if admin_g:
            festival = admin_g
            msg = (_settings.get(f"greeting_msg_{today_str}") or "").strip()
            if msg:
                greeting_msg = msg
        if not festival:
            cal_g = (_settings.get(f"holiday_{today_str}") or "").strip()
            if cal_g:
                festival = cal_g
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # Built-in fallback festival list
    if not festival:
        festive = {
            "0101": "New Year", "0126": "Republic Day", "0214": "Valentine's",
            "0310": "Holi", "0329": "Easter", "0414": "Baisakhi", "0501": "Labour Day",
            "0618": "Eid al-Adha", "0703": "Guru Purnima", "0815": "Independence Day",
            "0826": "Raksha Bandhan", "0831": "Janmashtami", "1002": "Gandhi Jayanti",
            "1007": "Dussehra", "1020": "Karwa Chauth", "1027": "Diwali", "1101": "Diwali",
            "1115": "Guru Nanak Jayanti", "1204": "Christmas", "1225": "Christmas",
        }
        festival = festive.get(today_str)

    greeting_data = {
        "timeOfDay": tod,
        "isNewUser": is_first_visit,
        "orderCount": order_count,
        "festival": festival,
        "msg": greeting_msg,
    }

    # Inject a helper for conditional nav in admin templates
    admin_perms = session.get("admin_permissions", [])
    admin_role = session.get("admin_role", "")
    def admin_can(perm):
        """Template helper: returns True if the logged-in admin has `perm`
        (or is master/admin.manage)."""
        if not admin_perms or not session.get("admin_id"):
            return False
        return "*" in admin_perms or perm in admin_perms

    return {
        "csrf_token": get_csrf_token,
        "cart_count": cart_count,
        "admin_permissions": admin_perms,
        "admin_role": admin_role,
        "admin_can": admin_can,
        "pending_count": pending_count,
        "turnstile_enabled": turnstile_enabled(),
        "turnstile_site_key": config.TURNSTILE_SITE_KEY,
        "firebase_auth_enabled": firebase_auth_enabled(),
        "gis_enabled": bool(config.GOOGLE_CLIENT_ID),
        "google_client_id": config.GOOGLE_CLIENT_ID,
        "firebase_config": {
            "apiKey": config.FIREBASE_API_KEY,
            "authDomain": config.FIREBASE_AUTH_DOMAIN,
            "projectId": config.FIREBASE_PROJECT_ID,
            "appId": config.FIREBASE_APP_ID,
            "messagingSenderId": config.FIREBASE_MESSAGING_SENDER_ID,
            "storageBucket": config.FIREBASE_STORAGE_BUCKET,
        },
        "current_customer_name": cust_name,
        "current_customer_phone": session.get("customer_phone", ""),
        "current_customer_email": session.get("customer_email", ""),
        "customer_logged_in": bool(cust_id),
        "otp_dev_mode": config.OTP_DEV_MODE,
        "settings": _settings,
        "test_checkout_mode": str(_settings.get("test_checkout_mode", "false")).lower() == "true",
        "checkout_available": (str(_settings.get("test_checkout_mode", "false")).lower() == "true") or rzp.is_configured(),
        "payment_gateway_ready": str(_settings.get("test_checkout_mode", "false")).lower() != "true" and rzp.is_configured(),
        "testing_mode_active": str(_settings.get("test_checkout_mode", "false")).lower() == "true",
        "razorpay_configured": rzp.is_configured(),
        "static_version": os.environ.get("STATIC_VERSION", "13"),
        "greeting_data": greeting_data,
        "config": config,
    }


def _is_coupon_active(coupon, now_str=None):
    """Check if a coupon is active, not expired, not started yet, and not
    exhausted by usage limits. Returns True if the coupon is usable right now."""
    if not coupon:
        return False
    if not coupon["active"]:
        return False
    if now_str is None:
        now_str = datetime.now(timezone.utc).isoformat()
    if coupon["starts_at"] and coupon["starts_at"] > now_str:
        return False
    if coupon["expires_at"] and coupon["expires_at"] < now_str:
        return False
    if coupon["usage_limit"] is not None and coupon["used_count"] >= coupon["usage_limit"]:
        return False
    return True


def _coupon_discount(coupon, base_price):
    """Calculate the discount amount for a coupon against a base price.
    Returns a non-negative int that never exceeds base_price - 1 (so the
    customer always pays at least ₹1)."""
    if coupon["discount_type"] == "percent":
        discount = int(round(base_price * coupon["discount_value"] / 100))
    else:
        discount = coupon["discount_value"]
    discount = max(0, min(discount, base_price - 1 if base_price > 0 else 0))
    return discount


def get_auto_coupons(conn, items, subtotal, product_id=None):
    """Return a list of auto-applicable coupons for the current cart/visitor.
    Checks trigger conditions (cart threshold, product-specific, customer
    segment, URL-driven) and skips expired/inactive coupons. If product_id
    is given (single-product page), checks against that product instead of
    the cart.

    Returns a list of coupon Row objects, sorted by discount (best first).
    """
    now_str = datetime.now(timezone.utc).isoformat()
    coupons = conn.execute("SELECT * FROM coupons WHERE active = 1").fetchall()
    customer_id = session.get("customer_id")
    is_logged_in = bool(customer_id)
    is_new_user = False
    if is_logged_in:
        order_count = conn.execute(
            "SELECT COUNT(*) c FROM orders WHERE customer_id = ? AND status IN ('paid','delivered')",
            (customer_id,)
        ).fetchone()["c"]
        is_new_user = order_count == 0

    # URL-driven coupon stored in session
    url_coupon_code = session.get("url_coupon_code", "")
    url_coupon = None
    if url_coupon_code:
        url_coupon = conn.execute(
            "SELECT * FROM coupons WHERE code = ? AND active = 1", (url_coupon_code.upper(),)
        ).fetchone()

    results = []
    cart_product_ids = set()
    if items:
        cart_product_ids = {it["product"]["id"] for it in items}

    for c in coupons:
        if not _is_coupon_active(c, now_str):
            continue

        trigger = c["trigger_type"]

        if trigger == "manual":
            # Manual coupons only apply if the user types the code — skip auto
            # unless it's the URL-driven one matching this code
            if url_coupon and url_coupon["id"] == c["id"]:
                results.append(c)
            continue

        if trigger == "url_driven":
            # URL-driven coupons only apply when the code was passed via URL
            # and stored in session
            if url_coupon and url_coupon["id"] == c["id"]:
                results.append(c)
            continue

        if trigger == "cart_threshold":
            if not items:
                continue
            min_val = c["min_cart_value"] or 0
            if subtotal >= min_val:
                results.append(c)
            continue

        if trigger == "product_specific":
            target_pid = c["target_product_id"]
            if not target_pid:
                continue
            if product_id is not None:
                if product_id == target_pid:
                    results.append(c)
            elif target_pid in cart_product_ids:
                results.append(c)
            continue

        if trigger == "customer_segment":
            segment = c["customer_segment"] or "all"
            if segment == "all":
                results.append(c)
            elif segment == "new_user" and is_new_user:
                results.append(c)
            elif segment == "logged_in" and is_logged_in:
                results.append(c)
            continue

    # Sort by best discount (descending). For cart, use subtotal; for single
    # product, use product price. The product price is fetched once so sorting
    # never issues one SQL query per coupon.
    product_base_price = 0
    if product_id is not None:
        p = conn.execute("SELECT price FROM products WHERE id = ?", (product_id,)).fetchone()
        product_base_price = p["price"] if p else 0

    def discount_amount(c):
        base = subtotal if items else product_base_price
        return _coupon_discount(c, base)

    results.sort(key=discount_amount, reverse=True)
    return results


def get_cart_items(conn):
    """Read the cart from the database (for logged-in users) or the session (for
    guests), look up live product data, and return a list of {product, quantity,
    line_total} plus the subtotal. Prices always come from the database, never the
    client, so a tampered cart can't change what's charged."""
    customer_id = session.get("customer_id")
    items = []
    subtotal = 0
    product_ids = []
    quantity_map = {}

    if customer_id:
        # Persistent cart for logged-in users
        rows = conn.execute(
            "SELECT ci.product_id, ci.quantity FROM cart_items ci "
            "JOIN products p ON p.id = ci.product_id AND p.active = 1 "
            "WHERE ci.customer_id = ?",
            (customer_id,),
        ).fetchall()
        for row in rows:
            qty = max(1, int(row["quantity"]))
            quantity_map[row["product_id"]] = qty
            product_ids.append(row["product_id"])
    else:
        # Session cart for guests
        cart = session.get("cart", {})
        for pid_str, qty in list(cart.items()):
            try:
                pid = int(pid_str)
                qty = max(1, int(qty))
            except (TypeError, ValueError):
                continue
            quantity_map[pid] = qty
            product_ids.append(pid)

    if not product_ids:
        return [], 0

    placeholders = ",".join("?" for _ in product_ids)
    rows = conn.execute(
        f"SELECT * FROM products WHERE id IN ({placeholders}) AND active = 1",
        tuple(product_ids),
    ).fetchall()
    products = {row["id"]: row for row in rows}

    for pid in product_ids:
        product = products.get(pid)
        qty = quantity_map.get(pid, 1)
        if not product:
            continue
        line_total = product["price"] * qty
        subtotal += line_total
        items.append({"product": product, "quantity": qty, "line_total": line_total})

    return items, subtotal


import time as _time

_table_columns_cache = {}


def _table_columns(table_name):
    """Return a cached set of column names for a table.

    This lets the app stay compatible with older Turso databases that may be
    missing newer columns added by later deployments.
    """
    cols = _table_columns_cache.get(table_name)
    if cols is not None:
        return cols
    conn = db.get_db()
    try:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        cols = {row["name"] for row in rows}
    except Exception:
        cols = set()
    # Don't close the connection — db.get_db() owns it (may be g-scoped).
    # Closing it poisons the cached proxy for the rest of the request.
    _table_columns_cache[table_name] = cols
    return cols


def _load_catalog():
    """Fetch the homepage catalog in one batch."""
    conn = None
    try:
        conn = db.get_db()
        section_cols = _table_columns("sections")
        section_select = (
            "SELECT id, title, content, style, visible, position FROM sections WHERE visible = 1 ORDER BY position ASC"
            if "style" in section_cols
            else "SELECT id, title, content, '' AS style, visible, position FROM sections WHERE visible = 1 ORDER BY position ASC"
        )
        sections = conn.execute(section_select).fetchall()
        products = conn.execute(
              """SELECT id, name, slug, category, price, compare_price,
                          short_description, created_at, views, ribbon, position, quantity,
                          delivery_content_type
                 FROM products WHERE active = 1 ORDER BY position ASC, id DESC"""
          ).fetchall()
        product_images = get_primary_image_map(conn, [p["id"] for p in products])
        categories = [
            r["category"] for r in conn.execute(
                "SELECT DISTINCT category FROM products WHERE active = 1 AND category != '' ORDER BY category ASC"
            ).fetchall()
        ]
        testimonials = conn.execute(
            "SELECT id, customer_name, quote, rating, visible, position FROM testimonials WHERE visible = 1 ORDER BY position ASC"
        ).fetchall()
        faqs = conn.execute(
            "SELECT id, question, answer, visible, position FROM faqs WHERE visible = 1 ORDER BY position ASC"
        ).fetchall()
        # Sold counts per product (from paid/delivered order_items)
        sold_counts = {}
        try:
            rows = conn.execute(
                """SELECT oi.product_id, SUM(oi.quantity) AS cnt
                   FROM order_items oi
                   JOIN orders o ON o.id = oi.order_id
                   WHERE o.status IN ('confirmed','delivered') AND o.paid_at IS NOT NULL
                   GROUP BY oi.product_id"""
            ).fetchall()
            for r in rows:
                sold_counts[r["product_id"]] = r["cnt"]
        except Exception as _exc:
            _startup_logger.warning("Sold-count query failed in _load_catalog: %s", _exc)
        return {
            "sections": sections,
            "products": products,
            "products_by_id": {p["id"]: p for p in products},
            "products_by_slug": {p["slug"]: p for p in products},
            "product_images": product_images,
            "categories": categories,
            "testimonials": testimonials,
            "faqs": faqs,
            "sold_counts": sold_counts,
        }
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception as _exc:
                _startup_logger.warning("Failed to close DB connection in _load_catalog: %s", _exc)


def get_catalog():
    global _catalog_cache, _catalog_cache_ts
    now = _time.time()
    if _catalog_cache is not None and (now - _catalog_cache_ts) < _CATALOG_TTL:
        return _catalog_cache
    _catalog_cache = _load_catalog()
    _catalog_cache_ts = now
    return _catalog_cache


def invalidate_catalog_cache():
    global _catalog_cache, _catalog_cache_ts
    _catalog_cache = None
    _catalog_cache_ts = 0


def invalidate_settings_cache():
    global _settings_cache, _settings_cache_ts
    _settings_cache = None
    _settings_cache_ts = 0


def invalidate_frontend_caches():
    """Invalidate all frontend-facing in-memory caches."""
    invalidate_catalog_cache()
    invalidate_settings_cache()


# ─── In-memory caches to avoid Turso HTTP round-trips ──────────────────────
# Turso adds 100-300ms per query. The homepage does 5+ queries per load.
# Caching eliminates all but the first load's DB latency.

_settings_cache = None
_settings_cache_ts = 0
_CATALOG_TTL = 21600  # seconds — 6 hours, same as settings TTL
_catalog_cache = None
_catalog_cache_ts = 0
# MAX_DOWNLOADS moved to config.py

_SETTINGS_TTL = 21600  # seconds — 6 hours, Turso remote (invalidated on admin edits)


def get_settings():
    global _settings_cache, _settings_cache_ts
    now = _time.time()
    if _settings_cache is not None and (now - _settings_cache_ts) < _SETTINGS_TTL:
        return dict(_settings_cache)
    settings = dict(db.DEFAULT_SETTINGS)
    conn = None
    try:
        conn = db.get_db()
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        for row in rows:
            settings[row["key"]] = row["value"]
    except Exception as exc:
        _startup_logger.warning("Settings load failed, using defaults: %s", exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    _settings_cache = dict(settings)
    _settings_cache_ts = now
    return settings


def is_testing_checkout():
    """Check if test checkout mode is active (true = simulate payment, no Razorpay)."""
    try:
        return str(get_settings().get("test_checkout_mode", "false")).lower() == "true"
    except Exception:
        return False


def checkout_enabled():
    try:
        return str(get_settings().get("disable_payments", "false")).lower() != "true"
    except Exception:
        return True


def _delivery_speed_stat(conn, product_id):
    """Compute historical delivery speed for a product from past orders.
    Returns a dict {hours, count} or None if insufficient data."""
    rows = conn.execute(
        """SELECT o.paid_at, o.delivered_at
           FROM orders o
           JOIN order_items oi ON oi.order_id = o.id
           WHERE oi.product_id = ? AND o.status = 'delivered'
             AND o.paid_at IS NOT NULL AND o.delivered_at IS NOT NULL
           ORDER BY o.delivered_at DESC
           LIMIT 20""",
        (product_id,),
    ).fetchall()
    if len(rows) < 3:
        return None
    from datetime import datetime, timezone
    total_hours = 0
    count = 0
    for r in rows:
        try:
            paid = datetime.fromisoformat(r["paid_at"])
            delivered = datetime.fromisoformat(r["delivered_at"])
            diff = (delivered - paid).total_seconds() / 3600
            total_hours += diff
            count += 1
        except Exception:
            continue
    if count < 3:
        return None
    avg_hours = round(total_hours / count, 1)
    return {"hours": avg_hours, "count": count}


@app.route("/", methods=["GET", "HEAD"])
def home():
    settings = get_settings()
    catalog = get_catalog()

    category = (request.args.get("category") or "").strip()
    query = (request.args.get("q") or "").strip()
    sort = request.args.get("sort", "")
    price_min = request.args.get("price_min", "")
    price_max = request.args.get("price_max", "")
    delivery_type = request.args.get("delivery_type", "")
    rating = request.args.get("rating", "")

    # Build active_filters dict for the template
    active_filters = {}
    if category:
        active_filters["category"] = category
    if query:
        active_filters["q"] = query
    if sort:
        active_filters["sort"] = sort
    if price_min:
        active_filters["price_min"] = price_min
    if price_max:
        active_filters["price_max"] = price_max
    if delivery_type:
        active_filters["delivery_type"] = delivery_type
    if rating:
        active_filters["rating"] = rating

    # ── Filter and sort in Python on cached data (zero Turso queries) ──
    products = list(catalog["products"])

    # ── Search filter ──
    if query:
        tokens = [t.lower() for t in query.split() if len(t) >= 2]
        if tokens:
            scored = []
            for p in products:
                name_lower = (p["name"] or "").lower()
                desc_lower = (p["short_description"] or "").lower()
                cat_lower = (p["category"] or "").lower()
                score = 0
                matched_all = True
                for t in tokens:
                    if t in name_lower:
                        score += 10
                    elif t in cat_lower:
                        score += 5
                    elif t in desc_lower:
                        score += 3
                    else:
                        matched_all = False
                        if not any(t in field for field in (name_lower, desc_lower, cat_lower)):
                            pass
                if matched_all or any(
                    any(t in field for field in (name_lower, desc_lower, cat_lower))
                    for t in tokens
                ):
                    scored.append((p, score))
            scored.sort(key=lambda x: (-x[1], x[0]["name"]))
            products = [p for p, _ in scored if _ > 0]

    # ── Category filter ──
    if category:
        products = [p for p in products if p["category"] == category]

    # ── Price range filter ──
    if price_min:
        try:
            min_val = int(price_min)
            products = [p for p in products if p["price"] >= min_val]
        except (ValueError, TypeError):
            pass
    if price_max:
        try:
            max_val = int(price_max)
            products = [p for p in products if p["price"] <= max_val]
        except (ValueError, TypeError):
            pass

    # ── Delivery type filter ──
    if delivery_type:
        dt_values = [d.strip() for d in delivery_type.split(",") if d.strip()]
        if dt_values:
            products = [p for p in products if p.get("delivery_content_type", "") in dt_values]

    # ── Rating filter (fetch average ratings for visible products) ──
    if rating:
        try:
            min_rating = int(rating)
            # Build a map of product_id -> avg_rating for products in the current set
            if products:
                conn = db.get_db()
                try:
                    pids = [p["id"] for p in products]
                    placeholders = ",".join("?" for _ in pids)
                    rows = conn.execute(
                        f"""SELECT product_id, ROUND(AVG(CAST(rating AS REAL)), 1) AS avg_rating
                             FROM reviews WHERE visible = 1 AND product_id IN ({placeholders})
                             GROUP BY product_id""",
                        pids,
                    ).fetchall()
                    avg_ratings = {r["product_id"]: r["avg_rating"] for r in rows}
                    products = [p for p in products if avg_ratings.get(p["id"], 0) >= min_rating]
                except Exception:
                    pass
                finally:
                    conn.close()
        except (ValueError, TypeError):
            pass

    # ── Compute min/max prices from all catalog products (for range inputs) ──
    all_products = catalog["products"]
    filter_price_min = min(p["price"] for p in all_products) if all_products else 0
    filter_price_max = max(p["price"] for p in all_products) if all_products else 0

    # ── Personalize for returning customers ──
    owned_ids = set()
    if not sort and not query and not category and not price_min and not price_max and not delivery_type and not rating and session.get("customer_id"):
        customer_id = session["customer_id"]
        conn = db.get_db()
        try:
            rows = conn.execute(
                "SELECT DISTINCT oi.product_id FROM orders o JOIN order_items oi ON oi.order_id = o.id WHERE o.customer_id = ? AND o.status = 'delivered'",
                (customer_id,),
            ).fetchall()
            owned_ids = {r["product_id"] for r in rows}
        except Exception:
            pass
        finally:
            conn.close()
    # For returning customers on the default view, promote unpurchased products first
    if owned_ids:
        unpurchased = [p for p in products if p["id"] not in owned_ids]
        purchased = [p for p in products if p["id"] in owned_ids]
        products = unpurchased + purchased

    # ── Sorting ──
    if sort == "price_low":
        products = sorted(products, key=lambda p: p["price"])
    elif sort == "price_high":
        products = sorted(products, key=lambda p: p["price"], reverse=True)
    elif sort == "newest":
        products = sorted(products, key=lambda p: p["id"], reverse=True)
    elif sort == "name":
        products = sorted(products, key=lambda p: p["name"].lower())
    elif sort == "popular":
        products = sorted(products, key=lambda p: p["views"] or 0, reverse=True)

    # Use the cached product_images map
    product_images = catalog["product_images"]

    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    new_product_ids = {p["id"] for p in products if p["created_at"] and p["created_at"] >= cutoff}

    return render_template(
        "index.html", settings=settings, sections=catalog["sections"],
        products=products, product_images=product_images,
        categories=catalog["categories"], active_category=category,
        testimonials=catalog["testimonials"], faqs=catalog["faqs"],
        search_query=query, new_product_ids=new_product_ids, sort=sort,
        sold_counts=catalog.get("sold_counts", {}),
        owned_ids=owned_ids,
        active_filters=active_filters,
        filter_price_min=filter_price_min,
        filter_price_max=filter_price_max,
    )


@app.route("/api/search")
def api_search():
    """Instant search for the nav search dropdown. Returns JSON."""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"results": []})
    catalog = get_catalog()
    tokens = [t.lower() for t in q.split() if len(t) >= 2]
    scored = []
    for p in catalog["products"]:
        name_lower = (p["name"] or "").lower()
        desc_lower = (p["short_description"] or "").lower()
        cat_lower = (p["category"] or "").lower()
        score = 0
        matched_any = False
        for t in tokens:
            if t in name_lower:
                score += 10
                matched_any = True
            elif t in cat_lower:
                score += 5
                matched_any = True
            elif t in desc_lower:
                score += 3
                matched_any = True
        if matched_any:
            scored.append((p, score))
    scored.sort(key=lambda x: (-x[1], x[0]["name"]))
    results = []
    for p, _ in scored[:8]:
        results.append({
            "name": p["name"],
            "slug": p["slug"],
            "category": p["category"],
            "price": p["price"],
            "image": catalog["product_images"].get(p["id"]),
        })
    return jsonify({"results": results})


@app.route("/api/product/<int:product_id>/quick-view")
def api_product_quick_view(product_id):
    """JSON data for the quick view modal."""
    catalog = get_catalog()
    p = catalog["products_by_id"].get(product_id)
    if not p:
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "id": p["id"],
        "name": p["name"],
        "slug": p["slug"],
        "price": p["price"],
        "compare_price": p["compare_price"],
        "short_description": p["short_description"],
        "category": p["category"],
        "image": catalog["product_images"].get(product_id),
    })


@app.route("/api/product/<int:product_id>/reviews")
def api_product_reviews(product_id):
    """JSON list of visible reviews for a product."""
    conn = db.get_db()
    rows = conn.execute(
        """SELECT id, customer_name, rating, title, body, verified, created_at
           FROM reviews WHERE product_id = ? AND visible = 1
           ORDER BY created_at DESC LIMIT 50""",
        (product_id,),
    ).fetchall()
    conn.close()
    reviews = []
    for r in rows:
        reviews.append({
            "id": r["id"],
            "customer_name": r["customer_name"],
            "rating": r["rating"],
            "title": r["title"],
            "body": r["body"],
            "verified": bool(r["verified"]),
            "created_at": r["created_at"],
        })
    return jsonify({"reviews": reviews})


@app.route("/api/product/<int:product_id>/reviews/create", methods=["POST"])
def api_product_reviews_create(product_id):
    """Submit a review for a product (must be from a customer who bought it)."""
    check_csrf_api()
    data = request.get_json(force=True, silent=True) or {}
    try:
        rating_val = int(data.get("rating", 5))
    except (ValueError, TypeError):
        return jsonify({"error": "Rating must be a number between 1 and 5."}), 400
    rating = max(1, min(5, rating_val))
    title = (data.get("title") or "").strip()
    body = (data.get("body") or "").strip()
    customer_id = session.get("customer_id")
    customer_name = (session.get("customer_name") or "").strip()
    customer_email = (session.get("customer_email") or "").strip()

    if not customer_id and not customer_name:
        return jsonify({"error": "Please sign in to leave a review."}), 403

    conn = db.get_db()
    # Check if user already reviewed this product
    existing = conn.execute(
        "SELECT id FROM reviews WHERE product_id = ? AND (customer_id = ? OR customer_name = ?) AND customer_name != ''",
        (product_id, customer_id, customer_name),
    ).fetchone()
    if existing:
        conn.close()
        return jsonify({"error": "You have already reviewed this product."}), 409

    # Verify purchase — check both direct product_id and order_items (for cart orders)
    order = conn.execute(
        """SELECT o.id FROM orders o
           LEFT JOIN order_items oi ON oi.order_id = o.id
           WHERE (o.product_id = ? OR oi.product_id = ?)
             AND o.status = 'paid'
             AND (o.customer_id = ? OR o.customer_email = ?)
           ORDER BY o.id DESC LIMIT 1""",
        (product_id, product_id, customer_id, customer_email),
    ).fetchone()
    verified = 1 if order else 0
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """INSERT INTO reviews (product_id, order_id, customer_id, customer_name, rating, title, body, verified, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (product_id, order["id"] if order else None, customer_id, customer_name, rating, title, body, verified, now),
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True, "verified": bool(verified)})


@app.route("/api/wishlist/add", methods=["POST"])
@customer_login_required
def api_wishlist_add():
    check_csrf_api()
    data = request.get_json(force=True, silent=True) or {}
    product_id = data.get("product_id")
    if not product_id:
        return jsonify({"error": "Missing product."}), 400
    conn = db.get_db()
    existing = conn.execute(
        "SELECT 1 FROM wishlist_items WHERE customer_id = ? AND product_id = ?",
        (session["customer_id"], product_id),
    ).fetchone()
    if existing:
        conn.close()
        return jsonify({"error": "Already in your wishlist."}), 409
    try:
        conn.execute(
            "INSERT INTO wishlist_items (customer_id, product_id, created_at) VALUES (?, ?, ?)",
            (session["customer_id"], product_id, db.now()),
        )
        conn.commit()
        return jsonify({"success": True, "message": "Added to wishlist!"})
    except Exception as exc:
        conn.close()
        app.logger.warning("Wishlist insert failed: %s", exc)
        return jsonify({"error": "Could not add to wishlist. Please try again."}), 500
    finally:
        conn.close()


@app.route("/api/wishlist/remove", methods=["POST"])
@customer_login_required
def api_wishlist_remove():
    check_csrf_api()
    data = request.get_json(force=True, silent=True) or {}
    product_id = data.get("product_id")
    if not product_id:
        return jsonify({"error": "Missing product."}), 400
    conn = db.get_db()
    conn.execute(
        "DELETE FROM wishlist_items WHERE customer_id = ? AND product_id = ?",
        (session["customer_id"], product_id),
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/wishlist/list")
@customer_login_required
def api_wishlist_list():
    conn = db.get_db()
    rows = conn.execute(
        """SELECT w.product_id, w.created_at, p.name, p.slug, p.price, p.compare_price
           FROM wishlist_items w JOIN products p ON p.id = w.product_id
           WHERE w.customer_id = ? AND p.active = 1
           ORDER BY w.created_at DESC""",
        (session["customer_id"],),
    ).fetchall()
    conn.close()
    catalog = get_catalog()
    results = []
    for r in rows:
        results.append({
            "product_id": r["product_id"],
            "name": r["name"],
            "slug": r["slug"],
            "price": r["price"],
            "compare_price": r["compare_price"],
            "image": catalog["product_images"].get(r["product_id"]),
        })
    return jsonify({"items": results})


@app.route("/api/product/<int:product_id>/view", methods=["POST"])
def api_product_view(product_id):
    """Record a product view after the page is already visible.

    This keeps the page render path fast and pushes the database write into a
    lightweight follow-up request instead of blocking the main HTML response.
    Rate-limited per IP: one view per product per 60s window.
    """
    if rate_limited(f"product-view-{product_id}", max_attempts=1, window_seconds=60):
        return ("", 204)
    conn = None
    try:
        conn = db.get_db()
        conn.execute("UPDATE products SET views = COALESCE(views, 0) + 1 WHERE id = ?", (product_id,))
        conn.commit()
    except Exception as exc:
        _startup_logger.warning("Product view tracking failed for %s: %s", product_id, exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return ("", 204)

@app.route("/product/<slug>")
def product_detail(slug):
    settings = get_settings()
    catalog = get_catalog()

    product = catalog["products_by_slug"].get(slug)
    if not product:
        abort(404)

    conn = db.get_db()
    detail = conn.execute(
        """SELECT id, name, slug, category, price, compare_price,
                  short_description, description, quantity,
                  delivery_mode, active, created_at, views, ribbon, position
           FROM products WHERE slug = ? AND active = 1 LIMIT 1""",
        (slug,),
    ).fetchone()
    if not detail:
        conn.close()
        abort(404)

    images = conn.execute(
        "SELECT filename FROM product_images WHERE product_id = ? ORDER BY position ASC",
        (detail["id"],),
    ).fetchall()

    related = []
    if detail["category"]:
        related = [
            p for p in catalog["products"]
            if p["category"] == detail["category"] and p["id"] != detail["id"]
        ][:4]
    if len(related) < 4:
        related_ids = {r["id"] for r in related}
        related += [
            p for p in catalog["products"]
            if p["id"] != detail["id"] and p["id"] not in related_ids
        ][:4 - len(related)]

    related_images = {r["id"]: catalog["product_images"].get(r["id"]) for r in related}

    delivery_speed = None
    if detail["delivery_mode"] == "manual":
        delivery_speed = _delivery_speed_stat(conn, detail["id"])

    conn.close()

    _track_product_view(detail["id"])

    return render_template(
        "product.html", settings=settings, product=detail,
        images=[i["filename"] for i in images],
        razorpay_key=config.RAZORPAY_KEY_ID,
        related=related, related_images=related_images,
        sold_counts=catalog.get("sold_counts", {}),
        delivery_speed=delivery_speed,
    )


@app.route("/api/product/<int:product_id>/stock-request", methods=["POST"])
def api_stock_request(product_id):
    """Customer requests notification when a sold-out product is back in stock."""
    if rate_limited("stock-request", max_attempts=5, window_seconds=300):
        return jsonify({"error": "Too many attempts. Please wait a few minutes."}), 429
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()

    if not email or "@" not in email:
        return jsonify({"error": "Please enter a valid email address."}), 400

    conn = db.get_db()
    product = conn.execute("SELECT id, quantity, name FROM products WHERE id = ? AND active = 1", (product_id,)).fetchone()
    if not product:
        conn.close()
        return jsonify({"error": "Product not found."}), 404
    if product["quantity"] is None or product["quantity"] > 0:
        conn.close()
        return jsonify({"error": "This product is in stock!"}), 400

    # Check for duplicate request from this email
    existing = conn.execute(
        "SELECT id FROM stock_requests WHERE product_id = ? AND customer_email = ? AND notified = 0",
        (product_id, email),
    ).fetchone()
    if existing:
        conn.close()
        return jsonify({"error": "You're already on the waitlist for this product!"}), 409

    conn.execute(
        "INSERT INTO stock_requests (product_id, customer_name, customer_email, customer_phone, created_at) VALUES (?, ?, ?, ?, ?)",
        (product_id, name, email, phone, db.now()),
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": f"We'll email {email} when '{product['name']}' is back in stock!"})


@app.route("/api/recent-purchases/<int:product_id>")
def api_recent_purchases(product_id):
    """Return recent confirmed/delivered order customer names for social proof."""
    conn = None
    try:
        conn = db.get_db()
        rows = conn.execute(
            """SELECT customer_name FROM orders
               WHERE product_id = ? AND status IN ('confirmed','delivered')
                 AND paid_at IS NOT NULL AND customer_name != ''
               ORDER BY paid_at DESC LIMIT 30""",
            (product_id,),
        ).fetchall()
        names = [r["customer_name"] for r in rows]
        return jsonify(names)
    except Exception:
        return jsonify([])
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@app.route("/api/products/recent")
def api_products_recent():
    """Return recently viewed product data (max 10)."""
    viewed_ids = session.get("recently_viewed", [])
    if not viewed_ids:
        return jsonify([])
    ids = []
    for vid in viewed_ids:
        try:
            ids.append(int(vid))
        except (ValueError, TypeError):
            continue
    if not ids:
        return jsonify([])
    placeholders = ",".join("?" for _ in ids)
    conn = db.get_db()
    rows = conn.execute(
        f"""SELECT id, name, slug, price, compare_price, quantity
            FROM products WHERE id IN ({placeholders}) AND active = 1""",
        tuple(ids),
    ).fetchall()
    product_map = {r["id"]: r for r in rows}
    image_map = get_primary_image_map(conn, ids)
    conn.close()
    catalog = get_catalog()
    cat_images = catalog.get("product_images", {})
    results = []
    for vid in ids:
        p = product_map.get(vid)
        if not p:
            continue
        raw_image = image_map.get(vid) or cat_images.get(vid)
        results.append({
            "id": p["id"],
            "name": p["name"],
            "slug": p["slug"],
            "price": p["price"],
            "compare_price": p["compare_price"],
            "image": _get_webp_path(raw_image) if raw_image else None,
        })
    return jsonify(results)


@app.route("/api/create-order", methods=["POST"])
def api_create_order():
    check_csrf_api()
    if not checkout_enabled():
        return jsonify({"error": "Checkout is temporarily disabled."}), 503
    if rate_limited("create-order", max_attempts=8, window_seconds=60):
        return jsonify({"error": "Too many attempts — please wait a minute and try again."}), 429

    data = request.get_json(force=True, silent=True) or {}
    product_id = data.get("product_id")
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    phone = (data.get("phone") or "").strip()
    coupon_code = (data.get("coupon_code") or "").strip().upper()

    if not all([product_id, name, email]):
        return jsonify({"error": "Please fill in your name and email."}), 400

    conn = db.get_db()
    product = conn.execute(
        "SELECT * FROM products WHERE id = ? AND active = 1", (product_id,)
    ).fetchone()
    if not product:
        conn.close()
        return jsonify({"error": "This product is not available."}), 404
    if product["quantity"] is not None and product["quantity"] <= 0:
        conn.close()
        return jsonify({"error": f"Sorry, \"{product['name']}\" is sold out."}), 410

    testing_mode = is_testing_checkout()
    if not testing_mode and not rzp.is_configured():
        conn.close()
        return jsonify({"error": "Payments are not configured yet. Please contact the site owner."}), 503

    final_amount = product["price"]
    discount_amount = 0
    applied_code = ""

    if coupon_code:
        coupon = conn.execute(
            "SELECT * FROM coupons WHERE code = ? AND active = 1", (coupon_code,)
        ).fetchone()
        if not coupon:
            conn.close()
            return jsonify({"error": "That coupon code isn't valid."}), 400

        # Check usage limit against actual completed uses
        actual_uses = conn.execute(
            "SELECT COUNT(*) AS cnt FROM coupon_usage WHERE coupon_id = ?", (coupon["id"],)
        ).fetchone()["cnt"]
        if coupon["usage_limit"] is not None and actual_uses >= coupon["usage_limit"]:
            conn.close()
            return jsonify({"error": "That coupon has already been fully redeemed."}), 400

        # Per-customer reuse check
        if coupon["max_per_customer"]:
            used = conn.execute(
                "SELECT COUNT(*) AS cnt FROM coupon_usage WHERE coupon_id = ? AND customer_email = ?",
                (coupon["id"], email),
            ).fetchone()
            if used and used["cnt"] >= coupon["max_per_customer"]:
                conn.close()
                return jsonify({"error": "You've already used this coupon."}), 400

        if coupon["discount_type"] == "percent":
            discount_amount = int(round(product["price"] * coupon["discount_value"] / 100))
        else:
            discount_amount = coupon["discount_value"]
        discount_amount = min(discount_amount, product["price"] - 1) if product["price"] > 0 else 0
        discount_amount = max(discount_amount, 0)
        final_amount = product["price"] - discount_amount
        applied_code = coupon["code"]

    order_ref = db.new_order_ref()
    payment_mode = "test" if testing_mode else "gateway"
    rzp_order = None
    if testing_mode:
        razorpay_order_id = None
    else:
        try:
            rzp_order = rzp.create_order(final_amount, receipt=order_ref)
        except Exception:
            conn.close()
            return jsonify({"error": "Could not start payment. Please try again."}), 502
        razorpay_order_id = rzp_order["id"]

    conn.execute(
        """INSERT INTO orders
           (order_ref, product_id, product_name, customer_name, customer_email,
            customer_phone, amount, coupon_code, discount_amount, razorpay_order_id,
            status, created_at, customer_id, payment_mode, paid_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (order_ref, product["id"], product["name"], name, email, phone,
         final_amount, applied_code, discount_amount, razorpay_order_id,
         'created', db.now(), session.get("customer_id"), payment_mode, None),
    )
    cur = conn.execute("SELECT * FROM orders WHERE order_ref = ?", (order_ref,))
    order = cur.fetchone()
    # Track abandoned cart contact info
    if "session_key" in session:
        track_cart_contact(session["session_key"], name, email, phone)
    conn.commit()

    if testing_mode:
        _confirm_order_payment(conn, order, [], payment_mode="test")
        return jsonify({
            "test_mode": True,
            "payment_mode": "test",
            "order_ref": order_ref,
            "product_name": product["name"],
            "customer_name": name,
            "customer_email": email,
            "customer_phone": phone,
            "redirect_url": url_for("track_order", order_ref=order_ref, email=email),
        })

    conn.close()

    return jsonify({
        "test_mode": False,
        "payment_mode": "gateway",
        "razorpay_order_id": rzp_order["id"],
        "razorpay_key": config.RAZORPAY_KEY_ID,
        "amount": rzp_order["amount"],
        "currency": rzp_order["currency"],
        "order_ref": order_ref,
        "product_name": product["name"],
        "customer_name": name,
        "customer_email": email,
        "customer_phone": phone,
    })


@app.route("/api/order/<order_id>/cancel-unpaid", methods=["POST"])
def api_cancel_unpaid_order(order_id):
    """Let a customer cancel their own unpaid order before payment."""
    check_csrf_api()
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    conn = db.get_db()
    order = conn.execute(
        "SELECT * FROM orders WHERE order_ref = ? AND lower(customer_email) = ? AND status = 'created'",
        (order_id.upper(), email),
    ).fetchone()
    if not order:
        conn.close()
        return jsonify({"error": "Unpaid order not found."}), 404
    conn.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (order["id"],))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "Your unpaid order has been cancelled."})


@app.route("/api/order/<order_id>/notes")
@login_required
def api_order_notes(order_id):
    """Return internal notes for an order (used by admin)."""
    conn = db.get_db()
    order = conn.execute("SELECT id FROM orders WHERE order_ref = ?", (order_id.upper(),)).fetchone()
    if not order:
        conn.close()
        return jsonify([])
    notes = conn.execute(
        "SELECT id, note, created_at FROM order_notes WHERE order_id = ? ORDER BY created_at DESC",
        (order["id"],),
    ).fetchall()
    conn.close()
    return jsonify([dict(n) for n in notes])


@app.route("/api/order/<order_id>/notes/add", methods=["POST"])
@login_required
def api_order_notes_add(order_id):
    check_csrf_api()
    data = request.get_json(force=True, silent=True) or {}
    note = (data.get("note") or "").strip()
    if not note:
        return jsonify({"error": "Note cannot be empty."}), 400
    conn = db.get_db()
    order = conn.execute("SELECT id FROM orders WHERE order_ref = ?", (order_id.upper(),)).fetchone()
    if not order:
        conn.close()
        return jsonify({"error": "Order not found."}), 404
    conn.execute(
        "INSERT INTO order_notes (order_id, admin_id, note, created_at) VALUES (?, ?, ?, ?)",
        (order["id"], session.get("admin_id"), note, db.now()),
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/cart/preview")
def api_cart_preview():
    """Return cart items for the nav hover preview dropdown."""
    conn = db.get_db()
    items, subtotal = get_cart_items(conn)
    conn.close()
    catalog = get_catalog()
    image_map = catalog["product_images"]
    result = []
    for it in items:
        raw_image = image_map.get(it["product"]["id"])
        result.append({
            "name": it["product"]["name"],
            "slug": it["product"]["slug"],
            "quantity": it["quantity"],
            "price": it["product"]["price"],
            "line_total": it["line_total"],
            "image": _get_webp_path(raw_image) if raw_image else None,
        })
    return jsonify({"items": result, "subtotal": subtotal, "count": sum(it["quantity"] for it in items)})


def _merge_guest_cart(conn, customer_id):
    """Merge the guest session cart into the user's DB-backed cart on login.
    Guest items that don't exist in the DB cart are inserted; guest items that
    already exist have their quantities added together."""
    cart = session.pop("cart", None)
    if not cart:
        return
    now_ts = db.now()
    for pid_str, qty in list(cart.items()):
        try:
            pid = int(pid_str)
            qty = max(1, int(qty))
        except (TypeError, ValueError):
            continue
        existing = conn.execute(
            "SELECT quantity FROM cart_items WHERE customer_id = ? AND product_id = ?",
            (customer_id, pid),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE cart_items SET quantity = MIN(quantity + ?, 99), updated_at = ? WHERE customer_id = ? AND product_id = ?",
                (qty, now_ts, customer_id, pid),
            )
        else:
            conn.execute(
                "INSERT INTO cart_items (customer_id, product_id, quantity, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (customer_id, pid, qty, now_ts, now_ts),
            )
    session.modified = True


@app.route("/api/auto-coupons")
def api_auto_coupons():
    """Return auto-applicable coupons for the current cart or a specific product.
    Query params: product_id (optional) — if given, checks against that product
    instead of the cart. Returns the best matching coupon(s) with discount info."""
    product_id = request.args.get("product_id", type=int)
    conn = db.get_db()
    if product_id:
        items = None
        subtotal = 0
    else:
        items, subtotal = get_cart_items(conn)

    auto_coupons = get_auto_coupons(conn, items, subtotal, product_id=product_id)

    results = []
    for c in auto_coupons:
        base = subtotal if items else 0
        if product_id and not items:
            p = conn.execute("SELECT price FROM products WHERE id = ?", (product_id,)).fetchone()
            base = p["price"] if p else 0
        discount = _coupon_discount(c, base)
        final = base - discount if base > 0 else 0
        results.append({
            "id": c["id"],
            "code": c["code"],
            "discount_type": c["discount_type"],
            "discount_value": c["discount_value"],
            "trigger_type": c["trigger_type"],
            "discount_amount": discount,
            "final_price": final,
            "auto_apply": bool(c["auto_apply"]),
            "description": _coupon_description(c),
        })
    conn.close()
    return jsonify({"coupons": results, "best": results[0] if results else None})


def _coupon_description(c):
    """Human-readable description of what a coupon does and how it triggers."""
    parts = []
    if c["discount_type"] == "percent":
        parts.append(f"{c['discount_value']}% off")
    else:
        parts.append(f"₹{c['discount_value']} off")

    trigger = c["trigger_type"]
    if trigger == "cart_threshold":
        parts.append(f"on orders over ₹{c['min_cart_value'] or 0}")
    elif trigger == "product_specific":
        parts.append("on this product")
    elif trigger == "customer_segment":
        seg = c["customer_segment"]
        if seg == "new_user":
            parts.append("for new customers")
        elif seg == "logged_in":
            parts.append("for signed-in customers")
    elif trigger == "url_driven":
        parts.append("from your referral link")

    return " ".join(parts)


@app.route("/cart")
def view_cart():
    conn = None
    try:
        conn = db.get_db()
        settings = get_settings()
        items, subtotal = get_cart_items(conn)
        product_images = get_primary_image_map(conn, [it["product"]["id"] for it in items])
        return render_template(
            "cart.html", settings=settings, items=items, subtotal=subtotal,
            product_images=product_images, razorpay_key=config.RAZORPAY_KEY_ID,
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@app.route("/cart/add", methods=["POST"])
def cart_add():
    check_csrf_api()
    if rate_limited("cart-add", max_attempts=40, window_seconds=60):
        return jsonify({"error": "Too many requests — please slow down."}), 429
    product_id = request.form.get("product_id") or (request.get_json(silent=True) or {}).get("product_id")
    try:
        qty = max(1, min(int(request.form.get("quantity", 1)), 99))
    except (TypeError, ValueError):
        qty = 1
    if not product_id:
        return jsonify({"error": "Missing product."}), 400

    conn = db.get_db()
    product = conn.execute("SELECT * FROM products WHERE id = ? AND active = 1", (product_id,)).fetchone()
    if not product:
        conn.close()
        return jsonify({"error": "This product is not available."}), 404
    if product["quantity"] is not None and product["quantity"] <= 0:
        conn.close()
        return jsonify({"error": f"Sorry, \"{product['name']}\" is sold out."}), 410

    customer_id = session.get("customer_id")
    if customer_id:
        # DB-backed cart for logged-in users
        existing = conn.execute(
            "SELECT quantity FROM cart_items WHERE customer_id = ? AND product_id = ?",
            (customer_id, product["id"]),
        ).fetchone()
        current_qty = existing["quantity"] if existing else 0

        if product["quantity"] is not None and current_qty + qty > product["quantity"]:
            qty = max(1, product["quantity"] - current_qty)
            if qty <= 0:
                conn.close()
                return jsonify({"error": f"Sorry, only {product['quantity']} of \"{product['name']}\" available."}), 410

        now_ts = db.now()
        if existing:
            conn.execute(
                "UPDATE cart_items SET quantity = quantity + ?, updated_at = ? WHERE customer_id = ? AND product_id = ?",
                (qty, now_ts, customer_id, product["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO cart_items (customer_id, product_id, quantity, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (customer_id, product["id"], qty, now_ts, now_ts),
            )
        conn.commit()
        cart_count = conn.execute(
            "SELECT COALESCE(SUM(quantity), 0) AS cnt FROM cart_items WHERE customer_id = ?",
            (customer_id,),
        ).fetchone()["cnt"]
        conn.close()
    else:
        # Session cart for guests
        conn.close()
        cart = session.get("cart", {})
        if product["quantity"] is not None:
            in_cart = cart.get(str(product["id"]), 0)
            if qty > product["quantity"]:
                qty = product["quantity"]
            if in_cart >= product["quantity"]:
                return jsonify({"error": f"Sorry, only {product['quantity']} of \"{product['name']}\" available."}), 410
        if len(cart) >= 50 and str(product["id"]) not in cart:
            return jsonify({"error": "Cart is full — please checkout or clear items before adding more."}), 400
        total_qty = sum(cart.values())
        if total_qty + qty > 500:
            return jsonify({"error": "Cart limit reached — please checkout before adding more."}), 400
        key = str(product["id"])
        cart[key] = cart.get(key, 0) + qty
        session["cart"] = cart
        session.modified = True
        cart_count = sum(cart.values())

    # Track abandoned cart
    if "session_key" not in session:
        session["session_key"] = os.urandom(16).hex()
    track_cart_add(session["session_key"], product["id"], product["name"], product["price"], qty)

    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"success": True, "cart_count": cart_count, "product_name": product["name"]})
    flash(f'Added "{product["name"]}" to your cart.', "success")
    return redirect(request.referrer or url_for("home"))


@app.route("/cart/update", methods=["POST"])
def cart_update():
    check_csrf()
    product_id = request.form.get("product_id", "")
    try:
        qty = int(request.form.get("quantity", 1))
    except (TypeError, ValueError):
        qty = 1
    customer_id = session.get("customer_id")
    if customer_id:
        # DB-backed cart for logged-in users
        if qty <= 0:
            conn = db.get_db()
            conn.execute("DELETE FROM cart_items WHERE customer_id = ? AND product_id = ?",
                         (customer_id, int(product_id)))
            conn.commit()
            conn.close()
        else:
            conn = db.get_db()
            p = conn.execute("SELECT quantity FROM products WHERE id = ? AND active = 1", (int(product_id),)).fetchone()
            if p and p["quantity"] is not None:
                qty = min(qty, p["quantity"])
            qty = min(qty, 99)
            now_ts = db.now()
            existing = conn.execute(
                "SELECT quantity FROM cart_items WHERE customer_id = ? AND product_id = ?",
                (customer_id, int(product_id)),
            ).fetchone()
            if existing:
                if qty <= 0:
                    conn.execute("DELETE FROM cart_items WHERE customer_id = ? AND product_id = ?",
                                 (customer_id, int(product_id)))
                else:
                    conn.execute("UPDATE cart_items SET quantity = ?, updated_at = ? WHERE customer_id = ? AND product_id = ?",
                                 (qty, now_ts, customer_id, int(product_id)))
            conn.commit()
            conn.close()
    else:
        # Session cart for guests
        product_id = str(product_id)
        cart = session.get("cart", {})
        if product_id in cart:
            if qty <= 0:
                del cart[product_id]
            else:
                # Cap at available stock
                conn = db.get_db()
                p = conn.execute("SELECT quantity FROM products WHERE id = ? AND active = 1", (int(product_id),)).fetchone()
                conn.close()
                if p and p["quantity"] is not None:
                    qty = min(qty, p["quantity"])
                cart[product_id] = min(qty, 99)
            session["cart"] = cart
            session.modified = True
    return redirect(url_for("view_cart"))


@app.route("/cart/remove/<int:product_id>", methods=["POST"])
def cart_remove(product_id):
    check_csrf()
    customer_id = session.get("customer_id")
    if customer_id:
        conn = db.get_db()
        conn.execute("DELETE FROM cart_items WHERE customer_id = ? AND product_id = ?",
                     (customer_id, product_id))
        conn.commit()
        conn.close()
    else:
        cart = session.get("cart", {})
        cart.pop(str(product_id), None)
        session["cart"] = cart
        session.modified = True
    flash("Item removed from cart.", "success")
    return redirect(url_for("view_cart"))


@app.route("/cart/clear", methods=["POST"])
def cart_clear():
    check_csrf()
    session["cart"] = {}
    session.modified = True
    return redirect(url_for("view_cart"))


@app.route("/api/cart/apply-coupon", methods=["POST"])
def api_cart_apply_coupon():
    check_csrf_api()
    if rate_limited("apply-coupon", max_attempts=15, window_seconds=60):
        return jsonify({"error": "Too many attempts — please wait a minute and try again."}), 429
    data = request.get_json(force=True, silent=True) or {}
    code = (data.get("code") or "").strip().upper()
    conn = db.get_db()
    items, subtotal = get_cart_items(conn)
    coupon = conn.execute("SELECT * FROM coupons WHERE code = ? AND active = 1", (code,)).fetchone()
    conn.close()
    if not items:
        return jsonify({"error": "Your cart is empty."}), 400
    if not coupon or not _is_coupon_active(coupon):
        return jsonify({"error": "That coupon code isn't valid or has expired."}), 400
    if coupon["usage_limit"] is not None and coupon["used_count"] >= coupon["usage_limit"]:
        return jsonify({"error": "That coupon has already been fully redeemed."}), 400

    if coupon["discount_type"] == "percent":
        discount = int(round(subtotal * coupon["discount_value"] / 100))
    else:
        discount = coupon["discount_value"]
    discount = max(0, min(discount, subtotal - 1 if subtotal > 0 else 0))
    final_total = subtotal - discount
    return jsonify({"success": True, "discount_amount": discount, "final_price": final_total, "code": coupon["code"]})


@app.route("/api/cart/create-order", methods=["POST"])
def api_cart_create_order():
    check_csrf_api()
    if not checkout_enabled():
        return jsonify({"error": "Checkout is temporarily disabled."}), 503
    if rate_limited("create-order", max_attempts=8, window_seconds=60):
        return jsonify({"error": "Too many attempts — please wait a minute and try again."}), 429
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    phone = (data.get("phone") or "").strip()
    coupon_code = (data.get("coupon_code") or "").strip().upper()

    if not all([name, email]):
        return jsonify({"error": "Please fill in your name and email."}), 400

    conn = db.get_db()
    items, subtotal = get_cart_items(conn)
    if not items:
        conn.close()
        return jsonify({"error": "Your cart is empty."}), 400

    # Verify stock for all cart items before creating order
    for it in items:
        p = it["product"]
        qty_needed = it["quantity"]
        if p["quantity"] is not None and p["quantity"] < qty_needed:
            conn.close()
            return jsonify({"error": f"Sorry, \"{p['name']}\" has only {p['quantity']} left in stock."}), 410

    testing_mode = is_testing_checkout()
    if not testing_mode and not rzp.is_configured():
        conn.close()
        return jsonify({"error": "Payments are not configured yet. Please contact the site owner."}), 503

    final_amount = subtotal
    discount_amount = 0
    applied_code = ""
    if coupon_code:
        coupon = conn.execute(
            "SELECT * FROM coupons WHERE code = ? AND active = 1", (coupon_code,)
        ).fetchone()
        if not coupon:
            conn.close()
            return jsonify({"error": "That coupon code isn't valid."}), 400

        # Check usage limit against actual completed uses
        actual_uses = conn.execute(
            "SELECT COUNT(*) AS cnt FROM coupon_usage WHERE coupon_id = ?", (coupon["id"],)
        ).fetchone()["cnt"]
        if coupon["usage_limit"] is not None and actual_uses >= coupon["usage_limit"]:
            conn.close()
            return jsonify({"error": "That coupon has already been fully redeemed."}), 400

        # Per-customer reuse check
        if coupon["max_per_customer"]:
            used = conn.execute(
                "SELECT COUNT(*) AS cnt FROM coupon_usage WHERE coupon_id = ? AND customer_email = ?",
                (coupon["id"], email),
            ).fetchone()
            if used and used["cnt"] >= coupon["max_per_customer"]:
                conn.close()
                return jsonify({"error": "You've already used this coupon."}), 400

        if coupon["discount_type"] == "percent":
            discount_amount = int(round(subtotal * coupon["discount_value"] / 100))
        else:
            discount_amount = coupon["discount_value"]
        discount_amount = max(0, min(discount_amount, subtotal - 1 if subtotal > 0 else 0))
        final_amount = subtotal - discount_amount
        applied_code = coupon["code"]

    order_ref = db.new_order_ref()
    rzp_order = None
    payment_mode = "test" if testing_mode else "gateway"
    if not testing_mode:
        try:
            rzp_order = rzp.create_order(final_amount, receipt=order_ref)
        except Exception:
            conn.close()
            return jsonify({"error": "Could not start payment. Please try again."}), 502

    item_count = sum(it["quantity"] for it in items)
    summary_name = items[0]["product"]["name"] if len(items) == 1 else f"{item_count} items ({len(items)} products)"

    cur = conn.execute(
        """INSERT INTO orders
           (order_ref, product_id, product_name, customer_name, customer_email,
            customer_phone, amount, coupon_code, discount_amount, razorpay_order_id,
            status, created_at, customer_id, payment_mode, paid_at)
           VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (order_ref, summary_name, name, email, phone,
         final_amount, applied_code, discount_amount, (rzp_order["id"] if rzp_order else None),
         'created', db.now(), session.get("customer_id"), payment_mode, None),
    )
    order_id = cur.lastrowid
    for it in items:
        conn.execute(
            """INSERT INTO order_items (order_id, product_id, product_name, unit_price, quantity, line_total)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (order_id, it["product"]["id"], it["product"]["name"], it["product"]["price"],
             it["quantity"], it["line_total"]),
        )
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    order_items = conn.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,)).fetchall()
    # Track abandoned cart contact info
    if "session_key" in session:
        track_cart_contact(session["session_key"], name, email, phone)
    conn.commit()

    if testing_mode:
        _confirm_order_payment(conn, order, order_items, payment_mode="test")
        return jsonify({
            "test_mode": True,
            "payment_mode": "test",
            "order_ref": order_ref,
            "product_name": summary_name,
            "customer_name": name,
            "customer_email": email,
            "customer_phone": phone,
            "redirect_url": url_for("track_order", order_ref=order_ref, email=email),
        })

    conn.close()

    return jsonify({
        "test_mode": False,
        "payment_mode": "gateway",
        "razorpay_order_id": rzp_order["id"],
        "razorpay_key": config.RAZORPAY_KEY_ID,
        "amount": rzp_order["amount"],
        "currency": rzp_order["currency"],
        "order_ref": order_ref,
        "product_name": summary_name,
        "customer_name": name,
        "customer_email": email,
        "customer_phone": phone,
    })


@app.route("/api/apply-coupon", methods=["POST"])
def api_apply_coupon():
    check_csrf_api()
    if rate_limited("apply-coupon", max_attempts=15, window_seconds=60):
        return jsonify({"error": "Too many attempts — please wait a minute and try again."}), 429
    data = request.get_json(force=True, silent=True) or {}
    code = (data.get("code") or "").strip().upper()
    product_id = data.get("product_id")
    conn = db.get_db()
    product = conn.execute("SELECT * FROM products WHERE id = ? AND active = 1", (product_id,)).fetchone()
    if not product:
        conn.close()
        return jsonify({"error": "Product not found."}), 404
    coupon = conn.execute("SELECT * FROM coupons WHERE code = ? AND active = 1", (code,)).fetchone()
    conn.close()
    if not coupon or not _is_coupon_active(coupon):
        return jsonify({"error": "That coupon code isn't valid or has expired."}), 400
    if coupon["usage_limit"] is not None and coupon["used_count"] >= coupon["usage_limit"]:
        return jsonify({"error": "That coupon has already been fully redeemed."}), 400

    if coupon["discount_type"] == "percent":
        discount = int(round(product["price"] * coupon["discount_value"] / 100))
    else:
        discount = coupon["discount_value"]
    discount = max(0, min(discount, product["price"] - 1 if product["price"] > 0 else 0))
    final_price = product["price"] - discount
    return jsonify({"success": True, "discount_amount": discount, "final_price": final_price, "code": coupon["code"]})


@app.route("/api/verify-payment", methods=["POST"])
def api_verify_payment():
    check_csrf_api()
    data = request.get_json(force=True, silent=True) or {}
    rzp_order_id = data.get("razorpay_order_id")
    rzp_payment_id = data.get("razorpay_payment_id")
    rzp_signature = data.get("razorpay_signature")

    if not all([rzp_order_id, rzp_payment_id, rzp_signature]):
        return jsonify({"error": "Missing payment details."}), 400

    conn = db.get_db()
    order = conn.execute(
        "SELECT * FROM orders WHERE razorpay_order_id = ?", (rzp_order_id,)
    ).fetchone()

    if not order:
        conn.close()
        return jsonify({"error": "Order not found."}), 404

    # Idempotency guard: if this order was already confirmed paid (or moved
    # further, e.g. delivered), don't re-run any of the side effects below —
    # a retried/replayed call just gets the same success response again,
    # without double-crediting coupon usage or re-sending the confirmation email.
    if order["status"] in ("paid", "delivered"):
        conn.close()
        return jsonify({"success": True, "order_ref": order["order_ref"]})

    valid = rzp.verify_payment_signature(rzp_order_id, rzp_payment_id, rzp_signature)

    if not valid:
        conn.execute(
            "UPDATE orders SET status = 'failed' WHERE id = ?", (order["id"],)
        )
        conn.commit()
        conn.close()
        return jsonify({"error": "Payment verification failed."}), 400

    order_items = conn.execute(
        "SELECT * FROM order_items WHERE order_id = ?", (order["id"],)
    ).fetchall()
    auto_message = _confirm_order_payment(
        conn,
        order,
        order_items,
        payment_mode="gateway",
        razorpay_payment_id=rzp_payment_id,
        razorpay_signature=rzp_signature,
    )

    return jsonify({"success": True, "order_ref": order["order_ref"], "auto_delivered": auto_message is not None})



def _confirm_order_payment(conn, order, order_items, *, payment_mode="gateway", razorpay_payment_id=None, razorpay_signature=None):
    """Mark an order as paid, update coupon usage, optionally auto-deliver, and
    trigger notifications. Returns the auto-delivery message when one is
    generated, otherwise None."""
    if not order:
        return None

    current_status = (order["status"] or "").lower()
    if current_status in {"paid", "delivered"}:
        return order["delivery_message"] if order["delivery_message"] else None

    coupon_code = (order["coupon_code"] or "").strip().upper()
    if coupon_code:
        try:
            # Atomically increment used_count only on confirmed payment
            conn.execute(
                "UPDATE coupons SET used_count = used_count + 1 WHERE code = ? AND (usage_limit IS NULL OR used_count < usage_limit)",
                (coupon_code,),
            )
            coupon = conn.execute("SELECT id FROM coupons WHERE code = ?", (coupon_code,)).fetchone()
            if coupon:
                conn.execute(
                    "INSERT INTO coupon_usage (coupon_id, order_id, customer_email, discount_amount, used_at) VALUES (?, ?, ?, ?, ?)",
                    (coupon["id"], order["id"], order["customer_email"], order["discount_amount"], db.now()),
                )
        except Exception:
            _startup_logger.exception("Coupon usage recording failed for order %s", order["order_ref"])

    auto_message = None
    auto_deliver_enabled = str(get_settings().get("auto_deliver_enabled", "true")).lower() != "false"
    if auto_deliver_enabled:
        auto_message = _maybe_auto_deliver(conn, order, order_items)

    paid_at = db.now()
    if auto_message is None:
        conn.execute(
            "UPDATE orders SET status = 'paid', paid_at = ?, payment_mode = ?, razorpay_payment_id = COALESCE(?, razorpay_payment_id), razorpay_signature = COALESCE(?, razorpay_signature) WHERE id = ?",
            (paid_at, payment_mode, razorpay_payment_id, razorpay_signature, order["id"]),
        )
    else:
        conn.execute(
            "UPDATE orders SET status = 'delivered', paid_at = COALESCE(paid_at, ?), payment_mode = ?, razorpay_payment_id = COALESCE(?, razorpay_payment_id), razorpay_signature = COALESCE(?, razorpay_signature) WHERE id = ?",
            (paid_at, payment_mode, razorpay_payment_id, razorpay_signature, order["id"]),
        )

    # Deduct stock for each item — atomic with built-in oversell guard
    for item in (order_items or []):
        if item.get("product_id"):
            conn.execute(
                "UPDATE products SET quantity = quantity - ? WHERE id = ? AND quantity >= ?",
                (item["quantity"], item["product_id"], item["quantity"]),
            )

    conn.commit()

    try:
        notify_admins_new_order(order["id"])
        webpush_notify_admins_new_order(order["id"])
    except Exception:
        pass

    if email_enabled():
        try:
            item_line = order["product_name"] if not order_items else ", ".join(
                f"{it['product_name']} x{it['quantity']}" for it in order_items
            )
            subject = f"Your order {order['order_ref']} is confirmed"
            body = (
                f"Hi {order['customer_name']},\n\n"
                f"We have received your order for \"{item_line}\".\n\n"
            )
            if auto_message:
                subject = f"Your order {order['order_ref']} has been delivered"
                body += f"Your download/details are ready below:\n\n{auto_message}\n\n"
            body += f"Order reference: {order['order_ref']}\n\nThank you for shopping with us."
            send_email(order["customer_email"], subject, body)
        except Exception:
            pass

    return auto_message


def _maybe_auto_deliver(conn, order, order_items):
    """If every product in this order has delivery_mode='automatic', marks
    the order delivered right away and returns the combined delivery
    message. Otherwise leaves the order at 'paid' for manual review and
    returns None. Must be called before conn.commit()/conn.close()."""
    if order_items:
        product_ids = [it["product_id"] for it in order_items if it["product_id"]]
        if len(product_ids) != len(order_items):
            return None  # a purchased product was later deleted — play it safe
        placeholders = ",".join("?" * len(product_ids))
        rows = conn.execute(
            f"SELECT id, name, delivery_mode, auto_delivery_content FROM products "
            f"WHERE id IN ({placeholders})",
            product_ids,
        ).fetchall()
        by_id = {r["id"]: r for r in rows}
        if len(by_id) != len(set(product_ids)):
            return None
        if not all(by_id[pid]["delivery_mode"] == "automatic" for pid in product_ids):
            return None
        parts = [
            f"{by_id[it['product_id']]['name']}:\n{(by_id[it['product_id']]['auto_delivery_content'] or '').strip()}"
            for it in order_items
            if (by_id[it["product_id"]]["auto_delivery_content"] or "").strip()
        ]
        message = "\n\n".join(parts).strip()
    else:
        if not order["product_id"]:
            return None
        product = conn.execute(
            "SELECT delivery_mode, auto_delivery_content FROM products WHERE id = ?",
            (order["product_id"],),
        ).fetchone()
        if not product or product["delivery_mode"] != "automatic":
            return None
        message = (product["auto_delivery_content"] or "").strip()

    conn.execute(
        "UPDATE orders SET status = 'delivered', delivery_message = ?, "
        "delivered_at = ?, auto_delivered = 1 WHERE id = ?",
        (message, db.now(), order["id"]),
    )
    return message


@app.route("/track", methods=["GET", "POST"])
def track_order():
    order = None
    searched = False
    prefill_ref = request.args.get("order_ref", "")
    prefill_email = request.args.get("email", "")

    if request.method == "POST":
        check_csrf()
        if rate_limited("track-order", max_attempts=10, window_seconds=60):
            settings = get_settings()
            return render_template(
                "track_order.html", settings=settings, order=None, searched=False,
                prefill_ref="", prefill_email="", order_items=[],
                product_map={}, delivery_content_type="instructions",
                error="Too many attempts — please wait a minute and try again.",
            )
        order_ref = (request.form.get("order_ref") or "").strip().upper()
        email = (request.form.get("email") or "").strip().lower()
        searched = True
    elif prefill_ref and prefill_email:
        order_ref = prefill_ref.strip().upper()
        email = prefill_email.strip().lower()
        searched = True
    else:
        order_ref = email = None

    if searched:
        conn = db.get_db()
        order = conn.execute(
            "SELECT * FROM orders WHERE order_ref = ? AND lower(customer_email) = ?",
            (order_ref, email),
        ).fetchone()
        order_items = []
        product_map = {}
        if order:
            order_items = conn.execute(
                "SELECT * FROM order_items WHERE order_id = ?", (order["id"],)
            ).fetchall()
            # Load delivery_content_type for each product in the order
            product_ids = list(set(it["product_id"] for it in order_items))
            if product_ids:
                placeholders = ",".join("?" for _ in product_ids)
                products_data = conn.execute(
                    f"SELECT id, name, delivery_content_type FROM products WHERE id IN ({placeholders})",
                    product_ids,
                ).fetchall()
                for p in products_data:
                    product_map[p["id"]] = {
                        "name": p["name"],
                        "delivery_content_type": p["delivery_content_type"],
                    }
        conn.close()
    else:
        order_items = []
        product_map = {}

    # Determine the primary delivery content type for this order
    delivery_content_type = "instructions"
    if order and order_items:
        types = set()
        for it in order_items:
            pinfo = product_map.get(it["product_id"], {})
            t = pinfo.get("delivery_content_type", "")
            if t:
                types.add(t)
        if len(types) == 1:
            delivery_content_type = types.pop()
        elif types:
            delivery_content_type = "|".join(sorted(types))

    settings = get_settings()
    return render_template(
        "track_order.html", settings=settings, order=order, searched=searched,
        prefill_ref=prefill_ref, prefill_email=prefill_email, order_items=order_items,
        product_map=product_map, delivery_content_type=delivery_content_type,
    )


@app.route("/track/<order_ref>/resend", methods=["POST"])
def track_order_resend(order_ref):
    """Self-serve route for customers to resend the delivery email."""
    check_csrf()
    if rate_limited("resend-delivery", max_attempts=3, window_seconds=300):
        flash("Too many attempts. Please try again in 5 minutes.", "error")
        return redirect(url_for("track_order"))

    email = (request.form.get("email") or "").strip().lower()
    if not email:
        flash("Email is required.", "error")
        return redirect(url_for("track_order"))

    conn = db.get_db()
    order = conn.execute(
        "SELECT * FROM orders WHERE order_ref = ? AND lower(customer_email) = ?",
        (order_ref.strip().upper(), email),
    ).fetchone()
    conn.close()

    if not order:
        flash("Order not found. Check the reference and email.", "error")
        return redirect(url_for("track_order"))

    if order["status"] != "delivered":
        flash("Delivery email can only be resent for delivered orders.", "error")
        return redirect(url_for("track_order"))

    if not order["delivery_message"]:
        flash("No delivery content available to resend for this order.", "error")
        return redirect(url_for("track_order"))

    try:
        settings = get_settings()
        site_name = settings.get("site_name", "Virtual Store")
        subject = f"Your order {order['order_ref']} delivery details — {site_name}"
        body = (
            f"Hi {order['customer_name']},\n\n"
            f"Here are the delivery details for your order {order['order_ref']}:\n\n"
            f"{order['delivery_message']}\n\n"
            f"If you have any questions, feel free to contact us.\n\n"
            f"Thank you for shopping with us!"
        )
        send_email(email, subject, body)
        flash("Delivery email has been resent. Check your inbox.", "success")
    except Exception:
        flash("Failed to resend delivery email. Please try again later.", "error")

    return redirect(url_for("track_order", order_ref=order["order_ref"], email=order["customer_email"]))


@app.route("/orders/<order_ref>/invoice")
def public_order_invoice(order_ref):
    """Generate and download a PDF invoice for a paid/delivered order
    — requires order_ref + email match (same model as track_order)."""
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        abort(404)
    conn = db.get_db()
    order = conn.execute(
        "SELECT * FROM orders WHERE order_ref = ? AND lower(customer_email) = ?",
        (order_ref.strip().upper(), email),
    ).fetchone()
    if not order or order["status"] in ("created", "cancelled"):
        conn.close()
        abort(404)
    order_items = conn.execute(
        "SELECT * FROM order_items WHERE order_id = ?", (order["id"],),
    ).fetchall()
    product_ids = {item["product_id"] for item in order_items}
    product_map = {}
    if product_ids:
        rows = conn.execute(
            f"SELECT id, name FROM products WHERE id IN ({','.join('?' for _ in product_ids)})",
            list(product_ids),
        ).fetchall()
        product_map = {r["id"]: dict(r) for r in rows}
    conn.close()
    pdf_bytes, filename = invoicing.generate_and_save_invoice(
        order, order_items, product_map, get_settings(),
    )
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ============================================================= LEGAL PAGES

@app.route("/terms")
def terms_of_service():
    """Terms of Service — rendered from a styled template, not a static file."""
    return render_template("terms_of_service.html", settings=get_settings())


@app.route("/privacy")
def privacy_policy():
    """Privacy Policy — rendered from a styled template, not a static file."""
    return render_template("privacy_policy.html", settings=get_settings())


@app.route("/refund-policy")
def refund_policy():
    """Refund Policy — rendered directly from the admin-configurable setting."""
    return render_template("refund_policy.html", settings=get_settings())


# ============================================================= CUSTOMER AUTH (self-contained OTP)

# ============================================================= CUSTOMER AUTH (self-contained OTP)

@app.route("/auth/send-otp", methods=["POST"])
def auth_send_otp():
    """Generate a 6-digit OTP, store it in the database with an expiry,
    and send it via Twilio SMS. Falls back to dev mode (code shown in UI)
    if Twilio credentials are not set."""
    check_csrf_api()
    if rate_limited("send-otp", max_attempts=5, window_seconds=60):
        return jsonify({"error": "Too many attempts. Please wait a minute and try again."}), 429

    data = request.get_json(force=True, silent=True) or {}
    phone = (data.get("phone") or "").strip()

    if not phone or not phone.startswith("+"):
        return jsonify({"error": "Please enter your phone number with the country code, e.g. +919876543210."}), 400
    if len(phone) < 8 or len(phone) > 16:
        return jsonify({"error": "That phone number doesn't look right. Please check and try again."}), 400

    code = generate_otp_code()
    conn = db.get_db()
    store_otp(conn, phone, code)
    conn.commit()
    conn.close()

    # --- Try Twilio first, fall back to dev mode ---
    twilio_enabled = bool(
        config.TWILIO_ACCOUNT_SID and
        config.TWILIO_AUTH_TOKEN and
        config.TWILIO_FROM_NUMBER
    )

    if twilio_enabled:
        try:
            from twilio.rest import Client as TwilioClient
            twilio = TwilioClient(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
            twilio.messages.create(
                body=f"Your verification code is {code}. It expires in {config.OTP_EXPIRY_MINUTES} minutes.",
                from_=config.TWILIO_FROM_NUMBER,
                to=phone,
            )
            return jsonify({"success": True, "message": "Code sent!"})
        except Exception as e:
            # Log but don't expose Twilio errors to the client
            app.logger.error(f"Twilio SMS error: {e}")
            return jsonify({"error": "Failed to send SMS. Please try again."}), 500

    # Dev mode fallback — never expose codes in production without Twilio
    response = {"success": True, "message": "Code sent!"}
    if config.OTP_DEV_MODE:
        response["dev_code"] = code
    return jsonify(response)


@app.route("/auth/verify-otp", methods=["POST"])
def auth_verify_otp():
    """Verify the OTP code. If valid, create or update the customer account
    and log them into a Flask session. If name/email are provided, they're
    saved with the account (new users must provide a name at least)."""
    check_csrf_api()
    if rate_limited("verify-otp", max_attempts=10, window_seconds=60):
        return jsonify({"error": "Too many attempts. Please wait a minute and try again."}), 429

    data = request.get_json(force=True, silent=True) or {}
    phone = (data.get("phone") or "").strip()
    code = (data.get("code") or "").strip()
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()

    if not phone or not code:
        return jsonify({"error": "Please enter the code we sent you."}), 400

    conn = db.get_db()
    valid, stored_name, stored_email = verify_otp_code(conn, phone, code)
    if not valid:
        conn.close()
        return jsonify({"error": "That code is wrong or expired. Please try again."}), 400

    # Use provided name/email, or fall back to what was stored with the OTP
    final_name = name or stored_name or ""
    final_email = email or stored_email or ""

    # Look up or create the customer by phone number
    customer = conn.execute("SELECT * FROM customers WHERE phone = ?", (phone,)).fetchone()
    if customer:
        new_name = final_name or customer["name"]
        new_email = final_email or customer["email"]
        conn.execute(
            "UPDATE customers SET name = ?, email = ?, last_login_at = ? WHERE id = ?",
            (new_name, new_email, db.now(), customer["id"]),
        )
        customer_id = customer["id"]
    else:
        cur = conn.execute(
            """INSERT INTO customers (phone, name, email, created_at, last_login_at)
               VALUES (?, ?, ?, ?, ?)""",
            (phone, final_name, final_email, db.now(), db.now()),
        )
        customer_id = cur.lastrowid
        new_name, new_email = final_name, final_email

    conn.commit()
    conn.close()

    session.permanent = True
    session["customer_id"] = customer_id
    session["customer_name"] = new_name
    session["customer_phone"] = phone
    session["customer_email"] = new_email
    # Store the current session token version for "Sign out everywhere" check
    try:
        ver_conn = db.get_db()
        ver_row = ver_conn.execute(
            "SELECT session_token_version FROM customers WHERE id = ?", (customer_id,)
        ).fetchone()
        if ver_row:
            session["session_token_version"] = ver_row["session_token_version"]
        ver_conn.close()
    except Exception:
        pass
    # Merge guest cart into persistent cart
    try:
        merge_conn = db.get_db()
        _merge_guest_cart(merge_conn, customer_id)
        merge_conn.commit()
        merge_conn.close()
    except Exception:
        pass
    return jsonify({"success": True, "name": new_name, "phone": phone, "email": new_email})


@app.route("/auth/phone/verify", methods=["POST"])
def auth_phone_verify():
    """Called from the browser right after Firebase confirms the SMS code
    OR after a successful Google Sign-In redirect/popup. The Firebase ID
    token is already server-verified proof of identity, so CSRF is not
    needed — an attacker who has a valid ID token already owns the session."""
    data = request.get_json(force=True, silent=True) or {}
    id_token = data.get("id_token", "")

    app.logger.info(
        "POST /auth/phone/verify: has_id_token=%s has_name=%s has_email=%s",
        bool(id_token),
        bool(data.get("name")),
        bool(data.get("email")),
    )

    # Skip CSRF when a Firebase ID token is present — it's the auth proof.
    # This avoids the "session expired" error after Google redirect flows
    # where the page reload may generate a new csrf_token.
    if id_token:
        pass  # Firebase ID token IS the auth — no CSRF needed
    else:
        check_csrf_api()

    if rate_limited("phone-verify", max_attempts=10, window_seconds=60):
        return jsonify({"error": "Too many attempts — please wait a minute and try again."}), 429
    if not firebase_auth_enabled():
        return jsonify({"error": "Phone sign-in isn't set up on this site yet."}), 503

    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()

    decoded = verify_firebase_id_token(id_token)
    if not decoded:
        return jsonify({"error": "We couldn't verify that code. Please try again."}), 400

    uid = decoded.get("uid") or decoded.get("sub")
    phone = decoded.get("phone_number", "")
    if not uid:
        return jsonify({"error": "We couldn't verify that code. Please try again."}), 400

    conn = db.get_db()
    # First try to find the customer by Firebase UID.
    customer = conn.execute(
        "SELECT * FROM customers WHERE firebase_uid = ?",
        (uid,),
    ).fetchone()

    # If not found, try matching by phone number.
    if not customer and phone:
        customer = conn.execute(
            "SELECT * FROM customers WHERE phone = ?",
            (phone,),
        ).fetchone()

    if customer:
        new_name = name or customer["name"]
        new_email = email or customer["email"]

        conn.execute(
            """
            UPDATE customers
            SET firebase_uid = ?, name = ?, email = ?, phone = ?, last_login_at = ?
            WHERE id = ?
            """,
            (
                uid,
                new_name,
                new_email,
                phone,
                db.now(),
                customer["id"],
            ),
        )

        customer_id = customer["id"]

    else:
        try:
            cur = conn.execute(
                """
                INSERT INTO customers
                (firebase_uid, phone, name, email, created_at, last_login_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    uid,
                    phone,
                    name,
                    email,
                    db.now(),
                    db.now(),
                ),
            )
            customer_id = cur.lastrowid
        except Exception as e:
            if "UNIQUE constraint failed: customers.phone" in str(e):
                # Phone already exists (race condition) — fetch and update that record.
                customer = conn.execute(
                    "SELECT * FROM customers WHERE phone = ?",
                    (phone,),
                ).fetchone()
                new_name = name or customer["name"]
                new_email = email or customer["email"]
                conn.execute(
                    """
                    UPDATE customers
                    SET firebase_uid = ?, name = ?, email = ?, last_login_at = ?
                    WHERE id = ?
                    """,
                    (uid, new_name, new_email, db.now(), customer["id"]),
                )
                customer_id = customer["id"]
            else:
                raise
        new_name = name
        new_email = email

    conn.commit()
    conn.close()

    session.permanent = True
    session["customer_id"] = customer_id
    session["customer_name"] = new_name
    session["customer_phone"] = phone
    session["customer_email"] = new_email
    # Store the current session token version for "Sign out everywhere" check
    try:
        ver_conn = db.get_db()
        ver_row = ver_conn.execute(
            "SELECT session_token_version FROM customers WHERE id = ?", (customer_id,)
        ).fetchone()
        if ver_row:
            session["session_token_version"] = ver_row["session_token_version"]
        ver_conn.close()
    except Exception:
        pass
    # Merge guest cart into persistent cart
    try:
        merge_conn = db.get_db()
        _merge_guest_cart(merge_conn, customer_id)
        merge_conn.commit()
        merge_conn.close()
    except Exception:
        pass
    return jsonify({"success": True, "name": new_name, "phone": phone, "email": new_email})


@app.route("/auth/update-profile", methods=["POST"])
def auth_update_profile():
    """Update the logged-in customer's name and email. Supports both
    JSON (from auth flow) and form POST (from account hub)."""
    check_csrf_api()
    if not session.get("customer_id"):
        flash("Please sign in first.", "error")
        return redirect(url_for("home"))
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or request.form.get("name") or "").strip()
    email = (data.get("email") or request.form.get("email") or "").strip()
    if not name:
        if request.is_json:
            return jsonify({"error": "Please enter your name."}), 400
        flash("Please enter your name.", "error")
        return redirect(url_for("account_hub"))
    conn = db.get_db()
    conn.execute(
        "UPDATE customers SET name = ?, email = ? WHERE id = ?",
        (name, email, session["customer_id"]),
    )
    conn.commit()
    conn.close()
    session["customer_name"] = name
    session["customer_email"] = email
    if request.is_json:
        return jsonify({"success": True, "name": name, "phone": session.get("customer_phone", ""), "email": email})
    flash("Profile updated.", "success")
    return redirect(url_for("account_hub"))


# ─── Direct Google OAuth (via GIS, no Firebase) ──────────────────────────

@app.route("/auth/google", methods=["POST"])
def auth_google():
    """Verify a Google ID token from the GIS one-tap / button flow and
    create or update a customer session. No Firebase SDK involved — the
    token is verified directly against Google's OAuth2 certs."""
    if rate_limited("auth-google", max_attempts=10, window_seconds=60):
        return jsonify({"error": "Too many attempts — please wait a minute and try again."}), 429
    if not config.GOOGLE_CLIENT_ID:
        return jsonify({"error": "Google sign-in is not set up on this site."}), 503
    check_csrf_api()

    data = request.get_json(force=True, silent=True) or {}
    credential = data.get("credential", "")
    if not credential:
        return jsonify({"error": "Missing credential."}), 400

    # Verify the JWT directly using google-auth library
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token as google_id_token
        req = google_requests.Request()
        decoded = google_id_token.verify_oauth2_token(
            credential, req, audience=config.GOOGLE_CLIENT_ID
        )
    except Exception as exc:
        _startup_logger.warning("Google OAuth token verification failed: %s", exc)
        return jsonify({"error": "We couldn't verify your Google sign-in. Please try again."}), 400

    google_uid = decoded.get("sub", "")
    name = decoded.get("name", "")
    email = decoded.get("email", "") if decoded.get("email_verified") else ""
    if not google_uid:
        return jsonify({"error": "We couldn't verify your Google sign-in. Please try again."}), 400

    conn = db.get_db()
    # First: look up by google_uid (fast match for returning users)
    customer = conn.execute(
        "SELECT * FROM customers WHERE google_uid = ?", (google_uid,)
    ).fetchone()

    # Second: try matching by email if not found by google_uid
    if not customer and email:
        customer = conn.execute(
            "SELECT * FROM customers WHERE email = ? AND email != ''",
            (email,),
        ).fetchone()

    if customer:
        # Update existing customer record with Google info
        new_name = name or customer["name"]
        new_email = email or customer["email"]
        conn.execute(
            """UPDATE customers
               SET google_uid = ?, name = ?, email = ?, last_login_at = ?
               WHERE id = ?""",
            (google_uid, new_name, new_email, db.now(), customer["id"]),
        )
        customer_id = customer["id"]
    else:
        # Create new customer from Google identity
        # Use a unique placeholder phone to avoid UNIQUE constraint collisions
        # with other Google-only users who don't have a real phone yet.
        unique_phone = f"g_{google_uid[:18]}"
        try:
            cur = conn.execute(
                """INSERT INTO customers
                   (google_uid, phone, name, email, created_at, last_login_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (google_uid, unique_phone, name, email, db.now(), db.now()),
            )
            customer_id = cur.lastrowid
        except Exception as e:
            if "UNIQUE constraint failed: customers.phone" in str(e):
                # Race — soft retry lookup + update
                customer = conn.execute(
                    "SELECT * FROM customers WHERE phone = ?", (unique_phone,)
                ).fetchone()
            else:
                raise
            if customer:
                new_name = name or customer["name"]
                new_email = email or customer["email"]
                conn.execute(
                    """UPDATE customers
                       SET google_uid = ?, name = ?, email = ?, last_login_at = ?
                       WHERE id = ?""",
                    (google_uid, new_name, new_email, db.now(), customer["id"]),
                )
                customer_id = customer["id"]
            else:
                conn.close()
                raise
        new_name = name
        new_email = email

    conn.commit()
    conn.close()

    session.permanent = True
    session["customer_id"] = customer_id
    session["customer_name"] = new_name or ""
    session["customer_phone"] = ""
    session["customer_email"] = new_email or ""
    # Store the current session token version for "Sign out everywhere" check
    try:
        ver_conn = db.get_db()
        ver_row = ver_conn.execute(
            "SELECT session_token_version FROM customers WHERE id = ?", (customer_id,)
        ).fetchone()
        if ver_row:
            session["session_token_version"] = ver_row["session_token_version"]
        ver_conn.close()
    except Exception:
        pass
    # Merge guest cart into persistent cart
    try:
        merge_conn = db.get_db()
        _merge_guest_cart(merge_conn, customer_id)
        merge_conn.commit()
        merge_conn.close()
    except Exception:
        pass
    return jsonify({"success": True, "name": new_name or "", "email": new_email or ""})


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    check_csrf()
    for key in ("customer_id", "customer_name", "customer_phone", "customer_email"):
        session.pop(key, None)
    flash("Signed out.", "success")
    return redirect(request.referrer or url_for("home"))


@app.route("/account/signout-everywhere", methods=["POST"])
@customer_login_required
def account_signout_everywhere():
    """Invalidate all active sessions for the current customer by incrementing
    the session_token_version column. This revokes every session cookie that
    carries an older version number — on any browser, any device."""
    check_csrf()
    customer_id = session.get("customer_id")
    conn = db.get_db()
    try:
        conn.execute(
            "UPDATE customers SET session_token_version = session_token_version + 1 WHERE id = ?",
            (customer_id,),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()
    # Clear the current session so this browser is logged out too
    for key in ("customer_id", "customer_name", "customer_phone", "customer_email", "session_token_version"):
        session.pop(key, None)
    flash("You've been signed out of all devices.", "success")
    return redirect(url_for("home"))


@app.route("/auth/delete-account", methods=["POST"])
@customer_login_required
def auth_delete_account():
    """Permanently delete the customer account and anonymise orders."""
    check_csrf()
    customer_id = session.get("customer_id")
    conn = db.get_db()
    # Anonymise orders
    conn.execute(
        "UPDATE orders SET customer_id = NULL, customer_email = 'deleted@anon', customer_name = 'Deleted Account' WHERE customer_id = ?",
        (customer_id,),
    )
    # Delete customer
    conn.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
    conn.commit()
    conn.close()
    for key in ("customer_id", "customer_name", "customer_phone", "customer_email"):
        session.pop(key, None)
    flash("Your account has been deleted.", "success")
    return redirect(url_for("home"))


@app.route("/account/wishlist")
@customer_login_required
def account_wishlist():
    """Page showing all wishlisted items."""
    conn = db.get_db()
    rows = conn.execute(
        """SELECT w.product_id, w.created_at, p.name, p.slug, p.price, p.compare_price, p.quantity
           FROM wishlist_items w JOIN products p ON p.id = w.product_id
           WHERE w.customer_id = ? AND p.active = 1
           ORDER BY w.created_at DESC""",
        (session["customer_id"],),
    ).fetchall()
    conn.close()
    catalog = get_catalog()
    product_images = catalog["product_images"]
    return render_template(
        "account_wishlist.html",
        settings=get_settings(),
        items=rows,
        product_images=product_images,
    )


@app.route("/account")
@customer_login_required
def account_hub():
    """Account hub: orders, library, change username, logout."""
    conn = db.get_db()
    customer_id = session.get("customer_id")
    customer = conn.execute(
        "SELECT name, phone FROM customers WHERE id = ?", (customer_id,)
    ).fetchone()
    return render_template("account_hub.html", customer=customer)


@app.route("/account/library")
@customer_login_required
def account_library():
    """Show every digital item a customer has ever purchased, grouped by product.
    Includes both delivered and paid items — a purchase is yours the moment
    payment clears, not just after the admin clicks "deliver"."""
    conn = db.get_db()
    customer_id = session.get("customer_id")
    orders = conn.execute(
        """SELECT o.id, o.order_ref, o.delivery_message, o.status, o.created_at, o.delivered_at,
                  oi.product_id, oi.product_name
           FROM orders o
           JOIN order_items oi ON oi.order_id = o.id
           WHERE o.customer_id = ? AND o.status IN ('paid', 'delivered')
           ORDER BY COALESCE(o.delivered_at, o.created_at) DESC""",
        (customer_id,),
    ).fetchall()
    # Load product info + files for each unique product
    product_ids = list(set(o["product_id"] for o in orders))
    products = {}
    product_files = {}
    if product_ids:
        placeholders = ",".join("?" for _ in product_ids)
        products_data = conn.execute(
            f"SELECT id, name, slug, delivery_content_type, delivery_mode FROM products WHERE id IN ({placeholders})",
            product_ids,
        ).fetchall()
        for p in products_data:
            products[p["id"]] = dict(p)
            files = conn.execute(
                "SELECT id, original_name, file_size, version, created_at FROM product_files WHERE product_id = ? ORDER BY created_at DESC",
                (p["id"],),
            ).fetchall()
            product_files[p["id"]] = [dict(f) for f in files]
    conn.close()
    return render_template(
        "account_library.html",
        settings=get_settings(),
        orders=orders,
        products=products,
        product_files=product_files,
    )


@app.route("/account/orders")
@customer_login_required
def account_orders():
    conn = db.get_db()
    try:
        orders = conn.execute(
            "SELECT * FROM orders WHERE customer_id = ? ORDER BY id DESC",
            (session.get("customer_id"),),
        ).fetchall()
    except Exception as exc:
        _dblog = getattr(db, "_db_logger", None)
        if _dblog:
            _dblog.warning(
                "customer_id query failed (migration missing?): %s",
                exc,
            )
        orders = conn.execute(
            "SELECT * FROM orders ORDER BY id DESC"
        ).fetchall()
    finally:
        conn.close()

    return render_template(
        "account_orders.html",
        settings=get_settings(),
        orders=orders,
    )


@app.route("/download/<token>")
def download_product(token):
    """Secure download link — validates the token, serves the file, marks
    it used. Tokens expire after 72 hours."""
    conn = db.get_db()
    row = conn.execute(
        "SELECT * FROM download_tokens WHERE token = ?", (token,)
    ).fetchone()
    if not row:
        conn.close()
        return abort(404)
    from datetime import datetime, timezone
    expires = datetime.fromisoformat(row["expires_at"])
    if expires < datetime.now(timezone.utc):
        conn.close()
        return abort(410)
    # Multi-download: decrement remaining count instead of one-shot used flag.
    # Existing tokens (migrated with downloads_remaining=1) get one download,
    # new tokens get MAX_DOWNLOADS (5) re-downloads within the expiry window.
    remaining = row["downloads_remaining"]
    if remaining <= 0:
        conn.close()
        return abort(410)
    conn.execute(
        "UPDATE download_tokens SET downloads_remaining = downloads_remaining - 1 WHERE id = ?",
        (row["id"],),
    )
    conn.commit()
    file_path = os.path.join(os.getcwd(), row["file_path"])
    if not os.path.exists(file_path):
        conn.close()
        return abort(404)
    conn.close()
    return send_file(file_path, as_attachment=True, download_name=row["filename"])


@app.route("/api/newsletter/subscribe", methods=["POST"])
def api_newsletter_subscribe():
    """JSON newsletter subscribe endpoint for AJAX."""
    if rate_limited("newsletter", max_attempts=5, window_seconds=60):
        return jsonify({"error": "Too many attempts."}), 429
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"error": "Please enter a valid email."}), 400
    conn = db.get_db()
    try:
        conn.execute(
            "INSERT INTO newsletter_subscribers (email, created_at) VALUES (?, ?)",
            (email, db.now()),
        )
        conn.commit()
        # Send welcome email with unsubscribe link
        if email_enabled():
            try:
                site_name = get_settings().get("site_name", "Virtual Store")
                unsub_url = url_for("newsletter_unsubscribe", email=email, _external=True)
                send_email(
                    email,
                    f"You're subscribed to {site_name}",
                    f"Thanks for subscribing to {site_name}!\n\nYou'll be the first to know about new products and offers.\n\nTo unsubscribe at any time: {unsub_url}",
                )
            except Exception:
                pass
        conn.close()
        return jsonify({"success": True})
    except Exception:
        conn.close()
        return jsonify({"error": "Already subscribed."}), 409


@app.route("/newsletter/unsubscribe", methods=["GET"])
def newsletter_unsubscribe():
    """One-click unsubscribe via email query param."""
    email = (request.args.get("email") or "").strip().lower()
    if email and "@" in email:
        conn = db.get_db()
        conn.execute("DELETE FROM newsletter_subscribers WHERE email = ?", (email,))
        conn.commit()
        conn.close()
    return render_template("unsubscribed.html", settings=get_settings())


@app.route("/newsletter/subscribe", methods=["POST"])
def newsletter_subscribe():
    check_csrf()
    if rate_limited("newsletter", max_attempts=5, window_seconds=60):
        flash("Too many attempts — please wait a minute and try again.", "error")
        return redirect(url_for("home") + "#newsletter")
    if turnstile_enabled() and not verify_turnstile(request.form.get("cf-turnstile-response", "")):
        flash("Please complete the verification and try again.", "error")
        return redirect(url_for("home") + "#newsletter")
    email = (request.form.get("email") or "").strip().lower()
    if not email or "@" not in email:
        flash("Please enter a valid email address.", "error")
        return redirect(url_for("home") + "#newsletter")
    conn = db.get_db()
    try:
        conn.execute(
            "INSERT INTO newsletter_subscribers (email, created_at) VALUES (?, ?)",
            (email, db.now()),
        )
        conn.commit()
        flash("You're subscribed! We'll keep you posted.", "success")
    except Exception:
        flash("You're already on the list — thank you!", "success")
    conn.close()
    return redirect(url_for("home") + "#newsletter")


@app.route("/robots.txt")
def robots():
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /admin/",
        # /instance/ isn't Flask-served (no real exposure), but disallowing
        # it explicitly is defense-in-depth since INITIAL_ADMIN_PASSWORD.txt
        # briefly lives there on first boot.
        "Disallow: /instance/",
        f"Sitemap: {url_for('sitemap', _external=True)}",
    ]
    return "\n".join(lines), 200, {"Content-Type": "text/plain"}


@app.route("/favicon.ico")
def favicon():
    # Only favicon.svg exists in the repo — serve it for .ico requests too
    # so older browsers/crawlers get a real icon instead of an empty 204.
    return app.send_static_file("favicon.svg")


@app.route("/uploads/")
def uploads_root():
    """Return the canonical uploads base URL used by templates and JS."""
    return "", 204


@app.route("/uploads/<path:filename>")
@app.route("/static/uploads/<path:filename>")
def uploaded_file(filename):
    """Serve uploaded images/files safely, falling back to a tiny placeholder
    SVG when a legacy reference points at a file that no longer exists.
    This avoids noisy 404s for old browser or DB references."""
    filename = (filename or "").strip()
    if not filename:
        return _missing_upload_placeholder()
    safe_path = os.path.join(config.UPLOAD_FOLDER, filename)
    if os.path.exists(safe_path):
        return send_from_directory(config.UPLOAD_FOLDER, filename)
    return _missing_upload_placeholder()


def _missing_upload_placeholder():
    svg = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 400 500' role='img' aria-label='Image unavailable'>
  <rect width='400' height='500' fill='#111827'/>
  <rect x='28' y='28' width='344' height='444' rx='28' fill='#1f2937' stroke='#374151'/>
  <circle cx='200' cy='190' r='46' fill='#374151'/>
  <path d='M110 370l64-78 46 50 26-30 54 58H110z' fill='#374151'/>
  <text x='200' y='420' text-anchor='middle' fill='#9ca3af' font-family='Arial, sans-serif' font-size='22'>Image unavailable</text>
</svg>"""
    return Response(svg, mimetype="image/svg+xml")


@app.route("/sitemap.xml")
def sitemap():
    catalog = get_catalog()
    urls = [url_for("home", _external=True), url_for("track_order", _external=True),
            url_for("terms_of_service", _external=True), url_for("privacy_policy", _external=True)]
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in urls:
        xml.append(f"<url><loc>{u}</loc><lastmod>{datetime.now(timezone.utc).date()}</lastmod><changefreq>weekly</changefreq><priority>0.8</priority></url>")
    for p in catalog["products"]:
        loc = url_for("product_detail", slug=p["slug"], _external=True)
        lastmod = (p["created_at"][:10] if p["created_at"] else datetime.now(timezone.utc).date())
        xml.append(f"<url><loc>{loc}</loc><lastmod>{lastmod}</lastmod><changefreq>weekly</changefreq><priority>0.6</priority></url>")
    xml.append("</urlset>")
    return "\n".join(xml), 200, {"Content-Type": "application/xml"}


@app.errorhandler(404)
def not_found(e):
    # Never touch the database here — missing assets can trigger 404s and we
    # do not want a broken image path to cascade into a Turso lookup.
    return render_template("404.html", settings=db.DEFAULT_SETTINGS), 404


@app.errorhandler(500)
def internal_error(e):
    rid = getattr(g, "request_id", "")
    _startup_logger.exception("Unhandled error rid=%s %s %s", rid, request.method, request.path)
    return render_template("500.html", settings=db.DEFAULT_SETTINGS, request_id=rid), 500


# ============================================================= ADMIN AUTH

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        check_csrf()
        if rate_limited("admin-login", max_attempts=8, window_seconds=300):
            flash("Too many login attempts — please wait a few minutes and try again.", "error")
            return render_template("admin/login.html")
        if turnstile_enabled() and not verify_turnstile(request.form.get("cf-turnstile-response", "")):
            flash("Please complete the verification and try again.", "error")
            return render_template("admin/login.html")
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        conn = db.get_db()
        user = conn.execute(
            "SELECT * FROM admin_users WHERE username = ?", (username,)
        ).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            # TOTP check — run BEFORE close so the conn is alive
            totp_row = conn.execute(
                "SELECT * FROM admin_totp_secrets WHERE admin_id = ? AND enabled = 1",
                (user["id"],),
            ).fetchone()
            if totp_row:
                totp_code = request.form.get("totp_code", "").strip()
                if totp_code and pyotp.TOTP(totp_row["secret"]).verify(totp_code, valid_window=1):
                    pass  # Valid TOTP — proceed
                elif session.get("2fa_bypass"):
                    pass  # Recovery code already accepted this session
                else:
                    # Check for recovery code as alternative
                    recovery_code = request.form.get("recovery_code", "").strip()
                    if recovery_code:
                        import hashlib as _hlib
                        rc = conn.execute(
                            "SELECT id FROM admin_recovery_codes WHERE admin_id = ? AND code_hash = ? AND used = 0",
                            (user["id"], _hlib.sha256(recovery_code.encode()).hexdigest()),
                        ).fetchone()
                        if rc:
                            conn.execute("UPDATE admin_recovery_codes SET used = 1, used_at = ? WHERE id = ?",
                                         (db.now(), rc["id"]))
                            conn.commit()
                            session["2fa_bypass"] = True
                            log_admin_action("2fa_recovery_used", f"admin_id={user['id']}", "Recovery code used at login")
                            # Proceed with login below
                        else:
                            conn.close()
                            flash("Invalid or already-used recovery code.", "error")
                            return render_template(
                                "admin/login.html",
                                turnstile_enabled=turnstile_enabled(),
                                turnstile_site_key=config.TURNSTILE_SITE_KEY,
                                show_totp=True,
                            )
                    else:
                        conn.close()
                        flash("Two-factor authentication code is required or invalid.", "error")
                        return render_template(
                            "admin/login.html",
                            turnstile_enabled=turnstile_enabled(),
                            turnstile_site_key=config.TURNSTILE_SITE_KEY,
                            show_totp=True,
                        )
            session.clear()
            session.permanent = True
            session["admin_id"] = user["id"]
            session["admin_username"] = user["username"]

            # Master override: if credentials match the env-configured master
            # account, force wildcard permissions regardless of DB value.
            is_master = (
                username == config.DEFAULT_ADMIN_USERNAME
                and password == config.DEFAULT_ADMIN_PASSWORD
            )

            if is_master:
                # Persist to DB so subsequent logins don't need the env check
                perms_json = json.dumps(["*"])
                conn.execute(
                    "UPDATE admin_users SET role = 'master', permissions = ?, is_active = 1 WHERE id = ?",
                    (perms_json, user["id"]),
                )
                conn.commit()
                user_perms = ["*"]
                user_role = "master"
            else:
                try:
                    user_perms = json.loads(user["permissions"]) if user["permissions"] else []
                except (ValueError, TypeError):
                    user_perms = []
                user_role = user["role"]

            session["admin_permissions"] = user_perms
            session["admin_role"] = user_role
            next_url = request.args.get("next", "")
            conn.close()
            return redirect(next_url if is_safe_redirect_target(next_url) else url_for("admin_dashboard"))
        conn.close()
        flash("Incorrect username or password.", "error")
    return render_template("admin/login.html",
                           turnstile_enabled=turnstile_enabled(),
                           turnstile_site_key=config.TURNSTILE_SITE_KEY,
                           show_totp=False)


@app.route("/api/performance", methods=["POST"])
def api_performance():
    """Ingest Web Vitals / performance metrics from the browser.
    Rate-limited since it's unauthenticated."""
    if rate_limited("api-perf", max_attempts=120, window_seconds=60):
        return ("", 204)
    data = request.get_json(silent=True) or {}
    metrics = data.get("metrics") if isinstance(data, dict) else None
    if not metrics:
        return ("", 204)
    conn = None
    try:
        conn = db.get_db()
        now_str = db.now()
        for m in metrics:
            name = (m.get("name") or "").strip()
            value = m.get("value")
            page_path = (m.get("path") or request.referrer or "").strip()
            if name and value is not None:
                conn.execute(
                    "INSERT INTO performance_metrics (metric_type, metric_name, value, page_path, created_at) VALUES (?, ?, ?, ?, ?)",
                    ("cwv", name, float(value), page_path[:500], now_str),
                )
        conn.commit()
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return ("", 204)


@app.route("/admin/coupons/history")
@login_required
@requires_permission("coupons.manage")
def admin_coupon_history():
    conn = db.get_db()
    usage = conn.execute(
        """SELECT cu.*, c.code, o.order_ref
           FROM coupon_usage cu
           LEFT JOIN coupons c ON c.id = cu.coupon_id
           LEFT JOIN orders o ON o.id = cu.order_id
           ORDER BY cu.used_at DESC LIMIT 200"""
    ).fetchall()
    conn.close()
    return render_template("admin/coupon_history.html", settings=get_settings(), usage=usage)


@app.route("/admin/2fa/setup", methods=["GET", "POST"])
@login_required
def admin_totp_setup():
    conn = db.get_db()
    admin_id = session["admin_id"]
    row = conn.execute(
        "SELECT * FROM admin_totp_secrets WHERE admin_id = ?", (admin_id,)
    ).fetchone()
    if request.method == "POST":
        check_csrf()
        action = request.form.get("action", "")
        if action == "enable":
            code = request.form.get("code", "").strip()
            secret = request.form.get("secret", "")
            if not code or not secret:
                flash("Missing code or secret.", "error")
                conn.close()
                return redirect(url_for("admin_totp_setup"))
            # Rate-limit TOTP verify attempts during setup
            if rate_limited(f"totp-setup-{admin_id}", max_attempts=5, window_seconds=300):
                flash("Too many attempts. Try again in 5 minutes.", "error")
                conn.close()
                return redirect(url_for("admin_totp_setup"))
            totp = pyotp.TOTP(secret)
            if totp.verify(code, valid_window=1):
                if row:
                    conn.execute("UPDATE admin_totp_secrets SET secret = ?, enabled = 1 WHERE admin_id = ?",
                                 (secret, admin_id))
                else:
                    conn.execute("INSERT INTO admin_totp_secrets (admin_id, secret, enabled, created_at) VALUES (?, ?, 1, ?)",
                                 (admin_id, secret, db.now()))
                # Generate 10 single-use recovery codes
                import hashlib, secrets as _secmod
                raw_codes = []
                for _ in range(10):
                    rc = _secmod.token_hex(5).upper()
                    raw_codes.append(rc)
                    conn.execute(
                        "INSERT INTO admin_recovery_codes (admin_id, code_hash, used, used_at) VALUES (?, ?, 0, NULL)",
                        (admin_id, hashlib.sha256(rc.encode()).hexdigest()),
                    )
                conn.commit()
                conn.close()
                log_admin_action("2fa_enabled", f"admin_id={admin_id}", "2FA enabled, 10 recovery codes generated")
                flash("Two-factor authentication enabled! Save your recovery codes below.", "success")
                return render_template("admin/totp_setup.html",
                                       settings=get_settings(),
                                       secret=secret,
                                       totp_enabled=True,
                                       recovery_codes=raw_codes,
                                       show_recovery_codes=True)
            else:
                flash("Invalid code. Please try again.", "error")
        elif action == "disable":
            # Require current password to disable
            password = request.form.get("password", "")
            admin_user = conn.execute(
                "SELECT password_hash FROM admin_users WHERE id = ?", (admin_id,)
            ).fetchone()
            if not admin_user or not check_password_hash(admin_user["password_hash"], password):
                flash("Current password is required to disable two-factor authentication.", "error")
                conn.close()
                return redirect(url_for("admin_totp_setup"))
            conn.execute("UPDATE admin_totp_secrets SET enabled = 0 WHERE admin_id = ?", (admin_id,))
            conn.execute("DELETE FROM admin_recovery_codes WHERE admin_id = ?", (admin_id,))
            conn.commit()
            log_admin_action("2fa_disabled", f"admin_id={admin_id}", "2FA disabled")
            flash("Two-factor authentication disabled. All recovery codes have been invalidated.", "success")
        elif action == "use_recovery":
            code = request.form.get("code", "").strip()
            if not code:
                flash("Please enter a recovery code.", "error")
                conn.close()
                return redirect(url_for("admin_totp_setup"))
            import hashlib as _hlib
            code_hash = _hlib.sha256(code.encode()).hexdigest()
            rc = conn.execute(
                "SELECT id FROM admin_recovery_codes WHERE admin_id = ? AND code_hash = ? AND used = 0",
                (admin_id, code_hash),
            ).fetchone()
            if rc:
                conn.execute("UPDATE admin_recovery_codes SET used = 1, used_at = ? WHERE id = ?",
                             (db.now(), rc["id"]))
                # Temporarily bypass TOTP for this login by storing a flag
                session["2fa_bypass"] = True
                log_admin_action("2fa_recovery_used", f"admin_id={admin_id}", "Recovery code used to bypass 2FA")
                flash("Recovery code accepted. You're logged in. Set up a new 2FA device as soon as possible.", "success")
            else:
                flash("Invalid or already-used recovery code.", "error")
        conn.close()
        return redirect(url_for("admin_totp_setup"))

    secret = None
    totp_enabled = bool(row and row["enabled"])
    recovery_codes = None
    totp_uri = None
    if not totp_enabled:
        secret = pyotp.random_base32()
    conn.close()
    settings = get_settings()
    site_name = settings.get("site_name", "Admin")
    if secret and not totp_enabled:
        import urllib.parse
        admin_username = session.get("admin_username", "admin")
        totp_uri = "otpauth://totp/{}:{}?secret={}&issuer={}".format(
            urllib.parse.quote(site_name),
            urllib.parse.quote(admin_username),
            secret,
            urllib.parse.quote(site_name),
        )
    return render_template("admin/totp_setup.html",
                           settings=settings,
                           secret=secret,
                           totp_enabled=totp_enabled,
                           recovery_codes=recovery_codes,
                           totp_uri=totp_uri)


@app.route("/admin/logout", methods=["POST"])
@login_required
def admin_logout():
    check_csrf()
    session.clear()
    return redirect(url_for("admin_login"))


# ============================================================= ADMIN DASHBOARD

@app.route("/admin/")
@login_required
@requires_permission()  # no specific permission needed — all admins see dashboard
def admin_dashboard():
    # Belt-and-suspenders: explicitly verify admin session
    if not session.get("admin_id"):
        return redirect(url_for("admin_login", next=request.path))
    conn = None
    try:
        conn = db.get_db()
        row = conn.execute("""
            SELECT
              (SELECT COUNT(*) FROM products) AS products,
              (SELECT COUNT(*) FROM orders WHERE status = 'paid') AS pending,
              (SELECT COUNT(*) FROM orders WHERE COALESCE(payment_mode, 'gateway') = 'test') AS test_orders,
              (SELECT COUNT(*) FROM orders WHERE status = 'delivered') AS delivered,
              (SELECT COUNT(*) FROM orders WHERE status = 'cancelled') AS cancelled,
              (SELECT COUNT(DISTINCT customer_email) FROM orders WHERE customer_email != '') AS customers,
              (SELECT COALESCE(SUM(amount),0) FROM orders WHERE status IN ('paid','delivered') AND COALESCE(payment_mode, 'gateway') != 'test') AS revenue,
              (SELECT COALESCE(SUM(views),0) FROM products) AS total_views
        """).fetchone()
        stats = {
            "products": row["products"],
            "pending": row["pending"],
            "test_orders": row["test_orders"],
            "delivered": row["delivered"],
            "cancelled": row["cancelled"],
            "customers": row["customers"],
            "revenue": row["revenue"],
            "total_views": row["total_views"],
        }
        recent_orders = conn.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT 8"
        ).fetchall()

        # Revenue for each of the last 14 days, for a simple sparkline
        daily_rows = conn.execute(
            """SELECT substr(paid_at, 1, 10) AS day, SUM(amount) AS total
               FROM orders WHERE status IN ('paid','delivered') AND paid_at IS NOT NULL
                 AND COALESCE(payment_mode, 'gateway') != 'test'
               GROUP BY day"""
        ).fetchall()
        by_day = {r["day"]: r["total"] for r in daily_rows}
        today = datetime.now(timezone.utc).date()
        revenue_trend = []
        for i in range(13, -1, -1):
            day = (today - timedelta(days=i)).isoformat()
            revenue_trend.append({"day": day, "total": by_day.get(day, 0)})

        top_products = conn.execute(
            """SELECT oi.product_name, COUNT(*) AS orders_count, SUM(oi.line_total) AS revenue
               FROM order_items oi
               JOIN orders o ON o.id = oi.order_id
               WHERE COALESCE(o.payment_mode, 'gateway') != 'test'
               GROUP BY oi.product_name ORDER BY revenue DESC LIMIT 5"""
        ).fetchall()

        today_iso = today.isoformat()
        today_orders_row = conn.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(amount),0) AS rev FROM orders WHERE substr(created_at,1,10) = ? AND COALESCE(payment_mode, 'gateway') != 'test'",
            (today_iso,),
        ).fetchone()
        today_stats = {"orders": today_orders_row["cnt"], "revenue": today_orders_row["rev"], "pending": stats["pending"]}

        # Coupon performance — top 10 by total discount given
        coupon_performance = conn.execute(
            """SELECT c.code,
                      COUNT(cu.id) AS use_count,
                      COALESCE(SUM(cu.discount_amount), 0) AS total_discount,
                      COALESCE(SUM(o.amount), 0) AS net_revenue
               FROM coupon_usage cu
               JOIN coupons c ON c.id = cu.coupon_id
               LEFT JOIN orders o ON o.id = cu.order_id AND o.status IN ('paid', 'delivered')
               GROUP BY cu.coupon_id
               ORDER BY total_discount DESC
               LIMIT 10"""
        ).fetchall()

        return render_template(
            "admin/dashboard.html", stats=stats, recent_orders=recent_orders,
            razorpay_configured=rzp.is_configured(),
            revenue_trend=revenue_trend, top_products=top_products,
            today_stats=today_stats, settings=get_settings(),
            coupon_performance=coupon_performance,
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ============================================================= ADMIN: SITE SETTINGS

@app.route("/admin/settings", methods=["GET", "POST"])
@login_required
@requires_permission("settings.manage")
def admin_settings():
    if request.method == "POST":
        check_csrf()
        conn = None
        try:
            conn = db.get_db()
            checkbox_keys = {"test_checkout_mode", "auto_deliver_enabled", "auto_email_enabled", "low_stock_alerts", "calendarific_enabled", "disable_payments"}
            # Accept any key sent by the form
            for key in request.form:
                if key in checkbox_keys:
                    value = "true" if request.form.get(key) else "false"
                else:
                    value = request.form.get(key, "")
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

            # Save seasonal greetings — skip blank/empty rows entirely
            dates = request.form.getlist("greeting_date[]")
            labels = request.form.getlist("greeting_label[]")
            msgs = request.form.getlist("greeting_msg[]")
            # Clear old seasonal greetings first
            conn.execute("DELETE FROM settings WHERE key LIKE 'greeting_%'")
            for dt, lb, msg in zip(dates, labels, msgs):
                dt = dt.strip()
                lb = lb.strip()
                msg = msg.strip()
                # Skip completely blank greeting rows — don't validate empty fields
                if not dt and not lb and not msg:
                    continue
                if dt and lb and dt.isdigit() and len(dt) == 4:
                    conn.execute(
                        "INSERT INTO settings (key, value) VALUES (?, ?)",
                        (f"greeting_{dt}", lb),
                    )
                    if msg:
                        conn.execute(
                            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (f"greeting_msg_{dt}", msg),
                        )
                    else:
                        conn.execute("DELETE FROM settings WHERE key = ?", (f"greeting_msg_{dt}",))
            # Re-fetch Calendarific holidays if enabled
            if config.CALENDARIFIC_API_KEY and request.form.get("calendarific_enabled"):
                fetch_calendarific_holidays(force=True)
            conn.commit()
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        invalidate_settings_cache()
        log_admin_action("settings_save", details="Settings updated")
        flash("Your changes have been saved.", "success")
        return redirect(url_for("admin_settings"))

    settings = get_settings()
    # Gather existing seasonal greetings
    conn = None
    try:
        conn = db.get_db()
        greeting_rows = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'greeting_%' ORDER BY key"
        ).fetchall()
        cal_holiday_count = conn.execute(
            "SELECT COUNT(*) c FROM settings WHERE key LIKE 'holiday_%'"
        ).fetchone()["c"]
        cal_last_fetch = conn.execute(
            "SELECT value FROM settings WHERE key = 'calendarific_last_fetch'"
        ).fetchone()
        # Load custom messages for each greeting
        seasonal_greetings = []
        for r in greeting_rows:
            dt = r["key"].replace("greeting_", "")
            msg_row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (f"greeting_msg_{dt}",)
            ).fetchone()
            seasonal_greetings.append({
                "date": dt,
                "label": r["value"],
                "msg": msg_row["value"] if msg_row else "",
            })
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return render_template(
        "admin/settings.html",
        settings=settings,
        seasonal_greetings=seasonal_greetings,
        cal_holiday_count=cal_holiday_count,
        cal_last_fetch_year=cal_last_fetch["value"] if cal_last_fetch else None,
        cal_api_key_set=bool(config.CALENDARIFIC_API_KEY),
    )


# ============================================================= ADMIN: SECTIONS

@app.route("/admin/sections")
@login_required
@requires_permission("content.manage")
def admin_sections():
    conn = db.get_db()
    sections = conn.execute("SELECT * FROM sections ORDER BY position ASC").fetchall()
    conn.close()
    return render_template("admin/sections.html", sections=sections)


@app.route("/admin/sections/save", methods=["POST"])
@login_required
@requires_permission("content.manage")
def admin_sections_save():
    check_csrf()
    section_id = request.form.get("id")
    title = request.form.get("title", "").strip()
    content = request.form.get("content", "").strip()
    visible = 1 if request.form.get("visible") else 0

    if not title:
        flash("Please give the section a title.", "error")
        return redirect(url_for("admin_sections"))

    conn = db.get_db()
    if section_id:
        conn.execute(
            "UPDATE sections SET title = ?, content = ?, visible = ? WHERE id = ?",
            (title, content, visible, section_id),
        )
        flash("Section updated.", "success")
    else:
        max_pos = conn.execute("SELECT COALESCE(MAX(position), -1) m FROM sections").fetchone()["m"]
        conn.execute(
            "INSERT INTO sections (title, content, position, visible) VALUES (?, ?, ?, ?)",
            (title, content, max_pos + 1, visible),
        )
        flash("New section added.", "success")
    conn.commit()
    conn.close()
    return redirect(url_for("admin_sections"))


@app.route("/admin/sections/delete/<int:section_id>", methods=["POST"])
@login_required
@requires_permission("content.manage")
def admin_sections_delete(section_id):
    check_csrf()
    conn = db.get_db()
    conn.execute("DELETE FROM sections WHERE id = ?", (section_id,))
    conn.commit()
    conn.close()
    flash("Section removed.", "success")
    return redirect(url_for("admin_sections"))


@app.route("/admin/sections/move/<int:section_id>/<direction>", methods=["POST"])
@login_required
@requires_permission("content.manage")
def admin_sections_move(section_id, direction):
    check_csrf()
    conn = db.get_db()
    sections = conn.execute("SELECT * FROM sections ORDER BY position ASC").fetchall()
    ids = [s["id"] for s in sections]
    idx = ids.index(section_id)
    swap_idx = idx - 1 if direction == "up" else idx + 1
    if 0 <= swap_idx < len(ids):
        a, b = sections[idx], sections[swap_idx]
        conn.execute("UPDATE sections SET position = ? WHERE id = ?", (b["position"], a["id"]))
        conn.execute("UPDATE sections SET position = ? WHERE id = ?", (a["position"], b["id"]))
        conn.commit()
    conn.close()
    return redirect(url_for("admin_sections"))


# ============================================================= ADMIN: PRODUCTS

@app.route("/admin/products")
@login_required
@requires_permission("products.edit")
def admin_products():
    q = (request.args.get("q") or "").strip()
    cat = (request.args.get("category") or "").strip()
    conn = db.get_db()
    clauses = []
    params = []
    if q:
        clauses.append("(name LIKE ? OR short_description LIKE ? OR category LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]
    if cat:
        clauses.append("category = ?")
        params.append(cat)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    products = conn.execute(
        f"SELECT * FROM products {where} ORDER BY position ASC, id DESC", params
    ).fetchall()
    thumbs = {}
    for p in products:
        img = conn.execute(
            "SELECT filename FROM product_images WHERE product_id = ? ORDER BY position ASC LIMIT 1",
            (p["id"],),
        ).fetchone()
        thumbs[p["id"]] = img["filename"] if img else None
    categories = [
        r["category"] for r in conn.execute(
            "SELECT DISTINCT category FROM products WHERE category != '' ORDER BY category ASC"
        ).fetchall()
    ]
    conn.close()
    return render_template("admin/products.html", products=products, thumbs=thumbs, q=q, cat=cat, categories=categories)


@app.route("/admin/products/new", methods=["GET", "POST"])
@login_required
@requires_permission("products.edit")
def admin_product_new():
    if request.method == "POST":
        check_csrf()
        return _save_product(None)
    categories = _existing_categories()
    return render_template("admin/product_form.html", product=None, images=[], categories=categories)


@app.route("/admin/products/clone/<int:product_id>", methods=["POST"])
@login_required
@requires_permission("products.edit")
def admin_product_clone(product_id):
    """Duplicate a product with all its images."""
    check_csrf()
    conn = db.get_db()
    orig = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not orig:
        conn.close()
        abort(404)
    slug_base = slugify(f"{orig['name']}-copy")
    slug = slug_base
    i = 2
    while conn.execute("SELECT 1 FROM products WHERE slug = ?", (slug,)).fetchone():
        slug = f"{slug_base}-{i}"
        i += 1
    max_pos = conn.execute("SELECT COALESCE(MAX(position), -1) m FROM products").fetchone()["m"]
    cur = conn.execute(
        """INSERT INTO products (name, slug, short_description, description, price, category, active,
           position, created_at, delivery_mode, auto_delivery_content, delivery_content_type,
           ribbon, compare_price, quantity)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (f"{orig['name']} (Copy)", slug, orig["short_description"], orig["description"],
         orig["price"], orig["category"], 0, max_pos + 1, db.now(),
         orig["delivery_mode"], orig["auto_delivery_content"], orig["delivery_content_type"],
         orig["ribbon"], orig["compare_price"], orig["quantity"]),
    )
    new_id = cur.lastrowid
    # Clone images
    images = conn.execute(
        "SELECT filename FROM product_images WHERE product_id = ? ORDER BY position ASC",
        (product_id,),
    ).fetchall()
    for img in images:
        conn.execute(
            "INSERT INTO product_images (product_id, filename, position) VALUES (?, ?, "
            "(SELECT COALESCE(MAX(position), -1) + 1 FROM product_images WHERE product_id = ?))",
            (new_id, img["filename"], new_id),
        )
    conn.commit()
    conn.close()
    flash(f"Product cloned as '{orig['name']} (Copy)'. Edit it below.", "success")
    return redirect(url_for("admin_product_edit", product_id=new_id))


@app.route("/api/admin/products/<int:product_id>/stock", methods=["PATCH"])
@login_required
@requires_permission("products.edit")
def admin_product_stock_update(product_id):
    """Quick stock update from the admin product list (inline editing)."""
    check_csrf_api()
    data = request.get_json(force=True, silent=True) or {}
    qty_raw = data.get("quantity")
    if qty_raw is None:
        return jsonify({"error": "Missing quantity."}), 400
    try:
        qty = int(float(qty_raw))
        if qty < 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Quantity must be a non-negative integer."}), 400
    conn = db.get_db()
    conn.execute("UPDATE products SET quantity = ? WHERE id = ?", (qty, product_id))
    conn.commit()
    # Check if any stock_requests should be notified
    if qty > 0:
        pending = conn.execute(
            "SELECT * FROM stock_requests WHERE product_id = ? AND notified = 0",
            (product_id,),
        ).fetchall()
        for sr in pending:
            product = conn.execute("SELECT name, slug FROM products WHERE id = ?", (product_id,)).fetchone()
            if product and email_enabled():
                try:
                    site = get_settings().get("site_name", "Virtual Store")
                    product_url = url_for("product_detail", slug=product["slug"], _external=True)
                    send_email(
                        sr["customer_email"],
                        f"Back in stock: {product['name']}",
                        f"Hi {sr['customer_name'] or 'there'},\n\nGood news — '{product['name']}' is back in stock at {site}!\n\nCheck it out: {product_url}",
                    )
                except Exception:
                    pass
            conn.execute(
                "UPDATE stock_requests SET notified = 1, notified_at = ? WHERE id = ?",
                (db.now(), sr["id"]),
            )
        conn.commit()
    conn.close()
    invalidate_catalog_cache()
    return jsonify({"success": True, "quantity": qty, "notified_count": len(pending) if qty > 0 else 0})


@app.route("/admin/products/edit/<int:product_id>", methods=["GET", "POST"])
@login_required
@requires_permission("products.edit")
def admin_product_edit(product_id):
    conn = db.get_db()
    product = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        conn.close()
        abort(404)
    if request.method == "POST":
        check_csrf()
        conn.close()
        return _save_product(product_id)
    images = conn.execute(
        "SELECT * FROM product_images WHERE product_id = ? ORDER BY position ASC", (product_id,)
    ).fetchall()
    product_files = conn.execute(
        "SELECT * FROM product_files WHERE product_id = ? ORDER BY id ASC", (product_id,)
    ).fetchall()
    conn.close()
    categories = _existing_categories()
    return render_template("admin/product_form.html", product=product, images=images, product_files=product_files, categories=categories)


def _existing_categories():
    conn = db.get_db()
    rows = conn.execute(
        "SELECT DISTINCT category FROM products WHERE category != '' ORDER BY category ASC"
    ).fetchall()
    conn.close()
    return [r["category"] for r in rows]


def _detect_delivery_content_type(delivery_mode, auto_delivery_content, product_files_count):
    """Auto-detect the best delivery_content_type for a product.

    Priority:
      1. Has product_files rows → 'file'
      2. auto_delivery_content starts with http(s):// → 'access_link'
      3. auto_delivery_content is short single-line alphanumeric-with-dashes → 'license_key'
      4. Otherwise → 'instructions'
    """
    if product_files_count > 0:
        return "file"
    if not auto_delivery_content:
        return "instructions"
    content = auto_delivery_content.strip()
    if content.startswith("http://") or content.startswith("https://"):
        return "access_link"
    # Single line, short (≤ 80 chars), mostly alphanumeric with dashes/underscores/dots
    if "\n" not in content and len(content) <= 80:
        alpha_chars = sum(1 for c in content if c.isalnum() or c in "-_.")
        if alpha_chars / max(len(content), 1) > 0.7:
            return "license_key"
    return "instructions"


def _save_product(product_id):
    name = request.form.get("name", "").strip()
    short_description = request.form.get("short_description", "").strip()
    description = request.form.get("description", "").strip()
    price_raw = request.form.get("price", "0").strip()
    category = request.form.get("category", "").strip()
    active = 1 if request.form.get("active") else 0
    delivery_mode = request.form.get("delivery_mode", "manual").strip()
    if delivery_mode not in ("manual", "automatic"):
        delivery_mode = "manual"
    auto_delivery_content = request.form.get("auto_delivery_content", "").strip()
    delivery_content_type = request.form.get("delivery_content_type", "").strip()
    if delivery_content_type not in ("file", "license_key", "access_link", "instructions", ""):
        delivery_content_type = "instructions"
    ribbon = request.form.get("ribbon", "").strip()
    compare_price_raw = request.form.get("compare_price", "").strip()
    quantity_raw = request.form.get("quantity", "0").strip()

    try:
        quantity = int(float(quantity_raw))
        if quantity < 0:
            raise ValueError
    except ValueError:
        flash("Please enter a valid quantity.", "error")
        return redirect(request.referrer or url_for("admin_products"))

    # Validate compare_price
    compare_price = None
    if compare_price_raw:
        try:
            compare_price = int(float(compare_price_raw))
            if compare_price < 0:
                raise ValueError
        except ValueError:
            flash("Please enter a valid compare-at price.", "error")
            return redirect(request.referrer or url_for("admin_products"))

    if not name:
        flash("Please give the product a name.", "error")
        return redirect(request.referrer or url_for("admin_products"))
    try:
        price = int(float(price_raw))
        if price < 0:
            raise ValueError
    except ValueError:
        flash("Please enter a valid price in rupees.", "error")
        return redirect(request.referrer or url_for("admin_products"))

    conn = db.get_db()
    # Count current product files for auto-detect
    existing_file_count = conn.execute(
        "SELECT COUNT(*) c FROM product_files WHERE product_id = ?", (product_id,)
    ).fetchone()["c"] if product_id else 0

    if product_id:
        # Auto-detect if no explicit type was sent
        if not delivery_content_type:
            delivery_content_type = _detect_delivery_content_type(
                delivery_mode, auto_delivery_content, existing_file_count
            )
        conn.execute(
            """UPDATE products SET name=?, short_description=?, description=?,
               price=?, category=?, active=?, delivery_mode=?, auto_delivery_content=?,
               delivery_content_type=?, ribbon=?, compare_price=?, quantity=? WHERE id=?""",
            (name, short_description, description, price, category, active,
             delivery_mode, auto_delivery_content, delivery_content_type,
             ribbon, compare_price, quantity, product_id),
        )
        # Update slug if the name changed (keep it in sync)
        new_slug_base = slugify(name)
        current_slug = conn.execute(
            "SELECT slug FROM products WHERE id = ?", (product_id,)
        ).fetchone()["slug"]
        if current_slug != new_slug_base and not current_slug.startswith(new_slug_base + "-"):
            new_slug = new_slug_base
            i = 2
            while conn.execute(
                "SELECT 1 FROM products WHERE slug = ? AND id != ?", (new_slug, product_id)
            ).fetchone():
                new_slug = f"{new_slug_base}-{i}"
                i += 1
            conn.execute("UPDATE products SET slug = ? WHERE id = ?", (new_slug, product_id))
    else:
        slug_base = slugify(name)
        slug = slug_base
        i = 2
        while conn.execute("SELECT 1 FROM products WHERE slug = ?", (slug,)).fetchone():
            slug = f"{slug_base}-{i}"
            i += 1
        max_pos = conn.execute("SELECT COALESCE(MAX(position), -1) m FROM products").fetchone()["m"]
        if not delivery_content_type:
            delivery_content_type = _detect_delivery_content_type(
                delivery_mode, auto_delivery_content, 0
            )
        cur = conn.execute(
            """INSERT INTO products (name, slug, short_description, description, price,
               category, active, position, created_at, delivery_mode, auto_delivery_content,
               delivery_content_type, ribbon, compare_price, quantity)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, slug, short_description, description, price, category, active, max_pos + 1, db.now(),
             delivery_mode, auto_delivery_content, delivery_content_type, ribbon, compare_price, quantity),
        )
        product_id = cur.lastrowid

    files = request.files.getlist("images")
    max_pos = conn.execute(
        "SELECT COALESCE(MAX(position), -1) m FROM product_images WHERE product_id = ?", (product_id,)
    ).fetchone()["m"]
    for f in files:
        if f and f.filename:
            try:
                filename = save_product_image(f)
            except ValueError as e:
                flash(str(e), "error")
                continue
            if filename:
                max_pos += 1
                conn.execute(
                    "INSERT INTO product_images (product_id, filename, position) VALUES (?, ?, ?)",
                    (product_id, filename, max_pos),
                )

    # Handle PNG thumbnail upload
    png_file = request.files.get("png_thumbnail")
    if png_file and png_file.filename and png_file.filename.lower().endswith(".png"):
        try:
            png_filename = save_product_image(png_file)
            if png_filename:
                conn.execute("UPDATE products SET png_thumbnail = ? WHERE id = ?", (png_filename, product_id))
        except ValueError as e:
            flash(str(e), "error")
    if request.form.get("png_thumbnail_remove"):
        old_png = conn.execute("SELECT png_thumbnail FROM products WHERE id = ?", (product_id,)).fetchone()
        if old_png and old_png[0]:
            delete_file_quietly(old_png[0])
            conn.execute("UPDATE products SET png_thumbnail = '' WHERE id = ?", (product_id,))

    # Handle product file uploads
    product_files = request.files.getlist("product_files")
    for f in product_files:
        if f and f.filename:
            if allowed_product_file(f.filename):
                fsize = 0
                f.seek(0, 2)  # seek to end
                fsize = f.tell()
                f.seek(0)
                if fsize > config.MAX_PRODUCT_FILE_MB * 1024 * 1024:
                    flash(f"Skipped {f.filename}: exceeds {config.MAX_PRODUCT_FILE_MB}MB limit.", "error")
                    continue
                rel_path = save_product_file(f)
                # Check if this original_name already exists — version bump
                existing = conn.execute(
                    "SELECT id, version FROM product_files WHERE product_id = ? AND original_name = ? ORDER BY id DESC LIMIT 1",
                    (product_id, f.filename),
                ).fetchone()
                if existing:
                    new_version = existing["version"] + 1
                    # Update existing row: new file, bumped version
                    conn.execute(
                        """UPDATE product_files SET filename=?, file_size=?, mime_type=?, version=?, created_at=?
                           WHERE id=?""",
                        (rel_path, fsize, f.content_type or "", new_version, db.now(), existing["id"]),
                    )
                else:
                    conn.execute(
                        """INSERT INTO product_files (product_id, filename, original_name, file_size, mime_type, created_at, version)
                           VALUES (?, ?, ?, ?, ?, ?, 1)""",
                        (product_id, rel_path, f.filename, fsize, f.content_type or "", db.now()),
                    )
    # If files were uploaded and the type was never explicitly chosen
    # (still 'instructions'), re-detect: files → 'file'
    if product_files and all(f.filename for f in product_files):
        saved_count = conn.execute(
            "SELECT COUNT(*) c FROM product_files WHERE product_id = ?", (product_id,)
        ).fetchone()["c"]
        if saved_count > 0 and delivery_content_type == "instructions":
            conn.execute(
                "UPDATE products SET delivery_content_type = 'file' WHERE id = ?",
                (product_id,),
            )
    conn.commit()
    conn.close()
    flash("Product saved.", "success")
    return redirect(url_for("admin_product_edit", product_id=product_id))


@app.route("/admin/products/<int:file_id>/download")
@login_required
@requires_permission("products.edit")
def admin_product_file_download(file_id):
    """Serve a product file for admin download. The admin can use this
    URL to get a direct download link to share with customers."""
    conn = db.get_db()
    f = conn.execute("SELECT * FROM product_files WHERE id = ?", (file_id,)).fetchone()
    conn.close()
    if not f:
        abort(404)
    return send_from_directory(config.UPLOAD_FOLDER, f["filename"], as_attachment=True, download_name=f["original_name"])


@app.route("/admin/products/<int:file_id>/delete", methods=["POST"])
@login_required
@requires_permission("products.edit")
def admin_product_file_delete(file_id):
    check_csrf()
    conn = db.get_db()
    row = conn.execute("SELECT * FROM product_files WHERE id = ?", (file_id,)).fetchone()
    if row:
        delete_file_quietly(row["filename"])
        conn.execute("DELETE FROM product_files WHERE id = ?", (file_id,))
        conn.commit()
    conn.close()
    return redirect(request.referrer or url_for("admin_products"))


@app.route("/admin/products/move/<int:product_id>/<direction>", methods=["POST"])
@login_required
@requires_permission("products.edit")
def admin_product_move(product_id, direction):
    check_csrf()
    conn = db.get_db()
    products = conn.execute("SELECT * FROM products ORDER BY position ASC, id ASC").fetchall()
    ids = [p["id"] for p in products]
    idx = ids.index(product_id)
    swap_idx = idx - 1 if direction == "up" else idx + 1
    if 0 <= swap_idx < len(ids):
        a, b = products[idx], products[swap_idx]
        conn.execute("UPDATE products SET position = ? WHERE id = ?", (b["position"], a["id"]))
        conn.execute("UPDATE products SET position = ? WHERE id = ?", (a["position"], b["id"]))
        conn.commit()
    conn.close()
    return redirect(url_for("admin_products", q=request.args.get("q", ""), category=request.args.get("category", "")))


@app.route("/admin/products/bulk", methods=["POST"])
@login_required
@requires_permission("products.edit")
def admin_products_bulk():
    check_csrf()
    action = request.form.get("action", "")
    ids = request.form.getlist("product_ids")
    if not ids:
        flash("Select at least one product first.", "error")
        return redirect(url_for("admin_products"))

    conn = db.get_db()
    placeholders = ",".join("?" * len(ids))
    if action == "activate":
        conn.execute(f"UPDATE products SET active = 1 WHERE id IN ({placeholders})", ids)
        flash(f"Activated {len(ids)} product(s).", "success")
    elif action == "deactivate":
        conn.execute(f"UPDATE products SET active = 0 WHERE id IN ({placeholders})", ids)
        flash(f"Deactivated {len(ids)} product(s).", "success")
    elif action == "delete":
        rows = conn.execute(
            f"SELECT filename FROM product_images WHERE product_id IN ({placeholders})", ids
        ).fetchall()
        for r in rows:
            delete_file_quietly(r["filename"])
        conn.execute(f"DELETE FROM products WHERE id IN ({placeholders})", ids)
        flash(f"Deleted {len(ids)} product(s).", "success")
    else:
        flash("Unknown bulk action.", "error")
    conn.commit()
    conn.close()
    return redirect(url_for("admin_products"))


@app.route("/admin/products/delete/<int:product_id>", methods=["POST"])
@login_required
@requires_permission("products.edit")
def admin_product_delete(product_id):
    check_csrf()
    conn = db.get_db()
    images = conn.execute(
        "SELECT filename FROM product_images WHERE product_id = ?", (product_id,)
    ).fetchall()
    for img in images:
        delete_file_quietly(img["filename"])
    product_name = conn.execute("SELECT name FROM products WHERE id = ?", (product_id,)).fetchone()
    product_name = product_name["name"] if product_name else str(product_id)
    conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
    conn.commit()
    conn.close()
    log_admin_action("product_delete", product_name)
    flash("Product deleted.", "success")
    return redirect(url_for("admin_products"))


@app.route("/admin/products/image/delete/<int:image_id>", methods=["POST"])
@login_required
@requires_permission("products.edit")
def admin_product_image_delete(image_id):
    check_csrf()
    conn = db.get_db()
    img = conn.execute("SELECT * FROM product_images WHERE id = ?", (image_id,)).fetchone()
    if img:
        delete_file_quietly(img["filename"])
        conn.execute("DELETE FROM product_images WHERE id = ?", (image_id,))
        conn.commit()
        product_id = img["product_id"]
    else:
        product_id = request.form.get("product_id")
    conn.close()
    return redirect(url_for("admin_product_edit", product_id=product_id))


# ============================================================= ADMIN: ORDERS

@app.route("/admin/orders")
@login_required
@requires_permission("orders.view")
def admin_orders():
    status_filter = request.args.get("status", "")
    mode_filter = request.args.get("mode", "")
    q = (request.args.get("q") or "").strip()
    conn = db.get_db()
    clauses = []
    params = []
    if status_filter:
        clauses.append("status = ?")
        params.append(status_filter)
    if mode_filter == "test":
        clauses.append("COALESCE(payment_mode, 'gateway') = 'test'")
    elif mode_filter == "gateway":
        clauses.append("COALESCE(payment_mode, 'gateway') != 'test'")
    if q:
        clauses.append("(order_ref LIKE ? OR customer_name LIKE ? OR customer_email LIKE ? OR customer_phone LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like, like]
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    orders = conn.execute(f"SELECT * FROM orders {where} ORDER BY id DESC", params).fetchall()
    conn.close()
    now_iso = datetime.now(timezone.utc).isoformat()
    return render_template("admin/orders.html", orders=orders, status_filter=status_filter, mode_filter=mode_filter, q=q, now_iso=now_iso)


@app.route("/admin/orders/<int:order_id>")
@login_required
@requires_permission("orders.view")
def admin_order_detail(order_id):
    conn = db.get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    order_items = conn.execute(
        "SELECT * FROM order_items WHERE order_id = ?", (order_id,)
    ).fetchall() if order else []
    # Grab product delivery_content_type for preview rendering
    product = None
    if order and order_items:
        pid = order_items[0]["product_id"]
        product = conn.execute("SELECT id, name, delivery_content_type, delivery_mode FROM products WHERE id = ?", (pid,)).fetchone()
    elif order:
        product = conn.execute("SELECT id, name, delivery_content_type, delivery_mode FROM products WHERE id = ?", (order["product_id"],)).fetchone()
    product_files = []
    if product:
        product_files = conn.execute(
            "SELECT id, original_name, filename FROM product_files WHERE product_id = ? ORDER BY id ASC",
            (product["id"],),
        ).fetchall()
    conn.close()
    if not order:
        abort(404)
    return render_template("admin/order_detail.html", order=order, order_items=order_items,
                            email_enabled=email_enabled(), product=product, product_files=product_files)


@app.route("/admin/orders/<int:order_id>/deliver", methods=["POST"])
@login_required
@requires_permission("orders.view")
def admin_order_deliver(order_id):
    check_csrf()
    message = request.form.get("delivery_message", "").strip()
    conn = db.get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        abort(404)
    order_items = conn.execute(
        "SELECT * FROM order_items WHERE order_id = ?", (order_id,)
    ).fetchall()
    conn.execute(
        "UPDATE orders SET status = 'delivered', delivery_message = ?, delivered_at = ? WHERE id = ?",
        (message, db.now(), order_id),
    )
    conn.commit()
    conn.close()

    if email_enabled():
        item_line = order["product_name"] if not order_items else ", ".join(
            f"{it['product_name']} x{it['quantity']}" for it in order_items
        )
        send_email(
            order["customer_email"],
            f"Your order {order['order_ref']} has been delivered",
            f"Hi {order['customer_name']},\n\n"
            f"Great news — your order for \"{item_line}\" is ready!\n\n"
            f"{message}\n\n"
            f"Order reference: {order['order_ref']}\n\nThank you for shopping with us.",
        )
    # SMS notification if customer phone available
    if order.get("customer_phone") and twilio_enabled():
        try:
            send_sms(order["customer_phone"],
                     f"Your order {order['order_ref']} is ready! Check your email for details.")
        except Exception:
            pass
    flash("✅ Order delivered! Customer has been notified." if email_enabled()
          else "✅ Order marked as delivered. Share the details with the customer directly "
               "(email sending isn't set up).", "success")
    return redirect(url_for("admin_order_detail", order_id=order_id))


@app.route("/admin/orders/<int:order_id>/invoice")
@login_required
@requires_permission("orders.view")
def admin_order_invoice(order_id):
    """Generate and download a PDF invoice for a paid or delivered order."""
    conn = db.get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order or order["status"] in ("created", "cancelled"):
        conn.close()
        if not order:
            abort(404)
        flash("Invoice is only available for paid/delivered orders.", "info")
        return redirect(url_for("admin_order_detail", order_id=order_id))
    order_items = conn.execute(
        "SELECT * FROM order_items WHERE order_id = ?", (order_id,),
    ).fetchall()
    product_ids = {item["product_id"] for item in order_items}
    product_map = {}
    if product_ids:
        rows = conn.execute(
            f"SELECT id, name FROM products WHERE id IN ({','.join('?' for _ in product_ids)})",
            list(product_ids),
        ).fetchall()
        product_map = {r["id"]: dict(r) for r in rows}
    conn.close()
    pdf_bytes, filename = invoicing.generate_and_save_invoice(
        order, order_items, product_map, get_settings(),
    )
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/admin/orders/<int:order_id>/cancel", methods=["POST"])
@login_required
@requires_permission("orders.refund")
def admin_order_cancel(order_id):
    check_csrf()
    refund_amount = request.form.get("refund_amount", "").strip()
    conn = db.get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        abort(404)
    refund_amt = 0
    try:
        refund_amt = int(refund_amount) if refund_amount else 0
    except (ValueError, TypeError):
        refund_amt = 0
    if refund_amt < 0:
        refund_amt = 0
    if refund_amt > order["amount"]:
        refund_amt = order["amount"]

    # ── Call Razorpay refund API before updating local state ──
    payment_id = order["razorpay_payment_id"] if order["razorpay_payment_id"] else None
    razorpay_refund_id = None
    refund_failed = False
    if refund_amt > 0 and payment_id and rzp.is_configured():
        try:
            resp = rzp.create_refund(payment_id, refund_amt * 100)
            razorpay_refund_id = resp.get("id", "")
        except Exception as exc:
            log_admin_action(
                "order_refund_failed", order["order_ref"],
                f"Razorpay refund call failed for ₹{refund_amt:,}: {exc}",
            )
            refund_failed = True
            flash(
                f"Razorpay refund call failed — the order was NOT cancelled. "
                f"Error: {exc}. Check the Razorpay dashboard and try again after fixing the issue.",
                "error",
            )
    if refund_failed:
        conn.close()
        return redirect(url_for("admin_order_detail", order_id=order_id))

    conn.execute(
        "UPDATE orders SET status = 'cancelled', refunded_amount = ?, refunded_at = ?, "
        "razorpay_refund_id = ? WHERE id = ?",
        (refund_amt, db.now() if refund_amt > 0 else None, razorpay_refund_id, order_id),
    )
    conn.commit()
    conn.close()
    log_admin_action("order_refund", order["order_ref"], f"Refunded ₹{refund_amt:,} via Razorpay ({razorpay_refund_id or 'none'})")
    if refund_amt > 0:
        flash(f"Order cancelled with ₹{refund_amt:,} refunded via Razorpay.", "success")
    else:
        flash("Order cancelled. No refund was recorded.", "success")
    return redirect(url_for("admin_order_detail", order_id=order_id))


@app.route("/admin/orders/bulk-deliver", methods=["POST"])
@login_required
@requires_permission("orders.view")
def admin_orders_bulk_deliver():
    check_csrf()
    conn = db.get_db()
    orders = conn.execute(
        "SELECT * FROM orders WHERE status = 'paid' ORDER BY id ASC"
    ).fetchall()
    delivered = 0
    for order in orders:
        order_items = conn.execute(
            "SELECT * FROM order_items WHERE order_id = ?", (order["id"],)
        ).fetchall()
        message = _maybe_auto_deliver(conn, order, order_items)
        if message is not None:
            delivered += 1
    conn.commit()
    conn.close()
    flash(f"Delivered {delivered} order{'s' if delivered != 1 else ''} automatically.", "success")
    if delivered < len(orders):
        flash(f"{len(orders) - delivered} order{'s' if (len(orders) - delivered) != 1 else ''} need{'s' if (len(orders) - delivered) == 1 else ''} manual delivery (not digital products).", "warning")
    return redirect(url_for("admin_orders", status="paid"))
  
  
# ============================================================= ADMIN: COUPONS
  
@app.route("/admin/coupons")
@login_required
@requires_permission("coupons.manage")
def admin_coupons():
    conn = db.get_db()
    coupons = conn.execute("SELECT * FROM coupons ORDER BY id DESC").fetchall()
    products = conn.execute("SELECT id, name FROM products ORDER BY name ASC").fetchall()
    conn.close()
    now_iso = datetime.now(timezone.utc).isoformat()
    return render_template("admin/coupons.html", coupons=coupons, products=products, now_iso=now_iso)


@app.route("/admin/coupons/save", methods=["POST"])
@login_required
@requires_permission("coupons.manage")
def admin_coupons_save():
    check_csrf()
    code = request.form.get("code", "").strip().upper()
    discount_type = request.form.get("discount_type", "percent")
    discount_value_raw = request.form.get("discount_value", "0").strip()
    usage_limit_raw = request.form.get("usage_limit", "").strip()
    active = 1 if request.form.get("active") else 0

    # Automatic coupon fields
    auto_apply = 1 if request.form.get("auto_apply") else 0
    trigger_type = request.form.get("trigger_type", "manual")
    if trigger_type not in ("manual", "cart_threshold", "product_specific", "customer_segment", "url_driven"):
        trigger_type = "manual"
    min_cart_value_raw = request.form.get("min_cart_value", "").strip()
    target_product_id_raw = request.form.get("target_product_id", "").strip()
    customer_segment = request.form.get("customer_segment", "all")
    if customer_segment not in ("all", "new_user", "logged_in"):
        customer_segment = "all"
    starts_at = request.form.get("starts_at", "").strip() or None
    expires_at = request.form.get("expires_at", "").strip() or None
    # Convert datetime-local inputs to ISO format for storage
    if starts_at:
        try:
            dt = datetime.fromisoformat(starts_at)
            starts_at = dt.isoformat()
        except ValueError:
            starts_at = None
    if expires_at:
        try:
            dt = datetime.fromisoformat(expires_at)
            expires_at = dt.isoformat()
        except ValueError:
            expires_at = None
    code = (request.form.get("code") or "").strip().upper()
    if not code:
        flash("Please enter a coupon code.", "error")
        return redirect(url_for("admin_coupons"))
    if len(code) > 50:
        flash("Coupon code is too long (max 50 characters).", "error")
        return redirect(url_for("admin_coupons"))
    # Restrict coupon codes to safe characters only (no spaces/symbols that
    # could break the ?coupon=CODE URL-driven flow or be awkward to type/share).
    import re as _re
    if not _re.match(r'^[A-Z0-9_-]+$', code):
        flash("Coupon code may only contain letters, numbers, hyphens, and underscores.", "error")
        return redirect(url_for("admin_coupons"))
    try:
        discount_value = int(discount_value_raw)
        if discount_value <= 0:
            raise ValueError
        if discount_type == "percent" and discount_value > 100:
            raise ValueError
    except ValueError:
        flash("Please enter a valid discount amount.", "error")
        return redirect(url_for("admin_coupons"))

    usage_limit = int(usage_limit_raw) if usage_limit_raw.isdigit() else None
    min_cart_value = int(min_cart_value_raw) if min_cart_value_raw.isdigit() else None
    target_product_id = int(target_product_id_raw) if target_product_id_raw.isdigit() else None

    # If auto_apply is checked, set trigger_type appropriately
    if auto_apply and trigger_type == "manual":
        trigger_type = "cart_threshold"  # sensible default

    conn = db.get_db()
    try:
        conn.execute(
            """INSERT INTO coupons
               (code, discount_type, discount_value, active, usage_limit, created_at,
                auto_apply, trigger_type, min_cart_value, target_product_id,
                customer_segment, starts_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (code, discount_type, discount_value, active, usage_limit, db.now(),
             auto_apply, trigger_type, min_cart_value, target_product_id,
             customer_segment, starts_at, expires_at),
        )
        conn.commit()
        flash(f"Coupon {code} created.", "success")
    except Exception:
        flash("A coupon with that code already exists.", "error")
    conn.close()
    return redirect(url_for("admin_coupons"))


@app.route("/admin/coupons/toggle/<int:coupon_id>", methods=["POST"])
@login_required
@requires_permission("coupons.manage")
def admin_coupons_toggle(coupon_id):
    check_csrf()
    conn = db.get_db()
    conn.execute("UPDATE coupons SET active = 1 - active WHERE id = ?", (coupon_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_coupons"))


@app.route("/admin/coupons/delete/<int:coupon_id>", methods=["POST"])
@login_required
@requires_permission("coupons.manage")
def admin_coupons_delete(coupon_id):
    check_csrf()
    conn = db.get_db()
    conn.execute("DELETE FROM coupons WHERE id = ?", (coupon_id,))
    conn.commit()
    conn.close()
    log_admin_action("coupon_delete", f"coupon_id={coupon_id}")
    flash("Coupon deleted.", "success")
    return redirect(url_for("admin_coupons"))


# ============================================================= ADMIN: STOCK REQUESTS (waitlist)

@app.route("/admin/stock-requests")
@login_required
@requires_permission("inventory.manage")
def admin_stock_requests():
    conn = db.get_db()
    requests = conn.execute(
        """SELECT sr.*, p.name AS product_name, p.quantity AS product_qty
           FROM stock_requests sr
           JOIN products p ON sr.product_id = p.id
           ORDER BY sr.notified ASC, sr.created_at DESC"""
    ).fetchall()
    conn.close()
    return render_template("admin/stock_requests.html", requests=requests)


@app.route("/admin/stock-requests/notify/<int:request_id>", methods=["POST"])
@login_required
@requires_permission("inventory.manage")
def admin_stock_request_notify(request_id):
    check_csrf()
    conn = db.get_db()
    req = conn.execute(
        "SELECT sr.*, p.name AS pname FROM stock_requests sr JOIN products p ON sr.product_id = p.id WHERE sr.id = ?",
        (request_id,),
    ).fetchone()
    if not req:
        conn.close()
        flash("Request not found.", "error")
        return redirect(url_for("admin_stock_requests"))
    if req["notified"]:
        conn.close()
        flash("Already notified.", "info")
        return redirect(url_for("admin_stock_requests"))
    subject = f"{req['pname']} is back in stock!"
    body = f"Hi {req['customer_name'] or 'there'},\n\nGood news — '{req['pname']}' is back in stock at {get_settings().get('site_name', 'our store')}!\n\nCheck it out: {request.url_root}product/{req['product_id']}"
    send_email(req["customer_email"], subject, body)
    conn.execute("UPDATE stock_requests SET notified = 1, notified_at = ? WHERE id = ?", (db.now(), request_id))
    conn.commit()
    conn.close()
    log_admin_action("stock_notify", f"request_id={request_id}", f"Emailed {req['customer_email']} about {req['pname']}")
    flash(f"Notified {req['customer_email']} that {req['pname']} is back.", "success")
    return redirect(url_for("admin_stock_requests"))


@app.route("/admin/stock-requests/notify-all/<int:product_id>", methods=["POST"])
@login_required
@requires_permission("inventory.manage")
def admin_stock_request_notify_all(product_id):
    check_csrf()
    conn = db.get_db()
    product = conn.execute("SELECT id, name FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        conn.close()
        flash("Product not found.", "error")
        return redirect(url_for("admin_stock_requests"))
    unotified = conn.execute(
        "SELECT * FROM stock_requests WHERE product_id = ? AND notified = 0", (product_id,)
    ).fetchall()
    site_name = get_settings().get("site_name", "Virtual Store")
    product_url = request.url_root + f"product/{product_id}"
    count = 0
    for req in unotified:
        subject = f"{product['name']} is back in stock!"
        body = f"Hi {req['customer_name'] or 'there'},\n\nGood news — '{product['name']}' is back in stock at {site_name or 'our store'}!\n\nCheck it out: {product_url}"
        send_email(req["customer_email"], subject, body)
        conn.execute("UPDATE stock_requests SET notified = 1, notified_at = ? WHERE id = ?", (db.now(), req["id"]))
        count += 1
    conn.commit()
    conn.close()
    log_admin_action("stock_notify_all", f"product_id={product_id}", f"Emailed {count} customers about {product['name']}")
    flash(f"Notified {count} customer{'s' if count != 1 else ''} that {product['name']} is back.", "success")
    return redirect(url_for("admin_stock_requests"))


# ============================================================= ADMIN: TESTIMONIALS

@app.route("/admin/testimonials")
@login_required
@requires_permission("content.manage")
def admin_testimonials():
    conn = db.get_db()
    testimonials = conn.execute("SELECT * FROM testimonials ORDER BY position ASC").fetchall()
    conn.close()
    return render_template("admin/testimonials.html", testimonials=testimonials)


@app.route("/admin/testimonials/save", methods=["POST"])
@login_required
@requires_permission("content.manage")
def admin_testimonials_save():
    check_csrf()
    testimonial_id = request.form.get("id")
    customer_name = request.form.get("customer_name", "").strip()
    quote = request.form.get("quote", "").strip()
    rating = request.form.get("rating", "5")
    visible = 1 if request.form.get("visible") else 0

    if not customer_name or not quote:
        flash("Please fill in both a name and a quote.", "error")
        return redirect(url_for("admin_testimonials"))

    conn = db.get_db()
    if testimonial_id:
        conn.execute(
            "UPDATE testimonials SET customer_name=?, quote=?, rating=?, visible=? WHERE id=?",
            (customer_name, quote, rating, visible, testimonial_id),
        )
    else:
        max_pos = conn.execute("SELECT COALESCE(MAX(position), -1) m FROM testimonials").fetchone()["m"]
        conn.execute(
            "INSERT INTO testimonials (customer_name, quote, rating, position, visible) VALUES (?, ?, ?, ?, ?)",
            (customer_name, quote, rating, max_pos + 1, visible),
        )
    conn.commit()
    conn.close()
    flash("Testimonial saved.", "success")
    return redirect(url_for("admin_testimonials"))


@app.route("/admin/testimonials/delete/<int:testimonial_id>", methods=["POST"])
@login_required
@requires_permission("content.manage")
def admin_testimonials_delete(testimonial_id):
    check_csrf()
    conn = db.get_db()
    t_row = conn.execute("SELECT customer_name FROM testimonials WHERE id = ?", (testimonial_id,)).fetchone()
    t_name = t_row["customer_name"] if t_row else str(testimonial_id)
    conn.execute("DELETE FROM testimonials WHERE id = ?", (testimonial_id,))
    conn.commit()
    conn.close()
    log_admin_action("testimonial_delete", t_name)
    flash("Testimonial removed.", "success")
    return redirect(url_for("admin_testimonials"))


# ============================================================= ADMIN: FAQS

@app.route("/admin/faqs")
@login_required
@requires_permission("content.manage")
def admin_faqs():
    conn = db.get_db()
    faqs = conn.execute("SELECT * FROM faqs ORDER BY position ASC").fetchall()
    conn.close()
    return render_template("admin/faqs.html", faqs=faqs)


@app.route("/admin/faqs/save", methods=["POST"])
@login_required
@requires_permission("content.manage")
def admin_faqs_save():
    check_csrf()
    faq_id = request.form.get("id")
    question = request.form.get("question", "").strip()
    answer = request.form.get("answer", "").strip()
    visible = 1 if request.form.get("visible") else 0

    if not question or not answer:
        flash("Please fill in both the question and the answer.", "error")
        return redirect(url_for("admin_faqs"))

    conn = db.get_db()
    if faq_id:
        conn.execute(
            "UPDATE faqs SET question=?, answer=?, visible=? WHERE id=?",
            (question, answer, visible, faq_id),
        )
    else:
        max_pos = conn.execute("SELECT COALESCE(MAX(position), -1) m FROM faqs").fetchone()["m"]
        conn.execute(
            "INSERT INTO faqs (question, answer, position, visible) VALUES (?, ?, ?, ?)",
            (question, answer, max_pos + 1, visible),
        )
    conn.commit()
    conn.close()
    flash("FAQ saved.", "success")
    return redirect(url_for("admin_faqs"))


@app.route("/admin/faqs/delete/<int:faq_id>", methods=["POST"])
@login_required
@requires_permission("content.manage")
def admin_faqs_delete(faq_id):
    check_csrf()
    conn = db.get_db()
    f_row = conn.execute("SELECT question FROM faqs WHERE id = ?", (faq_id,)).fetchone()
    f_q = f_row["question"] if f_row else str(faq_id)
    conn.execute("DELETE FROM faqs WHERE id = ?", (faq_id,))
    conn.commit()
    conn.close()
    log_admin_action("faq_delete", f_q[:80])
    flash("FAQ removed.", "success")
    return redirect(url_for("admin_faqs"))


# ============================================================= ADMIN: NEWSLETTER

@app.route("/admin/newsletter")
@login_required
@requires_permission("newsletter.view", "marketing.manage")
def admin_newsletter():
    conn = db.get_db()
    subscribers = conn.execute(
        "SELECT * FROM newsletter_subscribers ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return render_template("admin/newsletter.html", subscribers=subscribers)


def _csv_safe(value):
    """Sanitize a cell value against CSV/formula injection. If the value
    starts with =, +, -, @, tab, or CR, prefix it with a single quote so
    Excel/Sheets treats it as text rather than a formula (CWE-1236)."""
    if value is None:
        return ""
    s = str(value)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


@app.route("/admin/newsletter/export.csv")
@login_required
@requires_permission("newsletter.view", "marketing.manage")
def admin_newsletter_export():
    import csv
    import io
    conn = db.get_db()
    subscribers = conn.execute(
        "SELECT email, created_at FROM newsletter_subscribers ORDER BY id DESC"
    ).fetchall()
    conn.close()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Email", "Subscribed At"])
    for s in subscribers:
        writer.writerow([_csv_safe(s["email"]), _csv_safe(s["created_at"])])
    return buf.getvalue(), 200, {
        "Content-Type": "text/csv",
        "Content-Disposition": "attachment; filename=newsletter_subscribers.csv",
    }


# ============================================================= ADMIN: ORDERS EXPORT

@app.route("/admin/orders/export.csv")
@login_required
@requires_permission("orders.export")
def admin_orders_export():
    import csv
    import io
    conn = db.get_db()
    orders = conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
    all_items = conn.execute("SELECT * FROM order_items ORDER BY order_id ASC").fetchall()
    conn.close()
    items_by_order = {}
    for it in all_items:
        items_by_order.setdefault(it["order_id"], []).append(it)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Order Ref", "Items", "Customer Name", "Email", "Phone", "Amount",
        "Coupon", "Discount", "Status", "Created", "Paid", "Delivered",
    ])
    for o in orders:
        items = items_by_order.get(o["id"])
        item_summary = o["product_name"] if not items else "; ".join(
            f"{it['product_name']} x{it['quantity']}" for it in items
        )
        writer.writerow([
            _csv_safe(o["order_ref"]), _csv_safe(item_summary), _csv_safe(o["customer_name"]), _csv_safe(o["customer_email"]),
            _csv_safe(o["customer_phone"]), o["amount"], _csv_safe(o["coupon_code"]), o["discount_amount"],
            o["status"], _csv_safe(o["created_at"]), _csv_safe(o["paid_at"]), _csv_safe(o["delivered_at"]),
        ])
    return buf.getvalue(), 200, {
        "Content-Type": "text/csv",
        "Content-Disposition": "attachment; filename=orders.csv",
    }


# ============================================================= ADMIN: ACCOUNT

@app.route("/admin/account", methods=["GET", "POST"])
@login_required
def admin_account():
    if request.method == "POST":
        check_csrf()
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        conn = db.get_db()
        user = conn.execute("SELECT * FROM admin_users WHERE id = ?", (session["admin_id"],)).fetchone()

        if not check_password_hash(user["password_hash"], current):
            flash("Current password is incorrect.", "error")
        elif len(new) < 8:
            flash("New password must be at least 8 characters.", "error")
        elif new != confirm:
            flash("New passwords do not match.", "error")
        else:
            conn.execute(
                "UPDATE admin_users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(new), user["id"]),
            )
            conn.commit()
            # Rotate the session after password change so any other active
            # admin sessions (e.g. from a compromised credential) are forced
            # to re-authenticate.
            session.clear()
            session["admin_id"] = user["id"]
            session["admin_username"] = user["username"]
            flash("Password updated. For security, your session has been refreshed.", "success")
        conn.close()
        return redirect(url_for("admin_account"))
    return render_template("admin/account.html")


# ============================================================= ADMIN: AUDIT LOG

@app.route("/admin/audit-log")
@login_required
@requires_permission("audit.view")
def admin_audit_log():
    conn = db.get_db()
    page = request.args.get("page", 1, type=int)
    per_page = 50
    action_filter = (request.args.get("action") or "").strip()
    from_date = (request.args.get("from") or "").strip()
    to_date = (request.args.get("to") or "").strip()

    where_clauses = []
    params = []

    if action_filter:
        where_clauses.append("aal.action = ?")
        params.append(action_filter)
    if from_date:
        where_clauses.append("aal.created_at >= ?")
        params.append(from_date)
    if to_date:
        where_clauses.append("aal.created_at <= ?")
        params.append(to_date)

    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

    offset = (page - 1) * per_page

    # Count
    total_row = conn.execute(
        f"SELECT COUNT(*) AS c FROM admin_audit_log aal {where_sql}", params
    ).fetchone()
    total = total_row["c"] if total_row else 0

    # Fetch entries
    fetch_params = params + [per_page, offset]
    entries = conn.execute(
        f"""SELECT aal.*, au.username
            FROM admin_audit_log aal
            LEFT JOIN admin_users au ON aal.admin_id = au.id
            {where_sql}
            ORDER BY aal.created_at DESC LIMIT ? OFFSET ?""",
        fetch_params,
    ).fetchall()

    # Distinct actions for filter dropdown
    actions = conn.execute(
        "SELECT DISTINCT action FROM admin_audit_log ORDER BY action ASC"
    ).fetchall()

    conn.close()

    total_pages = max(1, (total + per_page - 1) // per_page)

    return render_template(
        "admin/audit_log.html",
        entries=entries,
        page=page,
        total_pages=total_pages,
        total=total,
        action_filter=action_filter,
        from_date=from_date,
        to_date=to_date,
        actions=[r["action"] for r in actions],
    )


@app.route("/admin/audit-log/export")
@login_required
@requires_permission("audit.export")
def admin_audit_log_export():
    import csv
    import io

    conn = db.get_db()
    action_filter = (request.args.get("action") or "").strip()
    from_date = (request.args.get("from") or "").strip()
    to_date = (request.args.get("to") or "").strip()

    where_clauses = []
    params = []

    if action_filter:
        where_clauses.append("aal.action = ?")
        params.append(action_filter)
    if from_date:
        where_clauses.append("aal.created_at >= ?")
        params.append(from_date)
    if to_date:
        where_clauses.append("aal.created_at <= ?")
        params.append(to_date)

    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

    entries = conn.execute(
        f"""SELECT aal.*, au.username
            FROM admin_audit_log aal
            LEFT JOIN admin_users au ON aal.admin_id = au.id
            {where_sql}
            ORDER BY aal.created_at DESC""",
        params,
    ).fetchall()
    conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ID", "Admin", "Action", "Target", "Details", "Timestamp"])
    for e in entries:
        writer.writerow([
            e["id"],
            _csv_safe(e["username"] or "unknown"),
            _csv_safe(e["action"]),
            _csv_safe(e["target"]),
            _csv_safe(e["details"]),
            _csv_safe(e["created_at"]),
        ])

    return buf.getvalue(), 200, {
        "Content-Type": "text/csv",
        "Content-Disposition": "attachment; filename=audit-log.csv",
    }


# ============================================================= ADMIN: TICKETS

@app.route("/admin/tickets")
@login_required
@requires_permission("tickets.create")
def admin_tickets():
    """List admin tickets. Master sees all; sub-admins see only their own."""
    conn = db.get_db()
    admin_id = session.get("admin_id")
    admin_perms = session.get("admin_permissions", [])
    is_master = "*" in admin_perms

    if is_master:
        tickets = conn.execute(
            """SELECT t.*, au.username AS creator_name
               FROM admin_tickets t
               LEFT JOIN admin_users au ON t.admin_id = au.id
               ORDER BY t.created_at DESC"""
        ).fetchall()
    else:
        tickets = conn.execute(
            """SELECT t.*, au.username AS creator_name
               FROM admin_tickets t
               LEFT JOIN admin_users au ON t.admin_id = au.id
               WHERE t.admin_id = ?
               ORDER BY t.created_at DESC""",
            (admin_id,),
        ).fetchall()

    conn.close()
    return render_template("admin/tickets.html", tickets=tickets, is_master=is_master)


@app.route("/admin/tickets/new", methods=["POST"])
@login_required
@requires_permission("tickets.create")
def admin_tickets_new():
    """Create a new ticket (sub-admins only — master cannot create tickets)."""
    check_csrf()
    admin_perms = session.get("admin_permissions", [])
    if "*" in admin_perms:
        flash("Master admins cannot create tickets.", "error")
        return redirect(url_for("admin_tickets"))

    admin_id = session.get("admin_id")
    category = (request.form.get("category") or "other").strip()
    title = (request.form.get("title") or "").strip()
    description = (request.form.get("description") or "").strip()

    valid_categories = {"feature_request", "bug_report", "content_update", "permission_request", "general", "other"}
    if category not in valid_categories:
        category = "other"

    if not title or not description:
        flash("Please fill in both title and description.", "error")
        return redirect(url_for("admin_tickets"))

    conn = db.get_db()
    conn.execute(
        "INSERT INTO admin_tickets (admin_id, category, title, description, status, created_at) VALUES (?, ?, ?, ?, 'open', ?)",
        (admin_id, category, title, description, db.now()),
    )
    conn.commit()
    conn.close()
    log_admin_action("ticket_created", f"category={category}", f"Title: {title[:80]}")
    flash("Ticket submitted successfully. Master admin has been notified (if connected).", "success")
    return redirect(url_for("admin_tickets"))


@app.route("/admin/tickets/<int:ticket_id>/status", methods=["POST"])
@login_required
@requires_permission("tickets.create")
def admin_tickets_status(ticket_id):
    """Update ticket status and add admin note (master only)."""
    check_csrf()
    admin_perms = session.get("admin_permissions", [])
    if "*" not in admin_perms:
        flash("Only the master admin can update ticket status.", "error")
        return redirect(url_for("admin_tickets"))

    conn = db.get_db()
    ticket = conn.execute("SELECT * FROM admin_tickets WHERE id = ?", (ticket_id,)).fetchone()
    if not ticket:
        conn.close()
        abort(404)

    status = (request.form.get("status") or "").strip()
    admin_note = (request.form.get("admin_note") or "").strip()

    valid_statuses = {"open", "in_progress", "resolved", "closed"}
    if status not in valid_statuses:
        conn.close()
        flash("Invalid status.", "error")
        return redirect(url_for("admin_tickets"))

    resolved_at = None
    if status in ("resolved", "closed") and ticket["status"] not in ("resolved", "closed"):
        resolved_at = db.now()

    conn.execute(
        "UPDATE admin_tickets SET status = ?, admin_note = ?, resolved_at = COALESCE(?, resolved_at) WHERE id = ?",
        (status, admin_note, resolved_at, ticket_id),
    )
    conn.commit()
    conn.close()
    log_admin_action("ticket_status", f"ticket_id={ticket_id}", f"Status: {status}, note: {admin_note[:100]}")
    flash(f"Ticket #{ticket_id} updated to '{status}'.", "success")
    return redirect(url_for("admin_tickets"))


@app.route("/admin/tickets/<int:ticket_id>/reply", methods=["POST"])
@login_required
@requires_permission("tickets.create")
def admin_tickets_reply(ticket_id):
    """Add a reply to a ticket. Both master and sub-admins can reply."""
    check_csrf()
    admin_id = session.get("admin_id")
    reply_text = (request.form.get("reply_text") or "").strip()
    if not reply_text:
        flash("Reply cannot be empty.", "error")
        return redirect(url_for("admin_tickets"))
    
    conn = db.get_db()
    ticket = conn.execute("SELECT * FROM admin_tickets WHERE id = ?", (ticket_id,)).fetchone()
    if not ticket:
        conn.close()
        abort(404)
    
    # If ticket is resolved/closed, reopen
    if ticket["status"] in ("resolved", "closed"):
        conn.execute("UPDATE admin_tickets SET status = 'in_progress' WHERE id = ?", (ticket_id,))
    
    conn.execute(
        "INSERT INTO admin_ticket_replies (ticket_id, admin_id, reply_text, created_at) VALUES (?, ?, ?, ?)",
        (ticket_id, admin_id, reply_text, db.now()),
    )
    conn.commit()
    conn.close()
    log_admin_action("ticket_reply", f"ticket_id={ticket_id}", f"Reply: {reply_text[:100]}")
    flash("Reply added.", "success")
    return redirect(url_for("admin_tickets"))


# ============================================================= ADMIN: TEAM MANAGEMENT (master only)

@app.route("/admin/team", methods=["GET"])
@login_required
@requires_permission("admin.manage")
def admin_team():
    """List all admin users as cards. Only master (admin.manage) can access."""
    conn = db.get_db()
    admins = conn.execute(
        "SELECT id, username, role, permissions, is_active, created_at FROM admin_users ORDER BY id ASC"
    ).fetchall()
    conn.close()
    new_creds = request.args.get("_new_creds")
    if new_creds:
        try:
            new_creds = json.loads(bytes.fromhex(new_creds).decode())
        except Exception:
            new_creds = None
    return render_template("admin/team.html", admins=admins, new_creds=new_creds)


@app.route("/admin/team/add", methods=["POST"])
@login_required
@requires_permission("admin.manage")
def admin_team_add():
    """Create a new admin user with a preset or custom role."""
    check_csrf()
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password", "") or request.form.get("manual_password", "")
    role_preset = request.form.get("role_preset", "custom")

    if not username or len(password) < 8:
        flash("Username is required and password must be at least 8 characters.", "error")
        return redirect(url_for("admin_team"))

    if role_preset in PRESET_PERMISSIONS:
        permissions = json.dumps(PRESET_PERMISSIONS[role_preset])
        role_name = role_preset
    elif role_preset == "custom":
        try:
            raw = request.form.get("custom_permissions", "[]")
            perms_list = json.loads(raw)
            if not isinstance(perms_list, list):
                raise ValueError
            permissions = json.dumps(perms_list)
        except (ValueError, TypeError):
            flash("Invalid permissions JSON. Enter a valid list like [\"orders.view\", \"products.edit\"].", "error")
            return redirect(url_for("admin_team"))
        role_name = "custom"
    else:
        flash("Unknown role preset.", "error")
        return redirect(url_for("admin_team"))

    conn = db.get_db()
    try:
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, role, permissions, is_active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            (username, generate_password_hash(password), role_name, permissions, db.now()),
        )
        conn.commit()
        log_admin_action("admin_add", username, f"Role: {role_name}")
        flash(f"Admin '{username}' created with role '{role_name}'.", "success")
        # Pass generated password to template via query param
        encoded = json.dumps({"username": username, "password": password}).encode().hex()
        return redirect(url_for("admin_team", _new_creds=encoded))
    except Exception:
        flash(f"Username '{username}' already exists.", "error")
    finally:
        conn.close()
    return redirect(url_for("admin_team"))


@app.route("/admin/team/edit/<int:admin_id>", methods=["POST"])
@login_required
@requires_permission("admin.manage")
def admin_team_edit(admin_id):
    """Change role/permissions/active status of a sub-admin. Cannot edit master."""
    check_csrf()
    conn = db.get_db()
    target = conn.execute("SELECT * FROM admin_users WHERE id = ?", (admin_id,)).fetchone()
    if not target:
        conn.close()
        flash("Admin not found.", "error")
        return redirect(url_for("admin_team"))
    if target["role"] == "master":
        conn.close()
        flash("Cannot edit the master admin.", "error")
        return redirect(url_for("admin_team"))

    role_preset = request.form.get("role_preset", "custom")
    is_active = 1 if request.form.get("is_active") else 0

    if role_preset in PRESET_PERMISSIONS:
        permissions = json.dumps(PRESET_PERMISSIONS[role_preset])
        role_name = role_preset
    elif role_preset == "custom":
        try:
            raw = request.form.get("custom_permissions", "[]")
            perms_list = json.loads(raw)
            if not isinstance(perms_list, list):
                raise ValueError
            permissions = json.dumps(perms_list)
        except (ValueError, TypeError):
            conn.close()
            flash("Invalid permissions JSON.", "error")
            return redirect(url_for("admin_team"))
        role_name = "custom"
    else:
        role_name = target["role"]
        permissions = target["permissions"]

    conn.execute(
        "UPDATE admin_users SET role = ?, permissions = ?, is_active = ? WHERE id = ?",
        (role_name, permissions, is_active, admin_id),
    )
    conn.commit()
    conn.close()
    log_admin_action("admin_edit", target["username"], f"Role: {role_name}, active: {is_active}")
    flash(f"Admin '{target['username']}' updated.", "success")
    return redirect(url_for("admin_team"))


@app.route("/admin/team/toggle/<int:admin_id>", methods=["POST"])
@login_required
@requires_permission("admin.manage")
def admin_team_toggle(admin_id):
    """Suspend/unsuspend a sub-admin. Cannot toggle master."""
    check_csrf()
    conn = db.get_db()
    target = conn.execute("SELECT * FROM admin_users WHERE id = ?", (admin_id,)).fetchone()
    if not target:
        conn.close()
        flash("Admin not found.", "error")
        return redirect(url_for("admin_team"))
    if target["role"] == "master":
        conn.close()
        flash("Cannot suspend the master admin.", "error")
        return redirect(url_for("admin_team"))

    new_status = 1 - target["is_active"]
    conn.execute("UPDATE admin_users SET is_active = ? WHERE id = ?", (new_status, admin_id))
    conn.commit()
    conn.close()
    label = "activated" if new_status else "suspended"
    log_admin_action("admin_toggle", target["username"], f"New status: {label}")
    flash(f"Admin '{target['username']}' {label}.", "success")
    return redirect(url_for("admin_team"))


# ============================================================= WEBHOOKS / REPORTING

@app.route("/webhook/razorpay", methods=["POST"])
def razorpay_webhook():
    """Razorpay webhook handler — catches payment.captured events server-side
    so payment confirmation isn't solely dependent on the fragile client-side
    verify call. If the customer's tab crashes between payment success and the
    /api/verify-payment call, this endpoint still confirms the order.

    Configure in Razorpay Dashboard -> Settings -> Webhooks with the webhook
    URL and set RAZORPAY_WEBHOOK_SECRET in your environment to the webhook
    secret shown in the dashboard."""
    webhook_body = request.get_data()
    webhook_signature = request.headers.get("X-Razorpay-Signature", "")

    if not rzp.verify_webhook_signature(webhook_body, webhook_signature):
        return jsonify({"error": "Invalid webhook signature"}), 400

    import json as _json
    try:
        payload = _json.loads(webhook_body)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid JSON"}), 400

    event = payload.get("event", "")
    # Only handle payment.captured — other events are ignored (but acknowledged)
    if event != "payment.captured":
        return jsonify({"status": "ignored"}), 200

    payment = payload.get("payload", {}).get("payment", {}).get("entity", {})
    razorpay_order_id = payment.get("order_id", "")
    razorpay_payment_id = payment.get("id", "")

    if not razorpay_order_id:
        return jsonify({"status": "no order_id"}), 200

    conn = db.get_db()
    order = conn.execute(
        "SELECT * FROM orders WHERE razorpay_order_id = ?", (razorpay_order_id,)
    ).fetchone()

    if not order:
        conn.close()
        return jsonify({"status": "order not found"}), 200

    # Idempotency: if already paid/delivered, don't re-run side effects
    if order["status"] in ("paid", "delivered"):
        conn.close()
        return jsonify({"status": "already confirmed"}), 200

    # Mark as paid (same transition as /api/verify-payment, but without the
    # client-side signature — the webhook signature itself is the proof)
    order_items = conn.execute(
        "SELECT * FROM order_items WHERE order_id = ?", (order["id"],)
    ).fetchall()
    _confirm_order_payment(conn, order, order_items, payment_mode="gateway", razorpay_payment_id=razorpay_payment_id, razorpay_signature="")

    return jsonify({"status": "confirmed"}), 200


@app.route("/csp-report", methods=["POST"])
def csp_report():
    """Receive Content-Security-Policy violation reports (from the report-uri
    directive in the CSP header). Logs them for monitoring — no response body
    needed, the browser just needs a 204."""
    import logging as _logging
    try:
        report = request.get_json(silent=True) or {}
        _logging.getLogger("virtual_store").warning(
            "CSP violation: %s", report.get("csp-report", {})
        )
    except Exception:
        pass
    return "", 204


@app.route("/health", methods=["GET", "HEAD"])
def health():
    """Alias for Render monitoring and quick checks."""
    return "ok", 200


@app.route("/healthz", methods=["GET", "HEAD"])
def healthz():
    """Lightweight health-check endpoint for Render health checks and future
    monitoring."""
    return "ok", 200


@app.route("/set-timezone", methods=["POST"])
def set_timezone():
    """Receives client-side timezone offset (seconds from UTC) and stores in session."""
    check_csrf_api()
    data = request.get_json(silent=True)
    if data and "offset" in data:
        session["timezone_offset"] = int(data["offset"])
        session.modified = True
    return "ok"


@app.route("/google-test")
def google_test():
    """Isolated debug page for testing Firebase Google Sign-In.
    Only available in debug/dev mode."""
    if not config.DEBUG:
        abort(404)
    return render_template("google_test.html")


# Google Search Console site ownership verification — must be served at /google*.html
@app.route("/googlead21c3b32e52177a.html")
def google_verification():
    import os
    return send_file(os.path.join(app.static_folder, "googlead21c3b32e52177a.html"))


# Service worker for admin offline + push support — served from app root for scope
@app.route("/sw-admin.js")
def service_worker_admin():
    resp = send_from_directory("static", "sw-admin.js")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=config.DEBUG)
