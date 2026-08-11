import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import database as db
from phase2_services import (
    claim_outbox_job,
    complete_outbox_job,
    enqueue_outbox_job,
    issue_entitlement,
    mark_payment_captured,
    record_download_audit,
    record_webhook_event,
    revoke_entitlement,
    transition_order_state,
)


class Phase2FoundationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        for stmt in db.MIGRATIONS:
            try:
                self.conn.execute(stmt)
            except Exception:
                pass
        for stmt in db.SCHEMA_EXTRA.split(";"):
            stmt = stmt.strip()
            if not stmt:
                continue
            try:
                self.conn.execute(stmt)
            except Exception:
                pass
        self.conn.execute("INSERT INTO products (name, slug, created_at) VALUES (?, ?, ?)", ("Test", "test", "now"))
        self.conn.execute(
            "INSERT INTO orders (order_ref, product_name, customer_name, customer_email, amount, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("ORD-1", "Test", "Buyer", "buyer@example.com", 100, "now"),
        )
        self.conn.commit()
        self.order_id = self.conn.execute("SELECT id FROM orders").fetchone()[0]
        self.product_id = self.conn.execute("SELECT id FROM products").fetchone()[0]

    def tearDown(self):
        self.conn.close()

    def test_schema_has_metrics_and_phase2_tables(self):
        names = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"performance_metrics", "payment_events", "outbox_jobs", "entitlements", "download_audit"}.issubset(names))

    def test_payment_and_order_state_guards_are_idempotent(self):
        self.assertTrue(mark_payment_captured(self.conn, self.order_id))
        self.assertFalse(mark_payment_captured(self.conn, self.order_id))
        self.assertEqual(self.conn.execute("SELECT payment_state FROM orders").fetchone()[0], "captured")
        self.assertEqual(self.conn.execute("SELECT order_state FROM orders").fetchone()[0], "paid")
        self.assertFalse(transition_order_state(self.conn, self.order_id, "created"))

    def test_webhook_event_is_deduplicated(self):
        payload = {"event": "payment.captured"}
        self.assertTrue(record_webhook_event(self.conn, "evt-1", "payment.captured", payload))
        self.assertFalse(record_webhook_event(self.conn, "evt-1", "payment.captured", payload))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM payment_events").fetchone()[0], 1)

    def test_outbox_is_idempotent_and_claimable(self):
        first = enqueue_outbox_job(self.conn, "order.paid", {"order_id": self.order_id}, idempotency_key="order-paid-1")
        second = enqueue_outbox_job(self.conn, "order.paid", {"order_id": self.order_id}, idempotency_key="order-paid-1")
        self.assertEqual(first, second)
        job = claim_outbox_job(self.conn)
        self.assertEqual(job["status"], "processing")
        complete_outbox_job(self.conn, job["id"])
        self.assertEqual(self.conn.execute("SELECT status FROM outbox_jobs").fetchone()[0], "completed")

    def test_entitlement_issue_and_revoke_are_idempotent(self):
        first = issue_entitlement(self.conn, self.order_id, self.product_id)
        second = issue_entitlement(self.conn, self.order_id, self.product_id)
        self.assertEqual(first, second)
        self.assertTrue(revoke_entitlement(self.conn, first, reason="refund"))
        self.assertFalse(revoke_entitlement(self.conn, first, reason="refund"))
        self.assertEqual(self.conn.execute("SELECT status FROM entitlements").fetchone()[0], "revoked")

    def test_download_audit_records_outcome(self):
        audit_id = record_download_audit(self.conn, order_id=self.order_id, product_id=self.product_id, success=False, failure_reason="missing")
        self.assertEqual(audit_id, 1)
        row = self.conn.execute("SELECT success, failure_reason FROM download_audit").fetchone()
        self.assertEqual((row[0], row[1]), (0, "missing"))


if __name__ == "__main__":
    unittest.main()
