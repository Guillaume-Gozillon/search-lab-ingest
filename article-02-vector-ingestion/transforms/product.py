"""Pure transform functions: CSV row dict → Elasticsearch document. No side effects.

Diverges from article-01: also requires a non-empty title, since title is the input
to the embedding step downstream.
"""

import math
from datetime import datetime, timezone


def _clean(value):
    """Return None for NaN or None, otherwise return the value unchanged."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _parse_float(value) -> float | None:
    v = _clean(value)
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _parse_int(value) -> int | None:
    v = _clean(value)
    if v is None:
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def _parse_bool(value) -> bool:
    v = _clean(value)
    if v is None:
        return False
    return bool(v)


def transform(row: dict) -> dict | None:
    """Convert a CSV row into an Elasticsearch bulk-action dict.

    Returns None if the row has no ASIN or no title (title is needed for embedding).
    """
    asin = _clean(row.get("asin"))
    if not asin:
        return None

    title = _clean(row.get("title"))
    if not title or not str(title).strip():
        return None

    cat_id = _parse_int(row.get("category_id"))

    return {
        "_id": str(asin),
        "asin": str(asin),
        "title": str(title),
        "imgUrl": _clean(row.get("imgUrl")),
        "productURL": _clean(row.get("productURL")),
        "stars": _parse_float(row.get("stars")),
        "reviews": _parse_int(row.get("reviews")),
        "price": _parse_float(row.get("price")),
        "listPrice": _parse_float(row.get("listPrice")),
        "category_id": str(cat_id) if cat_id is not None else None,
        "isBestSeller": _parse_bool(row.get("isBestSeller")),
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }
