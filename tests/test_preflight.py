"""Tests for the embedding pre-flight gate — and above all for its probe set.

The gate itself is small. The part that broke in 2026-07 was not the arithmetic, it
was `PROBE_GROUPS`: groups built by store aisle ("car maintenance", "baby gear")
instead of by object type. A sound TEI backend scored 71% on it and the run was
refused. So most of what is checked here is the *quality of the probe set*, not the
plumbing.

A note on what a mock can honestly prove. A fake backend that looks up the group
label and returns a vector near that group's centroid would validate nothing: it
would pass on any probe set, including the broken one, because the geometry comes
from the label rather than from the text. Two mocks are used instead:

  * `LexicalBackend` — a bag of words. Crude, but it reads the *strings* and knows
    nothing about the grouping. It is far weaker than a real model semantically, and
    far more sensitive to shared vocabulary, which is precisely the failure mode
    under test. If a probe set clusters correctly under bag-of-words, its groups are
    tight and its vocabulary is disjoint. The retired aisle-based set is kept in the
    file and must FAIL under the same backend — that is what makes the test
    discriminating rather than decorative.

  * `SyntheticBackend` — vectors with a chosen geometry, for the mechanical failure
    modes (noise, collapse, wrong dimensions, unreachable service).

Neither one is a real embedding model, and neither can tell you the number TEI will
produce. Only the Linux box can. What they guarantee is that a probe set carrying an
obvious design flaw does not reach it.

Run with `make test`, or directly: `python tests/test_preflight.py`.
"""

import re
import sys
import zlib
from collections import defaultdict
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "article-02-vector-ingestion"))

from embeddings.preflight import (  # noqa: E402
    HEALTHY_SEPARATION,
    MIN_NEIGHBOUR_RATE,
    MIN_SEPARATION,
    PROBE_GROUPS,
    check_embedding_backend,
)

DIMS = 768

# Le jeu retiré en 2026-07, conservé comme témoin négatif. Il ne doit jamais repasser.
AISLE_GROUPS = {
    "cookware": [
        "Nonstick Frying Pan Set with Stay-Cool Handles",
        "Cast Iron Dutch Oven Enameled 6 Quart with Lid",
        "Silicone Baking Mat Reusable Cookie Sheet Liner",
        "Wooden Cutting Board Bamboo with Juice Groove",
    ],
    "bath linen": [
        "Organic Cotton Bath Towel Set 6 Piece Hotel Quality",
        "Quick Dry Beach Towel Extra Large Sand Free",
        "Cotton Washcloths 12 Pack Soft Face Towels",
        "Memory Foam Bath Mat Non-Slip Highly Absorbent",
    ],
    "car maintenance": [
        "Digital Tire Pressure Gauge with Backlit Display",
        "Car Jump Starter Portable Battery Booster Pack",
        "Windshield Wiper Blades 24 Inch All Season",
        "Engine Oil Filter Wrench Adjustable Removal Tool",
    ],
}

# Modificateurs sans identité d'objet : tailles, quantités, adjectifs de qualité,
# prépositions. Ils ont le droit de se répéter d'un groupe à l'autre.
GENERIC = set(
    """
    a and for in of on the to with without
    inch inches cm mm ft oz ounce quart liter pack set piece count
    large small medium extra mini compact portable premium professional
    heavy duty adjustable reusable durable soft hard new best quality pro
    high low wide long short tall thick thin
    """.split()
)


def content_tokens(title):
    """Tokens that carry object identity. `[a-z]+` drops sizes and counts outright."""
    return {t for t in re.findall(r"[a-z]+", title.lower()) if t not in GENERIC}


