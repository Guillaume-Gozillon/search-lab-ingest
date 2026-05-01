"""Central configuration — all env vars loaded once here, imported everywhere else."""

import os

from dotenv import load_dotenv

load_dotenv()

ES_URL: str = os.getenv("ES_URL", "http://localhost:9200")
ES_INDEX: str = os.getenv("ES_INDEX", "amazon_products")
ES_ALIAS: str = os.getenv("ES_ALIAS", "products")

OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_EMBED_MODEL: str = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
OLLAMA_EMBED_DIMS: int = int(os.getenv("OLLAMA_EMBED_DIMS", "768"))
