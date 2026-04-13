# search-lab-ingest

Data engineering pipelines for ingesting product data into Elasticsearch 9.x.
Sibling of [search-lab](../search-lab) (Node.js / TypeScript — queries & aggregations).

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

Elasticsearch will be available at `https://localhost:9200`, Kibana at `http://localhost:5601`.

## Verify connection

```bash
python -c "import config; from es.client import build_es_client; build_es_client()"
```

## Run the pipeline

Dry run (transform only, no indexing):

```bash
python pipelines/hf_to_es.py --dry-run --limit 100
```

Full ingestion:

```bash
python pipelines/hf_to_es.py
```

## Project structure

```
search-lab-ingest/
├── pipelines/                  # one file per data source
│   └── hf_to_es.py             # HuggingFace → ES
├── transforms/                 # pure functions: raw record → ES document
│   └── product.py
├── mappings/                   # ES index mappings (JSON)
│   └── products_home_kitchen_v1.json
├── es/                         # ES client + index helpers
│   └── client.py
├── operations/                 # post-ingestion operations
│   └── forcemerge.py
├── notebooks/                  # Jupyter exploration
├── docker/
│   └── docker-compose.yml
├── config.py
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

## Ecosystem

Part of the **search-lab** ecosystem — a hands-on companion for Medium articles on Elasticsearch.
