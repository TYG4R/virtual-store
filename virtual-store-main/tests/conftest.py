"""Shared fixtures for virtual-store tests."""
import os
import sys
import sqlite3
import json
from datetime import datetime, timezone

import pytest

# Ensure the project root is on sys.path so imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
import database as db


# ── Patch config to use in-memory SQLite, no external services ──────
config.DATABASE_URL = ":memory:"
config.TURSO_DB_URL = None
config.TURSO_DB_AUTH_TOKEN = None
config.RAZORPAY_KEY_ID = "test_key"
config.RAZORPAY_KEY_SECRET = "test_secret"
config.ADMIN_PASSWORD_HASH = "$2b$12$placeholder"
config.SECRET_KEY = "test-secret-key-for-pytest"
config.DEBUG = False
config.SITE_URL = "http://localhost:5000"
config.CALENDARIFIC_API_KEY = ""
config.TURNSTILE_SITE_KEY = ""
config.TURNSTILE_SECRET_KEY = ""
config.RESEND_API_KEY = ""
config.SENDGRID_API_KEY = ""
config.SMTP_SERVER = ""
config.SMTP_PORT = 0
config.SMTP_USERNAME = ""
config.SMTP_PASSWORD = ""
config.FIREBASE_SERVICE_ACCOUNT_JSON = None
config.TWILIO_ACCOUNT_SID = ""
config.TWILIO_AUTH_TOKEN = ""
config.TWILIO_PHONE_NUMBER = ""
config.WHATSAPP_PHONE_NUMBER = ""
config.GOOGLE_OAUTH_CLIENT_ID = ""
config.FCM_SERVER_KEY = ""


def init_memory_db():
    """Create an in-memory SQLite DB with the full schema + migrations."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # Execute the SCHEMA
    conn.executescript(db.SCHEMA)

    # Run MIGRATIONS (ignore duplicate-column errors)
    for stmt in db.MIGRATIONS:
        try:
            conn.execute(stmt)
        except Exception:
            pass

    conn.commit()
    return conn


def seed_basic_data(conn):
    """Fill a fresh DB with products, settings, and an admin user."""
    now = datetime.now(timezone.utc).isoformat()

    # Settings
    settings = [
        ("site_name", "Test Store"),
        ("site_tagline", "Testing"),
        ("currency_symbol", "₹"),
        ("currency_code", "INR"),
        ("test_checkout_mode", "true"),
    ]
    for k, v in settings:
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    # Products
    conn.execute(
        """INSERT INTO products (name, slug, short_description, price, category,
                                 active, position, created_at, quantity, views)
           VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?, ?)""",
        ("Digital Widget", "digital-widget", "A fine widget", 499, "Widgets",
         now, 10, 120),
    )
    conn.execute(
        """INSERT INTO products (name, slug, short_description, price, category,
                                 active, position, created_at, quantity, views)
           VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?, ?)""",
        ("Premium Widget", "premium-widget", "The best widget", 1499, "Widgets",
         now, 5, 80),
    )
    conn.execute(
        """INSERT INTO products (name, slug, short_description, price, category,
                                 active, position, created_at, quantity, compare_price)
           VALUES (?, ?, ?, ?, ?, 1, 2, ?, ?, ?)""",
        ("Sale Widget", "sale-widget", "Discounted widget", 299, "Widgets",
         now, 0, 599),
    )
    conn.execute(
        """INSERT INTO products (name, slug, short_description, price, category,
                                 active, position, created_at, quantity)
           VALUES (?, ?, ?, ?, ?, 1, 3, ?, ?)""",
        ("Out of Stock", "out-of-stock", "Unavailable", 999, "Gadgets",
         now, 0),
    )

    # Coupons
    conn.execute(
        """INSERT INTO coupons (code, discount_type, discount_value, active,
                                usage_limit, used_count, created_at, trigger_type)
           VALUES (?, ?, ?, 1, NULL, 0, ?, 'manual')""",
        ("SAVE10", "percent", 10, now),
    )
    conn.execute(
        """INSERT INTO coupons (code, discount_type, discount_value, active,
                                usage_limit, used_count, created_at, trigger_type,
                                min_cart_value)
           VALUES (?, ?, ?, 1, NULL, 0, ?, 'cart_threshold', ?)""",
        ("FREESHIP", "flat", 50, now, 1000),
    )
    conn.execute(
        """INSERT INTO coupons (code, discount_type, discount_value, active,
                                usage_limit, used_count, created_at, trigger_type,
                                target_product_id)
           VALUES (?, ?, ?, 1, NULL, 0, ?, 'product_specific', ?)""",
        ("WIDGET20", "percent", 20, now, 1),  # 20% off Digital Widget (id=1)
    )
    # Expired coupon
    conn.execute(
        """INSERT INTO coupons (code, discount_type, discount_value, active,
                                usage_limit, used_count, created_at, trigger_type,
                                expires_at)
           VALUES (?, ?, ?, 1, 100, 0, ?, 'manual', ?)""",
        ("EXPIRED", "percent", 15, now, "2020-01-01T00:00:00"),
    )

    # Admin user
    from werkzeug.security import generate_password_hash
    conn.execute(
        "INSERT INTO admin_users (username, password_hash) VALUES (?, ?)",
        ("admin", generate_password_hash("admin")),
    )

    conn.commit()


@pytest.fixture
def db_conn():
    """Provide a clean in-memory database with full schema + seed data."""
    conn = init_memory_db()
    seed_basic_data(conn)
    yield conn
    conn.close()
