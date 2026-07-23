"""Bulk-index the Kaggle Amazon Products CSV into Elasticsearch with title embeddings.

Same shape as article-01's pipeline, with one extra stage between transform and bulk:
title strings are batched and sent to Ollama, and the returned vectors are attached
as `embedding` (dense_vector, 768d) on each document.
"""

import argparse
import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
from ollama import Client
from tqdm import tqdm

repo_root = Path(__file__).resolve().parent.parent
article_root = Path(__file__).resolve().parent
sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(article_root))

from shared import config
from shared.es.bulk import (
    bulk_index,
    forcemerge,
    optimize_for_import,
    restore_after_import,
    update_alias,
)
from shared.es.client import (
    build_es_client,
    ensure_index,
    next_version_index,
    resolve_write_index,
)
from shared.es.verify import verify_vector_index
from embeddings.ollama import embed_stream
from transforms.product import transform

logger = logging.getLogger(__name__)

INDEX_BASE = "amazon_products_embeddings"
MAPPING_PATH = (
    Path(__file__).resolve().parent / "mappings" / "amazon_products_embeddings_v1.json"
)
DEFAULT_CSV = Path(__file__).resolve().parent / "data" / "amazon_products.csv"
KAGGLE_DATASET = "asaniczka/amazon-products-dataset-2023-1-4m-products"
CSV_CHUNK_SIZE = 10_000
BULK_CHUNK_SIZE = 2_000
EMBED_BATCH_SIZE = 128

# Pas de force merge sur un index vectoriel. Chaque segment porte son propre graphe HNSW et
# Elasticsearch les interroge tous avec le budget `num_candidates` complet, donc écraser
# l'index en un seul segment par shard réduit l'exploration du kNN. Mesuré sur ce dataset
# (100k docs, rappel@10 contre recherche exacte) : 92 % sans fusion contre 87 % fusionné à
# 1 segment, pour 25 s de fusion en plus. Le coût est modeste mais gratuit à éviter, la
# politique de fusion de Lucene suffit. Mettre un entier ici pour réactiver une fusion —
# viser plusieurs segments par shard, jamais 1.
VECTOR_MAX_SEGMENTS: int | None = None

# Nombre de sondes du contrôle de rappel en fin de run. Chaque sonde fait une recherche
# exacte sur tout l'index — comptez ~35 s par sonde sur 1,4M documents.
VERIFY_PROBES = 5


def download_from_kaggle(dest: Path) -> Path:
    """Download the Kaggle dataset and copy the CSV to dest."""
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


def document_stream(csv_path: Path, limit: int | None, index: str) -> Iterator[dict]:
    """Yield transformed ES documents from the CSV, reading 10 000 rows at a time."""
    emitted = 0
    skipped = 0

    for chunk in pd.read_csv(csv_path, chunksize=CSV_CHUNK_SIZE, low_memory=False):
        for row in chunk.to_dict("records"):
            if limit is not None and emitted >= limit:
                logger.info(
                    "Limit reached (%d) — emitted %d, skipped %d",
                    limit,
                    emitted,
                    skipped,
                )
                return
            doc = transform(row)
            if doc is None:
                skipped += 1
                continue
            doc["_index"] = index
            emitted += 1
            yield doc

    logger.info("Stream finished — emitted %d, skipped %d", emitted, skipped)


