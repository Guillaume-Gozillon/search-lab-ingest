"""Concurrent embedding stage — backend-agnostic, order-preserving, bounded in memory.

The three stages of the pipeline used to be generators chained inside a single thread:
read a batch, embed it, bulk it, repeat. That serialises two things that have nothing to
say to each other — the GPU sat idle for the seven minutes Elasticsearch spent taking
documents, and Elasticsearch sat idle while the GPU worked.

Here the main thread does nothing but read documents and hand batches of text to a pool.
Two invariants make that safe to drop into an ingestion pipeline:

**Order is preserved.** Batches complete out of order — that is the point of the pool —
but they are consumed in submission order, oldest first. A document comes out of this
function at the same position it went in.

**Memory is bounded.** At most `workers * 2` batches are in flight, plus the one being
assembled. The upstream generator is never drained further ahead than that, so a 1.4M-row
CSV streams through in constant memory whatever the worker count.

Generators only run while someone pulls on them, so the pool is created and torn down
around the iteration itself: abandoning the stream half-way cancels whatever had not
started rather than leaking threads.
"""

import logging
import time
from collections import deque
from collections.abc import Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor

logger = logging.getLogger(__name__)

_LOG_EVERY_BATCHES = 20


def embed_stream(
    docs: Iterable[dict],
    backend,
    batch_size: int,
    workers: int = 4,
    text_field: str = "title",
    embedding_field: str = "embedding",
    prefix: str = "",
) -> Iterator[dict]:
    """Buffer docs, embed `text_field` in batches, attach `embedding_field`, yield in order.

    `prefix` is prepended to every text before it reaches the backend. Empty by default,
    which is the historical behaviour — see `EMBED_DOC_PREFIX` for what turning it on
    commits you to.
    """
    # Both are reachable from the CLI, and both fail badly rather than obviously:
    # batch_size=0 never flushes and buffers the whole CSV into memory, workers=0 dies
    # inside ThreadPoolExecutor with a message that says nothing about --workers.
    if batch_size < 1:
        raise ValueError(f"--embed-batch must be >= 1, got {batch_size}")
    if workers < 1:
        raise ValueError(f"--workers must be >= 1, got {workers}")

    max_inflight = workers * 2
    inflight: deque[tuple[list[dict], Future]] = deque()
    embedded = 0
    batches = 0
    start = time.time()

    def submit(batch: list[dict]) -> None:
        texts = [prefix + doc[text_field] for doc in batch]
        inflight.append((batch, executor.submit(backend.embed, texts)))

    def take_oldest() -> list[dict]:
        """Wait on the batch submitted longest ago and zip its vectors back on.

        `future.result()` re-raises whatever the worker raised, in this thread, at this
        point in the stream — a backend failure stops the run instead of quietly leaving
        a hole in the index.
        """
        nonlocal embedded, batches

        batch, future = inflight.popleft()
        vectors = future.result()
        if len(vectors) != len(batch):
            raise RuntimeError(
                f"{getattr(backend, 'name', backend)} returned {len(vectors)} vectors "
                f"for {len(batch)} inputs"
            )

        for doc, vector in zip(batch, vectors):
            doc[embedding_field] = vector

        embedded += len(batch)
        batches += 1
        if batches % _LOG_EVERY_BATCHES == 0:
            elapsed = time.time() - start
            logger.info(
                "  %d docs embedded — %.1fs elapsed (%.0f docs/s)",
                embedded,
                elapsed,
                embedded / elapsed if elapsed > 0 else 0,
            )
        return batch

    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="embed")
    try:
        batch: list[dict] = []
        for doc in docs:
            batch.append(doc)
            if len(batch) < batch_size:
                continue
            submit(batch)
            batch = []
            # C'est ici qu'agit la contre-pression : tant que la fenêtre est pleine, on
            # ne relit pas le CSV, on attend le plus ancien lot.
            while len(inflight) >= max_inflight:
                yield from take_oldest()

        if batch:
            submit(batch)
        while inflight:
            yield from take_oldest()

        elapsed = time.time() - start
        logger.info(
            "Embedding complete — %d docs in %d batches, %.1fs (%.0f docs/s) via %s",
            embedded,
            batches,
            elapsed,
            embedded / elapsed if elapsed > 0 else 0,
            getattr(backend, "name", backend),
        )
    finally:
        # wait=False : on ne veut pas bloquer sur les lots restants quand on sort par une
        # exception ou parce que le consommateur a abandonné le générateur.
        executor.shutdown(wait=False, cancel_futures=True)
