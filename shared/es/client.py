"""Elasticsearch client factory and index lifecycle helpers."""

import json
import logging
import sys
from pathlib import Path

from elasticsearch import Elasticsearch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from shared.config import ES_URL

logger = logging.getLogger(__name__)


def build_es_client() -> Elasticsearch:
    return Elasticsearch(ES_URL)


def ensure_index(client: Elasticsearch, index: str, mapping_path: str) -> None:
    """Create the index from a JSON mapping file if it does not already exist."""
    if client.indices.exists(index=index):
        logger.info("Index '%s' already exists — skipping creation", index)
        return

    with open(mapping_path, "r") as f:
        body = json.load(f)

    client.indices.create(index=index, body=body)
    logger.info("Created index '%s'", index)


def next_version_index(client: Elasticsearch, base: str) -> str:
    """Return the next free versioned index name, e.g. 'base_v3' after 'base_v2'.

    Only `base_v<digits>` names count; an unversioned legacy `base` index is ignored,
    so the first versioned build alongside it is `base_v1`.
    """
    existing = client.indices.get(
        index=f"{base}_v*", ignore_unavailable=True, allow_no_indices=True
    )
    versions = [
        int(suffix)
        for name in existing
        if (suffix := name.rsplit("_v", 1)[-1]).isdigit()
    ]
    return f"{base}_v{max(versions, default=0) + 1}"


def resolve_write_index(client: Elasticsearch, base: str, alias: str) -> str:
    """Return the index to write into for an incremental run.

    Follows the alias when it exists, falls back to the legacy unversioned index,
    and otherwise names a first versioned index.
    """
    if client.indices.exists_alias(name=alias):
        indices = sorted(client.indices.get_alias(name=alias))
        if len(indices) > 1:
            raise RuntimeError(
                f"Alias '{alias}' points at {len(indices)} indices ({', '.join(indices)}) "
                "— refusing to guess which one to write to. Resolve it by hand."
            )
        return indices[0]

    if client.indices.exists(index=base):
        return base

    return f"{base}_v1"
