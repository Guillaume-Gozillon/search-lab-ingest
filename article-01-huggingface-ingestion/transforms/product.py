"""Pure transform functions: HuggingFace item dict → Elasticsearch document. No side effects."""

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from shared import config


def parse_price(raw: str | None) -> float | None:
    """Extract a numeric price from strings like '$12.99', 'None', None, or ''."""
    if raw is None or str(raw).strip() in ("", "None"):
        return None
    cleaned = re.sub(r"[^\d.]", "", str(raw))
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def clean_details(raw: dict | str | None) -> dict | None:
    """Strip whitespace from keys, drop entries where the value is None or empty."""
    if not raw or not isinstance(raw, dict):
        return None
    cleaned = {
        k.strip(): v for k, v in raw.items() if v is not None and str(v).strip() != ""
    }
    return cleaned or None


def _join_list(value: list | str | None) -> str | None:
    """Join a list of strings into a single string, or return the string as-is."""
    if value is None:
        return None
    if isinstance(value, list):
        joined = " ".join(s.strip() for s in value if s and str(s).strip())
        return joined or None
    text = str(value).strip()
    return text or None


def _deduplicate(items: list | None) -> list[str]:
    """Deduplicate a list while preserving order."""
    if not items:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def transform(item: dict) -> dict | None:
    """Convert a raw HuggingFace record into an Elasticsearch bulk-action dict.

    Returns None if the item has neither an ASIN nor a title.
    """
    asin = item.get("parent_asin") or item.get("asin")
    title = (item.get("title") or "").strip() or None

    if not asin and not title:
        return None

    price = parse_price(item.get("price"))
    description = _join_list(item.get("description"))

    return {
        "_index": config.INDEX_NAME,
        "_id": asin,
        "asin": item.get("asin"),
        "parent_asin": item.get("parent_asin"),
        "title": title,
        "description": description,
        "features": _join_list(item.get("features")),
        "main_category": item.get("main_category"),
        "categories": _deduplicate(item.get("categories")),
        "store": item.get("store"),
        "price": price,
        "average_rating": item.get("average_rating"),
        "rating_number": item.get("rating_number"),
        "details": clean_details(item.get("details")),
        "has_description": description is not None,
        "has_price": price is not None,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }
