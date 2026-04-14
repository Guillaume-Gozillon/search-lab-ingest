# Article 01 — HuggingFace Ingestion

Ingestion pipeline that streams product metadata from HuggingFace datasets directly into Elasticsearch 9.x.

**Dataset:** McAuley-Lab/Amazon-Reviews-2023 (Home & Kitchen category)

## Run

From the repo root:

```bash
# Dry run (transform only, no indexing)
python article-01-huggingface-ingestion/pipeline.py --dry-run --limit 100

# Full ingestion
python article-01-huggingface-ingestion/pipeline.py

# Post-ingestion optimization
python article-01-huggingface-ingestion/operations/forcemerge.py
```

## Structure

```
article-01-huggingface-ingestion/
├── mappings/
│   └── products_home_kitchen_v1.json   # ES index mapping
├── transforms/
│   └── product.py                      # HF record → ES document
├── operations/
│   └── forcemerge.py                   # post-ingestion segment optimization
└── pipeline.py                         # main entry point
```
