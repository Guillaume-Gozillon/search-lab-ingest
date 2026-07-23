"""Embedding backends.

A backend turns a list of strings into a list of vectors, in order, one vector per input.
Nothing downstream knows which one answered — `EMBED_BACKEND` picks it at startup and the
rest of the pipeline is unchanged.

There is one today, `tei`. The indirection is kept because swapping the engine is not a
neutral operation and deserves an explicit seam: two servers running nominally the same
model produced vectors at a mean cosine of 0.51 on this dataset, which is why Ollama is no
longer here. An index is bound to the engine that built it.
"""

from typing import Protocol

from shared import config


class EmbeddingBackend(Protocol):
    """Texts in, vectors out — same order, same count."""

    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def describe(self) -> str:
        """One line naming the model and endpoint, for the run log."""
        ...


BACKENDS = ("tei",)


def get_backend(name: str | None = None) -> EmbeddingBackend:
    """Build the backend called `name`, defaulting to `EMBED_BACKEND`.

    The import is deferred so that a misconfigured backend, or one whose service is down,
    only breaks the run that actually asked for it.
    """
    resolved = (name or config.EMBED_BACKEND).strip().lower()

    if resolved == "tei":
        from .tei import TeiBackend

        return TeiBackend()

    raise ValueError(
        f"Unknown embedding backend '{resolved}' — expected one of: {', '.join(BACKENDS)}"
    )
