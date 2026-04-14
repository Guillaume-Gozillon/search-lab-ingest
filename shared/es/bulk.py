"""Shared Elasticsearch bulk and post-ingestion operations."""

import logging
import sys
import time
from collections.abc import Iterator
from pathlib import Path

from elasticsearch import Elasticsearch
from elasticsearch.helpers import streaming_bulk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
logger = logging.getLogger(__name__)

_DEFAULT_CHUNK_SIZE = 2000


def bulk_index(
    client: Elasticsearch,
    actions: Iterator[dict],
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
) -> tuple[int, list[dict]]:
    """Stream documents into Elasticsearch with error collection and progress logging.

    Returns (indexed_count, errors).
    """
    indexed = 0
    errors: list[dict] = []
    start = time.time()
    log_every = chunk_size * 10

    for ok, result in streaming_bulk(
        client,
        actions,
        chunk_size=chunk_size,
        raise_on_error=False,
    ):
        if ok:
            indexed += 1
        else:
            errors.append(result)

        if indexed > 0 and indexed % log_every == 0:
            elapsed = time.time() - start
            rate = indexed / elapsed if elapsed > 0 else 0
            logger.info(
                "  %d docs indexed — %.1fs elapsed (%.0f docs/s)",
                indexed,
                elapsed,
                rate,
            )

    elapsed = time.time() - start
    logger.info(
        "Bulk complete — %d indexed, %d errors in %.1fs",
        indexed,
        len(errors),
        elapsed,
    )
    for err in errors[:5]:
        logger.error("  bulk error: %s", err)

    return indexed, errors


def optimize_for_import(client: Elasticsearch, index: str) -> None:
    """Disable refresh and zero replicas before a bulk import for maximum throughput."""
    client.indices.put_settings(
        index=index,
        settings={"index": {"refresh_interval": "-1", "number_of_replicas": 0}},
    )
    logger.info("Index '%s' optimized for bulk import (refresh off, replicas=0)", index)


def restore_after_import(client: Elasticsearch, index: str) -> None:
    """Restore normal index settings and trigger a refresh after bulk import."""
    client.indices.put_settings(
        index=index,
        settings={"index": {"refresh_interval": "1s", "number_of_replicas": 1}},
    )
    client.indices.refresh(index=index)
    logger.info("Index '%s' settings restored (refresh=1s, replicas=1)", index)


def forcemerge(client: Elasticsearch, index: str, max_num_segments: int = 1) -> None:
    """Force merge the index to reduce segment count (run after bulk import is complete)."""
    stats = client.indices.stats(index=index)
    before = stats["_all"]["primaries"]["segments"]["count"]
    logger.info("Segments before merge: %d", before)

    logger.info(
        "Running force merge on '%s' (max_num_segments=%d)…", index, max_num_segments
    )
    client.indices.forcemerge(index=index, max_num_segments=max_num_segments, request_timeout=300)

    stats = client.indices.stats(index=index)
    after = stats["_all"]["primaries"]["segments"]["count"]
    logger.info("Segments after merge: %d", after)
