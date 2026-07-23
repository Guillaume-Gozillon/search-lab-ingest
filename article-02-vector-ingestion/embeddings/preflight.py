"""Pre-flight check of the embedding backend: do its vectors carry any meaning?

Every structural check this project had was green on an engine that was producing noise.
`docs.count` matched, every vector was 768 floats, every norm was 1.0000, and the recall
gate passed at 90% — because the recall gate compares approximate kNN against an exact
scan **over the same vectors**. If those vectors are noise, both methods retrieve the same
noise and recall is excellent. A 1.4M-document index was served for a week on embeddings
that were, measurably, unrelated to the text they came from:

    "Womens Shacket ... Button Down Shirts"  →  nearest neighbour: "Vintage Copper Train
                                                London Pocket Watch", cosine 0.9995

So this module checks the one thing nothing else does: that similar texts come back with
similar vectors and dissimilar texts do not. It embeds a small fixed set of product titles
grouped by theme, and asks whether each title's nearest neighbour stays inside its own
group. Measured on the two engines this project has run:

    | engine | intra-theme | inter-theme | separation | nearest neighbour correct |
    |--------|-------------|-------------|------------|---------------------------|
    | broken | 0.8013      | 0.8097      | -0.0083    | 11%  (worse than chance)  |
    | sound  | 0.6254      | 0.3602      | +0.2652    | 100%                      |

It costs one embedding call of a couple of dozen short strings — call it two seconds. That
is the point: finding out the engine is broken must not cost nine minutes of embedding
followed by a gate at the end. This runs before the first CSV row is read.

It does not replace `shared/es/verify.py`. That one validates the HNSW graph after the
build; this one validates the engine before it. Neither sees what the other sees.
"""

import logging
from dataclasses import dataclass, field

import numpy as np

logger = logging.getLogger(__name__)

# Six domaines volontairement disjoints, quatre titres chacun, dans le style du dataset.
# Le contenu importe moins que la séparation : deux titres du même groupe doivent se
# ressembler davantage que deux titres de groupes différents. Éviter les mots partagés
# entre groupes (« microfiber », « stainless steel ») qui créeraient de fausses collisions.
PROBE_GROUPS: dict[str, list[str]] = {
    "audio": [
        "Wireless Bluetooth Over-Ear Headphones with Active Noise Cancelling",
        "Portable Bluetooth Speaker Waterproof for Outdoor Parties",
        "In-Ear Wired Earbuds with Microphone and Inline Volume Control",
        "USB Condenser Microphone for Podcast and Streaming Recording",
    ],
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
    "pet supplies": [
        "Orthopedic Dog Bed for Large Breeds with Washable Cover",
        "Cat Water Fountain Automatic Circulating Pet Drinking",
        "Retractable Dog Leash 16 ft Heavy Duty Tangle Free",
        "Interactive Cat Toy Feather Wand with Bells",
    ],
    "car maintenance": [
        "Digital Tire Pressure Gauge with Backlit Display",
        "Car Jump Starter Portable Battery Booster Pack",
        "Windshield Wiper Blades 24 Inch All Season",
        "Engine Oil Filter Wrench Adjustable Removal Tool",
    ],
    "baby gear": [
        "Baby Onesies Short Sleeve 5 Pack Newborn Bodysuits",
        "Diaper Bag Backpack with Insulated Bottle Pockets",
        "Baby Bottle Sterilizer and Dryer Electric",
        "Infant Head and Body Support Cushion for Strollers",
    ],
}

# Planchers pour « cassé », pas cibles de qualité — même logique que le `min_recall` de
# shared/es/verify.py. Les deux moteurs mesurés tombent de part et d'autre sans ambiguïté
# (11 % / -0,0083 contre 100 % / +0,2652), donc un plancher au milieu tranche sans risquer
# de recaler un moteur correct. Ne pas les monter pour « améliorer » la qualité : ce n'est
# pas ce qu'ils mesurent.
MIN_NEIGHBOUR_RATE = 0.80
MIN_SEPARATION = 0.05

# Deux textes différents qui reviennent au-dessus de ce cosinus sont le même vecteur. Le
# moteur cassé rendait 5 vecteurs distincts pour 9 textes distincts — en appels groupés
# comme unitaires, donc ce n'était pas un défaut de batching.
_DUPLICATE_COSINE = 0.9999


