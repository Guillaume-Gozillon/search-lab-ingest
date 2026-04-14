"""Main ingestion pipeline: streams McAuley Amazon Reviews 2023 metadata from HuggingFace into Elasticsearch."""

import argparse
import logging
import sys
from collections.abc import Iterator
from pathlib import Path

from datasets import load_dataset
from datasets.iterable_dataset import IterableDataset
from elasticsearch import Elasticsearch
from elasticsearch.helpers import streaming_bulk
from tqdm import tqdm

repo_root = Path(__file__).resolve().parent.parent
article_root = Path(__file__).resolve().parent
sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(article_root))

from shared import config
from shared.es.client import build_es_client, ensure_index
from transforms.product import transform

logger = logging.getLogger(__name__)

MAPPING_PATH = Path(__file__).resolve().parent / "mappings" / "products_home_kitchen_v1.json"


def document_stream(dataset: IterableDataset, limit: int | None) -> Iterator[dict]:
    """Yield transformed documents from the HuggingFace dataset.

    Logs the number of skipped records at the end.
    """
    skipped = 0
    emitted = 0

    for item in dataset:
        if limit is not None and emitted >= limit:
            break
        doc = transform(item)
        if doc is None:
            skipped += 1
            continue
        emitted += 1
        yield doc

    logger.info("Stream finished — emitted %d, skipped %d", emitted, skipped)


def _log_field_coverage(client: Elasticsearch) -> None:
    """Log the percentage of documents that have price, description, and rating."""
    resp = client.search(
        index=config.INDEX_NAME,
        size=0,
        aggs={
            "has_price": {"filter": {"term": {"has_price": True}}},
            "has_description": {"filter": {"term": {"has_description": True}}},
            "has_rating": {"filter": {"exists": {"field": "average_rating"}}},
        },
    )
    total = resp["hits"]["total"]["value"]
    if total == 0:
        logger.warning("Index is empty — no coverage stats")
        return

    for field in ("has_price", "has_description", "has_rating"):
        count = resp["aggregations"][field]["doc_count"]
        pct = count / total * 100
        logger.info("  %s: %d / %d (%.1f%%)", field, count, total, pct)


def run(dry_run: bool = False, limit: int | None = None) -> None:
    """Execute the ingestion pipeline."""
    logger.info(
        "Loading dataset %s [%s] (streaming=True)",
        config.HF_DATASET,
        config.HF_CONFIG,
    )
    dataset = load_dataset(
        config.HF_DATASET,
        config.HF_CONFIG,
        split="full",
        streaming=True,
    )

    if dry_run:
        _run_dry(dataset, limit)
        return

    client = build_es_client()
    ensure_index(client, config.INDEX_NAME, str(MAPPING_PATH))
    _run_bulk(client, dataset, limit)


def _run_dry(dataset: IterableDataset, limit: int | None) -> None:
    """Transform without indexing — useful for validating the pipeline."""
    count = 0
    for doc in tqdm(document_stream(dataset, limit), desc="dry-run"):
        count += 1
    logger.info("Dry run complete — %d documents would be indexed", count)


def _run_bulk(
    client: Elasticsearch, dataset: IterableDataset, limit: int | None
) -> None:
    """Stream documents into Elasticsearch via streaming_bulk."""
    indexed = 0
    errors: list[dict] = []

    progress = tqdm(desc="indexing", unit="docs")
    for ok, result in streaming_bulk(
        client,
        document_stream(dataset, limit),
        chunk_size=config.BULK_SIZE,
        raise_on_error=False,
    ):
        if ok:
            indexed += 1
        else:
            errors.append(result)
        progress.update(1)
    progress.close()

    logger.info("Indexed %d documents, %d errors", indexed, len(errors))
    for err in errors[:5]:
        logger.error("  %s", err)

    client.indices.refresh(index=config.INDEX_NAME)
    total = client.count(index=config.INDEX_NAME)["count"]
    logger.info("Total documents in '%s': %d", config.INDEX_NAME, total)

    logger.info("Field coverage:")
    _log_field_coverage(client)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Transform without indexing"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Max documents to process"
    )
    args = parser.parse_args()

    run(dry_run=args.dry_run, limit=args.limit)
