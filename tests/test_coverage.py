"""Tests for payment/refund, download-token, TOTP login, and webhook verification.

Run with:  python3 -m unittest tests/test_coverage.py -v
"""
import sys
import os
import json
import hmac
import hashlib
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config

config.DATABASE_URL = ":memory:"
config.TURSO_DB_URL = None
config.TURSO_DB_AUTH_TOKEN = None
config.RAZORPAY_KEY_ID = "test_key"
config.RAZORPAY_KEY_SECRET = "test_secret"
config.RAZORPAY_WEBHOOK_SECRET = "whsec_test_secret"
config.SECRET_KEY = "test-secret-key-for-coverage"
config.DEBUG = False
config.CSRF_ENABLED = False
config.ADMIN_USERNAME = "admin"
config.ADMIN_PASSWORD = "testpass"
config.SITE_URL = "http://localhost:5000"

# Must import app after config patches
from app import app
import app as app_module
import database as db


_HELPERS_SUPPORTED = True
try:
    import helpers as hlp
except Exception:
    _HELPERS_SUPPORTED = False


def _make_admin_client():
    """Return a test client logged in as master admin."""
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["admin_id"] = 1
        sess["admin_username"] = "admin"
        sess["admin_permissions"] = ["*"]
        sess["admin_role"] = "master"
        sess["csrf_token"] = "test-csrf"
    return client


def _fresh_db():
    """Initialize a fresh in-memory database and return its connection."""
    db._db_initialized = False
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(db.SCHEMA)
    # Run SCHEMA_EXTRA but skip individual ALTER TABLE failures (columns that
    # now live in CREATE TABLE). CREATE TABLE IF NOT EXISTS lines are fine.
    for stmt in db.SCHEMA_EXTRA.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except Exception:
            pass
    for stmt in db.MIGRATIONS:
        try:
            conn.execute(stmt)
        except Exception:
            pass
    conn.commit()
    return conn


def _seed_product(conn):
    conn.execute(
        "INSERT OR IGNORE INTO products (id, name, slug, short_description, price, category, active, position, created_at, quantity, views, delivery_mode) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, 10, 120, 'manual')",
        (1, "Digital Widget", "digital-widget", "A fine widget", 499, "Widgets", db.now()),
    )
    conn.commit()


def _seed_order(conn):
    conn.execute(
        "INSERT INTO orders (order_ref, product_id, product_name, customer_name, "
        "customer_email, customer_phone, amount, status, created_at, paid_at, "
        "payment_mode, razorpay_payment_id, razorpay_order_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("ORD-TEST-001", 1, "Digital Widget", "Alice", "alice@test.com",
         "9999999999", 499, "paid", db.now(), db.now(), "gateway",
         "pay_test_payment_id", "order_test_order_id"),
    )
    conn.execute(
        "INSERT INTO order_items (order_id, product_id, product_name, unit_price, quantity, line_total) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (1, 1, "Digital Widget", 499, 1, 499),
    )
    conn.commit()
    return 1


class _PatchedGetDb:
    """Context manager that patches db.get_db to return a specific connection.

    The route handlers call conn.close() which would close ours.  We wrap the
    real conn so close() becomes a no-op inside the route.
    """

    def __init__(self, real_conn):
        self.real = real_conn

    def __enter__(self):
        proxy = _NoCloseConn(self.real)
        self.patcher = patch.object(db, "get_db", return_value=proxy)
        self.mock = self.patcher.start()
        return self.mock

    def __exit__(self, *args):
        self.patcher.stop()


class _NoCloseConn:
    """Wraps a sqlite3.Connection so .close() is a no-op.

    Forwards every attribute/method to the wrapped connection, so this
    works with sqlite3.Row row_factory, PRAGMA calls, etc.
    """

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)

    def close(self):
        pass  # no-op — don't close our test connection


# ── Webhook signature verification tests (no DB needed) ─────────────


