"""Elasticsearch client factory and index lifecycle helpers."""

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from elasticsearch import Elasticsearch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from shared.config import ES_URL

logger = logging.getLogger(__name__)

# amazon_products_embeddings_v3_20260722-1730 — le timestamp est optionnel pour
# rester compatible avec les index `_v<n>` créés avant son introduction.
_VERSION_RE = re.compile(r"_v(\d+)(?:_\d{8}-\d{4})?$")


def build_es_client() -> Elasticsearch:
    """Build the ES client, preferring orjson for serialization when it is installed.

    Elasticsearch-py ships an `OrjsonSerializer` but keeps the standard library one as the
    default — installing orjson is not enough, it has to be handed over explicitly.

    It matters here because serializing a document carrying 768 floats is the hot path:
    `json.dumps` measured 4 990 docs/s at 15.5 KB per document, which is invisible while
    embedding runs at ~450 docs/s and becomes the ceiling as soon as it does not.
    """
    try:
        from elasticsearch.serializer import OrjsonSerializer
    except ImportError:
        # orjson absent : la stdlib fait le même travail, plus lentement.
        return Elasticsearch(ES_URL)

    return Elasticsearch(ES_URL, serializer=OrjsonSerializer())


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
    """Return the next index name, e.g. 'base_v3_20260722-1730'.

    The version counter orders the builds and the UTC timestamp says when each one
    started, so `_cat/indices` sorted by name reads chronologically. An unversioned
    legacy `base` index is ignored, so the first versioned build is `_v1`.
    """
    existing = client.indices.get(
        index=f"{base}_v*", ignore_unavailable=True, allow_no_indices=True
    )
    versions = [
        int(match.group(1))
        for name in existing
        if (match := _VERSION_RE.search(name[len(base) :]))
    ]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return f"{base}_v{max(versions, default=0) + 1}_{stamp}"


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