def run(
    csv_path: Path,
    dry_run: bool = False,
    limit: int | None = None,
    embed_batch: int = EMBED_BATCH_SIZE,
    recreate: bool = False,
) -> None:
    """Execute the ingestion pipeline."""
    if not csv_path.exists():
        csv_path = download_from_kaggle(csv_path)

    logger.info("Reading CSV: %s", csv_path)

    ollama_client = Client(host=config.OLLAMA_URL)

    if dry_run:
        stream = tqdm(
            document_stream(csv_path, limit, INDEX_BASE),
            desc="dry-run-transform",
            unit="docs",
        )
        embedded = embed_stream(
            stream,
            ollama_client,
            model=config.OLLAMA_EMBED_MODEL,
            batch_size=embed_batch,
        )
        count = 0
        for doc in tqdm(embedded, desc="dry-run-embed", unit="docs"):
            if count == 0:
                logger.info(
                    "First doc embedding sample — len=%d, first 5 dims=%s",
                    len(doc["embedding"]),
                    doc["embedding"][:5],
                )
            count += 1
        logger.info("Dry run complete — %d documents would be indexed", count)
        return

    es_client = build_es_client()
    alias = config.ES_EMBEDDINGS_ALIAS

    if recreate:
        index_name = next_version_index(es_client, INDEX_BASE)
        logger.info(
            "Recreate — building '%s'; the index currently behind '%s' stays queryable "
            "until the alias swap at the end",
            index_name,
            alias,
        )
    else:
        index_name = resolve_write_index(es_client, INDEX_BASE, alias)
        logger.info("Incremental — writing into '%s'", index_name)

    ensure_index(es_client, index_name, str(MAPPING_PATH))
    optimize_for_import(es_client, index_name)

    raw_stream = tqdm(
        document_stream(csv_path, limit, index_name), desc="transform", unit="docs"
    )
    embedded_stream = embed_stream(
        raw_stream,
        ollama_client,
        model=config.OLLAMA_EMBED_MODEL,
        batch_size=embed_batch,
    )
    indexed, errors = bulk_index(es_client, embedded_stream, chunk_size=BULK_CHUNK_SIZE)
    logger.info("Indexed %d documents, %d errors", indexed, len(errors))

    restore_after_import(es_client, index_name)

    total = es_client.count(index=index_name)["count"]
    logger.info("Total documents in '%s': %d", index_name, total)

    if VECTOR_MAX_SEGMENTS is not None:
        forcemerge(es_client, index_name, max_num_segments=VECTOR_MAX_SEGMENTS)

    # Un index peut être complet, bien formé, et malgré tout inutilisable en recherche :
    # compter les documents ne dit rien de la capacité du graphe HNSW à les retrouver.
    # On mesure avant de publier.
    report = verify_vector_index(es_client, index_name, probes=VERIFY_PROBES)
    logger.info("Verification — %s", report.summary())
    if not report.ok:
        for failure in report.failures:
            logger.error("  %s", failure)
        if recreate:
            raise SystemExit(
                f"'{index_name}' failed verification — the alias was NOT moved. "
                f"'{alias}' still serves the previous index, so nothing in production "
                f"changed. Inspect the new index by name, or drop it: DELETE /{index_name}"
            )
        raise SystemExit(
            f"'{index_name}' failed verification, and this was an incremental run — the "
            f"documents went straight into the index '{alias}' already serves. Rebuild "
            f"with --recreate, which keeps the current index live until the new one passes."
        )

    # En dernier : l'alias ne bascule que sur un index complet, rafraîchi et vérifié.
    detached = update_alias(es_client, alias, index_name)
    if detached:
        logger.info(
            "Previous index kept — roll back with: "
            "POST /_aliases {'actions':[{'add':{'index':'%s','alias':'%s'}}]}, "
            "or drop it with: DELETE /%s",
            detached[0],
            alias,
            detached[0],
        )


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
        help="Transform + embed without indexing — validates the pipeline",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max documents to process",
    )
    parser.add_argument(
        "--embed-batch",
        type=int,
        default=EMBED_BATCH_SIZE,
        help=f"Ollama embedding batch size (default: {EMBED_BATCH_SIZE})",
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help=(
            "Build a fresh versioned index instead of writing into the current one, "
            "then swap the alias onto it once the run completes"
        ),
    )
    args = parser.parse_args()

    run(
        csv_path=args.csv,
        dry_run=args.dry_run,
        limit=args.limit,
        embed_batch=args.embed_batch,
        recreate=args.recreate,
    )
