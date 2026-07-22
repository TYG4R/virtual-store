"""Tests for store helpers — coupons, catalog, cart, dashboard stats, sold counts.

Run with:  python3 -m unittest tests/test_helpers.py
"""
import sys
import os
import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
import database as db

# ── Patch configs before any app imports ──
config.DATABASE_URL = ":memory:"
config.TURSO_DB_URL = None
config.TURSO_DB_AUTH_TOKEN = None
config.RAZORPAY_KEY_ID = "test_key"
config.RAZORPAY_KEY_SECRET = "test_secret"
config.SECRET_KEY = "test-secret-key-for-test"
config.DEBUG = False

from app import _is_coupon_active, _coupon_discount, _coupon_description
from app import _table_columns


def _memory_db():
    """Create an in-memory SQLite DB with full schema + migrations."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(db.SCHEMA)
    for stmt in db.MIGRATIONS:
        try:
            conn.execute(stmt)
        except Exception:
            pass
    conn.commit()
    return conn


def _seed(conn):
    """Seed basic test data."""
    now = datetime.now(timezone.utc).isoformat()
    settings = [
        ("site_name", "Test Store"),
        ("site_tagline", "Testing"),
        ("currency_symbol", "₹"),
        ("currency_code", "INR"),
        ("test_checkout_mode", "true"),
    ]
    for k, v in settings:
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    conn.execute(
        "INSERT INTO products (name, slug, short_description, price, category, active, position, created_at, quantity, views) "
        "VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?, ?)",
        ("Digital Widget", "digital-widget", "A fine widget", 499, "Widgets", now, 10, 120),
    )
    conn.execute(
        "INSERT INTO products (name, slug, short_description, price, category, active, position, created_at, quantity, views) "
        "VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?, ?)",
        ("Premium Widget", "premium-widget", "The best widget", 1499, "Widgets", now, 5, 80),
    )
    conn.execute(
        "INSERT INTO products (name, slug, short_description, price, category, active, position, created_at, quantity, compare_price) "
        "VALUES (?, ?, ?, ?, ?, 1, 2, ?, ?, ?)",
        ("Sale Widget", "sale-widget", "Discounted", 299, "Widgets", now, 0, 599),
    )
    conn.execute(
        "INSERT INTO products (name, slug, short_description, price, category, active, position, created_at, quantity) "
        "VALUES (?, ?, ?, ?, ?, 1, 3, ?, ?)",
        ("Out of Stock", "out-of-stock", "Unavailable", 999, "Gadgets", now, 0),
    )

    conn.execute(
        "INSERT INTO coupons (code, discount_type, discount_value, active, usage_limit, used_count, created_at, trigger_type) "
        "VALUES (?, ?, ?, 1, NULL, 0, ?, 'manual')",
        ("SAVE10", "percent", 10, now),
    )
    conn.execute(
        "INSERT INTO coupons (code, discount_type, discount_value, active, usage_limit, used_count, created_at, trigger_type, min_cart_value) "
        "VALUES (?, ?, ?, 1, NULL, 0, ?, 'cart_threshold', ?)",
        ("FREESHIP", "flat", 50, now, 1000),
    )
    conn.execute(
        "INSERT INTO coupons (code, discount_type, discount_value, active, usage_limit, used_count, created_at, trigger_type, target_product_id) "
        "VALUES (?, ?, ?, 1, NULL, 0, ?, 'product_specific', ?)",
        ("WIDGET20", "percent", 20, now, 1),
    )
    conn.execute(
        "INSERT INTO coupons (code, discount_type, discount_value, active, usage_limit, used_count, created_at, trigger_type, expires_at) "
        "VALUES (?, ?, ?, 1, 100, 0, ?, 'manual', ?)",
        ("EXPIRED", "percent", 15, now, "2020-01-01T00:00:00"),
    )

    from werkzeug.security import generate_password_hash
    conn.execute("INSERT INTO admin_users (username, password_hash) VALUES (?, ?)",
                 ("admin", generate_password_hash("admin")))
    conn.commit()


# ============== Coupon Tests ==============

class TestCouponActive(unittest.TestCase):
    def setUp(self):
        self.conn = _memory_db()
        _seed(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_active_coupon(self):
        c = self.conn.execute("SELECT * FROM coupons WHERE code = 'SAVE10'").fetchone()
        self.assertTrue(_is_coupon_active(c))

    def test_expired_coupon(self):
        c = self.conn.execute("SELECT * FROM coupons WHERE code = 'EXPIRED'").fetchone()
        self.assertFalse(_is_coupon_active(c))

    def test_exhausted_coupon(self):
        c = dict(self.conn.execute("SELECT * FROM coupons WHERE code = 'SAVE10'").fetchone())
        c["usage_limit"] = 5
        c["used_count"] = 5
        self.assertFalse(_is_coupon_active(c))

    def test_inactive_coupon(self):
        c = dict(self.conn.execute("SELECT * FROM coupons WHERE code = 'SAVE10'").fetchone())
        c["active"] = 0
        self.assertFalse(_is_coupon_active(c))

    def test_null_coupon(self):
        self.assertFalse(_is_coupon_active(None))

    def test_not_started_yet(self):
        c = dict(self.conn.execute("SELECT * FROM coupons WHERE code = 'SAVE10'").fetchone())
        c["starts_at"] = "2026-06-01T00:00:00"
        self.assertFalse(_is_coupon_active(c, "2025-01-01T00:00:00"))


class TestCouponDiscount(unittest.TestCase):
    def test_percent_discount(self):
        c = {"discount_type": "percent", "discount_value": 10}
        self.assertEqual(_coupon_discount(c, 499), 50)

    def test_flat_discount(self):
        c = {"discount_type": "flat", "discount_value": 50}
        self.assertEqual(_coupon_discount(c, 1500), 50)

    def test_discount_caps_at_price_minus_one(self):
        c = {"discount_type": "percent", "discount_value": 100}
        self.assertEqual(_coupon_discount(c, 100), 99)

    def test_not_negative(self):
        c = {"discount_type": "flat", "discount_value": 9999}
        self.assertEqual(_coupon_discount(c, 10), 9)  # 10-1=9

    def test_zero_price(self):
        c = {"discount_type": "flat", "discount_value": 50}
        self.assertEqual(_coupon_discount(c, 0), 0)


class TestCouponDescription(unittest.TestCase):
    def test_percent(self):
        c = {"discount_type": "percent", "discount_value": 10, "trigger_type": "manual", "min_cart_value": None, "customer_segment": ""}
        desc = _coupon_description(c)
        self.assertIn("10%", desc)

    def test_flat(self):
        c = {"discount_type": "flat", "discount_value": 50, "trigger_type": "manual", "min_cart_value": None, "customer_segment": ""}
        desc = _coupon_description(c)
        self.assertIn("₹50", desc)


class TestTableColumns(unittest.TestCase):
    def setUp(self):
        self.conn = _memory_db()
        _seed(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_products_columns(self):
        from app import _table_columns_cache
        _table_columns_cache.clear()
        # Need to patch db.get_db before calling
        conn = _NoCloseConn(_memory_db())
        _seed(conn)
        old_get_db = db.get_db
        db.get_db = lambda: conn
        try:
            cols = _table_columns("products")
            self.assertIsInstance(cols, set)
            self.assertIn("id", cols)
            self.assertIn("name", cols)
            self.assertIn("quantity", cols)
        finally:
            db.get_db = old_get_db
            conn._conn.close()

    def test_unknown_table(self):
        self.assertEqual(_table_columns("nonexistent"), set())


class _NoCloseConn:
    """Wraps a SQLite connection so .close() is a no-op.
    This prevents _table_columns (which opens+closes its own handle)
    from closing the caller's active connection."""
    def __init__(self, conn):
        self._conn = conn
    def __getattr__(self, name):
        return getattr(self._conn, name)
    def close(self):
        pass