class TestWebhookSignature(unittest.TestCase):
    """Tests for Razorpay webhook signature verification."""

    def test_valid_signature(self):
        body = b'{"event":"payment.captured"}'
        expected_sig = hmac.new(b"whsec_test_secret", body, hashlib.sha256).hexdigest()
        from razorpay_client import verify_webhook_signature
        self.assertTrue(verify_webhook_signature(body, expected_sig))

    def test_invalid_signature(self):
        body = b'{"event":"payment.captured"}'
        from razorpay_client import verify_webhook_signature
        self.assertFalse(verify_webhook_signature(body, "invalid_signature"))

    def test_missing_webhook_secret(self):
        body = b'{"event":"payment.captured"}'
        with patch.object(config, "RAZORPAY_WEBHOOK_SECRET", ""):
            from razorpay_client import verify_webhook_signature
            self.assertFalse(verify_webhook_signature(body, "anything"))

    def test_empty_body_signature(self):
        from razorpay_client import verify_webhook_signature
        self.assertFalse(verify_webhook_signature(b"", ""))

    def test_create_refund_validates_payment_id(self):
        from razorpay_client import create_refund
        with self.assertRaises(ValueError):
            create_refund("", 100)


# ── Razorpay client unit tests (no DB needed) ───────────────────────


class TestRazorpayClient(unittest.TestCase):
    """Direct unit tests for razorpay_client.py functions."""

    def test_create_refund_requires_payment_id(self):
        from razorpay_client import create_refund
        with self.assertRaises(ValueError):
            create_refund(None, 100)

    def test_is_configured_with_keys(self):
        from razorpay_client import is_configured
        self.assertTrue(is_configured())

    def test_is_configured_without_keys(self):
        with patch.object(config, "RAZORPAY_KEY_ID", ""):
            with patch.object(config, "RAZORPAY_KEY_SECRET", ""):
                from razorpay_client import is_configured
                self.assertFalse(is_configured())


# ── Refund route tests (P0-1) ────────────────────────────────────────


