"""
Standalone script to reconcile Razorpay orders — detects payments that
succeeded on Razorpay's side but were never recorded locally (e.g. the
webhook was missed, or the customer closed the browser before the redirect).

Run via cron/scheduler:  python3 reconcile_razorpay.py

Safe to run repeatedly (idempotent). Only touches orders that are still in
'created' status and have a razorpay_order_id set.
"""
import os
import sys
import logging
from datetime import datetime, timezone, timedelta

# Ensure the app root is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import database as db
import razorpay_client as rzp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("reconcile_razorpay")


def get_db():
    """Get a database connection outside of Flask context."""
    return db.get_db()


def main():
    if not rzp.is_configured():
        logger.warning("Razorpay not configured — skipping reconciliation.")
        return

    now = datetime.now(timezone.utc)
    # Only look at orders older than 30 minutes — anything younger might
    # still be in-flight (customer hasn't been redirected back yet).
    min_age = (now - timedelta(minutes=30)).isoformat()

    conn = get_db()

    # Find orders that were 'created' locally, have a Razorpay order ID,
    # and are at least 30 minutes old so we know the payment window has closed.
    orders = conn.execute(
        """SELECT id, order_ref, razorpay_order_id, amount, created_at
           FROM orders
           WHERE status = 'created'
             AND razorpay_order_id IS NOT NULL
             AND razorpay_order_id != ''
             AND created_at <= ?
           ORDER BY created_at ASC""",
        (min_age,),
    ).fetchall()

    if not orders:
        logger.info("No orders found to reconcile.")
        conn.close()
        return

    reconciled = 0
    errors = 0

    for row in orders:
        order_id = row["id"]
        order_ref = row["order_ref"]
        rzp_order_id = row["razorpay_order_id"]

        try:
            payments = rzp.fetch_order_payments(rzp_order_id)
        except Exception as exc:
            logger.error(
                "Failed to fetch payments for order %s (razorpay=%s): %s",
                order_ref, rzp_order_id, exc,
            )
            errors += 1
            continue

        # Look for a successful (captured) payment in the list.
        successful_payment = None
        for p in payments:
            if p.get("status") == "captured" and p.get("captured") is True:
                successful_payment = p
                break

        if not successful_payment:
            # No successful payment found — order genuinely hasn't been paid.
            continue

        payment_id = successful_payment["id"]
        paid_at_ts = successful_payment.get("created_at")
        # Razorpay returns Unix timestamps in seconds.
        if paid_at_ts:
            paid_at = datetime.fromtimestamp(paid_at_ts, tz=timezone.utc).isoformat()
        else:
            paid_at = db.now()

        # Update the local order — mark as paid, record payment ID and timestamp.
        conn.execute(
            """UPDATE orders
               SET status = 'paid',
                   razorpay_payment_id = ?,
                   paid_at = ?,
                   razorpay_signature = COALESCE(razorpay_signature, 'reconciled')
               WHERE id = ?
                 AND status = 'created'""",
            (payment_id, paid_at, order_id),
        )

        logger.info(
            "Reconciled order %s: status created -> paid (payment=%s)",
            order_ref, payment_id,
        )
        reconciled += 1

    conn.commit()
    conn.close()

    logger.info(
        "Reconciliation complete: %d orders reconciled, %d errors, %d skipped.",
        reconciled, errors, len(orders) - reconciled - errors,
    )


if __name__ == "__main__":
    main()