def _make_mock_get_db():
    """Return a get_db function that creates a fresh in-memory DB each call,
    seeded with basic data, and wraps it in a NoCloseConn so callers that
    close their handle don't invalidate later uses."""
    def get_db():
        conn = _memory_db()
        _seed(conn)
        return _NoCloseConn(conn)
    return get_db


# ============== Catalog Tests ==============

class TestCatalogLoaded(unittest.TestCase):
    def test_load_catalog_shape(self):
        conn = _NoCloseConn(_memory_db())
        _seed(conn)
        old_get_db = db.get_db
        db.get_db = lambda: conn
        try:
            from app import _load_catalog
            catalog = _load_catalog()
            self.assertIn("products", catalog)
            self.assertEqual(len(catalog["products"]), 4)
            self.assertEqual(catalog["products_by_id"][1]["name"], "Digital Widget")
            self.assertEqual(catalog["products_by_slug"]["sale-widget"]["price"], 299)
            self.assertIn("sold_counts", catalog)
            self.assertIn("product_images", catalog)
        finally:
            db.get_db = old_get_db
            conn._conn.close()


class TestCatalogSoldCounts(unittest.TestCase):
    def test_no_orders_means_zero_sold(self):
        conn = _NoCloseConn(_memory_db())
        _seed(conn)
        old_get_db = db.get_db
        db.get_db = lambda: conn
        try:
            from app import _load_catalog
            catalog = _load_catalog()
            self.assertEqual(catalog["sold_counts"], {})
        finally:
            db.get_db = old_get_db
            conn._conn.close()

    def test_sold_counts_from_paid_orders(self):
        conn = _NoCloseConn(_memory_db())
        _seed(conn)
        old_get_db = db.get_db
        db.get_db = lambda: conn
        now = datetime.now(timezone.utc).isoformat()

        conn.execute(
            "INSERT INTO orders (order_ref, product_id, product_name, customer_name, customer_email, amount, status, created_at, paid_at, payment_mode) "
            "VALUES ('O1', 1, 'Widget', 'A', 'a@t.com', 499, 'confirmed', ?, ?, 'gateway')", (now, now))
        conn.execute("INSERT INTO order_items (order_id, product_id, product_name, unit_price, quantity, line_total) VALUES (1, 1, 'Widget', 499, 3, 1497)")
        conn.execute("INSERT INTO orders (order_ref, product_id, product_name, customer_name, customer_email, amount, status, created_at, paid_at, delivered_at, payment_mode) "
                     "VALUES ('O2', 2, 'Premium', 'B', 'b@t.com', 1499, 'delivered', ?, ?, ?, 'gateway')", (now, now, now))
        conn.execute("INSERT INTO order_items (order_id, product_id, product_name, unit_price, quantity, line_total) VALUES (2, 2, 'Premium', 1499, 1, 1499)")
        # Cancelled should not count
        conn.execute("INSERT INTO orders (order_ref, product_id, product_name, customer_name, customer_email, amount, status, created_at, payment_mode) "
                     "VALUES ('O3', 1, 'Widget', 'C', 'c@t.com', 499, 'cancelled', ?, 'gateway')", (now,))
        conn.execute("INSERT INTO order_items (order_id, product_id, product_name, unit_price, quantity, line_total) VALUES (3, 1, 'Widget', 499, 1, 499)")
        conn.commit()

        try:
            from app import _load_catalog
            catalog = _load_catalog()
            self.assertEqual(catalog["sold_counts"].get(1), 3)
            self.assertEqual(catalog["sold_counts"].get(2), 1)
            self.assertNotIn(3, catalog["sold_counts"])
        finally:
            db.get_db = old_get_db
            conn._conn.close()


