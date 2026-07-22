"""Shared storefront helpers.

Small pure functions live here so catalog/search logic does not have to be
copied across routes. This is the first step toward a clearer service layer
without changing the site's behavior.
"""
from __future__ import annotations

import os
from typing import Iterable, Sequence


def _webp_path(filename: str) -> str:
    """If a WebP version of the image exists alongside it, return the WebP path.
    Otherwise return the original filename.  The WebP file is <filename>.webp."""
    if not filename:
        return filename
    # Load UPLOAD_FOLDER from the app config on first call
    upload_folder = getattr(_webp_path, "_upload_folder", None)
    if upload_folder is None:
        try:
            import config as _cfg
            _webp_path._upload_folder = _cfg.UPLOAD_FOLDER
        except Exception:
            _webp_path._upload_folder = "static/uploads"
        upload_folder = _webp_path._upload_folder
    webp_filename = filename + ".webp"
    if os.path.exists(os.path.join(upload_folder, webp_filename)):
        return webp_filename
    return filename


def get_primary_image_map(conn, product_ids: Sequence[int]) -> dict[int, str]:
    """Return {product_id: first_image_filename}, preferring WebP versions."""
    ids = [int(pid) for pid in product_ids if pid is not None]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""SELECT product_id, filename
            FROM product_images
            WHERE product_id IN ({placeholders})
            ORDER BY product_id ASC, position ASC, id ASC""",
        tuple(ids),
    ).fetchall()
    image_map: dict[int, str] = {}
    for row in rows:
        filename = row["filename"]
        image_map.setdefault(int(row["product_id"]), _webp_path(filename))
    return image_map


def sort_products(products: Iterable, sort_key: str):
    """Return products sorted using the storefront's existing sort modes."""
    products = list(products)
    if sort_key == "price_low":
        return sorted(products, key=lambda p: p["price"])
    if sort_key == "price_high":
        return sorted(products, key=lambda p: p["price"], reverse=True)
    if sort_key == "newest":
        return sorted(products, key=lambda p: p["id"], reverse=True)
    if sort_key == "name":
        return sorted(products, key=lambda p: (p["name"] or "").lower())
    return products


def search_products(conn, query: str, *, limit: int = 8):
    """Return live search results with the primary image already attached."""
    query = (query or "").strip()
    if len(query) < 2:
        return []
    like = f"%{query}%"
    rows = conn.execute(
        """SELECT id, name, slug, category, price
           FROM products
           WHERE active = 1 AND (name LIKE ? OR short_description LIKE ? OR category LIKE ?)
           ORDER BY position ASC, id DESC
           LIMIT ?""",
        (like, like, like, limit),
    ).fetchall()
    image_map = get_primary_image_map(conn, [row["id"] for row in rows])
    results = []
    for row in rows:
        results.append(
            {
                "name": row["name"],
                "slug": row["slug"],
                "category": row["category"],
                "price": row["price"],
                "image": image_map.get(row["id"]),
            }
        )
    return results
