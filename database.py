"""
Very small data layer on top of sqlite3 (built into Python — no ORM,
no extra dependency, easy to back up: it's a single .db file).

-- Performance monitoring tables
CREATE TABLE IF NOT EXISTS performance_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_type TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    value REAL NOT NULL,
    page_path TEXT,
    user_agent TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_perf_type ON performance_metrics(metric_type, created_at);
CREATE INDEX IF NOT EXISTS idx_perf_name ON performance_metrics(metric_name, created_at);

Set TURSO_DB_URL and TURSO_DB_AUTH_TOKEN to use a remote Turso database
instead of the local SQLite file. No other changes needed — everything
below uses the same SQLite-compatible API.
"""
import atexit
import hashlib
import logging
import os
import secrets
import sqlite3
import string
import sys
import time
from datetime import datetime, timezone
from werkzeug.security import generate_password_hash

import config

try:
    from flask import g, has_request_context
except Exception:
    g = None
    def has_request_context():
        return False

_db_logger = logging.getLogger("virtual_store.database")
_SLOW_QUERY_MS = int(os.environ.get("DB_SLOW_QUERY_MS", "250"))
_MIGRATION_TABLE = "schema_migrations"

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sections (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    title    TEXT NOT NULL,
    content  TEXT NOT NULL DEFAULT '',
    style    TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    visible  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS products (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    slug              TEXT UNIQUE NOT NULL,
    short_description TEXT NOT NULL DEFAULT '',
    description       TEXT NOT NULL DEFAULT '',
    price             INTEGER NOT NULL DEFAULT 0,
    category          TEXT NOT NULL DEFAULT '',
    active            INTEGER NOT NULL DEFAULT 1,
    position          INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    delivery_mode     TEXT NOT NULL DEFAULT 'manual',
    auto_delivery_content TEXT NOT NULL DEFAULT '',
    ribbon            TEXT NOT NULL DEFAULT '',
    compare_price     INTEGER,
    views             INTEGER NOT NULL DEFAULT 0,
    quantity          INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_products_active ON products(active);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
CREATE INDEX IF NOT EXISTS idx_products_active_category ON products(active, category);
CREATE INDEX IF NOT EXISTS idx_products_position ON products(position, id);

CREATE TABLE IF NOT EXISTS product_images (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    filename   TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_product_images_product_id ON product_images(product_id);

CREATE TABLE IF NOT EXISTS orders (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    order_ref          TEXT UNIQUE NOT NULL,
    product_id         INTEGER REFERENCES products(id),
    product_name       TEXT NOT NULL,
    customer_name      TEXT NOT NULL,
    customer_email     TEXT NOT NULL,
    customer_phone     TEXT NOT NULL DEFAULT '',
    amount             INTEGER NOT NULL,
    coupon_code        TEXT NOT NULL DEFAULT '',
    discount_amount    INTEGER NOT NULL DEFAULT 0,
    razorpay_order_id  TEXT,
    razorpay_payment_id TEXT,
    razorpay_signature TEXT,
    status             TEXT NOT NULL DEFAULT 'created',
    delivery_message   TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    paid_at            TEXT,
    delivered_at       TEXT,
    refunded_amount    INTEGER NOT NULL DEFAULT 0,
    refunded_at        TEXT,
    razorpay_refund_id TEXT
);

CREATE TABLE IF NOT EXISTS admin_users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'custom',
    permissions   TEXT NOT NULL DEFAULT '[]',
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS coupons (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    code           TEXT UNIQUE NOT NULL,
    discount_type  TEXT NOT NULL DEFAULT 'percent',   -- 'percent' or 'flat'
    discount_value INTEGER NOT NULL DEFAULT 0,
    active         INTEGER NOT NULL DEFAULT 1,
    usage_limit    INTEGER,                            -- NULL = unlimited
    used_count     INTEGER NOT NULL DEFAULT 0,
    max_per_customer INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    -- Automatic coupon system
    auto_apply        INTEGER NOT NULL DEFAULT 0,      -- 0 = manual, 1 = auto-applies
    trigger_type      TEXT NOT NULL DEFAULT 'manual',  -- manual, cart_threshold, product_specific, customer_segment, url_driven
    min_cart_value    INTEGER,                         -- for cart_threshold trigger
    target_product_id INTEGER REFERENCES products(id), -- for product_specific trigger
    customer_segment  TEXT NOT NULL DEFAULT 'all',     -- all, new_user, logged_in
    starts_at         TEXT,                            -- ISO datetime, coupon active from
    expires_at        TEXT                             -- ISO datetime, coupon auto-expires at
);

CREATE TABLE IF NOT EXISTS testimonials (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name TEXT NOT NULL,
    quote         TEXT NOT NULL,
    rating        INTEGER NOT NULL DEFAULT 5,
    position      INTEGER NOT NULL DEFAULT 0,
    visible       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS faqs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    answer   TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    visible  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS newsletter_subscribers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    firebase_uid  TEXT UNIQUE,
    phone         TEXT UNIQUE NOT NULL DEFAULT '',
    name          TEXT NOT NULL DEFAULT '',
    email         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    last_login_at TEXT,
    session_token_version INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS otps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    phone      TEXT NOT NULL,
    code       TEXT NOT NULL,
    name       TEXT NOT NULL DEFAULT '',
    email      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS order_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id   INTEGER REFERENCES products(id),
    product_name TEXT NOT NULL,
    unit_price   INTEGER NOT NULL,
    quantity     INTEGER NOT NULL DEFAULT 1,
    line_total   INTEGER NOT NULL
);

-- ---- Android admin app support ----
-- One row per phone that has registered for push notifications. A single
-- admin user can have several devices (e.g. a phone and a tablet); each
-- gets its own FCM token row so pushes go to all of them.
CREATE TABLE IF NOT EXISTS admin_devices (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_user_id INTEGER NOT NULL REFERENCES admin_users(id),
    fcm_token     TEXT UNIQUE NOT NULL,
    device_label  TEXT NOT NULL DEFAULT '',
    platform      TEXT NOT NULL DEFAULT 'android',
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT
);

-- Opaque bearer tokens for the mobile app's /api/admin/* endpoints. We store
-- a hash (never the raw token) so a leaked database dump can't be replayed
-- as a live session, and each row can be revoked independently (e.g. "sign
-- out this device" from the web admin panel without affecting other devices).
CREATE TABLE IF NOT EXISTS api_tokens (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_user_id INTEGER NOT NULL REFERENCES admin_users(id),
    token_hash    TEXT UNIQUE NOT NULL,
    device_label  TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    revoked       INTEGER NOT NULL DEFAULT 0
);
"""


# Safe, additive migrations
# Each entry is (table, column, "ALTER TABLE ... " statement). Errors from
# already-applied migrations (duplicate column) are ignored on purpose.
MIGRATIONS = [
    "ALTER TABLE products ADD COLUMN category TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE sections ADD COLUMN style TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE orders ADD COLUMN coupon_code TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE orders ADD COLUMN discount_amount INTEGER NOT NULL DEFAULT 0",
    # 'manual' = admin reviews and delivers by hand (default, unchanged behaviour).
    # 'automatic' = the moment payment is confirmed, auto_delivery_content is
    # sent to the customer immediately — no admin step needed. Good for things
    # like license keys or download links that don't need per-order review.
    "ALTER TABLE products ADD COLUMN delivery_mode TEXT NOT NULL DEFAULT 'manual'",
    "ALTER TABLE products ADD COLUMN auto_delivery_content TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE coupons ADD COLUMN max_per_customer INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE admin_users ADD COLUMN role TEXT NOT NULL DEFAULT 'custom'",
    "ALTER TABLE admin_users ADD COLUMN permissions TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE admin_users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE orders ADD COLUMN customer_id INTEGER REFERENCES customers(id)",
    "ALTER TABLE orders ADD COLUMN auto_delivered INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE orders ADD COLUMN payment_mode TEXT NOT NULL DEFAULT 'gateway'",
    # Make firebase_uid nullable in existing databases (for self-contained OTP auth)
    # SQLite doesn't support ALTER COLUMN, so this is handled gracefully —
    # the schema change only applies to fresh databases, existing ones still
    # work because the new OTP auth flow uses phone as the unique key.
    # ---- Automatic coupon system ----
    # auto_apply: 0 = manual code entry, 1 = applies automatically when conditions match
    "ALTER TABLE coupons ADD COLUMN auto_apply INTEGER NOT NULL DEFAULT 0",
    # trigger_type: 'manual' (user types code), 'cart_threshold' (min spend),
    # 'product_specific' (target product in cart), 'customer_segment' (new/logged-in user),
    # 'url_driven' (URL param ?coupon=CODE stores it in session)
    "ALTER TABLE coupons ADD COLUMN trigger_type TEXT NOT NULL DEFAULT 'manual'",
    "ALTER TABLE coupons ADD COLUMN min_cart_value INTEGER",
    "ALTER TABLE coupons ADD COLUMN target_product_id INTEGER REFERENCES products(id)",
    "ALTER TABLE coupons ADD COLUMN customer_segment TEXT NOT NULL DEFAULT 'all'",
    "ALTER TABLE coupons ADD COLUMN starts_at TEXT",
    "ALTER TABLE coupons ADD COLUMN expires_at TEXT",
    # ---- Product ribbons + strike-through pricing ----
    # ribbon: optional label shown on product card (e.g. 'Sale', 'Bestseller', 'New', 'Hot')
    "ALTER TABLE products ADD COLUMN ribbon TEXT NOT NULL DEFAULT ''",
    # compare_price: original price shown struck-through next to the selling price
    "ALTER TABLE products ADD COLUMN compare_price INTEGER",
    # views: simple popularity counter, incremented on each product detail view
    "ALTER TABLE products ADD COLUMN views INTEGER NOT NULL DEFAULT 0",
    # quantity: inventory tracking for each product
    "ALTER TABLE products ADD COLUMN quantity INTEGER NOT NULL DEFAULT 0",
    # Rename payments_enabled → test_checkout_mode in settings (invert semantics)
    # Old: payments_enabled=true means real payments. payments_enabled=false means testing.
    # New: test_checkout_mode=true means testing, false/none means real payments.
    "UPDATE settings SET key = 'test_checkout_mode' WHERE key = 'payments_enabled'",
    # ---- Multi-download tokens (Phase 5) ----
    # Replace single-use (used=0/1) with a countdown counter
    "ALTER TABLE download_tokens ADD COLUMN downloads_remaining INTEGER NOT NULL DEFAULT 1",
    # ---- Delivery content type (Phase 5) ----
    # Explicit type per product: file, license_key, access_link, instructions
    "ALTER TABLE products ADD COLUMN delivery_content_type TEXT NOT NULL DEFAULT 'instructions'",
    # ---- File versioning (Phase 5) ----
    "ALTER TABLE product_files ADD COLUMN version INTEGER NOT NULL DEFAULT 1",
    # ---- Admin roles/permissions/is_active (Phase 6) ----
    "ALTER TABLE admin_users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE admin_users ADD COLUMN created_at TEXT NOT NULL DEFAULT ''",
    # Data migration: ensure the first (master) admin has wildcard permissions
    "UPDATE admin_users SET permissions = '[\"*\"]', role = 'master' WHERE permissions = '[]' OR permissions IS NULL OR permissions = ''",
    # Data migration: force master role and wildcard perms for all non-empty admin accounts
    # that still have empty/null permissions (newly created or pre-migration accounts).
    "UPDATE admin_users SET permissions = '[\"*\"]', role = 'master' WHERE (permissions IS NULL OR permissions = '[]' OR permissions = '') AND username != ''",
    # google_uid column for fast direct Google OAuth (no Firebase needed)
    # Split into two steps: LibSQL/Turso (used on Render) rejects UNIQUE on ADD COLUMN.
    "ALTER TABLE customers ADD COLUMN google_uid TEXT",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_customers_google_uid ON customers(google_uid)",
    # ---- Session token version for "Sign out everywhere" ----
    "ALTER TABLE customers ADD COLUMN session_token_version INTEGER NOT NULL DEFAULT 0",
]

SCHEMA_EXTRA = """
  CREATE TABLE IF NOT EXISTS product_files (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
      filename   TEXT NOT NULL,
      original_name TEXT NOT NULL,
      file_size  INTEGER NOT NULL DEFAULT 0,
      mime_type  TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL
  );

  CREATE TABLE IF NOT EXISTS download_tokens (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      order_id   INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
      product_id INTEGER NOT NULL REFERENCES products(id),
      token      TEXT UNIQUE NOT NULL,
      file_path  TEXT NOT NULL,
      filename   TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      used       INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL
  );

  CREATE TABLE IF NOT EXISTS cart_items (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      customer_id INTEGER NOT NULL REFERENCES customers(id),
      product_id  INTEGER NOT NULL REFERENCES products(id),
      quantity    INTEGER NOT NULL DEFAULT 1,
      created_at  TEXT NOT NULL,
      updated_at  TEXT NOT NULL,
      UNIQUE(customer_id, product_id)
  );

  CREATE TABLE IF NOT EXISTS abandoned_carts (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      session_key     TEXT NOT NULL,
      phone           TEXT NOT NULL DEFAULT '',
      email           TEXT NOT NULL DEFAULT '',
      name            TEXT NOT NULL DEFAULT '',
      product_data    TEXT NOT NULL DEFAULT '[]',
      notification_count INTEGER NOT NULL DEFAULT 0,
      last_notified_at TEXT,
      status          TEXT NOT NULL DEFAULT 'active',
      created_at      TEXT NOT NULL,
      updated_at      TEXT NOT NULL
  );

  CREATE TABLE IF NOT EXISTS stock_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    customer_name TEXT NOT NULL DEFAULT '',
    customer_email TEXT NOT NULL,
    customer_phone TEXT NOT NULL DEFAULT '',
    notified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    notified_at TEXT
  );
  CREATE TABLE IF NOT EXISTS coupon_usage (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      coupon_id INTEGER REFERENCES coupons(id),
      order_id INTEGER,
      customer_email TEXT,
      discount_amount INTEGER NOT NULL DEFAULT 0,
      used_at TEXT NOT NULL,
      FOREIGN KEY(order_id) REFERENCES orders(id)
  );
  CREATE INDEX IF NOT EXISTS idx_coupon_usage_email ON coupon_usage(customer_email);
  CREATE INDEX IF NOT EXISTS idx_coupon_usage_coupon ON coupon_usage(coupon_id);
  -- The columns below are now in the CREATE TABLE statement above so fresh
  -- databases get them automatically. Existing databases get them via the
  -- MIGRATIONS loop below. The ALTER TABLE lines remain here as a safety net
  -- for databases where the MIGRATIONS loop hasn't run yet.
  ALTER TABLE orders ADD COLUMN coupon_code TEXT NOT NULL DEFAULT '';
  ALTER TABLE orders ADD COLUMN discount_amount INTEGER NOT NULL DEFAULT 0;
  ALTER TABLE orders ADD COLUMN refunded_amount INTEGER NOT NULL DEFAULT 0;
  ALTER TABLE orders ADD COLUMN refunded_at TEXT;
  ALTER TABLE orders ADD COLUMN razorpay_refund_id TEXT;
  CREATE TABLE IF NOT EXISTS admin_totp_secrets (
      admin_id INTEGER PRIMARY KEY,
      secret TEXT NOT NULL,
      enabled INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL
  );
  CREATE TABLE IF NOT EXISTS admin_recovery_codes (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      admin_id INTEGER NOT NULL REFERENCES admin_users(id),
      code_hash TEXT NOT NULL,
      used INTEGER NOT NULL DEFAULT 0,
      used_at TEXT
  );
  CREATE INDEX IF NOT EXISTS idx_recovery_admin ON admin_recovery_codes(admin_id);
  ALTER TABLE admin_recovery_codes ADD COLUMN created_at TEXT NOT NULL DEFAULT '';
  CREATE INDEX IF NOT EXISTS idx_stock_requests_product ON stock_requests(product_id, notified);

  CREATE TABLE IF NOT EXISTS wishlist_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    created_at TEXT NOT NULL,
    UNIQUE(customer_id, product_id)
  );
  CREATE INDEX IF NOT EXISTS idx_wishlist_customer ON wishlist_items(customer_id);

  CREATE TABLE IF NOT EXISTS order_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    admin_id INTEGER REFERENCES admin_users(id),
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
  );
  CREATE INDEX IF NOT EXISTS idx_order_notes_order ON order_notes(order_id);

  CREATE TABLE IF NOT EXISTS admin_audit_log (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      admin_id   INTEGER NOT NULL REFERENCES admin_users(id),
      action     TEXT NOT NULL,
      target     TEXT NOT NULL DEFAULT '',
      details    TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL
  );
  CREATE INDEX IF NOT EXISTS idx_admin_audit_admin ON admin_audit_log(admin_id);
  CREATE INDEX IF NOT EXISTS idx_admin_audit_created ON admin_audit_log(created_at);

  CREATE TABLE IF NOT EXISTS reviews (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
      order_id   INTEGER REFERENCES orders(id),
      customer_id INTEGER REFERENCES customers(id),
      customer_name TEXT NOT NULL DEFAULT '',
      rating     INTEGER NOT NULL DEFAULT 5,
      title      TEXT NOT NULL DEFAULT '',
      body       TEXT NOT NULL DEFAULT '',
      verified   INTEGER NOT NULL DEFAULT 0,
      visible    INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL
  );

  CREATE TABLE IF NOT EXISTS admin_tickets (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      admin_id    INTEGER NOT NULL REFERENCES admin_users(id),
      category    TEXT NOT NULL DEFAULT 'other',
      title       TEXT NOT NULL,
      description TEXT NOT NULL,
      status      TEXT NOT NULL DEFAULT 'open',
      admin_note  TEXT DEFAULT '',
      created_at  TEXT NOT NULL,
      resolved_at TEXT
  );
"""

INDEXES = [
    # Core storefront/admin lookups
    "CREATE INDEX IF NOT EXISTS idx_products_active_position ON products(active, position)",
    "CREATE INDEX IF NOT EXISTS idx_products_category ON products(category)",
    "CREATE INDEX IF NOT EXISTS idx_products_slug ON products(slug)",
    "CREATE INDEX IF NOT EXISTS idx_orders_status_created_at ON orders(status, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_orders_customer_id_created_at ON orders(customer_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_orders_order_ref ON orders(order_ref)",
    "CREATE INDEX IF NOT EXISTS idx_orders_customer_email ON orders(customer_email)",
    "CREATE INDEX IF NOT EXISTS idx_orders_payment_mode ON orders(payment_mode)",
    "CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items(order_id)",
    "CREATE INDEX IF NOT EXISTS idx_product_images_product_position ON product_images(product_id, position)",
    "CREATE INDEX IF NOT EXISTS idx_sections_visible_position ON sections(visible, position)",
    "CREATE INDEX IF NOT EXISTS idx_testimonials_visible_position ON testimonials(visible, position)",
    "CREATE INDEX IF NOT EXISTS idx_faqs_visible_position ON faqs(visible, position)",
    "CREATE INDEX IF NOT EXISTS idx_newsletter_subscribers_email ON newsletter_subscribers(email)",
    "CREATE INDEX IF NOT EXISTS idx_settings_key ON settings(key)",
    "CREATE INDEX IF NOT EXISTS idx_customers_phone ON customers(phone)",
    "CREATE INDEX IF NOT EXISTS idx_coupons_code ON coupons(code)",
    "CREATE INDEX IF NOT EXISTS idx_product_files_product_id ON product_files(product_id)",
    "CREATE INDEX IF NOT EXISTS idx_download_tokens_token ON download_tokens(token)",
    "CREATE INDEX IF NOT EXISTS idx_download_tokens_order_id ON download_tokens(order_id)",
    "CREATE INDEX IF NOT EXISTS idx_abandoned_carts_session_key ON abandoned_carts(session_key)",
    "CREATE INDEX IF NOT EXISTS idx_abandoned_carts_status ON abandoned_carts(status)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_abandoned_carts_session_status ON abandoned_carts(session_key, status)",
    "CREATE INDEX IF NOT EXISTS idx_products_views ON products(views DESC)",
    "CREATE INDEX IF NOT EXISTS idx_orders_email_status ON orders(customer_email, status)",
]


def _sql_preview(sql):
    sql = " ".join(str(sql).split())
    return sql[:180] + ("…" if len(sql) > 180 else "")


def _migration_id(stmt):
    return hashlib.sha1(stmt.encode("utf-8")).hexdigest()[:16]


def _ensure_migration_table(conn):
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_MIGRATION_TABLE} ("
        "id TEXT PRIMARY KEY, "
        "applied_at TEXT NOT NULL"
        ")"
    )


def _migration_applied(conn, migration_id):
    row = conn.execute(
        f"SELECT 1 AS ok FROM {_MIGRATION_TABLE} WHERE id = ?",
        (migration_id,),
    ).fetchone()
    return bool(row)


def _record_migration(conn, migration_id):
    conn.execute(
        f"INSERT OR IGNORE INTO {_MIGRATION_TABLE} (id, applied_at) VALUES (?, ?)",
        (migration_id, now()),
    )


def _apply_migrations(conn):
    """Apply additive migrations once, and remember which ones were applied.

    All applied IDs are fetched in a *single* query up front so we don't
    hammer Turso with one SELECT per migration (each round-trip can take
    hundreds of milliseconds and, if it times out, leaves the stream dead
    for the next statement).
    """
    _ensure_migration_table(conn)
    _migrate_err = (sqlite3.OperationalError, ValueError)

    # One round-trip to get every applied migration ID.
    rows = conn.execute(f"SELECT id FROM {_MIGRATION_TABLE}").fetchall()
    applied = {r[0] for r in rows}

    for stmt in MIGRATIONS:
        migration_id = _migration_id(stmt)
        if migration_id in applied:
            continue
        try:
            conn.execute(stmt)
        except _migrate_err as exc:
            message = str(exc).lower()
            already_there = (
                "duplicate column" in message
                or "already exists" in message
                or "duplicate index" in message
                or "cannot add a unique column" in message
            )
            if not already_there:
                raise
            _db_logger.info("Migration already applied or skipped: %s", _sql_preview(stmt))
        try:
            _record_migration(conn, migration_id)
        except _migrate_err:
            # Recording the migration is best-effort — if the INSERT fails
            # (e.g. stream timeout) we'll just re-check next boot and the
            # ALTER TABLE will be skipped via the duplicate-column guard above.
            pass


DEFAULT_SETTINGS = {
    "site_name": "Atelier",
    "site_tagline": "Curated digital goods, delivered with care.",
    "hero_title": "Exceptional digital products,\ncarefully made.",
    "hero_subtitle": "A small, considered catalogue — nothing mass produced.",
    "about_title": "About Us",
    "about_content": "We are a small studio creating premium digital products. "
                      "Every item in our catalogue is reviewed personally before "
                      "it reaches you.\n\nWrite your own story here from the Admin Panel.",
    "contact_email": "hello@example.com",
    "contact_phone": "+91 00000 00000",
    "footer_text": "Crafted with care.",
    "meta_description": "A curated catalogue of premium digital products.",
    "currency_symbol": "₹",
    "test_checkout_mode": "false",
    "auto_deliver_enabled": "true",
    "auto_email_enabled": "true",
    "low_stock_alerts": "true",
    "calendarific_enabled": "true",
    "greetings_json": "[]",
    "disable_payments": "false",
    "refund_policy_text": "We offer a 7-day refund policy on most products. Contact us at hello@example.com for refund requests.",
    "privacy_policy_text": "<!-- Write your Privacy Policy content here. Use HTML for formatting. -->\n\n<h2>1. Information We Collect</h2>\n<p>We collect information you provide directly, such as your name, email, phone number, and order details. We also collect basic technical data for site performance monitoring.</p>\n\n<h2>2. How We Use Your Information</h2>\n<p>We use your information to process orders, communicate order status, and improve our service. We do not sell your data to third parties.</p>\n\n<h2>3. Data Protection</h2>\n<p>We implement reasonable security measures to protect your information. Payments are processed securely through Razorpay.</p>\n\n<h2>4. Your Rights</h2>\n<p>You may request access to, correction of, or deletion of your personal data by contacting us.</p>\n\n<h2>5. Contact Us</h2>\n<p>Email us at your-email@example.com for any privacy-related questions.</p>",
    "terms_of_service_text": "<!-- Write your Terms of Service content here. Use HTML for formatting. -->\n\n<h2>1. About Us</h2>\n<p>We are a seller of digital products. By using this site, you agree to these terms.</p>\n\n<h2>2. Products &amp; Delivery</h2>\n<p>All products are digital and delivered electronically. Delivery may be manual or automatic depending on the product.</p>\n\n<h2>3. Orders &amp; Payment</h2>\n<p>Orders are processed securely through Razorpay. You may check out as a guest or sign in with your phone number.</p>\n\n<h2>4. Refunds</h2>\n<p>Please refer to our Refund Policy for details on returns and cancellations.</p>\n\n<h2>5. Contact Us</h2>\n<p>Email us at your-email@example.com for any questions about these terms.</p>",
    "last_policy_update": "July 2026",
    "business_name": "Virtual Store",
    "business_address": "Kanpur, Uttar Pradesh, India",
    "gstin": "",
}


_turso_conn_cache = None
_turso_db_url = ""
_turso_db_token = ""
_db_initialized = False




def _close_turso():
    """Release the cached Turso reference without force-closing the underlying
    libsql runtime.

    In practice, closing the native connection during worker shutdown has been
    triggering tokio runtime panics on Render. Clearing the reference is enough
    for Python-side cleanup; the process exit will reclaim the rest safely.
    """
    global _turso_conn_cache
    _turso_conn_cache = None


def get_db():
    global _turso_conn_cache, _turso_db_url, _turso_db_token
    turso_url = os.environ.get("TURSO_DB_URL", "").strip()
    turso_token = os.environ.get("TURSO_DB_AUTH_TOKEN", "").strip()
    if turso_url and turso_token:
        # Cache a single Turso connection per worker process. Each
        # libsql.connect() spins up its own tokio runtime; creating one
        # per request and closing it causes tokio thread-join panics
        # ("Resource deadlock avoided"). One persistent connection is
        # safe because the HTTP transport is stateless and thread-safe.
        if _turso_conn_cache is None:
            _turso_db_url = turso_url
            _turso_db_token = turso_token
            _turso_conn_cache = _get_turso_db(turso_url, turso_token)
            atexit.register(_close_turso)
        return _turso_conn_cache
    if has_request_context() and g is not None:
        cached = getattr(g, "_sqlite_db_conn", None)
        if cached is not None and not getattr(cached, "_closed", False):
            return cached
    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    proxy = _SQLiteConnectionProxy(conn)
    if has_request_context() and g is not None:
        g._sqlite_db_conn = proxy
    return proxy


def init_db_if_needed():
    """Lazy wrapper around init_db() — runs once per worker, on the first
    request. This prevents the worker from hanging during import if the
    Turso/libsql connection is slow or unreachable."""
    global _db_initialized
    if not _db_initialized:
        init_db()
        _db_initialized = True


def _get_turso_db(url, token):
    """Connect to a remote Turso database via the libsql package (HTTP)."""
    try:
        import libsql
    except ImportError:
        print(
            "TURSO_DB_URL is set but libsql is not installed.\n"
            "Run: pip install libsql",
            file=sys.stderr,
        )
        raise
    # The old libsql-client used wss:// (WebSocket), which Turso no longer
    # supports. The new libsql package uses HTTP and expects libsql:// or
    # https://. Convert wss:// to libsql:// automatically.
    if url.startswith("wss://"):
        url = "libsql://" + url[len("wss://"):]
    elif url.startswith("ws://"):
        url = "libsql://" + url[len("ws://"):]
    conn = libsql.connect(database=url, auth_token=token)
    # Wrap so it quacks like a sqlite3 connection with Row factory
    return _TursoConnection(conn, url=url, token=token)


class _TursoConnection:
    """Wraps a libsql connection to match sqlite3.Connection interface.
    The new libsql package returns plain tuples; we convert them to
    sqlite3.Row so the rest of the codebase can access columns by name."""

    def __init__(self, conn, url=None, token=None):
        self._conn = conn
        self._url = url
        self._token = token
        self._row_factory = sqlite3.Row

    @property
    def row_factory(self):
        return self._row_factory

    @row_factory.setter
    def row_factory(self, factory):
        self._row_factory = factory

    def execute(self, sql, params=None):
        started = time.perf_counter()
        try:
            cursor = self._conn.execute(sql, params or ())
        except Exception as exc:
            message = str(exc).lower()
            transient = ("stream not found" in message or "connection closed" in message or "hrana" in message)
            if not transient or not self._url or not self._token:
                raise
            try:
                import libsql
                url = self._url
                if url.startswith("wss://"):
                    url = "libsql://" + url[len("wss://"):]
                elif url.startswith("ws://"):
                    url = "libsql://" + url[len("ws://"):]
                self._conn = libsql.connect(database=url, auth_token=self._token)
                cursor = self._conn.execute(sql, params or ())
            except Exception:
                raise exc
        finally:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            if elapsed_ms >= _SLOW_QUERY_MS:
                _db_logger.warning("slow turso query (%sms): %s", elapsed_ms, _sql_preview(sql))
        return _TursoCursor(cursor, self._row_factory)

    def executescript(self, script):
        self._conn.executescript(script)

    def commit(self):
        self._conn.commit()

    def close(self):
        # No-op: the cached Turso connection lives for the worker's lifetime.
        # Calling the underlying close() tears down the tokio runtime and
        # causes "failed to join thread" panics on shutdown.
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def __getattr__(self, name):
        return getattr(self._conn, name)

class _SQLiteConnectionProxy:
    """Small wrapper around sqlite3.Connection for consistent logging and API shape."""

    def __init__(self, conn):
        self._conn = conn
        self._row_factory = sqlite3.Row
        self._closed = False
        self._request_scoped = False

    @property
    def row_factory(self):
        return self._row_factory

    @row_factory.setter
    def row_factory(self, factory):
        self._row_factory = factory
        self._conn.row_factory = factory

    def execute(self, sql, params=None):
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
        started = time.perf_counter()
        try:
            return self._conn.execute(sql, params or ())
        finally:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            if elapsed_ms >= _SLOW_QUERY_MS:
                _db_logger.warning("slow sqlite query (%sms): %s", elapsed_ms, _sql_preview(sql))

    def executescript(self, script):
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
        return self._conn.executescript(script)

    def commit(self):
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
        return self._conn.commit()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            return self._conn.close()
        finally:
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.close()
        except Exception:
            pass
        return False

    def __getattr__(self, name):
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
        return getattr(self._conn, name)


class _Row:
    """A sqlite3.Row-compatible object that works across Python versions.
    Python 3.14 changed sqlite3.Row's constructor to require a real cursor,
    so we use this lightweight dict-backed class instead."""

    def __init__(self, columns, values):
        self._columns = list(columns)
        self._values = tuple(values)
        self._map = {c: v for c, v in zip(self._columns, self._values)}

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        # Case-insensitive lookup — Turso/libsql returns column names in
        # uppercase for SQL keywords (e.g. "key" -> "KEY"), but the app
        # code accesses them with the original lowercase name.
        if key in self._map:
            return self._map[key]
        lower = key.lower()
        for k, v in self._map.items():
            if k.lower() == lower:
                return v
        raise KeyError(key)

    def keys(self):
        return self._columns

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __repr__(self):
        return f"<Row {dict(zip(self._columns, self._values))}>"

    def __eq__(self, other):
        if isinstance(other, _Row):
            return self._columns == other._columns and self._values == other._values
        if isinstance(other, sqlite3.Row):
            return list(self._values) == list(other)
        return NotImplemented


class _TursoCursor:
    """Wraps a libsql cursor to return Row-compatible objects like sqlite3.Cursor."""

    def __init__(self, cursor, row_factory):
        self._cursor = cursor
        self._row_factory = row_factory
        # Capture column names immediately — libsql's description getter
        # accesses stmt.columns() which may be unavailable after the cursor
        # is consumed by fetchall/fetchone.
        self._columns = self._extract_columns(cursor)

    def _extract_columns(self, cursor):
        """Extract column names from the cursor's description, handling
        the various formats libsql may return."""
        cols = []
        desc = getattr(cursor, "description", None)
        if desc:
            for d in desc:
                if isinstance(d, str):
                    cols.append(d)
                elif isinstance(d, (tuple, list)) and len(d) > 0:
                    cols.append(str(d[0]))
                elif hasattr(d, "name"):
                    cols.append(str(d.name))
                else:
                    cols.append(str(d))
        if not cols:
            names = getattr(cursor, "column_names", None)
            if names:
                cols = [str(n) for n in names]
        return cols

    def _wrap_row(self, row):
        if row is None:
            return None
        if self._row_factory is sqlite3.Row and self._columns:
            return _Row(self._columns, row)
        return row

    @property
    def description(self):
        return self._cursor.description

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    def fetchone(self):
        return self._wrap_row(self._cursor.fetchone())

    def fetchall(self):
        rows = self._cursor.fetchall()
        if not rows:
            return []
        return [self._wrap_row(r) for r in rows]

    def __iter__(self):
        return self

    def __next__(self):
        row = self._wrap_row(self._cursor.fetchone())
        if row is None:
            raise StopIteration
        return row


def init_db():
    conn = get_db()
    # The libsql package raises ValueError (not sqlite3.OperationalError) for
    # SQL errors, so catch both to keep migrations idempotent on either backend.
    _migrate_err = (sqlite3.OperationalError, ValueError)
    conn.executescript(SCHEMA)
    # Run SCHEMA_EXTRA one statement at a time so a single redundant ALTER
    # TABLE (columns now in CREATE TABLE) doesn't abort the entire block.
    for stmt in SCHEMA_EXTRA.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except _migrate_err:
            pass  # e.g. duplicate column on an ALTER TABLE for a column now in CREATE TABLE

    try:
        _apply_migrations(conn)
    except _migrate_err as exc:
        _db_logger.exception("Migration pass failed: %s", exc)
        raise

    # Run all index creations in one round-trip via executescript.
    try:
        conn.executescript(";\n".join(INDEXES))
    except _migrate_err:
        # Fall back to one-by-one if executescript is unsupported (shouldn't happen).
        for stmt in INDEXES:
            try:
                conn.execute(stmt)
            except _migrate_err:
                pass

    # Seed default settings in one round-trip.
    if DEFAULT_SETTINGS:
        placeholders = ", ".join("(?, ?)" for _ in DEFAULT_SETTINGS)
        params = [v for pair in DEFAULT_SETTINGS.items() for v in pair]
        conn.execute(
            f"INSERT OR IGNORE INTO settings (key, value) VALUES {placeholders}",
            params,
        )

    # Seed a default admin user, only if none exists yet
    existing = conn.execute("SELECT COUNT(*) AS c FROM admin_users").fetchone()["c"]
    if existing == 0:
        password = config.DEFAULT_ADMIN_PASSWORD
        generated = False
        if not password:
            # No ADMIN_PASSWORD env var set — generate one instead of using a
            # predictable default, and write it somewhere only the site owner
            # can read it.
            password = secrets.token_urlsafe(12)
            generated = True
        conn.execute(
                "INSERT INTO admin_users (username, password_hash, role, permissions, created_at) VALUES (?, ?, 'master', '[\"*\"]', ?)",
                (config.DEFAULT_ADMIN_USERNAME, generate_password_hash(password), now()),
            )
        if generated:
            try:
                path = os.path.join(os.path.dirname(config.DB_PATH) or ".", "INITIAL_ADMIN_PASSWORD.txt")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(
                        "No ADMIN_PASSWORD was set, so this one was generated automatically.\n"
                        f"Username: {config.DEFAULT_ADMIN_USERNAME}\n"
                        f"Password: {password}\n\n"
                        "Log in once with this, then change it immediately from "
                        "My Account in the admin panel. Delete this file afterwards.\n"
                    )
            except OSError:
                pass
            print(
                f"\n[first run] No ADMIN_PASSWORD set — generated one for you:\n"
                f"  Username: {config.DEFAULT_ADMIN_USERNAME}\n"
                f"  Password: {password}\n"
                f"  (also saved to instance/INITIAL_ADMIN_PASSWORD.txt)\n"
                f"  Please log in and change it right away.\n"
            )

    conn.commit()
    conn.close()


def new_order_ref():
    """Short, human-friendly, unambiguous order reference e.g. ORD-7F3K9Q."""
    alphabet = string.ascii_uppercase + string.digits
    alphabet = alphabet.replace("O", "").replace("0", "").replace("I", "").replace("1", "")
    suffix = "".join(secrets.choice(alphabet) for _ in range(6))
    return f"ORD-{suffix}"


def now():
    return datetime.now(timezone.utc).isoformat()
