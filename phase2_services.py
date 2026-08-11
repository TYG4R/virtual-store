"""Small SQLite-native commerce primitives for Phase 2.

The functions accept an existing connection so callers can compose them with
existing request transactions. They intentionally do not start a worker or
introduce an ORM.
"""
import json
from datetime import datetime, timedelta, timezone


PAYMENT_STATES = {"pending", "authorized", "captured", "failed", "partially_refunded", "refunded"}
ORDER_STATES = {"created", "paid", "delivered", "cancelled", "refunded"}

_PAYMENT_TRANSITIONS = {
    "pending": {"authorized", "captured", "failed"},
    "authorized": {"captured", "failed", "refunded"},
    "captured": {"partially_refunded", "refunded"},
    "partially_refunded": {"refunded"},
    "failed": {"pending", "captured"},
    "refunded": set(),
}
_ORDER_TRANSITIONS = {
    "created": {"paid", "cancelled"},
    "paid": {"delivered", "cancelled", "refunded"},
    "delivered": {"cancelled", "refunded"},
    "cancelled": set(),
    "refunded": set(),
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row_value(row, name, default=None):
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return default


def transition_payment_state(conn, order_id, new_state, *, expected_states=None):
    """Guard the denormalized order payment state.

    Returns True when the requested state is already current or was applied;
    returns False for a missing row or a disallowed stale transition.
    """
    if new_state not in PAYMENT_STATES:
        raise ValueError(f"Unknown payment state: {new_state}")
    row = conn.execute("SELECT payment_state, status FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not row:
        return False
    current = _row_value(row, "payment_state") or "pending"
    # Existing databases may have status populated before the additive field.
    if current == "pending" and _row_value(row, "status") in ("paid", "delivered"):
        current = "captured"
    if current == new_state:
        return True
    if expected_states is not None and current not in set(expected_states):
        return False
    if new_state not in _PAYMENT_TRANSITIONS.get(current, set()):
        return False
    updated = conn.execute(
        "UPDATE orders SET payment_state = ? WHERE id = ? AND payment_state = ?",
        (new_state, order_id, _row_value(row, "payment_state") or "pending"),
    )
    return updated.rowcount == 1


def transition_order_state(conn, order_id, new_state, *, expected_states=None):
    """Guard the additive order state without changing legacy ``status``."""
    if new_state not in ORDER_STATES:
        raise ValueError(f"Unknown order state: {new_state}")
    row = conn.execute("SELECT order_state, status FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not row:
        return False
    current = _row_value(row, "order_state") or "created"
    legacy_status = _row_value(row, "status")
    if current == "created" and legacy_status in ORDER_STATES:
        current = legacy_status
    if current == new_state:
        return True
    if expected_states is not None and current not in set(expected_states):
        return False
    if new_state not in _ORDER_TRANSITIONS.get(current, set()):
        return False
    stored_current = _row_value(row, "order_state") or "created"
    updated = conn.execute(
        "UPDATE orders SET order_state = ? WHERE id = ? AND order_state = ?",
        (new_state, order_id, stored_current),
    )
    return updated.rowcount == 1


def mark_payment_captured(conn, order_id, *, provider="razorpay", provider_order_id=None,
                          provider_payment_id=None, amount=None, currency="INR", metadata=None):
    """Capture a payment once and set the separate order state fields.

    ``order_payments`` is intentionally not required here: the narrow Phase 2
    foundation keeps the existing order payment identifiers while exposing
    guarded payment/order state transitions. A later payment ledger can build
    on the additive outbox/inbox tables without changing this contract.
    """
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not row:
        return False
    current = _row_value(row, "payment_state") or "pending"
    if current == "pending" and _row_value(row, "status") in ("paid", "delivered"):
        current = "captured"
    if current != "pending":
        return False
    if not transition_payment_state(conn, order_id, "captured", expected_states={"pending"}):
        return False
    transition_order_state(conn, order_id, "paid", expected_states={"created"})
    return True


def record_webhook_event(conn, event_id, event_type, payload, *, provider="razorpay", signature=""):
    """Insert one inbox row and return True only for a first-seen event."""
    if not event_id:
        raise ValueError("event_id is required")
    raw_payload = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True, separators=(",", ":"))
    cursor = conn.execute(
        """INSERT OR IGNORE INTO payment_events
           (provider, event_id, event_type, payload_json, signature, status, received_at)
           VALUES (?, ?, ?, ?, ?, 'received', ?)""",
        (provider, str(event_id), event_type or "", raw_payload, signature or "", _now()),
    )
    return cursor.rowcount == 1


def mark_webhook_event_processed(conn, event_id, *, status="processed", error=None, provider="razorpay"):
    conn.execute(
        """UPDATE payment_events
           SET status = ?, processed_at = ?, error = ?
           WHERE provider = ? AND event_id = ?""",
        (status, _now(), error, provider, str(event_id)),
    )


def enqueue_outbox_job(conn, job_type, payload, *, idempotency_key, max_attempts=5, available_at=None):
    """Enqueue one durable job idempotently and return its id."""
    if not idempotency_key:
        raise ValueError("idempotency_key is required")
    raw_payload = payload if isinstance(payload, str) else json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))
    now = _now()
    conn.execute(
        """INSERT OR IGNORE INTO outbox_jobs
           (job_type, payload_json, idempotency_key, status, attempts, max_attempts,
            available_at, created_at, updated_at)
           VALUES (?, ?, ?, 'pending', 0, ?, ?, ?, ?)""",
        (job_type, raw_payload, idempotency_key, max_attempts, available_at or now, now, now),
    )
    row = conn.execute("SELECT id FROM outbox_jobs WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
    return row["id"] if row else None


def claim_outbox_job(conn, *, job_type=None, now=None, worker_id="phase2", lease_seconds=300):
    """Claim the oldest due job and persist its lease."""
    now = now or _now()
    where = "status IN ('pending', 'retry') AND available_at <= ?"
    params = [now]
    if job_type:
        where += " AND job_type = ?"
        params.append(job_type)
    row = conn.execute(f"SELECT * FROM outbox_jobs WHERE {where} ORDER BY id LIMIT 1", params).fetchone()
    if not row:
        return None
    lease_until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
    updated = conn.execute(
        """UPDATE outbox_jobs
           SET status = 'processing', attempts = attempts + 1,
               locked_at = ?, locked_by = ?, updated_at = ?
           WHERE id = ? AND status IN ('pending', 'retry')""",
        (lease_until, worker_id, now, row["id"]),
    )
    if updated.rowcount != 1:
        return None
    return conn.execute("SELECT * FROM outbox_jobs WHERE id = ?", (row["id"],)).fetchone()


def complete_outbox_job(conn, job_id):
    conn.execute(
        """UPDATE outbox_jobs SET status = 'completed', updated_at = ?,
           locked_at = NULL, locked_by = NULL WHERE id = ? AND status = 'processing'""",
        (_now(), job_id),
    )


def retry_outbox_job(conn, job_id, error, *, delay_seconds=60, base_delay_seconds=None,
                     max_delay_seconds=300):
    """Put a claimed job back into retry with bounded exponential backoff."""
    row = conn.execute("SELECT attempts FROM outbox_jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return False
    base = base_delay_seconds if base_delay_seconds is not None else delay_seconds
    attempts = int(row["attempts"] or 0)
    delay = min(max_delay_seconds, max(0, base) * (2 ** max(0, attempts - 1)))
    available = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
    conn.execute(
        """UPDATE outbox_jobs SET status = 'retry', last_error = ?,
           available_at = ?, locked_at = NULL, locked_by = NULL, updated_at = ?
           WHERE id = ?""",
        (str(error or "")[:2000], available, _now(), job_id),
    )
    return True


def issue_entitlement(conn, order_id, product_id, *, customer_id=None, metadata=None):
    """Issue one active entitlement per order/product, idempotently."""
    raw_metadata = metadata if isinstance(metadata, str) else json.dumps(metadata or {}, sort_keys=True)
    now = _now()
    conn.execute(
        """INSERT OR IGNORE INTO entitlements
           (order_id, customer_id, product_id, status, metadata, issued_at)
           VALUES (?, ?, ?, 'active', ?, ?)""",
        (order_id, customer_id, product_id, raw_metadata, now),
    )
    row = conn.execute("SELECT * FROM entitlements WHERE order_id = ? AND product_id = ?", (order_id, product_id)).fetchone()
    if row and row["status"] == "revoked":
        conn.execute(
            "UPDATE entitlements SET status = 'active', revoked_at = NULL, revoke_reason = '' WHERE id = ?",
            (row["id"],),
        )
    return row["id"] if row else None


def issue_entitlements(conn, *, order_id, product_ids, customer_id=None, metadata=None):
    return [issue_entitlement(conn, order_id, pid, customer_id=customer_id, metadata=metadata)
            for pid in dict.fromkeys(pid for pid in product_ids if pid)]


def revoke_entitlement(conn, entitlement_id, *, reason=""):
    updated = conn.execute(
        """UPDATE entitlements SET status = 'revoked', revoked_at = ?, revoke_reason = ?
           WHERE id = ? AND status != 'revoked'""",
        (_now(), reason or "", entitlement_id),
    )
    return updated.rowcount == 1


def revoke_entitlements(conn, *, order_id=None, product_ids=None, reason=""):
    clauses = ["status != 'revoked'"]
    params = []
    if order_id is not None:
        clauses.append("order_id = ?")
        params.append(order_id)
    if product_ids:
        ids = list(dict.fromkeys(pid for pid in product_ids if pid))
        clauses.append("product_id IN (" + ",".join("?" for _ in ids) + ")")
        params.extend(ids)
    updated = conn.execute(
        f"UPDATE entitlements SET status = 'revoked', revoked_at = ?, revoke_reason = ? WHERE {' AND '.join(clauses)}",
        [_now(), reason or "", *params],
    )
    return updated.rowcount


def record_download_audit(conn, *, order_id=None, entitlement_id=None, download_token=None,
                          customer_id=None, product_id=None, token=None, success=True,
                          ip_address="", user_agent="", failure_reason=""):
    """Append a row for every protected-download attempt."""
    token = token if token is not None else download_token
    cursor = conn.execute(
        """INSERT INTO download_audit
           (order_id, entitlement_id, download_token, customer_id, product_id, success,
            ip_address, user_agent, failure_reason, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (order_id, entitlement_id, download_token or token, customer_id, product_id, 1 if success else 0,
         ip_address or "", user_agent or "", failure_reason or "", _now()),
    )
    return cursor.lastrowid


# Friendly aliases for callers/tests that prefer explicit guarded names.
guarded_payment_state = transition_payment_state
guarded_order_state = transition_order_state
claim_retry_outbox_job = claim_outbox_job
