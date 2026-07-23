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
"""

import logging
from dataclasses import dataclass, field

from elasticsearch import Elasticsearch

logger = logging.getLogger(__name__)


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


def verify_vector_index(
    client: Elasticsearch,
    index: str,
    field_name: str = "embedding",
    probes: int = 5,
    k: int = 10,
    num_candidates: int = 500,
    min_recall: float = 0.40,
    seed: int = 0,
) -> VerifyReport:
    """Check that `index` is complete *and* that its kNN actually retrieves.

    Probe queries are the embeddings of documents drawn from the index itself. That matters
    more than it looks: a document is its own nearest neighbour, so every probe has a
    genuine, well-separated neighbourhood. Hand-written text queries do not — on this
    dataset their ranks 2-10 sit within 0.04 cosine of one another, so thousands of the
    1.4M documents are effectively tied and recall@k degenerates into measuring which ties
    the ANN happened to return.

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

    sample = client.options(request_timeout=600).search(
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

    recalls, self_hits = [], 0
    for hit in sample:
        vector = hit["_source"].get(field_name)
        if not vector:
            continue

        truth = _exact_top(client, index, field_name, vector, k)
        if not truth:
            continue
        found = _ann_top(client, index, field_name, vector, k, num_candidates)

        recalls.append(len(truth & found) / len(truth))
        self_hits += hit["_id"] in found

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