class TestAdminOrderCancel(unittest.TestCase):
    """Tests for the admin_order_cancel route with Razorpay integration."""

    def setUp(self):
        self.conn = _fresh_db()
        _seed_product(self.conn)
        self.client = _make_admin_client()

    def tearDown(self):
        self.conn.close()

    @patch("razorpay_client.create_refund")
    def test_full_refund(self, mock_refund):
        oid = _seed_order(self.conn)
        mock_refund.return_value = {"id": "rfnd_test_abc123", "status": "processed"}
        with _PatchedGetDb(self.conn):
            resp = self.client.post(
                f"/admin/orders/{oid}/cancel",
                data={"refund_amount": "499", "csrf_token": "test-csrf"},
            )
        self.assertEqual(resp.status_code, 302)
        order = self.conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
        self.assertEqual(order["status"], "cancelled")
        self.assertEqual(order["refunded_amount"], 499)
        self.assertEqual(order["razorpay_refund_id"], "rfnd_test_abc123")
        mock_refund.assert_called_once()

    @patch("razorpay_client.create_refund")
    def test_partial_refund(self, mock_refund):
        oid = _seed_order(self.conn)
        mock_refund.return_value = {"id": "rfnd_test_partial"}
        with _PatchedGetDb(self.conn):
            resp = self.client.post(
                f"/admin/orders/{oid}/cancel",
                data={"refund_amount": "200", "csrf_token": "test-csrf"},
            )
        self.assertEqual(resp.status_code, 302)
        order = self.conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
        self.assertEqual(order["refunded_amount"], 200)

    @patch("razorpay_client.create_refund")
    def test_zero_refund_skips_razorpay(self, mock_refund):
        oid = _seed_order(self.conn)
        with _PatchedGetDb(self.conn):
            resp = self.client.post(
                f"/admin/orders/{oid}/cancel",
                data={"refund_amount": "0", "csrf_token": "test-csrf"},
            )
        mock_refund.assert_not_called()
        self.assertEqual(resp.status_code, 302)
        order = self.conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
        self.assertEqual(order["status"], "cancelled")
        self.assertEqual(order["refunded_amount"], 0)

    @patch("razorpay_client.create_refund")
    def test_empty_refund_string(self, mock_refund):
        oid = _seed_order(self.conn)
        with _PatchedGetDb(self.conn):
            resp = self.client.post(
                f"/admin/orders/{oid}/cancel",
                data={"refund_amount": "", "csrf_token": "test-csrf"},
            )
        mock_refund.assert_not_called()
        self.assertEqual(resp.status_code, 302)
        order = self.conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
        self.assertEqual(order["status"], "cancelled")

    @patch("razorpay_client.create_refund")
    def test_negative_refund(self, mock_refund):
        oid = _seed_order(self.conn)
        with _PatchedGetDb(self.conn):
            resp = self.client.post(
                f"/admin/orders/{oid}/cancel",
                data={"refund_amount": "-50", "csrf_token": "test-csrf"},
            )
        mock_refund.assert_not_called()
        self.assertEqual(resp.status_code, 302)

    @patch("razorpay_client.create_refund")
    def test_refund_capped_at_order_amount(self, mock_refund):
        oid = _seed_order(self.conn)
        mock_refund.return_value = {"id": "rfnd_capped"}
        with _PatchedGetDb(self.conn):
            resp = self.client.post(
                f"/admin/orders/{oid}/cancel",
                data={"refund_amount": "9999", "csrf_token": "test-csrf"},
            )
        self.assertEqual(resp.status_code, 302)
        order = self.conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
        self.assertLessEqual(order["refunded_amount"], 499)

    @patch("razorpay_client.create_refund")
    def test_refund_api_failure_does_not_cancel(self, mock_refund):
        oid = _seed_order(self.conn)
        mock_refund.side_effect = Exception("Razorpay API error")
        with _PatchedGetDb(self.conn):
            resp = self.client.post(
                f"/admin/orders/{oid}/cancel",
                data={"refund_amount": "499", "csrf_token": "test-csrf"},
            )
        self.assertEqual(resp.status_code, 302)
        order = self.conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
        self.assertEqual(order["status"], "paid",
                         "Order should NOT be cancelled when Razorpay fails")

    @patch("razorpay_client.create_refund")
    def test_no_payment_id_skips_razorpay(self, mock_refund):
        self.conn.execute(
            "INSERT INTO orders (order_ref, product_id, product_name, customer_name, "
            "customer_email, customer_phone, amount, status, created_at, paid_at, "
            "payment_mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("ORD-NO-PAYID", 1, "Test", "Bob", "bob@test.com", "8888888888",
             199, "paid", db.now(), db.now(), "gateway"),
        )
        self.conn.commit()
        order_id = self.conn.execute(
            "SELECT id FROM orders WHERE order_ref = ?", ("ORD-NO-PAYID",)
        ).fetchone()["id"]
        with _PatchedGetDb(self.conn):
            resp = self.client.post(
                f"/admin/orders/{order_id}/cancel",
                data={"refund_amount": "199", "csrf_token": "test-csrf"},
            )
        mock_refund.assert_not_called()
        self.assertEqual(resp.status_code, 302)
        order = self.conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        self.assertEqual(order["status"], "cancelled")

    @patch("razorpay_client.is_configured", return_value=False)
    @patch("razorpay_client.create_refund")
    def test_unconfigured_razorpay_skips_api(self, mock_refund, _mock_conf):
        oid = _seed_order(self.conn)
        with _PatchedGetDb(self.conn):
            resp = self.client.post(
                f"/admin/orders/{oid}/cancel",
                data={"refund_amount": "499", "csrf_token": "test-csrf"},
            )
        mock_refund.assert_not_called()
        self.assertEqual(resp.status_code, 302)
        order = self.conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
        self.assertEqual(order["status"], "cancelled")

    def test_requires_refund_permission(self):
        """Test permission rejection — doesn't need our patched DB."""
        client = app.test_client()
        # Seed an admin user with LIMITED permissions (no orders.refund/edit)
        # so the requires_permission decorator rejects the request.
        db._db_initialized = False
        db.init_db_if_needed()
        conn = db.get_db()
        from werkzeug.security import generate_password_hash
        existing = conn.execute("SELECT id FROM admin_users WHERE username = 'perm_admin'").fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO admin_users (id, username, password_hash, role, permissions) VALUES (?, ?, ?, ?, ?)",
                (99, "perm_admin", generate_password_hash("admin"), "admin", '["orders.view"]'),
            )
            conn.commit()
            admin_id = 99
        else:
            admin_id = existing["id"]
        conn.close()

        with client.session_transaction() as sess:
            sess["admin_id"] = admin_id
            sess["admin_permissions"] = ["orders.view"]
            sess["csrf_token"] = "test-csrf"
        resp = client.post("/admin/orders/1/cancel", data={"refund_amount": "0", "csrf_token": "test-csrf"})
        self.assertIn(resp.status_code, (302, 403))

    @patch("razorpay_client.create_refund")
    def test_cancel_missing_order_404(self, mock_refund):
        with _PatchedGetDb(self.conn):
            resp = self.client.post(
                "/admin/orders/9999/cancel",
                data={"refund_amount": "0", "csrf_token": "test-csrf"},
            )
        self.assertEqual(resp.status_code, 404)
        mock_refund.assert_not_called()


# ── Payment/order correctness regressions ────────────────────────────


class TestPaymentOrderCorrectness(unittest.TestCase):
    """Regression tests for payment confirmation and inventory accounting."""

    def setUp(self):
        self.conn = _fresh_db()
        _seed_product(self.conn)
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES ('test_checkout_mode', 'true')"
        )
        self.conn.commit()
        app_module.invalidate_settings_cache()
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["csrf_token"] = "test-csrf"

    def tearDown(self):
        app_module.invalidate_settings_cache()
        self.conn.close()

    def test_single_product_checkout_creates_item_and_decrements_stock(self):
        with _PatchedGetDb(self.conn):
            response = self.client.post(
                "/api/create-order",
                json={
                    "product_id": 1,
                    "name": "Alice",
                    "email": "alice@test.com",
                    "phone": "9999999999",
                },
                headers={"X-CSRF-Token": "test-csrf"},
            )

        self.assertEqual(response.status_code, 200)
        order_ref = response.get_json()["order_ref"]
        order = self.conn.execute(
            "SELECT * FROM orders WHERE order_ref = ?", (order_ref,)
        ).fetchone()
        item = self.conn.execute(
            "SELECT * FROM order_items WHERE order_id = ?", (order["id"],)
        ).fetchone()
        product = self.conn.execute(
            "SELECT quantity FROM products WHERE id = 1"
        ).fetchone()

        self.assertEqual(order["status"], "paid")
        self.assertIsNotNone(item)
        self.assertEqual(item["product_id"], 1)
        self.assertEqual(item["quantity"], 1)
        self.assertEqual(product["quantity"], 9)


# ── Download token tests ─────────────────────────────────────────────


