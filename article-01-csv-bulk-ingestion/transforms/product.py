"""Pure transform functions: CSV row dict → Elasticsearch document. No side effects."""

import math
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from shared import config


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

    Returns None if the row has no ASIN.
    """
    asin = _clean(row.get("asin"))
    if not asin:
        return None

    cat_id = _parse_int(row.get("category_id"))

    return {
        "_index": config.INDEX_NAME,
        "_id": str(asin),
        "asin": str(asin),
        "title": _clean(row.get("title")),
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
