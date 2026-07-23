"""Ollama embedding backend — the historical path, byte-identical behaviour.

`from ollama import Client` below reaches the installed `ollama` package, not this module:
Python 3 imports are absolute unless written with a leading dot.
"""

import logging

from ollama import Client

from shared import config

logger = logging.getLogger(__name__)


class OllamaBackend:
    """Embeds through Ollama's `/api/embed`.

    One `Client` is shared by every worker thread. That is safe — it wraps an
    `httpx.Client`, which is thread-safe and pools connections. Whether the requests are
    served in parallel is the server's decision, not ours: Ollama honours
    `OLLAMA_NUM_PARALLEL`, and at 1 it queues them however many workers push.
    """

    name = "ollama"

    def __init__(self, url: str | None = None, model: str | None = None) -> None:
        self.url = url or config.OLLAMA_URL
        self.model = model or config.OLLAMA_EMBED_MODEL
        self._client = Client(host=self.url)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """The client returns one vector per input, in input order — we rely on that."""
        response = self._client.embed(model=self.model, input=texts)
        return response["embeddings"]

    def describe(self) -> str:
        return f"ollama {self.model} at {self.url}"

    def __repr__(self) -> str:
        return f"OllamaBackend(url={self.url!r}, model={self.model!r})"