class TestDownloadProduct(unittest.TestCase):
    """Tests for download token validation and expiry."""

    def setUp(self):
        self.conn = _fresh_db()
        _seed_product(self.conn)
        _seed_order(self.conn)  # create an order for download tokens to reference
        self.client = app.test_client()
        self._private_root = os.path.abspath(config.PRODUCT_UPLOAD_FOLDER)
        os.makedirs(self._private_root, exist_ok=True)
        self._test_file = os.path.join(self._private_root, "_dl_test_file.txt")

    def tearDown(self):
        try:
            os.remove(self._test_file)
        except FileNotFoundError:
            pass
        self.conn.close()

    def _create_dummy_file(self, path, content=b"test data"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)

    def _seed_token(self, remaining=5, expires_delta=72, token_str="test-dl-token", create_file=True):
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(hours=expires_delta)).isoformat()
        fpath = self._test_file
        if create_file:
            self._create_dummy_file(fpath)
        self.conn.execute(
            "INSERT INTO download_tokens (order_id, product_id, token, file_path, "
            "filename, expires_at, created_at, downloads_remaining) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (1, 1, token_str, fpath, "test.txt",
             expires, db.now(), remaining),
        )
        self.conn.commit()
        return fpath

    def test_valid_token_serves_file(self):
        self._seed_token()
        with _PatchedGetDb(self.conn):
            resp = self.client.get("/download/test-dl-token")
        self.assertEqual(resp.status_code, 200)

    def test_expired_token_returns_410(self):
        self._seed_token(expires_delta=-1)
        with _PatchedGetDb(self.conn):
            resp = self.client.get("/download/test-dl-token")
        self.assertEqual(resp.status_code, 410)

    def test_exhausted_token_returns_410(self):
        self._seed_token(remaining=0)
        with _PatchedGetDb(self.conn):
            resp = self.client.get("/download/test-dl-token")
        self.assertEqual(resp.status_code, 410)

    def test_missing_file_does_not_consume_token(self):
        self._seed_token(remaining=3, create_file=False)
        with _PatchedGetDb(self.conn):
            resp = self.client.get("/download/test-dl-token")
        self.assertEqual(resp.status_code, 404)
        remaining = self.conn.execute(
            "SELECT downloads_remaining FROM download_tokens WHERE token = ?",
            ("test-dl-token",),
        ).fetchone()[0]
        self.assertEqual(remaining, 3)

    def test_invalid_token_returns_404(self):
        with _PatchedGetDb(self.conn):
            resp = self.client.get("/download/invalid-token-xxx")
        self.assertEqual(resp.status_code, 404)

    def test_decrements_remaining(self):
        self._seed_token(remaining=3)
        with _PatchedGetDb(self.conn):
            resp = self.client.get("/download/test-dl-token")
        self.assertEqual(resp.status_code, 200)
        token = self.conn.execute(
            "SELECT downloads_remaining FROM download_tokens WHERE token = ?",
            ("test-dl-token",),
        ).fetchone()
        self.assertIsNotNone(token)
        self.assertEqual(token["downloads_remaining"], 2)


# ── TOTP admin login tests ────────────────────────────────────────────


# ── Phase 1 hardening regressions ────────────────────────────────────


