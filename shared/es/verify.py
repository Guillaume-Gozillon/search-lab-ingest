"""Post-build checks for a vector index.

A vector index can be complete, well-formed, and still not be what you think: every
document present, every embedding a valid 768-float unit vector, and nothing anywhere
saying whether the kNN query can actually retrieve them. The pipeline used to report a
clean success on exactly that evidence — document counts and error counts — because
counting documents says nothing about the quality of the HNSW graph built over them.

So this module does not check that the vectors exist. It checks that searching finds
them, by comparing the approximate kNN against an exact brute-force scan over the same
index. Exact search is expensive — tens of seconds per probe on a million documents —
which is why the probe count is small and configurable rather than free.

Probe vectors come from one of two places. `ProbeReservoir` taps the ingestion stream and
keeps a uniform sample of the documents as they go past; that is the only option once the
mapping excludes `embedding` from `_source`, because the vectors can no longer be read
back out of the index. Failing that, `verify_vector_index` samples them from the index
itself, which still works anywhere the vectors live in `_source`.
"""

import logging
import random
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field

from elasticsearch import Elasticsearch

logger = logging.getLogger(__name__)

# (_id, vecteur) — l'identifiant sert à mesurer l'auto-récupération.
Probe = tuple[str, list[float]]


class ProbeReservoir:
    """Pass-through sampler: yields every document, keeps `size` of them uniformly.

    Reservoir sampling (Algorithm R), so the sample is uniform over a stream whose length
    is not known in advance, at the cost of one `random()` per document — roughly 0.1 s
    across 1.4M. Keeping the first N would be cheaper and much worse: this CSV is sorted
    by category, so the head of the stream is one narrow slice of the catalogue.

    Tap it between the embedding stage and the bulk stage. The documents have to carry
    their vector already, and they have to still be on their way into the index.
    """

    def __init__(
        self, size: int, seed: int = 0, embedding_field: str = "embedding"
    ) -> None:
        self.size = size
        self.items: list[Probe] = []
        self._rng = random.Random(seed)
        self._field = embedding_field
        self._seen = 0

    def tap(self, docs: Iterable[dict]) -> Iterator[dict]:
        for doc in docs:
            vector = doc.get(self._field)
            if vector is not None:
                self._seen += 1
                if len(self.items) < self.size:
                    self.items.append((doc["_id"], vector))
                else:
                    slot = self._rng.randrange(self._seen)
                    if slot < self.size:
                        self.items[slot] = (doc["_id"], vector)
            yield doc


@dataclass
class VerifyReport:
    """Outcome of `verify_vector_index`."""

    total: int = 0
    with_vector: int = 0
    probes: int = 0
    k: int = 0
    mean_recall: float = 0.0
    self_hit: float = 0.0
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        return (
            f"{self.with_vector}/{self.total} documents carry a vector — "
            f"recall@{self.k} {self.mean_recall:.0%}, "
            f"self-retrieval {self.self_hit:.0%} over {self.probes} probes"
        )


def _exact_top(client, index, field_name, vector, k):
    """Brute-force nearest neighbours: the ground truth the ANN is judged against.

    Documents are identified by `_id`, so nothing here depends on the document schema.

    The inner query filters on the vector's existence rather than matching everything:
    `cosineSimilarity` raises at runtime the moment it is handed a document without the
    field, which would turn a partially-embedded index into an opaque 400 rather than a
    reportable finding.
    """
    res = client.options(request_timeout=600).search(
        index=index,
        size=k,
        query={
            "script_score": {
                "query": {"exists": {"field": field_name}},
                "script": {
                    "source": f"cosineSimilarity(params.q, '{field_name}') + 1.0",
                    "params": {"q": vector},
                },
            }
        },
        source=False,
    )
    return {h["_id"] for h in res["hits"]["hits"]}


def _ann_top(client, index, field_name, vector, k, num_candidates):
    res = client.options(request_timeout=600).search(
        index=index,
        knn={
            "field": field_name,
            "query_vector": vector,
            "k": k,
            "num_candidates": num_candidates,
        },
        source=False,
    )
    return {h["_id"] for h in res["hits"]["hits"]}


