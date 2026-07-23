"""Pluggable embedding backends.

Both backends turn a list of strings into a list of vectors, in order, one vector per
input. Nothing downstream needs to know which one answered — `EMBED_BACKEND` picks one at
startup and the rest of the pipeline is unchanged.

Why there are two: Ollama is a token-by-token generation engine pressed into embedding
service. It serves one request at a time unless `OLLAMA_NUM_PARALLEL` says otherwise, and
it pads 28-token titles into a 2048-token context. text-embeddings-inference is built for
this one job and batches dynamically. Same model, so the vectors should be
interchangeable — `tools/compare_embeddings.py` exists to prove that before you swap the
backend on an index you care about.
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


BACKENDS = ("ollama", "tei")


def get_backend(name: str | None = None) -> EmbeddingBackend:
    """Build the backend called `name`, defaulting to `EMBED_BACKEND`.

    The imports are deferred so that a backend which is misconfigured, or whose service is
    not running, only breaks the run that actually asked for it.
    """
    resolved = (name or config.EMBED_BACKEND).strip().lower()

    if resolved == "ollama":
        from .ollama import OllamaBackend

        return OllamaBackend()

    if resolved == "tei":
        from .tei import TeiBackend

        return TeiBackend()

    raise ValueError(
        f"Unknown embedding backend '{resolved}' — expected one of: {', '.join(BACKENDS)}"
    )
