"""
JSON API for the Android admin app.

Mirrors the existing server-rendered /admin/* routes in app.py, but:
  - auth is a bearer token (helpers.api_admin_required) instead of a session
    cookie, since a mobile app can't hold a browser cookie jar
  - every response is JSON: {"success": true, "data": {...}}
                          or {"success": false, "error": "..."}

This file is intentionally self-contained (it doesn't import from app.py, to
avoid a circular import — app.py imports *this* module to register the
blueprint). Where logic needs to match the web admin exactly (slugs, image
handling, coupon validation, delivery emails) it's re-implemented here in a
small, API-appropriate way rather than sharing app.py's form-parsing helpers.
"""
import re
import math
import json
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, g, session
from werkzeug.security import check_password_hash, generate_password_hash

import config
import database as db
import razorpay_client as rzp
from helpers import (
    api_admin_required, api_requires_permission, generate_api_token, hash_api_token,
    slugify, save_product_image, delete_file_quietly,
    send_email, email_enabled, rate_limited, has_permission,
)

PRESET_PERMISSIONS = {
    "order_manager": ["orders.view", "orders.edit", "orders.refund", "orders.export"],
    "catalog_manager": ["products.edit"],
    "support_agent": ["orders.view"],
    "admin_manager": ["admin.manage", "audit.view", "audit.export"],
    "content_manager": ["testimonials.manage", "faqs.manage", "newsletter.view"],
}

admin_api = Blueprint("admin_api", __name__, url_prefix="/api/admin")


def ok(data=None, status=200):
    return jsonify({"success": True, "data": data if data is not None else {}}), status


def err(message, status=400):
    return jsonify({"success": False, "error": message}), status


def _paginate_args():
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    per_page = 20
    return page, per_page


def _row_to_dict(row):
    return dict(row) if row is not None else None


def _invalidate_frontend_cache(*, catalog=False, settings=False):
    try:
        from app import invalidate_frontend_caches
        if catalog or settings:
            invalidate_frontend_caches()
    except Exception:
        pass


# ============================================================= AUTH

@admin_api.route("/login", methods=["POST"])
def login():
    if rate_limited("admin-api-login", max_attempts=8, window_seconds=300):
        return err("Too many login attempts — please wait a few minutes and try again.", 429)

    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    device_label = (data.get("device_label") or "").strip()

    if not username or not password:
        return err("Username and password are required.", 400)

    conn = db.get_db()
    user = conn.execute(
        "SELECT * FROM admin_users WHERE username = ?", (username,)
    ).fetchone()

    if not user or not check_password_hash(user["password_hash"], password):
        conn.close()
        return err("Incorrect username or password.", 401)

    # TOTP check — require a valid code when 2FA is enabled
    import pyotp
    totp_row = conn.execute(
        "SELECT * FROM admin_totp_secrets WHERE admin_id = ? AND enabled = 1",
        (user["id"],),
    ).fetchone()
    if totp_row:
        totp_code = (data.get("totp_code") or "").strip()
        if not totp_code or not pyotp.TOTP(totp_row["secret"]).verify(totp_code, valid_window=1):
            conn.close()
            return err("Two-factor authentication code is required or invalid.", 401)

    token = generate_api_token()
    conn.execute(
        "INSERT INTO api_tokens (admin_user_id, token_hash, device_label, created_at) "
        "VALUES (?, ?, ?, ?)",
        (user["id"], hash_api_token(token), device_label, db.now()),
    )
    conn.commit()
    conn.close()

    return ok({
        "token": token,
        "expires_in": config.API_TOKEN_EXPIRY_DAYS * 24 * 60 * 60,
        "username": user["username"],
    })


@admin_api.route("/logout", methods=["POST"])
@api_admin_required
def logout():
    data = request.get_json(silent=True) or {}
    fcm_token = data.get("fcm_token")

    conn = db.get_db()
    conn.execute("UPDATE api_tokens SET revoked = 1 WHERE id = ?", (g.api_token_id,))
    if fcm_token:
        conn.execute("DELETE FROM admin_devices WHERE fcm_token = ?", (fcm_token,))
    conn.commit()
    conn.close()
    return ok({"logged_out": True})


# ============================================================= DEVICES (push registration)

@admin_api.route("/device/register", methods=["POST"])
@api_admin_required
def device_register():
    data = request.get_json(silent=True) or {}
    fcm_token = (data.get("fcm_token") or "").strip()
    device_label = (data.get("device_label") or "").strip()
    platform = (data.get("platform") or "android").strip()

    if not fcm_token:
        return err("fcm_token is required.", 400)

    conn = db.get_db()
    conn.execute(
        "INSERT INTO admin_devices (admin_user_id, fcm_token, device_label, platform, created_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(fcm_token) DO UPDATE SET "
        "admin_user_id = excluded.admin_user_id, device_label = excluded.device_label, "
        "platform = excluded.platform, last_seen_at = excluded.last_seen_at",
        (g.admin_id, fcm_token, device_label, platform, db.now(), db.now()),
    )
    conn.commit()
    conn.close()
    return ok({"registered": True})


@admin_api.route("/devices", methods=["GET"])
@api_admin_required
def devices_list():
    conn = db.get_db()
    rows = conn.execute(
        "SELECT id, device_label, platform, created_at, last_seen_at FROM admin_devices "
        "WHERE admin_user_id = ? ORDER BY id DESC",
        (g.admin_id,),
    ).fetchall()
    conn.close()
    return ok({"devices": [dict(r) for r in rows]})


@admin_api.route("/devices/<int:device_id>", methods=["DELETE"])
@api_admin_required
def device_revoke(device_id):
    conn = db.get_db()
    conn.execute(
        "DELETE FROM admin_devices WHERE id = ? AND admin_user_id = ?",
        (device_id, g.admin_id),
    )
    conn.commit()
    conn.close()
    return ok({"revoked": True})


# Web Push subscription endpoints (session-based, used by the browser admin panel)
# These reuse the same admin_devices table with platform='web'.


@admin_api.route("/push-subscribe", methods=["POST"])
def push_subscribe():
    """Register a Web Push subscription from the browser admin panel."""
    from helpers import check_csrf
    check_csrf()
    # Require admin login via session
    if not session.get("admin_id"):
        return err("Not authenticated.", 401)
    data = request.get_json(silent=True) or {}
    sub = data.get("subscription")
    if not sub:
        return err("subscription is required.", 400)
    sub_json = json.dumps(sub)

    conn = db.get_db()
    conn.execute(
        "INSERT INTO admin_devices (admin_user_id, fcm_token, device_label, platform, created_at, last_seen_at) "
        "VALUES (?, ?, ?, 'web', ?, ?) "
        "ON CONFLICT(fcm_token) DO UPDATE SET "
        "admin_user_id = excluded.admin_user_id, last_seen_at = excluded.last_seen_at",
        (session["admin_id"], sub_json, "Browser (" + (request.user_agent or "Unknown") + ")", db.now(), db.now()),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@admin_api.route("/push-unsubscribe", methods=["POST"])
def push_unsubscribe():
    """Remove a Web Push subscription."""
    from helpers import check_csrf
    check_csrf()
    if not session.get("admin_id"):
        return err("Not authenticated.", 401)
    data = request.get_json(silent=True) or {}
    sub = data.get("subscription")
    if not sub:
        return err("subscription is required.", 400)
    sub_json = json.dumps(sub)

    conn = db.get_db()
    conn.execute(
        "DELETE FROM admin_devices WHERE fcm_token = ? AND admin_user_id = ? AND platform = 'web'",
        (sub_json, session["admin_id"]),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ============================================================= DASHBOARD

@admin_api.route("/dashboard", methods=["GET"])
@api_admin_required
def dashboard():
    conn = db.get_db()
    stats = {
        "products": conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"],
        "pending": conn.execute("SELECT COUNT(*) c FROM orders WHERE status = 'paid'").fetchone()["c"],
        "delivered": conn.execute("SELECT COUNT(*) c FROM orders WHERE status = 'delivered'").fetchone()["c"],
        "revenue": conn.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM orders WHERE status IN ('paid','delivered')"
        ).fetchone()["s"],
    }
    recent_orders = conn.execute(
        "SELECT * FROM orders ORDER BY id DESC LIMIT 8"
    ).fetchall()
    conn.close()
    return ok({
        "stats": stats,
        "recent_orders": [dict(r) for r in recent_orders],
        "razorpay_configured": rzp.is_configured(),
    })


# ============================================================= ORDERS

@admin_api.route("/orders", methods=["GET"])
@api_admin_required
def orders_list():
    status_filter = request.args.get("status", "")
    q = (request.args.get("q") or "").strip()
    page, per_page = _paginate_args()

    conn = db.get_db()
    clauses, params = [], []
    if status_filter:
        clauses.append("status = ?")
        params.append(status_filter)
    if q:
        clauses.append("(order_ref LIKE ? OR customer_name LIKE ? OR customer_email LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    total = conn.execute(f"SELECT COUNT(*) c FROM orders {where}", params).fetchone()["c"]
    orders = conn.execute(
        f"SELECT * FROM orders {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [per_page, (page - 1) * per_page],
    ).fetchall()
    conn.close()

    return ok({
        "orders": [dict(o) for o in orders],
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": max(1, math.ceil(total / per_page)),
    })


@admin_api.route("/orders/<int:order_id>", methods=["GET"])
@api_admin_required
def order_detail(order_id):
    conn = db.get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return err("Order not found.", 404)
    items = conn.execute(
        "SELECT * FROM order_items WHERE order_id = ?", (order_id,)
    ).fetchall()
    conn.close()
    return ok({"order": dict(order), "items": [dict(i) for i in items]})


@admin_api.route("/orders/<int:order_id>/deliver", methods=["POST"])
@api_admin_required
@api_requires_permission("orders.edit")
def order_deliver(order_id):
    data = request.get_json(silent=True) or {}
    message = (data.get("delivery_message") or "").strip()

    conn = db.get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return err("Order not found.", 404)

    order_items = conn.execute(
        "SELECT * FROM order_items WHERE order_id = ?", (order_id,)
    ).fetchall()
    conn.execute(
        "UPDATE orders SET status = 'delivered', delivery_message = ?, delivered_at = ? WHERE id = ?",
        (message, db.now(), order_id),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()

    if email_enabled():
        item_line = order["product_name"] if not order_items else ", ".join(
            f"{it['product_name']} x{it['quantity']}" for it in order_items
        )
        send_email(
            order["customer_email"],
            f"Your order {order['order_ref']} has been delivered",
            f"Hi {order['customer_name']},\n\n"
            f"Great news — your order for \"{item_line}\" is ready!\n\n"
            f"{message}\n\n"
            f"Order reference: {order['order_ref']}\n\nThank you for shopping with us.",
        )

    return ok({"order": dict(updated), "email_sent": email_enabled()})


@admin_api.route("/orders/<int:order_id>/cancel", methods=["POST"])
@api_admin_required
@api_requires_permission("orders.edit")
def order_cancel(order_id):
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()

    conn = db.get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return err("Order not found.", 404)

    conn.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (order_id,))
    conn.commit()
    updated = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    # `reason` isn't persisted today (no column for it) — accepted here so the
    # app's UI can offer the field without a backend error; add an
    # orders.cancel_reason column later if it should be stored/reported on.
    return ok({"order": dict(updated), "reason": reason})


# ============================================================= PRODUCTS

def _unique_slug(conn, name, exclude_id=None):
    base = slugify(name)
    slug = base
    i = 2
    while True:
        row = conn.execute(
            "SELECT 1 FROM products WHERE slug = ? AND id != ?",
            (slug, exclude_id if exclude_id is not None else -1),
        ).fetchone()
        if not row:
            return slug
        slug = f"{base}-{i}"
        i += 1


@admin_api.route("/products", methods=["GET"])
@api_admin_required
def products_list():
    q = (request.args.get("q") or "").strip()
    category = (request.args.get("category") or "").strip()
    conn = db.get_db()
    clauses, params = [], []
    if q:
        clauses.append("(name LIKE ? OR short_description LIKE ? OR category LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]
    if category:
        clauses.append("category = ?")
        params.append(category)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    products = conn.execute(
        f"SELECT * FROM products {where} ORDER BY position ASC, id DESC", params
    ).fetchall()

    # Batch-load images in one query (fixes N+1)
    product_ids = [p["id"] for p in products]
    image_map = {}
    if product_ids:
        placeholders = ",".join("?" for _ in product_ids)
        for row in conn.execute(
            f"SELECT product_id, filename FROM product_images WHERE product_id IN ({placeholders}) ORDER BY position ASC",
            tuple(product_ids),
        ).fetchall():
            image_map.setdefault(row["product_id"], []).append(row["filename"])

    result = [dict(p, images=image_map.get(p["id"], [])) for p in products]
    conn.close()
    return ok({"products": result})


@admin_api.route("/products", methods=["POST"])
@api_admin_required
@api_requires_permission("products.edit")
def product_create():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return err("Please give the product a name.", 400)
    try:
        price = int(float(data.get("price", 0)))
        if price < 0:
            raise ValueError
    except (TypeError, ValueError):
        return err("Please enter a valid price.", 400)

    compare_price = data.get("compare_price")
    if compare_price not in (None, ""):
        try:
            compare_price = int(float(compare_price))
        except (TypeError, ValueError):
            return err("Please enter a valid compare-at price.", 400)
    else:
        compare_price = None

    delivery_mode = data.get("delivery_mode", "manual")
    if delivery_mode not in ("manual", "automatic"):
        delivery_mode = "manual"

    quantity = data.get("quantity")
    try:
        quantity = int(float(quantity)) if quantity is not None else 0
        if quantity < 0:
            raise ValueError
    except (TypeError, ValueError):
        return err("Please enter a valid quantity.", 400)

    conn = db.get_db()
    slug = _unique_slug(conn, name)
    max_pos = conn.execute("SELECT COALESCE(MAX(position), -1) m FROM products").fetchone()["m"]
    cur = conn.execute(
        """INSERT INTO products (name, slug, short_description, description, price,
           category, active, position, created_at, delivery_mode, auto_delivery_content,
           ribbon, compare_price, quantity)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            name, slug, (data.get("short_description") or "").strip(),
            (data.get("description") or "").strip(), price,
            (data.get("category") or "").strip(),
            1 if data.get("active", True) else 0, max_pos + 1, db.now(),
            delivery_mode, (data.get("auto_delivery_content") or "").strip(),
            (data.get("ribbon") or "").strip(), compare_price, quantity,
        ),
    )
    product_id = cur.lastrowid
    conn.commit()
    product = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    conn.close()
    _invalidate_frontend_cache(catalog=True)
    return ok({"product": dict(product)}, 201)


@admin_api.route("/products/<int:product_id>", methods=["PUT"])
@api_admin_required
@api_requires_permission("products.edit")
def product_update(product_id):
    conn = db.get_db()
    product = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        conn.close()
        return err("Product not found.", 404)

    data = request.get_json(silent=True) or {}

    def field(key, current):
        return data[key] if key in data else current

    name = (field("name", product["name"]) or "").strip()
    if not name:
        conn.close()
        return err("Please give the product a name.", 400)

    price_raw = field("price", product["price"])
    try:
        price = int(float(price_raw))
        if price < 0:
            raise ValueError
    except (TypeError, ValueError):
        conn.close()
        return err("Please enter a valid price.", 400)

    compare_price_raw = field("compare_price", product["compare_price"])
    if compare_price_raw not in (None, ""):
        try:
            compare_price = int(float(compare_price_raw))
        except (TypeError, ValueError):
            conn.close()
            return err("Please enter a valid compare-at price.", 400)
    else:
        compare_price = None

    delivery_mode = field("delivery_mode", product["delivery_mode"])
    if delivery_mode not in ("manual", "automatic"):
        delivery_mode = "manual"

    active = data["active"] if "active" in data else bool(product["active"])
    active = 1 if active else 0

    quantity_raw = field("quantity", product["quantity"])
    try:
        quantity = int(float(quantity_raw)) if quantity_raw is not None else 0
        if quantity < 0:
            raise ValueError
    except (TypeError, ValueError):
        conn.close()
        return err("Please enter a valid quantity.", 400)

    conn.execute(
        """UPDATE products SET name=?, short_description=?, description=?, price=?,
           category=?, active=?, delivery_mode=?, auto_delivery_content=?, ribbon=?,
           compare_price=?, quantity=? WHERE id=?""",
        (
            name, (field("short_description", product["short_description"]) or "").strip(),
            (field("description", product["description"]) or "").strip(), price,
            (field("category", product["category"]) or "").strip(), active,
            delivery_mode, (field("auto_delivery_content", product["auto_delivery_content"]) or "").strip(),
            (field("ribbon", product["ribbon"]) or "").strip(), compare_price, quantity, product_id,
        ),
    )

    new_slug_base = slugify(name)
    if not product["slug"].startswith(new_slug_base):
        new_slug = _unique_slug(conn, name, exclude_id=product_id)
        conn.execute("UPDATE products SET slug = ? WHERE id = ?", (new_slug, product_id))

    conn.commit()
    updated = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    conn.close()
    _invalidate_frontend_cache(catalog=True)
    return ok({"product": dict(updated)})


@admin_api.route("/products/<int:product_id>", methods=["DELETE"])
@api_admin_required
@api_requires_permission("products.edit")
def product_delete(product_id):
    conn = db.get_db()
    images = conn.execute(
        "SELECT filename FROM product_images WHERE product_id = ?", (product_id,)
    ).fetchall()
    for img in images:
        delete_file_quietly(img["filename"])
    conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
    conn.commit()
    conn.close()
    _invalidate_frontend_cache(catalog=True)
    return ok({"deleted": True})


@admin_api.route("/products/<int:product_id>/images", methods=["POST"])
@api_admin_required
@api_requires_permission("products.edit")
def product_image_upload(product_id):
    conn = db.get_db()
    product = conn.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        conn.close()
        return err("Product not found.", 404)

    files = request.files.getlist("images")
    if not files or not any(f.filename for f in files):
        conn.close()
        return err("No image files were uploaded.", 400)

    max_pos = conn.execute(
        "SELECT COALESCE(MAX(position), -1) m FROM product_images WHERE product_id = ?",
        (product_id,),
    ).fetchone()["m"]

    saved, failed = [], []
    for f in files:
        if not f or not f.filename:
            continue
        try:
            filename = save_product_image(f)
        except ValueError as e:
            failed.append(str(e))
            continue
        if filename:
            max_pos += 1
            conn.execute(
                "INSERT INTO product_images (product_id, filename, position) VALUES (?, ?, ?)",
                (product_id, filename, max_pos),
            )
            saved.append(filename)

    conn.commit()
    conn.close()
    return ok({"saved": saved, "failed": failed})


@admin_api.route("/products/images/<int:image_id>", methods=["DELETE"])
@api_admin_required
@api_requires_permission("products.edit")
def product_image_delete(image_id):
    conn = db.get_db()
    img = conn.execute("SELECT * FROM product_images WHERE id = ?", (image_id,)).fetchone()
    if not img:
        conn.close()
        return err("Image not found.", 404)
    delete_file_quietly(img["filename"])
    conn.execute("DELETE FROM product_images WHERE id = ?", (image_id,))
    conn.commit()
    conn.close()
    _invalidate_frontend_cache(catalog=True)
    return ok({"deleted": True})


# ============================================================= COUPONS

@admin_api.route("/coupons", methods=["GET"])
@api_admin_required
def coupons_list():
    conn = db.get_db()
    coupons = conn.execute("SELECT * FROM coupons ORDER BY id DESC").fetchall()
    conn.close()
    return ok({"coupons": [dict(c) for c in coupons]})


@admin_api.route("/coupons", methods=["POST"])
@api_admin_required
@api_requires_permission("coupons.manage")
def coupon_create():
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip().upper()
    if not code:
        return err("Please enter a coupon code.", 400)
    if len(code) > 50:
        return err("Coupon code is too long (max 50 characters).", 400)
    if not re.match(r'^[A-Z0-9_-]+$', code):
        return err("Coupon code may only contain letters, numbers, hyphens, and underscores.", 400)

    discount_type = data.get("discount_type", "percent")
    if discount_type not in ("percent", "flat"):
        discount_type = "percent"
    try:
        discount_value = int(data.get("discount_value", 0))
        if discount_value <= 0:
            raise ValueError
        if discount_type == "percent" and discount_value > 100:
            raise ValueError
    except (TypeError, ValueError):
        return err("Please enter a valid discount amount.", 400)

    active = 1 if data.get("active", True) else 0
    usage_limit = data.get("usage_limit")
    usage_limit = int(usage_limit) if isinstance(usage_limit, (int, float)) or (
        isinstance(usage_limit, str) and usage_limit.isdigit()
    ) else None

    auto_apply = 1 if data.get("auto_apply") else 0
    trigger_type = data.get("trigger_type", "manual")
    if trigger_type not in ("manual", "cart_threshold", "product_specific", "customer_segment", "url_driven"):
        trigger_type = "manual"
    if auto_apply and trigger_type == "manual":
        trigger_type = "cart_threshold"

    min_cart_value = data.get("min_cart_value")
    min_cart_value = int(min_cart_value) if isinstance(min_cart_value, (int, float)) else None
    target_product_id = data.get("target_product_id")
    target_product_id = int(target_product_id) if isinstance(target_product_id, (int, float)) else None
    customer_segment = data.get("customer_segment", "all")
    if customer_segment not in ("all", "new_user", "logged_in"):
        customer_segment = "all"

    starts_at = data.get("starts_at") or None
    expires_at = data.get("expires_at") or None
    for key, value in (("starts_at", starts_at), ("expires_at", expires_at)):
        if value:
            try:
                datetime.fromisoformat(value)
            except ValueError:
                return err(f"'{key}' must be a valid ISO datetime.", 400)

    conn = db.get_db()
    try:
        cur = conn.execute(
            """INSERT INTO coupons
               (code, discount_type, discount_value, active, usage_limit, created_at,
                auto_apply, trigger_type, min_cart_value, target_product_id,
                customer_segment, starts_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (code, discount_type, discount_value, active, usage_limit, db.now(),
             auto_apply, trigger_type, min_cart_value, target_product_id,
             customer_segment, starts_at, expires_at),
        )
        conn.commit()
        coupon = conn.execute("SELECT * FROM coupons WHERE id = ?", (cur.lastrowid,)).fetchone()
        conn.close()
        return ok({"coupon": dict(coupon)}, 201)
    except Exception:
        conn.close()
        return err("A coupon with that code already exists.", 409)


@admin_api.route("/coupons/<int:coupon_id>/toggle", methods=["POST"])
@api_admin_required
@api_requires_permission("coupons.manage")
def coupon_toggle(coupon_id):
    conn = db.get_db()
    conn.execute("UPDATE coupons SET active = 1 - active WHERE id = ?", (coupon_id,))
    conn.commit()
    coupon = conn.execute("SELECT * FROM coupons WHERE id = ?", (coupon_id,)).fetchone()
    conn.close()
    if not coupon:
        return err("Coupon not found.", 404)
    return ok({"coupon": dict(coupon)})


@admin_api.route("/coupons/<int:coupon_id>", methods=["DELETE"])
@api_admin_required
@api_requires_permission("coupons.manage")
def coupon_delete(coupon_id):
    conn = db.get_db()
    conn.execute("DELETE FROM coupons WHERE id = ?", (coupon_id,))
    conn.commit()
    conn.close()
    _invalidate_frontend_cache(catalog=True)
    return ok({"deleted": True})


# ============================================================= TESTIMONIALS

@admin_api.route("/testimonials", methods=["GET"])
@api_admin_required
def testimonials_list():
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM testimonials ORDER BY position ASC").fetchall()
    conn.close()
    return ok({"testimonials": [dict(r) for r in rows]})


@admin_api.route("/testimonials", methods=["POST"])
@api_admin_required
@api_requires_permission("testimonials.manage")
def testimonial_save():
    data = request.get_json(silent=True) or {}
    testimonial_id = data.get("id")
    customer_name = (data.get("customer_name") or "").strip()
    quote = (data.get("quote") or "").strip()
    rating = data.get("rating", 5)
    visible = 1 if data.get("visible", True) else 0

    if not customer_name or not quote:
        return err("Please fill in both a name and a quote.", 400)

    conn = db.get_db()
    if testimonial_id:
        conn.execute(
            "UPDATE testimonials SET customer_name=?, quote=?, rating=?, visible=? WHERE id=?",
            (customer_name, quote, rating, visible, testimonial_id),
        )
        result_id = testimonial_id
    else:
        max_pos = conn.execute("SELECT COALESCE(MAX(position), -1) m FROM testimonials").fetchone()["m"]
        cur = conn.execute(
            "INSERT INTO testimonials (customer_name, quote, rating, position, visible) VALUES (?, ?, ?, ?, ?)",
            (customer_name, quote, rating, max_pos + 1, visible),
        )
        result_id = cur.lastrowid
    conn.commit()
    testimonial = conn.execute("SELECT * FROM testimonials WHERE id = ?", (result_id,)).fetchone()
    conn.close()
    _invalidate_frontend_cache(catalog=True)
    return ok({"testimonial": dict(testimonial)})


@admin_api.route("/testimonials/<int:testimonial_id>", methods=["DELETE"])
@api_admin_required
@api_requires_permission("testimonials.manage")
def testimonial_delete(testimonial_id):
    conn = db.get_db()
    conn.execute("DELETE FROM testimonials WHERE id = ?", (testimonial_id,))
    conn.commit()
    conn.close()
    _invalidate_frontend_cache(catalog=True)
    return ok({"deleted": True})


# ============================================================= FAQS

@admin_api.route("/faqs", methods=["GET"])
@api_admin_required
def faqs_list():
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM faqs ORDER BY position ASC").fetchall()
    conn.close()
    return ok({"faqs": [dict(r) for r in rows]})


@admin_api.route("/faqs", methods=["POST"])
@api_admin_required
@api_requires_permission("faqs.manage")
def faq_save():
    data = request.get_json(silent=True) or {}
    faq_id = data.get("id")
    question = (data.get("question") or "").strip()
    answer = (data.get("answer") or "").strip()
    visible = 1 if data.get("visible", True) else 0

    if not question or not answer:
        return err("Please fill in both the question and the answer.", 400)

    conn = db.get_db()
    if faq_id:
        conn.execute(
            "UPDATE faqs SET question=?, answer=?, visible=? WHERE id=?",
            (question, answer, visible, faq_id),
        )
        result_id = faq_id
    else:
        max_pos = conn.execute("SELECT COALESCE(MAX(position), -1) m FROM faqs").fetchone()["m"]
        cur = conn.execute(
            "INSERT INTO faqs (question, answer, position, visible) VALUES (?, ?, ?, ?)",
            (question, answer, max_pos + 1, visible),
        )
        result_id = cur.lastrowid
    conn.commit()
    faq = conn.execute("SELECT * FROM faqs WHERE id = ?", (result_id,)).fetchone()
    conn.close()
    _invalidate_frontend_cache(catalog=True)
    return ok({"faq": dict(faq)})


@admin_api.route("/faqs/<int:faq_id>", methods=["DELETE"])
@api_admin_required
@api_requires_permission("faqs.manage")
def faq_delete(faq_id):
    conn = db.get_db()
    conn.execute("DELETE FROM faqs WHERE id = ?", (faq_id,))
    conn.commit()
    conn.close()
    _invalidate_frontend_cache(catalog=True)
    return ok({"deleted": True})


# ============================================================= SETTINGS

@admin_api.route("/settings", methods=["GET"])
@api_admin_required
def settings_get():
    conn = db.get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return ok({"settings": {r["key"]: r["value"] for r in rows}})


@admin_api.route("/settings", methods=["POST"])
@api_admin_required
@api_requires_permission("settings.edit")
def settings_save():
    data = request.get_json(silent=True) or {}
    checkbox_keys = {"auto_deliver_enabled", "auto_email_enabled", "low_stock_alerts", "disable_payments"}
    conn = db.get_db()
    # Accept any key sent by the admin client (Android app)
    for key, value in data.items():
        if key in checkbox_keys:
            value = "true" if value else "false"
        elif not isinstance(value, str):
            value = str(value)
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    conn.commit()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    _invalidate_frontend_cache(settings=True)
    return ok({"settings": {r["key"]: r["value"] for r in rows}})


# ============================================================= NEWSLETTER

@admin_api.route("/newsletter", methods=["GET"])
@api_admin_required
@api_requires_permission("newsletter.view")
def newsletter_list():
    conn = db.get_db()
    rows = conn.execute(
        "SELECT id, email, created_at FROM newsletter_subscribers ORDER BY id DESC"
    ).fetchall()
    conn.close()

    if request.args.get("format") == "csv":
        import csv
        import io
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Email", "Subscribed At"])
        for r in rows:
            writer.writerow([r["email"], r["created_at"]])
        return buf.getvalue(), 200, {
            "Content-Type": "text/csv",
            "Content-Disposition": "attachment; filename=newsletter_subscribers.csv",
        }

    return ok({"subscribers": [dict(r) for r in rows], "count": len(rows)})


# ============================================================= ACCOUNT

@admin_api.route("/account/change-password", methods=["POST"])
@api_admin_required
def change_password():
    data = request.get_json(silent=True) or {}
    current = data.get("current_password") or ""
    new = data.get("new_password") or ""
    confirm = data.get("confirm_password") or ""

    conn = db.get_db()
    user = conn.execute("SELECT * FROM admin_users WHERE id = ?", (g.admin_id,)).fetchone()

    if not user or not check_password_hash(user["password_hash"], current):
        conn.close()
        return err("Current password is incorrect.", 400)
    if len(new) < 8:
        conn.close()
        return err("New password must be at least 8 characters.", 400)
    if new != confirm:
        conn.close()
        return err("New passwords do not match.", 400)

    conn.execute(
        "UPDATE admin_users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(new), user["id"]),
    )
    # Same security posture as the web admin: rotating the password revokes
    # every other active API token/device so a leaked credential can't keep
    # using the old password's session after it's changed.
    conn.execute(
        "UPDATE api_tokens SET revoked = 1 WHERE admin_user_id = ? AND id != ?",
        (user["id"], g.api_token_id),
    )
    conn.commit()
    conn.close()
    return ok({"changed": True})


# ============================================================= TEAM (admin management)

@admin_api.route("/team", methods=["GET"])
@api_admin_required
def team_list():
    """List all admin users. Only master or admins with admin.manage can see this."""
    conn = db.get_db()
    user = conn.execute(
        "SELECT role, permissions FROM admin_users WHERE id = ?", (g.admin_id,)
    ).fetchone()
    if not user or not has_permission(json.loads(user["permissions"] or "[]"), "admin.manage"):
        conn.close()
        return err("Insufficient permissions.", 403)
    admins = conn.execute(
        "SELECT id, username, role, permissions, is_active, created_at FROM admin_users ORDER BY id ASC"
    ).fetchall()
    conn.close()
    return ok({"admins": [dict(a) for a in admins]})


@admin_api.route("/team", methods=["POST"])
@api_admin_required
def team_create():
    """Create a new admin user (preset or custom role). Master only."""
    conn = db.get_db()
    user = conn.execute(
        "SELECT role, permissions FROM admin_users WHERE id = ?", (g.admin_id,)
    ).fetchone()
    if not user or not has_permission(json.loads(user["permissions"] or "[]"), "admin.manage"):
        conn.close()
        return err("Insufficient permissions.", 403)

    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role_preset = data.get("role_preset", "custom")

    if not username or len(password) < 8:
        conn.close()
        return err("Username is required and password must be at least 8 characters.", 400)

    if role_preset in PRESET_PERMISSIONS:
        permissions = json.dumps(PRESET_PERMISSIONS[role_preset])
        role_name = role_preset
    elif role_preset == "custom":
        try:
            raw = data.get("permissions", "[]")
            perms_list = json.loads(raw)
            if not isinstance(perms_list, list):
                raise ValueError
            permissions = json.dumps(perms_list)
        except (ValueError, TypeError):
            conn.close()
            return err("Invalid permissions JSON.", 400)
        role_name = "custom"
    else:
        conn.close()
        return err("Unknown role preset.", 400)

    try:
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, role, permissions, is_active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            (username, generate_password_hash(password), role_name, permissions, db.now()),
        )
        conn.commit()
        conn.close()
        return ok({"created": True, "username": username, "role": role_name}, 201)
    except Exception:
        conn.close()
        return err(f"Username '{username}' already exists.", 409)


@admin_api.route("/team/<int:admin_id>", methods=["GET"])
@api_admin_required
def team_get(admin_id):
    """Get a single admin's details."""
    conn = db.get_db()
    user = conn.execute(
        "SELECT role, permissions FROM admin_users WHERE id = ?", (g.admin_id,)
    ).fetchone()
    if not user or not has_permission(json.loads(user["permissions"] or "[]"), "admin.manage"):
        conn.close()
        return err("Insufficient permissions.", 403)
    target = conn.execute(
        "SELECT id, username, role, permissions, is_active, created_at FROM admin_users WHERE id = ?",
        (admin_id,),
    ).fetchone()
    conn.close()
    if not target:
        return err("Admin not found.", 404)
    return ok({"admin": dict(target)})


@admin_api.route("/team/<int:admin_id>", methods=["PUT"])
@api_admin_required
def team_update(admin_id):
    """Update a sub-admin's role/permissions/active status. Cannot edit master."""
    conn = db.get_db()
    current = conn.execute(
        "SELECT role, permissions FROM admin_users WHERE id = ?", (g.admin_id,)
    ).fetchone()
    if not current or not has_permission(json.loads(current["permissions"] or "[]"), "admin.manage"):
        conn.close()
        return err("Insufficient permissions.", 403)

    target = conn.execute("SELECT * FROM admin_users WHERE id = ?", (admin_id,)).fetchone()
    if not target:
        conn.close()
        return err("Admin not found.", 404)
    if target["role"] == "master":
        conn.close()
        return err("Cannot edit the master admin.", 403)

    data = request.get_json(silent=True) or {}
    role_preset = data.get("role_preset", target["role"])
    is_active = data.get("is_active", target["is_active"])
    is_active = 1 if is_active else 0

    if role_preset in PRESET_PERMISSIONS:
        permissions = json.dumps(PRESET_PERMISSIONS[role_preset])
        role_name = role_preset
    elif role_preset == "custom":
        try:
            raw = data.get("permissions", target["permissions"])
            perms_list = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(perms_list, list):
                raise ValueError
            permissions = json.dumps(perms_list)
        except (ValueError, TypeError):
            conn.close()
            return err("Invalid permissions JSON.", 400)
        role_name = "custom"
    else:
        role_name = target["role"]
        permissions = target["permissions"]

    conn.execute(
        "UPDATE admin_users SET role = ?, permissions = ?, is_active = ? WHERE id = ?",
        (role_name, permissions, is_active, admin_id),
    )
    conn.commit()
    updated = conn.execute(
        "SELECT id, username, role, permissions, is_active, created_at FROM admin_users WHERE id = ?",
        (admin_id,),
    ).fetchone()
    conn.close()
    return ok({"admin": dict(updated)})


@admin_api.route("/team/<int:admin_id>/toggle", methods=["POST"])
@api_admin_required
def team_toggle(admin_id):
    """Suspend/unsuspend a sub-admin. Cannot toggle master."""
    conn = db.get_db()
    current = conn.execute(
        "SELECT role, permissions FROM admin_users WHERE id = ?", (g.admin_id,)
    ).fetchone()
    if not current or not has_permission(json.loads(current["permissions"] or "[]"), "admin.manage"):
        conn.close()
        return err("Insufficient permissions.", 403)

    target = conn.execute("SELECT * FROM admin_users WHERE id = ?", (admin_id,)).fetchone()
    if not target:
        conn.close()
        return err("Admin not found.", 404)
    if target["role"] == "master":
        conn.close()
        return err("Cannot suspend the master admin.", 403)

    new_status = 1 - target["is_active"]
    conn.execute("UPDATE admin_users SET is_active = ? WHERE id = ?", (new_status, admin_id))
    conn.commit()
    updated = conn.execute(
        "SELECT id, username, role, permissions, is_active, created_at FROM admin_users WHERE id = ?",
        (admin_id,),
    ).fetchone()
    conn.close()
    label = "activated" if new_status else "suspended"
    return ok({"admin": dict(updated), "label": label})


# ============================================================= PERFORMANCE

@admin_api.route("/performance", methods=["GET"])
@api_admin_required
def performance_metrics():
    from datetime import datetime, timedelta, timezone
    conn = db.get_db()

    # Last 24 hours
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    # Core Web Vitals aggregates
    cwv = conn.execute(
        """SELECT metric_name, AVG(value) as avg_val, MIN(value) as min_val,
                  MAX(value) as max_val, COUNT(*) as count
           FROM performance_metrics
           WHERE metric_type = 'cwv' AND created_at > ?
           GROUP BY metric_name""", (since,)
    ).fetchall()

    # API latency aggregates
    api_lat = conn.execute(
        """SELECT metric_name, AVG(value) as avg_val, MAX(value) as max_val, COUNT(*) as count
           FROM performance_metrics
           WHERE metric_type = 'api' AND created_at > ?
           GROUP BY metric_name
           ORDER BY avg_val DESC
           LIMIT 20""", (since,)
    ).fetchall()

    conn.close()

    return ok({
        "period_hours": 24,
        "core_web_vitals": [
            {"name": r["metric_name"], "avg": round(r["avg_val"] or 0, 2),
             "min": round(r["min_val"] or 0, 2), "max": round(r["max_val"] or 0, 2),
             "count": r["count"]} for r in cwv
        ],
        "api_latency": [
            {"endpoint": r["metric_name"], "avg_ms": round(r["avg_val"] or 0, 2),
             "max_ms": round(r["max_val"] or 0, 2), "count": r["count"]} for r in api_lat
        ]
    })
