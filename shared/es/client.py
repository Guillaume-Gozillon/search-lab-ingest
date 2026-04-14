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