@dataclass
class PreflightReport:
    """Outcome of `check_embedding_backend`."""

    backend: str = ""
    dims: int = 0
    texts: int = 0
    groups: int = 0
    neighbour_rate: float = 0.0
    intra: float = 0.0
    inter: float = 0.0
    duplicates: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def separation(self) -> float:
        """Intra-theme minus inter-theme similarity — the most legible single signal."""
        return self.intra - self.inter

    @property
    def ok(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        return (
            f"{self.backend} — {self.dims}d, "
            f"nearest neighbour in-theme {self.neighbour_rate:.0%}, "
            f"intra {self.intra:.4f} / inter {self.inter:.4f} "
            f"(separation {self.separation:+.4f}) "
            f"over {self.texts} titles in {self.groups} themes"
        )


def _cosine_matrix(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return (vectors / norms) @ (vectors / norms).T


def check_embedding_backend(
    backend,
    prefix: str = "",
    expect_dims: int | None = None,
    groups: dict[str, list[str]] | None = None,
    min_neighbour_rate: float = MIN_NEIGHBOUR_RATE,
    min_separation: float = MIN_SEPARATION,
) -> PreflightReport:
    """Embed a fixed set of themed titles and check the vectors carry their meaning.

    `prefix` is applied exactly as the ingestion will apply it, so the check exercises the
    configuration that is about to run rather than an idealised one.

    `expect_dims` comes from the index mapping. Catching a dimension mismatch here turns a
    mid-run bulk rejection into a two-second refusal to start.

    Returns a report; the caller decides what to do with `report.ok`.
    """
    groups = groups or PROBE_GROUPS
    labels = [name for name, titles in groups.items() for _ in titles]
    texts = [title for titles in groups.values() for title in titles]

    report = PreflightReport(
        backend=getattr(backend, "name", str(backend)),
        texts=len(texts),
        groups=len(groups),
    )

    try:
        raw = backend.embed([prefix + text for text in texts])
    except Exception as exc:
        report.failures.append(
            f"the '{report.backend}' backend did not answer: {exc.__class__.__name__}: "
            f"{exc}. Is the service running and finished loading its model? "
            f"`docker logs -f search-lab-tei` should show 'Ready'."
        )
        return report

    if len(raw) != len(texts):
        report.failures.append(
            f"asked for {len(texts)} vectors, got {len(raw)} — the backend does not "
            f"return one vector per input, which the pipeline relies on"
        )
        return report

    vectors = np.asarray(raw, dtype=np.float64)
    if vectors.ndim != 2 or vectors.shape[1] == 0:
        report.failures.append(f"malformed vectors: shape {vectors.shape}")
        return report

    report.dims = int(vectors.shape[1])

    if expect_dims is not None and report.dims != expect_dims:
        report.failures.append(
            f"the backend returns {report.dims}d vectors but the index mapping declares "
            f"{expect_dims} — indexing would fail on every document. Align the mapping "
            f"with the model, or the model with the mapping."
        )
        return report

    norms = np.linalg.norm(vectors, axis=1)
    if not np.all(np.isfinite(vectors)) or np.any(norms == 0):
        report.failures.append(
            "the backend returned zero-length or non-finite vectors — cosine similarity "
            "is undefined on those and Elasticsearch will reject them"
        )
        return report

    similarity = _cosine_matrix(vectors)
    np.fill_diagonal(similarity, -np.inf)

    same_group = np.array(labels)[:, None] == np.array(labels)[None, :]

    nearest = similarity.argmax(axis=1)
    report.neighbour_rate = float(
        np.mean([same_group[i, j] for i, j in enumerate(nearest)])
    )

    off_diagonal = ~np.eye(len(texts), dtype=bool)
    report.intra = float(similarity[same_group & off_diagonal].mean())
    report.inter = float(similarity[~same_group].mean())

    report.duplicates = int(np.sum(similarity[off_diagonal] > _DUPLICATE_COSINE) // 2)
    if report.duplicates:
        report.failures.append(
            f"{report.duplicates} pair(s) of different titles came back as the same vector "
            f"(cosine > {_DUPLICATE_COSINE}) — the backend is collapsing distinct inputs"
        )

    if report.neighbour_rate < min_neighbour_rate:
        report.failures.append(
            f"only {report.neighbour_rate:.0%} of titles have their nearest neighbour "
            f"inside their own theme, below the {min_neighbour_rate:.0%} floor — these "
            f"embeddings do not encode what the text says"
        )

    if report.separation < min_separation:
        report.failures.append(
            f"themes are not separated: intra {report.intra:.4f} vs inter "
            f"{report.inter:.4f} ({report.separation:+.4f}, floor {min_separation:+.4f}). "
            f"Unrelated products look as similar as related ones."
        )

    return report
