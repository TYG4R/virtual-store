"""
Tiny Razorpay wrapper. We deliberately avoid the official `razorpay`
python SDK to keep the dependency list short — the Orders API is a
simple authenticated REST call, and signature verification is just
one line of HMAC-SHA256.
"""
import hmac
import hashlib
import os
import requests

import config

API_BASE = "https://api.razorpay.com/v1"


def _auth():
    return (config.RAZORPAY_KEY_ID, config.RAZORPAY_KEY_SECRET)


def is_configured():
    return bool(config.RAZORPAY_KEY_ID and config.RAZORPAY_KEY_SECRET)


def create_order(amount_rupees, receipt, notes=None):
    """
    Creates a Razorpay order. Amount must be sent to Razorpay in paise.
    Returns the parsed JSON response (contains 'id' = razorpay_order_id).
    """
    payload = {
        "amount": int(round(amount_rupees * 100)),
        "currency": "INR",
        "receipt": receipt,
        "notes": notes or {},
    }
    resp = requests.post(f"{API_BASE}/orders", json=payload, auth=_auth(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def verify_payment_signature(razorpay_order_id, razorpay_payment_id, razorpay_signature):
    """
    Recomputes the HMAC-SHA256 signature Razorpay sends back after a
    successful checkout and compares it to what the browser gave us.
    This is what proves the payment wasn't tampered with client-side.
    """
    body = f"{razorpay_order_id}|{razorpay_payment_id}"
    expected = hmac.new(
        config.RAZORPAY_KEY_SECRET.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, razorpay_signature)


def create_refund(payment_id, amount_paise, notes=None):
    """
    Issues a refund via Razorpay's /v1/payments/{id}/refund endpoint.
    Use amount_paise=0 or omit for a full refund; specify amount_paise for a
    partial refund. Returns the parsed JSON response on success (contains 'id'
    = razorpay_refund_id). Raises requests.HTTPError on failure.
    """
    if not payment_id:
        raise ValueError("payment_id is required for Razorpay refund")
    payload = {"notes": notes or {}}
    if amount_paise:
        payload["amount"] = int(amount_paise)
    resp = requests.post(
        f"{API_BASE}/payments/{payment_id}/refund",
        json=payload, auth=_auth(), timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_order_payments(order_id):
    """
    Fetch all payments made for a Razorpay order. Returns a list of payment
    dicts, each containing at least 'id', 'status', 'captured', etc.

    Used by the reconciliation cron to detect payments that succeeded on
    Razorpay's side but were not recorded locally (e.g. webhook missed).
    """
    resp = requests.get(
        f"{API_BASE}/orders/{order_id}/payments",
        auth=_auth(),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("items", [])


def verify_webhook_signature(webhook_body, webhook_signature):
    """
    Verifies a Razorpay webhook signature. Razorpay sends the raw request body
    (not JSON-parsed) as the message, HMAC-SHA256 with the webhook secret as key.
    The webhook secret is separate from the API key secret and is configured in
    the Razorpay dashboard under Settings -> Webhooks.
    """
    webhook_secret = config.RAZORPAY_WEBHOOK_SECRET
    if not webhook_secret:
        return False
    expected = hmac.new(
        webhook_secret.encode("utf-8"),
        webhook_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, webhook_signature)
