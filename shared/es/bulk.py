"""Shared Elasticsearch bulk and post-ingestion operations."""

import logging
import sys
import time
from collections.abc import Iterator
from pathlib import Path

from elasticsearch import ConnectionTimeout, Elasticsearch
from elasticsearch.helpers import streaming_bulk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from shared.config import ES_REPLICAS

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


def restore_after_import(
    client: Elasticsearch, index: str, replicas: int | None = None
) -> None:
    """Restore normal index settings and trigger a refresh after bulk import.

    `replicas` defaults to `ES_REPLICAS`, itself 0 unless overridden — asking for a replica
    on the single-node lab cluster leaves it unassigned and pins the index to yellow.
    """
    if replicas is None:
        replicas = ES_REPLICAS

    client.indices.put_settings(
        index=index,
        settings={"index": {"refresh_interval": "1s", "number_of_replicas": replicas}},
    )
    client.indices.refresh(index=index)
    logger.info(
        "Index '%s' settings restored (refresh=1s, replicas=%d)", index, replicas
    )


def update_alias(client: Elasticsearch, alias: str, index: str) -> list[str]:
    """Point `alias` at `index`, detaching it from any other index in one atomic call.

    Returns the indices the alias was detached from — they keep their data and stay
    queryable by name, so a rollback is just another call to this function.
    """
    detached = []
    actions: list[dict] = []

    if client.indices.exists_alias(name=alias):
        detached = [i for i in client.indices.get_alias(name=alias) if i != index]
        actions += [{"remove": {"index": i, "alias": alias}} for i in detached]

    actions.append({"add": {"index": index, "alias": alias}})
    client.indices.update_aliases(actions=actions)

    if detached:
        logger.info(
            "Alias '%s' → '%s' (detached from %s, kept on disk)",
            alias,
            index,
            ", ".join(detached),
        )
    else:
        logger.info("Alias '%s' → '%s'", alias, index)

    return detached


def forcemerge(
    client: Elasticsearch,
    index: str,
    max_num_segments: int = 1,
    request_timeout: int = 300,
) -> bool:
    """Force merge the index to reduce segment count (run after bulk import is complete).

    Returns True if the merge finished within the timeout.

    A client-side timeout is **not** treated as a failure: Elasticsearch keeps merging in
    the background regardless of whether the client is still waiting. Raising here would
    abort the caller mid-run — and since the alias swap happens after this call, it would
    leave a complete index that nothing points at. We log and hand control back instead.

    Careful with `max_num_segments=1` on an index carrying a `dense_vector`: each segment
    holds its own HNSW graph and Elasticsearch searches every one of them with the full
    `num_candidates` budget, so collapsing to a single segment cuts the exploration the
    kNN query gets. See the recall figures in article-02's README.
    """
    stats = client.indices.stats(index=index)
    before = stats["_all"]["primaries"]["segments"]["count"]
    logger.info("Segments before merge: %d", before)

    logger.info(
        "Running force merge on '%s' (max_num_segments=%d)…", index, max_num_segments
    )
    try:
        client.options(request_timeout=request_timeout).indices.forcemerge(
            index=index, max_num_segments=max_num_segments
        )
    except ConnectionTimeout:
        logger.warning(
            "Force merge on '%s' passed the %ds client timeout — Elasticsearch carries on "
            "merging in the background, the run continues",
            index,
            request_timeout,
        )
        return False

    stats = client.indices.stats(index=index)
    after = stats["_all"]["primaries"]["segments"]["count"]
    logger.info("Segments after merge: %d", after)
    return True