# --------------------------------------------------------------------- mocks
class LexicalBackend:
    """Bag of words. Sees the strings, never the group labels.

    Strictly weaker than a real embedding model — it has no idea a percolator is a
    coffee maker — but that is what makes it a fair judge of a probe set: it can only
    cluster titles that genuinely share vocabulary, and it is unforgiving of the same
    word appearing in two different groups.
    """

    name = "lexical"

    def embed(self, texts):
        out = []
        for text in texts:
            v = np.zeros(DIMS)
            for token in re.findall(r"[a-z0-9]+", text.lower()):
                v[zlib.crc32(token.encode()) % DIMS] += 1.0
            norm = np.linalg.norm(v)
            out.append((v / norm if norm else v).tolist())
        return out

    def describe(self):
        return "bag of words"


class SyntheticBackend:
    """Vectors with a chosen geometry, for the mechanical failure modes."""

    name = "synthetic"

    def __init__(self, mode="sound", dims=DIMS):
        self.mode = mode
        self.dims = dims
        self.seen = []

    def _unit(self, seed):
        rng = np.random.default_rng(seed % (2**32))
        v = rng.normal(size=self.dims)
        return v / np.linalg.norm(v)

    def embed(self, texts):
        self.seen.extend(texts)
        if self.mode == "unreachable":
            raise ConnectionError("All connection attempts failed")
        if self.mode == "short":
            texts = texts[:-2]
        if self.mode == "collapse":  # 5 vecteurs distincts pour 24 textes
            return [self._unit(i % 5).tolist() for i, _ in enumerate(texts)]
        if self.mode == "zero":
            return [[0.0] * self.dims for _ in texts]

        out = []
        for text in texts:
            noise = self._unit(zlib.crc32(text.encode()))
            if self.mode == "noise":  # aucun contenu sémantique — l'incident
                out.append(noise.tolist())
                continue
            group = next(
                (g for g, ts in PROBE_GROUPS.items() if any(t in text for t in ts)),
                "?",
            )
            v = self._unit(zlib.crc32(group.encode())) + 0.6 * noise
            out.append((v / np.linalg.norm(v)).tolist())
        return out

    def describe(self):
        return f"synthetic/{self.mode}"


# ------------------------------------------------- 1. quality of the probe set
def test_each_group_shares_a_head_noun():
    """Within a group, titles must be the same object — so they share its name.

    The aisle-based set shared NOTHING inside any group. That is the purely lexical
    signature of the design error, visible without embedding anything.
    """
    for name, titles in PROBE_GROUPS.items():
        common = set.intersection(*(content_tokens(t) for t in titles))
        assert common, (
            f"group '{name}' has no word common to all its titles — its members are "
            f"probably four products from one aisle rather than four variants of one "
            f"object. Group by object type, not by shelf."
        )


def test_no_content_word_is_shared_between_groups():
    """The check that would have caught 'Mat' in both cookware and bath linen."""
    where = defaultdict(set)
    for name, titles in PROBE_GROUPS.items():
        for title in titles:
            for token in content_tokens(title):
                where[token].add(name)

    collisions = {tok: sorted(g) for tok, g in where.items() if len(g) > 1}
    assert not collisions, (
        f"these words appear in more than one group and will pull them together: "
        f"{collisions}"
    )


def test_probe_set_is_structurally_sound():
    titles = [t for ts in PROBE_GROUPS.values() for t in ts]
    assert len(set(titles)) == len(titles), "duplicate title in the probe set"
    assert len(PROBE_GROUPS) >= 3, "too few groups for inter-theme mean to mean much"
    for name, group in PROBE_GROUPS.items():
        assert (
            len(group) >= 2
        ), f"group '{name}' has one title — nearest-neighbour-in-theme is impossible"


def test_probe_set_clusters_under_a_model_that_only_reads_the_strings():
    """The load-bearing test: a bag of words already sorts this set correctly.

    Bag-of-words is a floor, not a target. A real model understands that a percolator
    and a French press are both coffee makers; this one only sees the words they
    share. Passing here means the groups are tight and the vocabulary disjoint, which
    is what leaves a real model the wide margin the floors assume.
    """
    report = check_embedding_backend(LexicalBackend(), expect_dims=DIMS)
    assert report.ok, report.failures
    assert report.neighbour_rate == 1.0, (
        f"only {report.neighbour_rate:.0%} of titles find their own group under plain "
        f"word overlap — {report.summary()}"
    )
    assert report.separation > HEALTHY_SEPARATION, (
        f"separation {report.separation:+.4f} is under the {HEALTHY_SEPARATION:+.2f} "
        f"design target. Tighten the groups; do not lower MIN_SEPARATION."
    )


