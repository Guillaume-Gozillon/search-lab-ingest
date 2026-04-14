"""Main ingestion pipeline: streams Amazon Products CSV (Kaggle) into Elasticsearch.

Blue/green pattern: each run creates a fresh versioned index, swaps the alias,
then deletes the previous index — zero downtime, clean rollback point.
"""

import argparse
import logging
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from tqdm import tqdm

repo_root = Path(__file__).resolve().parent.parent
article_root = Path(__file__).resolve().parent
sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(article_root))

from shared import config
from shared.es.bulk import (
    bulk_index,
    forcemerge,
    get_current_index,
    optimize_for_import,
    restore_after_import,
    update_alias,
)
from shared.es.client import build_es_client, ensure_index
from transforms.product import transform

logger = logging.getLogger(__name__)

MAPPING_PATH = Path(__file__).resolve().parent / "mappings" / "amazon_products_v1.json"
DEFAULT_CSV = Path(__file__).resolve().parent / "data" / "amazon_products.csv"
KAGGLE_DATASET = "asaniczka/amazon-products-dataset-2023-1-4m-products"
CSV_CHUNK_SIZE = 10_000
BULK_CHUNK_SIZE = 2_000


def _versioned_index() -> str:
    """Return a timestamped index name, e.g. amazon_products_20260414_160532."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{config.ES_INDEX}_{ts}"


def download_from_kaggle(dest: Path) -> Path:
    """Download the dataset from Kaggle and return the path to the CSV."""
    import shutil

    import kagglehub

    logger.info("Downloading dataset from Kaggle: %s", KAGGLE_DATASET)
    dataset_dir = Path(kagglehub.dataset_download(KAGGLE_DATASET))

    src = dataset_dir / "amazon_products.csv"
    if not src.exists():
        csv_files = list(dataset_dir.glob("*.csv"))
        if not csv_files:
            raise FileNotFoundError(
                f"No CSV found in downloaded dataset at {dataset_dir}"
            )
        src = csv_files[0]
        logger.warning("amazon_products.csv not found, using %s", src.name)

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    logger.info("Dataset saved to %s", dest)
    return dest


def document_stream(csv_path: Path, index: str, limit: int | None) -> Iterator[dict]:
    """Yield transformed ES documents from the CSV, reading in chunks of 10 000 rows."""
    emitted = 0
    skipped = 0

    for chunk in pd.read_csv(csv_path, chunksize=CSV_CHUNK_SIZE, low_memory=False):
        for row in chunk.to_dict("records"):
            if limit is not None and emitted >= limit:
                logger.info("Stream finished — limit reached (%d)", limit)
                logger.info("Emitted %d, skipped %d", emitted, skipped)
                return
            doc = transform(row)
            if doc is None:
                skipped += 1
                continue
            doc["_index"] = index
            emitted += 1
            yield doc

    logger.info("Stream finished — emitted %d, skipped %d", emitted, skipped)


def run(csv_path: Path, dry_run: bool = False, limit: int | None = None) -> None:
    """Execute the ingestion pipeline."""
    if not csv_path.exists():
        csv_path = download_from_kaggle(csv_path)

    logger.info("Reading CSV: %s", csv_path)

    if dry_run:
        new_index = _versioned_index()
        count = sum(
            1
            for _ in tqdm(
                document_stream(csv_path, new_index, limit), desc="dry-run", unit="docs"
            )
        )
        logger.info("Dry run complete — %d documents would be indexed", count)
        return

    client = build_es_client()
    new_index = _versioned_index()
    old_index = get_current_index(client, config.ES_ALIAS)
    logger.info("New index: %s | Old index: %s", new_index, old_index or "none")

    ensure_index(client, new_index, str(MAPPING_PATH))
    optimize_for_import(client, new_index)

    stream = tqdm(
        document_stream(csv_path, new_index, limit), desc="indexing", unit="docs"
    )
    indexed, errors = bulk_index(client, stream, chunk_size=BULK_CHUNK_SIZE)
    logger.info("Indexed %d documents, %d errors", indexed, len(errors))

    restore_after_import(client, new_index)

    total = client.count(index=new_index)["count"]
    logger.info("Total documents in '%s': %d", new_index, total)

    forcemerge(client, new_index)

    update_alias(
        client, alias=config.ES_ALIAS, new_index=new_index, old_index=old_index
    )

    if old_index:
        client.indices.delete(index=old_index)
        logger.info("Deleted old index '%s'", old_index)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv",
        type=Path,
        default=DEFAULT_CSV,
        help="Path to amazon_products.csv (default: article data/ folder)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Transform without indexing — validates the pipeline",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max documents to process",
    )
    args = parser.parse_args()

    run(csv_path=args.csv, dry_run=args.dry_run, limit=args.limit)