class TestPhase1Hardening(unittest.TestCase):
    def setUp(self):
        self.conn = _fresh_db()
        self.client = app.test_client()
        self.patcher = _PatchedGetDb(self.conn)
        self.patcher.__enter__()
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES ('test_checkout_mode', 'true')"
        )
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES ('disable_payments', 'false')"
        )
        self.conn.commit()

    def tearDown(self):
        self.patcher.__exit__(None, None, None)
        self.conn.close()

    def test_otp_fails_closed_without_provider(self):
        config.TWILIO_ACCOUNT_SID = ""
        config.TWILIO_AUTH_TOKEN = ""
        config.TWILIO_FROM_NUMBER = ""
        config.OTP_DEV_MODE = False
        with self.client.session_transaction() as sess:
            sess["csrf_token"] = "test-csrf"
        response = self.client.post(
            "/auth/send-otp",
            json={"phone": "+919876543210"},
            headers={"X-CSRF-Token": "test-csrf"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM otps").fetchone()[0], 0)

    def test_checkout_context_honors_disable_payments(self):
        self.conn.execute(
            "UPDATE settings SET value = 'true' WHERE key = 'disable_payments'"
        )
        self.conn.commit()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Checkout temporarily disabled", response.data)

    def test_healthz_probes_database(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        with patch.object(db, "init_db_if_needed", side_effect=RuntimeError("db down")):
            response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 503)

    def test_delivery_requires_orders_edit_permission(self):
        _seed_product(self.conn)
        order_id = _seed_order(self.conn)
        from werkzeug.security import generate_password_hash
        self.conn.execute(
            "INSERT INTO admin_users (id, username, password_hash, role, permissions) "
            "VALUES (?, ?, ?, ?, ?)",
            (42, "viewer", generate_password_hash("testpass"), "support_agent", '["orders.view"]'),
        )
        self.conn.commit()
        with self.client.session_transaction() as sess:
            sess["admin_id"] = 42
            sess["admin_permissions"] = ["orders.view"]
            sess["admin_role"] = "support_agent"
            sess["csrf_token"] = "test-csrf"
        with patch.object(db, "init_db_if_needed"):
            response = self.client.post(
                f"/admin/orders/{order_id}/deliver",
                data={"delivery_message": "details", "csrf_token": "test-csrf"},
            )
        self.assertIn(response.status_code, (302, 403))
        self.assertEqual(
            self.conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()[0],
            "paid",
        )

    def test_private_storage_and_token_generation_after_payment(self):
        _seed_product(self.conn)
        product_path = os.path.join(config.PRODUCT_UPLOAD_FOLDER, "phase1.txt")
        os.makedirs(os.path.dirname(product_path), exist_ok=True)
        with open(product_path, "wb") as handle:
            handle.write(b"private product")
        self.conn.execute(
            "INSERT INTO product_files (product_id, filename, original_name, file_size, mime_type, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (1, product_path, "phase1.txt", 15, "text/plain", db.now()),
        )
        self.conn.commit()
        order_id = _seed_order(self.conn)
        self.conn.execute("UPDATE orders SET status = 'created' WHERE id = ?", (order_id,))
        self.conn.commit()
        order = self.conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        items = self.conn.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,)).fetchall()
        with patch.object(app_module, "notify_admins_new_order"), patch.object(app_module, "webpush_notify_admins_new_order"):
            with app.test_request_context(base_url="http://testserver"):
                app_module._confirm_order_payment(self.conn, order, items, payment_mode="test")
        token = self.conn.execute(
            "SELECT token, file_path FROM download_tokens WHERE order_id = ?", (order_id,)
        ).fetchone()
        self.assertIsNotNone(token)
        self.assertTrue(token["file_path"].startswith(config.PRODUCT_UPLOAD_FOLDER))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM download_tokens WHERE order_id = ?", (order_id,)).fetchone()[0], 1)

    def test_public_static_product_files_are_not_served(self):
        public_root = os.path.join(os.getcwd(), "static", "product_files")
        public_path = os.path.join(public_root, "_phase1_public.txt")
        os.makedirs(public_root, exist_ok=True)
        with open(public_path, "wb") as handle:
            handle.write(b"must stay private")
        try:
            with patch.object(db, "init_db_if_needed"):
                response = self.client.get("/static/product_files/_phase1_public.txt")
            self.assertEqual(response.status_code, 404)
        finally:
            try:
                os.remove(public_path)
            except FileNotFoundError:
                pass
            try:
                os.rmdir(public_root)
            except OSError:
                pass

    def test_admin_product_download_uses_private_storage(self):
        _seed_product(self.conn)
        from werkzeug.security import generate_password_hash
        self.conn.execute(
            "INSERT INTO admin_users (id, username, password_hash, role, permissions) "
            "VALUES (?, ?, ?, ?, ?)",
            (77, "phase1_admin", generate_password_hash("testpass"), "master", '["*"]'),
        )
        private_path = os.path.join(config.PRODUCT_UPLOAD_FOLDER, "_phase1_admin.txt")
        os.makedirs(os.path.dirname(private_path), exist_ok=True)
        with open(private_path, "wb") as handle:
            handle.write(b"admin private file")
        cursor = self.conn.execute(
            "INSERT INTO product_files (product_id, filename, original_name, file_size, mime_type, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (1, private_path, "admin.txt", 18, "text/plain", db.now()),
        )
        self.conn.commit()
        with self.client.session_transaction() as sess:
            sess["admin_id"] = 77
            sess["admin_permissions"] = ["*"]
            sess["admin_role"] = "master"
        try:
            with patch.object(db, "init_db_if_needed"):
                response = self.client.get(f"/admin/products/{cursor.lastrowid}/download")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"admin private file")
        finally:
            try:
                os.remove(private_path)
            except FileNotFoundError:
                pass

    def test_auto_email_setting_suppresses_order_email(self):
        _seed_product(self.conn)
        order_id = _seed_order(self.conn)
        self.conn.execute("UPDATE orders SET status = 'created' WHERE id = ?", (order_id,))
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES ('auto_email_enabled', 'false')"
        )
        self.conn.commit()
        app_module.invalidate_settings_cache()
        old_resend = config.RESEND_API_KEY
        config.RESEND_API_KEY = "configured-for-test"
        try:
            order = self.conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            items = self.conn.execute(
                "SELECT * FROM order_items WHERE order_id = ?", (order_id,)
            ).fetchall()
            with patch.object(app_module, "send_email") as mock_send, \
                 patch.object(app_module, "notify_admins_new_order"), \
                 patch.object(app_module, "webpush_notify_admins_new_order"):
                app_module._confirm_order_payment(self.conn, order, items, payment_mode="test")
            mock_send.assert_not_called()
        finally:
            config.RESEND_API_KEY = old_resend
            app_module.invalidate_settings_cache()


