"""Central configuration — all env vars loaded once here, imported everywhere else."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

ES_HOST: str = os.getenv("ES_HOST", "https://localhost:9200")
ES_USER: str = os.getenv("ES_USER", "elastic")
ES_PASSWORD: str = os.getenv("ES_PASSWORD", "")
ES_CA_CERT: str | None = os.getenv("ES_CA_CERT") or None

INDEX_NAME: str = os.getenv("INDEX_NAME", "products-home-kitchen-v1")

HF_DATASET: str = os.getenv("HF_DATASET", "McAuley-Lab/Amazon-Reviews-2023")
HF_CONFIG: str = os.getenv("HF_CONFIG", "raw_meta_Home_and_Kitchen")

BULK_SIZE: int = int(os.getenv("BULK_SIZE", "500"))
