"""
Standalone script to scan for abandoned carts and send recovery notifications.
Run via cron/scheduler: python3 recover_abandoned_carts.py

Abandoned carts older than 30 minutes and under 48 hours are eligible.
Sends a maximum of 2 recovery messages per cart.
"""
import os
import sys
import json
import sqlite3
from datetime import datetime, timezone, timedelta

# Ensure the app root is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import database as db

APP_URL = os.environ.get("APP_URL", "https://your-store.com")

def get_db():
    conn = db.get_db()
    return conn


def send_recovery_email(to_email, name, items_summary, recovery_ttl_hours=48):
    """Attempt to send a recovery email. Returns True on success."""
    from helpers import send_email, email_enabled
    if not email_enabled() or not to_email:
        return False
    subject = "You left something in your cart — complete your order?"
    body = (
        f"Hi {name or 'there'},\n\n"
        f"You recently added the following to your cart:\n{items_summary}\n\n"
        f"Your cart is saved and ready. Come back to complete your order anytime "
        f"within the next {recovery_ttl_hours} hours.\n\n"
        f"Browse again: {APP_URL}/cart\n\n"
        f"Thank you!"
    )
    send_email(to_email, subject, body)
    return True


def main():
    now = datetime.now(timezone.utc)
    cutoff_created = (now - timedelta(hours=48)).isoformat()
    min_age = (now - timedelta(minutes=30)).isoformat()

    conn = get_db()
    carts = conn.execute(
        """SELECT * FROM abandoned_carts
           WHERE status = 'active'
             AND created_at >= ? AND created_at <= ?
             AND notification_count < 2
             AND (last_notified_at IS NULL OR last_notified_at <= ?)
           ORDER BY created_at ASC""",
        (cutoff_created, min_age,
         (now - timedelta(hours=4)).isoformat()),  # don't re-notify within 4h
    ).fetchall()

    recovered = 0
    for cart in carts:
        if not cart["email"]:
            continue

        try:
            items = json.loads(cart["product_data"])
        except (json.JSONDecodeError, TypeError):
            continue

        if not items:
            continue

        lines = "\n".join(f"  - {it.get('name','Item')} x{it.get('quantity',1)}" for it in items)
        summary = f"Items:\n{lines}"

        ok = send_recovery_email(cart["email"], cart["name"], summary)
        if ok:
            conn.execute(
                "UPDATE abandoned_carts SET notification_count = notification_count + 1, "
                "last_notified_at = ? WHERE id = ?",
                (now.isoformat(), cart["id"]),
            )
            recovered += 1

    conn.commit()
    conn.close()
    print(f"Processed {len(carts)} abandoned carts, sent {recovered} recovery emails.")


if __name__ == "__main__":
    main()