def test_the_retired_aisle_based_set_still_fails():
    """Guards the guard: if this passes, the test above proves nothing."""
    report = check_embedding_backend(LexicalBackend(), groups=AISLE_GROUPS)
    assert not report.ok, (
        f"the aisle-based probe set passed — the check has lost its teeth: "
        f"{report.summary()}"
    )


# ------------------------------------------------------ 2. mechanics of the gate
def test_a_sound_engine_passes():
    report = check_embedding_backend(SyntheticBackend("sound"), expect_dims=DIMS)
    assert report.ok, report.failures
    assert report.dims == DIMS
    assert report.texts == sum(len(t) for t in PROBE_GROUPS.values())
    assert report.groups == len(PROBE_GROUPS)


def test_meaningless_vectors_are_refused():
    """The 2026-07 incident: 768 finite floats, norm 1.0, and no meaning."""
    report = check_embedding_backend(SyntheticBackend("noise"), expect_dims=DIMS)
    assert not report.ok
    assert (
        report.dims == DIMS
    ), "the vectors are structurally perfect — that is the point"
    assert any("nearest neighbour" in f for f in report.failures), report.failures


def test_collapsed_vectors_are_refused():
    report = check_embedding_backend(SyntheticBackend("collapse"), expect_dims=DIMS)
    assert not report.ok
    assert report.duplicates > 0
    assert any("same vector" in f for f in report.failures), report.failures


def test_an_unreachable_backend_names_the_service():
    report = check_embedding_backend(SyntheticBackend("unreachable"))
    assert not report.ok
    assert any("did not answer" in f for f in report.failures), report.failures
    assert any("Ready" in f for f in report.failures), "should point at the TEI logs"


def test_a_dimension_mismatch_is_caught_before_indexing():
    report = check_embedding_backend(
        SyntheticBackend("sound", dims=384), expect_dims=768
    )
    assert not report.ok
    assert any("384d" in f and "768" in f for f in report.failures), report.failures


def test_a_truncated_response_is_caught():
    report = check_embedding_backend(SyntheticBackend("short"), expect_dims=DIMS)
    assert not report.ok
    assert any("asked for" in f for f in report.failures), report.failures


def test_zero_vectors_are_refused():
    report = check_embedding_backend(SyntheticBackend("zero"), expect_dims=DIMS)
    assert not report.ok
    assert any("zero-length" in f for f in report.failures), report.failures


def test_the_prefix_reaches_the_backend():
    """The check must exercise the configuration about to run, not an idealised one."""
    backend = SyntheticBackend("sound")
    check_embedding_backend(backend, prefix="search_document: ")
    assert backend.seen, "the backend was never called"
    assert all(t.startswith("search_document: ") for t in backend.seen), backend.seen[
        :2
    ]


def test_floors_sit_between_the_two_engines_ever_measured():
    """11% / -0.0083 for the broken engine, 100% / +0.2652 for the sound one."""
    assert 0.11 < MIN_NEIGHBOUR_RATE < 1.0
    assert -0.0083 < MIN_SEPARATION < 0.2652
    assert HEALTHY_SEPARATION > MIN_SEPARATION


# ------------------------------------------------------------------- runner
if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}\n     {exc}")
        else:
            print(f"ok   {name}")

    report = check_embedding_backend(LexicalBackend(), expect_dims=DIMS)
    print(f"\nprobe set under bag-of-words — {report.summary()}")
    print(f"{failures} failure(s)")
    sys.exit(1 if failures else 0)
