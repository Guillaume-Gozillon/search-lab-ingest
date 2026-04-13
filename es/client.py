"""Elasticsearch client factory and index lifecycle helpers."""

import json
import logging
import sys
from pathlib import Path

from elasticsearch import Elasticsearch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

logger = logging.getLogger(__name__)


def build_es_client() -> Elasticsearch:
    """Create an Elasticsearch client from config and verify the connection."""
    kwargs: dict = {
        "hosts": [config.ES_HOST],
        "basic_auth": (config.ES_USER, config.ES_PASSWORD),
    }

    if config.ES_CA_CERT:
        kwargs["ca_certs"] = config.ES_CA_CERT
    else:
        logger.warning("No CA certificate configured — TLS verification disabled (local dev only)")
        kwargs["verify_certs"] = False
        kwargs["ssl_show_warn"] = False

    client = Elasticsearch(**kwargs)
    info = client.info()
    logger.info(
        "Connected to cluster '%s' (Elasticsearch %s)",
        info["cluster_name"],
        info["version"]["number"],
    )
    return client


def ensure_index(client: Elasticsearch, index: str, mapping_path: str) -> None:
    """Create the index from a JSON mapping file if it does not already exist."""
    if client.indices.exists(index=index):
        logger.info("Index '%s' already exists — skipping creation", index)
        return

    with open(mapping_path, "r") as f:
        body = json.load(f)

    client.indices.create(index=index, body=body)
    logger.info("Created index '%s'", index)
