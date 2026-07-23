"""Compare the vectors two embedding backends produce for the same titles.

Run this before pointing `EMBED_BACKEND` at a different engine for an index you care
about. It settles two things nothing else in the repo can:

  - `ollama show` reports 137M parameters for both nomic-embed-text v1 and v1.5, so it
    cannot tell you which one Ollama is actually serving. TEI's `/info` names the exact
    model and revision.
  - TEI reads `config_sentence_transformers.json` out of the model repo and will happily
    apply a `--default-prompt` nobody asked for, producing prefixed vectors on one side
    while Ollama produces bare ones.

Reading the mean cosine similarity:

    > 0.999   same model, same convention — the swap is safe
    ~ 0.99    kernel and dtype differences, benign
    ~ 0.68    the signature of a task prefix applied on one side only. Measured on this
              dataset: cos(title, "search_document: " + title) = 0.684. Check TEI's
              /info and EMBED_DOC_PREFIX before rebuilding anything.
    ~ 0.51    what ollama and tei actually give on this dataset, over 1000 titles, with
              not one pair above 0.95. Different weights or a different pipeline; the
              cause has not been established. It is why tei is the default: it reports
              its model SHA, dtype and pooling, and Ollama's GGUF conversion does not.
    < 0.95    otherwise: investigate — do not rebuild the index on the assumption that
              it is noise.

Both services have to be up: `make start OLLAMA=1`.
"""

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

repo_root = Path(__file__).resolve().parent.parent
article_root = repo_root / "article-02-vector-ingestion"
sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(article_root))

from shared import config
from embeddings.backends import get_backend

logger = logging.getLogger(__name__)

DEFAULT_CSV = article_root / "data" / "amazon_products.csv"
CSV_CHUNK_SIZE = 10_000


def sample_titles(csv_path: Path, count: int, stride: int) -> list[str]:
    """Take `count` titles spread across the file, not the first `count`.

    The CSV is sorted by category. Reading the head would compare the two backends on a
    single slice of the catalogue — the same mistake that made an earlier recall
    measurement meaningless.
    """
    titles: list[str] = []
    seen = 0

    for chunk in pd.read_csv(
        csv_path, chunksize=CSV_CHUNK_SIZE, low_memory=False, usecols=["title"]
    ):
        for value in chunk["title"]:
            if seen % stride == 0 and isinstance(value, str) and value.strip():
                titles.append(value)
                if len(titles) >= count:
                    return titles
            seen += 1

    logger.warning(
        "CSV exhausted after %d rows — %d titles instead of %d (lower --stride)",
        seen,
        len(titles),
        count,
    )
    return titles


