"""text-embeddings-inference backend.

A TEI server serves exactly one model, fixed by the container's `--model-id`, so there is
no model parameter on the wire. Batching happens server-side: the client's only job is to
keep requests coming, which is what the concurrent stage in `embeddings/stream.py` does.
"""

import logging

import httpx

from shared import config

logger = logging.getLogger(__name__)

# Généreux : une requête de 512 titres tourne en quelques secondes sur GPU. Le temps long
# de TEI est le premier démarrage (téléchargement du modèle), pas les requêtes.
_DEFAULT_TIMEOUT = 120.0


class TeiBackend:
    """Embeds through TEI's `/embed`.

    `normalize` is sent explicitly rather than left to the server default: Ollama returns
    L2-normalised vectors (measured norm 1.0000) and the index is built on cosine
    similarity, so the two backends have to agree on this or the vectors stop being
    comparable.
    """

    name = "tei"

    def __init__(
        self,
        url: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        normalize: bool = True,
    ) -> None:
        self.url = (url or config.TEI_URL).rstrip("/")
        self.normalize = normalize
        self._client = httpx.Client(base_url=self.url, timeout=timeout)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """POST /embed — the response is the list of vectors, in input order."""
        response = self._client.post(
            "/embed",
            json={"inputs": texts, "truncate": True, "normalize": self.normalize},
        )

        if response.status_code == 413:
            raise RuntimeError(
                f"TEI refused a batch of {len(texts)} inputs (413). Its "
                f"--max-client-batch-size is lower than the pipeline's --embed-batch: "
                f"raise the flag on the text-embeddings service in docker-compose.yml, or "
                f"lower the batch."
            )
        if response.status_code != 200:
            raise RuntimeError(
                f"TEI returned {response.status_code} on /embed at {self.url}: "
                f"{response.text[:300]}"
            )

        return response.json()

    def info(self) -> dict:
        """TEI's `/info` — model id, revision, pooling, max input length.

        Worth reading before trusting any comparison: it names the exact model revision,
        which `ollama show` does not (v1 and v1.5 both report 137M parameters). It also
        reveals a `--default-prompt` picked up from the model repo, which would silently
        prefix every vector.
        """
        response = self._client.get("/info")
        response.raise_for_status()
        return response.json()

    def describe(self) -> str:
        # Ligne de log : elle dégrade, elle ne fait jamais tomber le run.
        try:
            info = self.info()
        except Exception as exc:
            return f"tei at {self.url} (/info unreachable: {exc})"
        model = info.get("model_id", "?")
        sha = (info.get("model_sha") or "")[:8]
        return f"tei {model}@{sha or '?'} at {self.url}"

    def __repr__(self) -> str:
        return f"TeiBackend(url={self.url!r}, normalize={self.normalize!r})"
