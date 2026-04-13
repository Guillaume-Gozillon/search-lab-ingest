"""Post-ingestion optimization: force merge index to 1 segment per shard."""

import logging
import sys
from pathlib import Path

from elasticsearch import Elasticsearch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from es.client import build_es_client

logger = logging.getLogger(__name__)


def _get_segment_count(client: Elasticsearch, index: str) -> int:
    """Return the total number of segments across all shards."""
    stats = client.indices.stats(index=index)
    return stats["_all"]["primaries"]["segments"]["count"]


def run() -> None:
    """Force merge the index to 1 segment per shard and log before/after counts."""
    client = build_es_client()
    index = config.INDEX_NAME

    before = _get_segment_count(client, index)
    logger.info("Segments before merge: %d", before)

    logger.info("Running force merge on '%s' (max_num_segments=1)…", index)
    client.indices.forcemerge(index=index, max_num_segments=1)

    after = _get_segment_count(client, index)
    logger.info("Segments after merge: %d", after)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    run()
