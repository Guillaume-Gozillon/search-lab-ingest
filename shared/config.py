"""Central configuration — all env vars loaded once here, imported everywhere else."""

import os

from dotenv import load_dotenv

load_dotenv()

ES_URL: str = os.getenv("ES_URL", "http://localhost:9200")
ES_INDEX: str = os.getenv("ES_INDEX", "amazon_products")