class TestTOTPAdminLogin(unittest.TestCase):
    """Tests for TOTP-based two-factor admin authentication."""

    def setUp(self):
        self.conn = _fresh_db()
        # Seed an admin user for login (password: testpass)
        from werkzeug.security import generate_password_hash
        pwhash = generate_password_hash("testpass")
        self.conn.execute(
            "INSERT OR IGNORE INTO admin_users (id, username, password_hash, role, permissions, is_active, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)",
            (1, "admin", pwhash, "master", '["*"]', db.now()),
        )
        # Seed settings
        self.conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('site_name', 'Test')",
        )
        self.conn.commit()
        self.client = app.test_client()
        self.patcher = _PatchedGetDb(self.conn)
        self.patcher.__enter__()

    def tearDown(self):
        self.patcher.__exit__(None, None, None)
        self.conn.close()

    def _enable_totp(self, admin_id=1, secret="JBSWY3DPEHPK3PXP"):
        self.conn.execute(
            "INSERT OR REPLACE INTO admin_totp_secrets (admin_id, secret, enabled, created_at) "
            "VALUES (?, ?, 1, ?)",
            (admin_id, secret, db.now()),
        )
        self.conn.commit()

    def _seed_recovery_code(self, admin_id=1, code_hash="AAAAA-BBBBB"):
        self.conn.execute(
            "INSERT INTO admin_recovery_codes (admin_id, code_hash, used, created_at) VALUES (?, ?, 0, ?)",
            (admin_id, code_hash, db.now()),
        )
        self.conn.commit()

    def _login_post(self, data):
        """POST to /admin/login with a CSRF token in session."""
        # Set up CSRF token in the session before posting
        with self.client.session_transaction() as sess:
            sess["csrf_token"] = "test-csrf"
        data["csrf_token"] = "test-csrf"
        return self.client.post("/admin/login", data=data)

    def test_totp_enabled_shows_form(self):
        self._enable_totp()
        resp = self._login_post({"username": "admin", "password": "testpass"})
        self.assertIn(resp.status_code, (200, 302))

    def test_recovery_code_used_once(self):
        self._enable_totp()
        self._seed_recovery_code(code_hash="AAAAA-BBBBB")
        resp = self._login_post({
            "username": "admin", "password": "testpass",
            "recovery_code": "AAAAA-BBBBB",
        })
        self.assertIn(resp.status_code, (200, 302))

    def test_wrong_password_with_totp_enabled(self):
        self._enable_totp()
        resp = self._login_post({"username": "admin", "password": "wrongpass"})
        self.assertIn(resp.status_code, (200, 400))


