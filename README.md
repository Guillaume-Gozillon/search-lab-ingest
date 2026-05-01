# search-lab-ingest

Data engineering pipelines for ingesting product data into Elasticsearch 9.x.
Sibling of [search-lab](../search-lab) (Node.js / TypeScript — queries & aggregations).

Each article in this repo explores a different ingestion approach or technique.

## Prerequisites

- Docker Desktop
- Python 3.12

## Setup

```bash
git clone <repo-url> && cd search-lab-ingest
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in ES_PASSWORD
```

## Start the cluster

```bash
docker compose -f docker/docker-compose.yml --env-file .env up -d
```

Elasticsearch will be available at `https://localhost:9200`.

## Articles

| Folder | Topic |
|--------|-------|
| [article-01-csv-bulk-ingestion](./article-01-csv-bulk-ingestion) | Bulk-index a 1.4M-row Kaggle CSV into ES with pandas |
| [article-02-vector-ingestion](./article-02-vector-ingestion) | Add 768-dim `dense_vector` embeddings at ingestion time via Ollama |

## Project structure

```
search-lab-ingest/
├── shared/                              # shared across all articles
│   ├── config.py                        # central config (env vars)
│   └── es/
│       ├── client.py                    # ES client factory + index helpers
│       └── bulk.py                      # bulk_index, update_alias, optimize_for_import, forcemerge
├── article-01-csv-bulk-ingestion/
│   ├── data/                            # gitignored — put CSV here
│   ├── mappings/                        # ES index mappings
│   ├── transforms/                      # raw row → ES document
│   └── pipeline.py                      # main entry point
├── article-02-vector-ingestion/
│   ├── data/                            # gitignored — put CSV here
│   ├── mappings/                        # ES index mapping with dense_vector
│   ├── transforms/                      # raw row → ES document
│   ├── embeddings/                      # Ollama batch embedding helper
│   └── pipeline.py                      # main entry point
├── docker/
│   └── docker-compose.yml
├── .env.example
└── requirements.txt
```

## Ecosystem

Part of the **search-lab** ecosystem — a hands-on companion for Medium articles on Elasticsearch.
