"""Integration tests for the Flask app routes.

Run with:  python3 -m unittest tests/test_routes.py
"""
import sys
import os
import json
import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config

config.DATABASE_URL = ":memory:"
config.TURSO_DB_URL = None
config.TURSO_DB_AUTH_TOKEN = None
config.RAZORPAY_KEY_ID = "test_key"
config.RAZORPAY_KEY_SECRET = "test_secret"
config.SECRET_KEY = "test-secret-key-for-route-test"
config.DEBUG = False
config.CSRF_ENABLED = False  # disable CSRF for testing

# Must import app after config patches
from app import app, invalidate_catalog_cache
import database as db


def _seed_test_data():
    """Seed the app's in-memory DB with test products, settings, and admin user."""
    import database as db_mod
    conn = db_mod.get_db()
    # Only seed if no products exist yet
    existing = conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"]
    if existing > 0:
        conn.close()
        return

    now = datetime.now(timezone.utc).isoformat()
    settings = [
        ("site_name", "Atelier"),
        ("site_tagline", "A curated catalogue"),
        ("currency_symbol", "₹"),
        ("currency_code", "INR"),
        ("test_checkout_mode", "true"),
    ]
    for k, v in settings:
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

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

    from werkzeug.security import generate_password_hash
    conn.execute(
        "INSERT OR IGNORE INTO admin_users (id, username, password_hash, role, permissions) VALUES (?, ?, ?, ?, ?)",
        (1, "admin", generate_password_hash("admin"), "master", '["*"]'),
    )
    conn.commit()
    conn.close()


# Seed once at module load
_seed_test_data()


class TestHomePage(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        # Invalidate the catalog cache so seeded data is picked up
        invalidate_catalog_cache()

    def test_home_200(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Atelier", resp.data)

    def test_home_products_rendered(self):
        resp = self.client.get("/")
        self.assertIn(b"Digital Widget", resp.data)
        self.assertIn(b"499", resp.data)

    def test_home_404(self):
        resp = self.client.get("/nonexistent-page")
        self.assertEqual(resp.status_code, 404)


class TestSearch(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        invalidate_catalog_cache()

    def test_search_widget(self):
        resp = self.client.get("/?q=widget")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Digital Widget", resp.data)

    def test_search_no_results(self):
        resp = self.client.get("/?q=zzzzznotfound")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"Digital Widget", resp.data)


class TestProductDetail(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        invalidate_catalog_cache()

    def test_product_page(self):
        resp = self.client.get("/product/digital-widget")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Digital Widget", resp.data)
        self.assertIn(b"499", resp.data)

    def test_product_sold_count(self):
        resp = self.client.get("/product/digital-widget")
        self.assertEqual(resp.status_code, 200)
        # No sold count since no orders exist
        self.assertNotIn(b"0 sold", resp.data)  # only shown when > 0

    def test_product_not_found(self):
        resp = self.client.get("/product/nonexistent-product")
        self.assertEqual(resp.status_code, 404)


class TestCart(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        invalidate_catalog_cache()
        with self.client.session_transaction() as sess:
            sess["csrf_token"] = "test-csrf-token"

    def _csrf(self):
        return {"csrf_token": "test-csrf-token"}

    def test_empty_cart(self):
        resp = self.client.get("/cart")
        self.assertEqual(resp.status_code, 200)

    def test_add_to_cart(self):
        data = {"product_id": "1", "quantity": "1", **self._csrf()}
        resp = self.client.post("/cart/add", data=data, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

    def test_add_sold_out_product(self):
        """Adding a zero-stock product returns 410 Gone (product-level check)."""
        data = {"product_id": "4", "quantity": "1", **self._csrf()}
        resp = self.client.post("/cart/add", data=data, follow_redirects=True)
        self.assertEqual(resp.status_code, 410)

    def test_cart_remove(self):
        with self.client as c:
            c.post("/cart/add", data={**self._csrf(), "product_id": "1", "quantity": "1"})
            resp = c.post("/cart/remove/1", data=self._csrf(), follow_redirects=True)
            self.assertEqual(resp.status_code, 200)


class TestHealthEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_health(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.decode(), "ok")

    def test_healthz(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.decode(), "ok")


class TestApiEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        invalidate_catalog_cache()

    def test_api_search(self):
        resp = self.client.get("/api/search?q=widget")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("results", data)

    def test_api_search_short_query(self):
        resp = self.client.get("/api/search?q=a")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(len(data["results"]), 0)

    def test_api_quick_view(self):
        resp = self.client.get("/api/product/1/quick-view")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["name"], "Digital Widget")

    def test_api_recent_purchases(self):
        resp = self.client.get("/api/recent-purchases/1")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data, [])  # no orders yet

    def test_api_cart_preview(self):
        resp = self.client.get("/api/cart/preview")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("count", data)
        self.assertIn("subtotal", data)


class TestCSPReport(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_csp_report_returns_204(self):
        resp = self.client.post("/csp-report",
                                content_type="application/json",
                                data=json.dumps({"csp-report": {"blocked-uri": "http://evil.com"}}))
        self.assertEqual(resp.status_code, 204)


class TestRobotsSitemap(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_robots_txt(self):
        resp = self.client.get("/robots.txt")
        self.assertEqual(resp.status_code, 200)

    def test_sitemap_xml(self):
        resp = self.client.get("/sitemap.xml")
        self.assertEqual(resp.status_code, 200)


class TestTimezone(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["csrf_token"] = "test-csrf-token"

    def test_set_timezone(self):
        resp = self.client.post("/set-timezone",
                                content_type="application/json",
                                data=json.dumps({"csrf_token": "test-csrf-token", "offset": 330}))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.decode(), "ok")


class TestSorting(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        invalidate_catalog_cache()

    def test_sort_by_price(self):
        resp = self.client.get("/?sort=price_low")
        self.assertEqual(resp.status_code, 200)

    def test_sort_popular(self):
        resp = self.client.get("/?sort=popular")
        self.assertEqual(resp.status_code, 200)

    def test_sort_name(self):
        resp = self.client.get("/?sort=name")
        self.assertEqual(resp.status_code, 200)


class TestCategoryFilter(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        invalidate_catalog_cache()

    def test_category_filter(self):
        resp = self.client.get("/?category=Widgets")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Digital Widget", resp.data)

    def test_category_no_results(self):
        resp = self.client.get("/?category=NonExistent")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"Digital Widget", resp.data)


if __name__ == "__main__":
    unittest.main()