# ── Invoicing module tests (no DB needed) ────────────────────────────


class TestInvoicing(unittest.TestCase):
    """Tests for the invoicing module."""

    def _make_order_mock(self, overrides=None):
        data = {
            "id": 1, "order_ref": "ORD-TEST-001", "customer_name": "Alice",
            "customer_email": "alice@test.com", "customer_phone": "",
            "amount": 499, "discount_amount": 0, "coupon_code": "",
            "status": "paid", "razorpay_payment_id": "pay_test",
            "paid_at": "2026-07-21T12:00:00+00:00",
            "created_at": "2026-07-21T12:00:00+00:00",
        }
        if overrides:
            data.update(overrides)
        order = MagicMock()
        order.__getitem__.side_effect = lambda k: data.get(k, "")
        order.get.side_effect = lambda k, d=None: data.get(k, d)
        return order

    def test_generate_invoice_returns_bytes(self):
        import invoicing
        order = self._make_order_mock()
        item = MagicMock()
        item.__getitem__.side_effect = lambda k: {"product_id": 1, "quantity": 1, "unit_price": 499}.get(k, "")
        item.get.side_effect = lambda k, d=None: {"product_id": 1}.get(k, d)
        settings = {"business_name": "Test Store", "business_address": "Test", "gstin": ""}
        pdf_bytes, filename = invoicing.generate_and_save_invoice(
            order, [item], {1: {"name": "Digital Widget"}}, settings,
        )
        self.assertIsInstance(pdf_bytes, (bytes, bytearray))
        self.assertGreater(len(pdf_bytes), 100)
        self.assertIn("ORD-TEST-001", filename)

    def test_invoice_filename_format(self):
        import invoicing
        order = self._make_order_mock()
        item = MagicMock()
        item.__getitem__.side_effect = lambda k: {"product_id": 1, "quantity": 1, "unit_price": 100}.get(k, "")
        item.get.side_effect = lambda k, d=None: {"product_id": 1}.get(k, d)
        _, filename = invoicing.generate_and_save_invoice(
            order, [item], {1: {"name": "Widget"}},
            {"business_name": "S", "business_address": "", "gstin": ""},
        )
        self.assertTrue(filename.startswith("invoice-"))
        self.assertTrue(filename.endswith(".pdf"))

    def test_invoice_accepts_sqlite_rows(self):
        """Invoice generation must work with the rows returned by sqlite3."""
        import invoicing
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        order = conn.execute(
            """SELECT 1 AS id, 'ORD-SQLITE-001' AS order_ref,
                      'Alice' AS customer_name, 'alice@test.com' AS customer_email,
                      '' AS customer_phone, 499 AS amount, 0 AS discount_amount,
                      '' AS coupon_code, 'paid' AS status,
                      'pay_test' AS razorpay_payment_id,
                      '2026-07-21T12:00:00+00:00' AS paid_at,
                      '2026-07-21T12:00:00+00:00' AS created_at"""
        ).fetchone()
        item = conn.execute(
            """SELECT 1 AS product_id, 'Widget' AS product_name,
                      499 AS unit_price, 1 AS quantity, 499 AS line_total"""
        ).fetchone()
        try:
            pdf_bytes, filename = invoicing.generate_and_save_invoice(
                order, [item], {1: {"name": "Widget"}},
                {"business_name": "S", "business_address": "", "gstin": ""},
            )
        finally:
            conn.close()
        self.assertGreater(len(pdf_bytes), 100)
        self.assertEqual(filename, "invoice-ORD-SQLITE-001.pdf")


if __name__ == "__main__":
    unittest.main()