def cosine(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Row-wise cosine similarity. Normalises explicitly rather than trusting the backend."""
    a_norm = a / np.linalg.norm(a, axis=1, keepdims=True)
    b_norm = b / np.linalg.norm(b, axis=1, keepdims=True)
    return np.sum(a_norm * b_norm, axis=1)


def embed_all(
    backend, texts: list[str], batch_size: int, prefix: str = ""
) -> np.ndarray:
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        window = texts[start : start + batch_size]
        vectors.extend(backend.embed([prefix + t for t in window]))
    return np.asarray(vectors, dtype=np.float64)


def _bar(count: int, total: int, width: int = 36) -> str:
    filled = 0 if total == 0 else round(width * count / total)
    return "█" * filled


def report_pairwise(sims: np.ndarray, name_a: str, name_b: str) -> None:
    print(f"\nSame {len(sims)} titles through both, cosine similarity per pair\n")
    print(f"  mean    {sims.mean():.4f}")
    print(f"  min     {sims.min():.4f}")
    print(f"  p1      {np.percentile(sims, 1):.4f}")
    print(f"  p50     {np.percentile(sims, 50):.4f}")
    print(f"  max     {sims.max():.4f}")

    buckets = [
        ("> 0.999", int((sims > 0.999).sum())),
        ("0.99–0.999", int(((sims > 0.99) & (sims <= 0.999)).sum())),
        ("0.95–0.99", int(((sims > 0.95) & (sims <= 0.99)).sum())),
        ("<= 0.95", int((sims <= 0.95).sum())),
    ]
    print()
    for label, count in buckets:
        print(f"  {label:<11} {_bar(count, len(sims)):<36} {count}")

    mean = sims.mean()
    print()
    if mean > 0.999:
        print(
            f"  → same model, same convention: {name_a} and {name_b} are interchangeable."
        )
    elif mean > 0.99:
        print("  → kernel/dtype differences only. Benign, the swap is safe.")
    elif 0.60 < mean < 0.80:
        print(
            "  → this is the signature of a task prefix applied on ONE side only "
            f"(measured reference: 0.684). Check {name_b}'s /info for a default prompt, "
            "and EMBED_DOC_PREFIX on this end. Do NOT rebuild the index yet."
        )
    else:
        print(
            "  → the two backends are not serving the same thing. Most likely a model "
            "version mismatch (v1 vs v1.5). Investigate before rebuilding the index."
        )


def report_prefix_gap(backend, titles: list[str], batch_size: int) -> None:
    """Measure what the document/query prefix pair costs if only one side is applied.

    This is the concrete version of the warning in the README: an index built with
    EMBED_DOC_PREFIX and queried without EMBED_QUERY_PREFIX degrades silently, and this
    number is how much alignment is lost.
    """
    doc_prefix = config.EMBED_DOC_PREFIX
    query_prefix = config.EMBED_QUERY_PREFIX

    print(f"\nTask prefixes on {backend.name}")
    print(f"  EMBED_DOC_PREFIX    {doc_prefix!r}")
    print(f"  EMBED_QUERY_PREFIX  {query_prefix!r}")

    if not doc_prefix and not query_prefix:
        print(
            "\n  Both empty — index and queries agree by construction. Nothing to check."
        )
        return

    sample = titles[: min(200, len(titles))]
    doc_side = embed_all(backend, sample, batch_size, prefix=doc_prefix)
    query_side = embed_all(backend, sample, batch_size, prefix=query_prefix)
    aligned = cosine(doc_side, query_side)

    bare = embed_all(backend, sample, batch_size, prefix="")
    doc_vs_bare = cosine(doc_side, bare)

    print(f"\n  cos(doc-side, query-side)   mean {aligned.mean():.4f}")
    print(f"  cos(doc-side, no prefix)    mean {doc_vs_bare.mean():.4f}")
    print(
        "\n  The second number is what a mismatch costs: an index built with the document\n"
        "  prefix and queried without it lands there. Both sides move together or neither."
    )


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    # Raw : la grille de lecture ci-dessus ne veut rien dire reformatée en paragraphe.
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--limit", type=int, default=1000, help="Titles to compare")
    parser.add_argument(
        "--stride",
        type=int,
        default=500,
        help="Take one row in N, to span the whole catalogue (default: 500)",
    )
    parser.add_argument("--batch", type=int, default=128)
    parser.add_argument(
        "--reference", default="ollama", help="Backend the index was built with"
    )
    parser.add_argument("--candidate", default="tei", help="Backend being considered")
    parser.add_argument(
        "--skip-prefix-check",
        action="store_true",
        help="Skip the document/query prefix alignment measurement",
    )
    args = parser.parse_args()

    if not args.csv.exists():
        raise SystemExit(f"CSV not found: {args.csv}")

    backend_a = get_backend(args.reference)
    backend_b = get_backend(args.candidate)

    print("Backends")
    print(f"  A  {backend_a.describe()}")
    print(f"  B  {backend_b.describe()}")

    titles = sample_titles(args.csv, args.limit, args.stride)
    if not titles:
        raise SystemExit("No usable title found in the CSV")

    vectors_a = embed_all(backend_a, titles, args.batch)
    vectors_b = embed_all(backend_b, titles, args.batch)

    if vectors_a.shape != vectors_b.shape:
        raise SystemExit(
            f"Dimension mismatch: {backend_a.name} returns {vectors_a.shape[1]}d, "
            f"{backend_b.name} returns {vectors_b.shape[1]}d. Different models — the "
            f"index mapping fixes dims at {config.OLLAMA_EMBED_DIMS}."
        )

    report_pairwise(cosine(vectors_a, vectors_b), backend_a.name, backend_b.name)

    if not args.skip_prefix_check:
        report_prefix_gap(backend_a, titles, args.batch)


if __name__ == "__main__":
    main()
