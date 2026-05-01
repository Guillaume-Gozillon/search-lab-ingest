"""Ollama batch embedding helper.

Buffers documents into fixed-size batches, calls the Ollama embed endpoint once
per batch, attaches the resulting vector to each document under the `embedding`
field, and yields enriched documents downstream.
"""

import logging
import time
from collections.abc import Iterator

from ollama import Client

logger = logging.getLogger(__name__)


def embed_stream(
    docs: Iterator[dict],
    client: Client,
    model: str,
    batch_size: int,
    text_field: str = "title",
    embedding_field: str = "embedding",
) -> Iterator[dict]:
    """Buffer docs, embed `text_field` in batches, attach `embedding_field`, yield.

    The Ollama Python client's `embed` method accepts a list of strings via the
    `input` parameter and returns one vector per input, in order — we rely on
    that ordering to zip vectors back onto the buffered docs.
    """
    buffer: list[dict] = []
    embedded = 0
    batches = 0
    start = time.time()
    log_every_batches = 20

    def flush(batch: list[dict]) -> Iterator[dict]:
        nonlocal embedded, batches
        if not batch:
            return
        texts = [doc[text_field] for doc in batch]
        response = client.embed(model=model, input=texts)
        vectors = response["embeddings"]
        if len(vectors) != len(batch):
            raise RuntimeError(
                f"Ollama returned {len(vectors)} vectors for {len(batch)} inputs"
            )
        for doc, vector in zip(batch, vectors):
            doc[embedding_field] = vector
            yield doc
        embedded += len(batch)
        batches += 1
        if batches % log_every_batches == 0:
            elapsed = time.time() - start
            rate = embedded / elapsed if elapsed > 0 else 0
            logger.info(
                "  %d docs embedded — %.1fs elapsed (%.0f docs/s)",
                embedded,
                elapsed,
                rate,
            )

    for doc in docs:
        buffer.append(doc)
        if len(buffer) >= batch_size:
            yield from flush(buffer)
            buffer = []

    yield from flush(buffer)

    elapsed = time.time() - start
    logger.info(
        "Embedding complete — %d docs in %d batches, %.1fs (%.0f docs/s)",
        embedded,
        batches,
        elapsed,
        embedded / elapsed if elapsed > 0 else 0,
    )