# ============== Dashboard Stats Tests ==============

class TestDashboardStats(unittest.TestCase):
    def test_with_orders(self):
        conn = _memory_db()
        _seed(conn)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("INSERT INTO orders (order_ref, product_id, product_name, customer_name, customer_email, customer_phone, amount, status, created_at, paid_at, payment_mode) "
                     "VALUES ('O1', 1, 'Widget', 'Alice', 'alice@t.com', '999', 499, 'paid', ?, ?, 'gateway')", (now, now))
        conn.execute("INSERT INTO order_items (order_id, product_id, product_name, unit_price, quantity, line_total) VALUES (1, 1, 'Widget', 499, 1, 499)")
        conn.execute("INSERT INTO orders (order_ref, product_id, product_name, customer_name, customer_email, customer_phone, amount, status, created_at, paid_at, delivered_at, payment_mode) "
                     "VALUES ('O2', 2, 'Premium', 'Bob', 'bob@t.com', '888', 1499, 'delivered', ?, ?, ?, 'gateway')", (now, now, now))
        conn.execute("INSERT INTO order_items (order_id, product_id, product_name, unit_price, quantity, line_total) VALUES (2, 2, 'Premium', 1499, 1, 1499)")
        conn.execute("INSERT INTO orders (order_ref, product_id, product_name, customer_name, customer_email, customer_phone, amount, status, created_at, payment_mode) "
                     "VALUES ('O3', 1, 'Widget', 'Charlie', 'c@t.com', '777', 499, 'cancelled', ?, 'gateway')", (now,))
        conn.execute("INSERT INTO orders (order_ref, product_id, product_name, customer_name, customer_email, customer_phone, amount, status, created_at, paid_at, payment_mode) "
                     "VALUES ('O4', 3, 'Sale', 'Test', 'test@t.com', '666', 100, 'paid', ?, ?, 'test')", (now, now))
        conn.commit()

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

        self.assertEqual(row["products"], 4)
        # ORD-001 (paid) and ORD-004 (paid+test) both have status='paid'
        self.assertEqual(row["pending"], 2)
        self.assertEqual(row["delivered"], 1)
        self.assertEqual(row["cancelled"], 1)
        self.assertEqual(row["test_orders"], 1)
        self.assertEqual(row["revenue"], 1998)
        # Alice, Bob, Charlie (cancelled but has email), Test
        self.assertEqual(row["customers"], 4)
        self.assertTrue(row["total_views"] > 0)

        conn.close()

    def test_empty(self):
        conn = _memory_db()
        _seed(conn)
        row = conn.execute("""
            SELECT
                (SELECT COUNT(*) FROM orders WHERE status = 'paid') AS pending,
                (SELECT COUNT(*) FROM orders WHERE status = 'delivered') AS delivered,
                (SELECT COUNT(*) FROM orders WHERE status = 'cancelled') AS cancelled,
                (SELECT COUNT(DISTINCT customer_email) FROM orders WHERE customer_email != '') AS customers,
                (SELECT COALESCE(SUM(amount),0) FROM orders WHERE status IN ('paid','delivered') AND COALESCE(payment_mode, 'gateway') != 'test') AS revenue
        """).fetchone()
        self.assertEqual(row["pending"], 0)
        self.assertEqual(row["delivered"], 0)
        self.assertEqual(row["cancelled"], 0)
        self.assertEqual(row["customers"], 0)
        self.assertEqual(row["revenue"], 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