def _sample_probes(client, index, field_name, probes, seed) -> list[Probe]:
    """Draw probe vectors back out of the index — the fallback when none were captured.

    Returns an empty list when the mapping excludes `field_name` from `_source`. That is
    not an error at this level: it means the caller has to supply `probe_vectors`, and
    `verify_vector_index` says so.
    """
    hits = client.options(request_timeout=600).search(
        index=index,
        size=probes,
        query={
            "function_score": {
                "query": {"exists": {"field": field_name}},
                "random_score": {"seed": seed, "field": "_seq_no"},
            }
        },
        source=[field_name],
    )["hits"]["hits"]

    return [
        (hit["_id"], hit["_source"][field_name])
        for hit in hits
        if hit.get("_source", {}).get(field_name)
    ]


def verify_vector_index(
    client: Elasticsearch,
    index: str,
    field_name: str = "embedding",
    probes: int = 5,
    k: int = 10,
    num_candidates: int = 500,
    min_recall: float = 0.40,
    seed: int = 0,
    probe_vectors: Sequence[Probe] | None = None,
) -> VerifyReport:
    """Check that `index` is complete *and* that its kNN actually retrieves.

    Probe queries are the embeddings of documents that are in the index. That matters more
    than it looks: a document is its own nearest neighbour, so every probe has a genuine,
    well-separated neighbourhood. Hand-written text queries do not — on this dataset their
    ranks 2-10 sit within 0.04 cosine of one another, so thousands of the 1.4M documents
    are effectively tied and recall@k degenerates into measuring which ties the ANN
    happened to return.

    Pass `probe_vectors` (see `ProbeReservoir`) when the mapping keeps `embedding` out of
    `_source`; otherwise they are sampled from the index.

    `min_recall` is a floor for *broken*, not a target for *good*. Measured on the full
    index at these defaults: 68% on a naturally-merged index, 63% on one force-merged to a
    single segment per shard. The 40% floor clears both with margin while still catching a
    graph that genuinely fails to retrieve. Five probes is a coarse instrument — read a
    pass as "search works", never as "recall is tuned".

    Returns a report; the caller decides what to do with `report.ok`.
    """
    report = VerifyReport(k=k)

    report.total = client.count(index=index)["count"]
    report.with_vector = client.count(
        index=index, query={"exists": {"field": field_name}}
    )["count"]

    if report.total == 0:
        report.failures.append("index is empty")
        return report

    if report.with_vector != report.total:
        missing = report.total - report.with_vector
        report.failures.append(
            f"{missing} document(s) have no '{field_name}' — the run did not embed everything"
        )
        # Inutile de mesurer le rappel d'un index incomplet : il faut le reconstruire de
        # toute façon, et chaque sonde coûte une recherche exacte sur tout l'index.
        return report

    if probe_vectors is None:
        probe_vectors = _sample_probes(client, index, field_name, probes, seed)

    if not probe_vectors:
        report.failures.append(
            f"no probe vector available — '{field_name}' is indexed but absent from "
            f"_source, so it cannot be read back. Capture probes during ingestion with "
            f"ProbeReservoir and pass them as probe_vectors."
        )
        return report

    recalls, self_hits = [], 0
    for doc_id, vector in list(probe_vectors)[:probes]:
        if not vector:
            continue

        truth = _exact_top(client, index, field_name, vector, k)
        if not truth:
            continue
        found = _ann_top(client, index, field_name, vector, k, num_candidates)

        recalls.append(len(truth & found) / len(truth))
        self_hits += doc_id in found

    if not recalls:
        report.failures.append("no probe returned a usable neighbourhood")
        return report

    report.probes = len(recalls)
    report.mean_recall = sum(recalls) / len(recalls)
    report.self_hit = self_hits / len(recalls)

    if report.mean_recall < min_recall:
        report.failures.append(
            f"kNN recall@{k} is {report.mean_recall:.0%} at num_candidates={num_candidates}, "
            f"below the {min_recall:.0%} floor — the documents are indexed but the HNSW "
            f"graph does not retrieve them"
        )

    return report
