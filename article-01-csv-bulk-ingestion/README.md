# Article 01 — CSV Bulk Ingestion

Ingestion pipeline that reads the Kaggle **Amazon Products Dataset 2023** from a local CSV and bulk-indexes it into Elasticsearch 9.x using pandas and `streaming_bulk`.

**Dataset:** [Amazon Products Dataset 2023 (1.4M products)](https://www.kaggle.com/datasets/asaniczka/amazon-products-dataset-2023-1-4m-products) — download `amazon_products.csv` and place it in `article-01-csv-bulk-ingestion/data/`.

## Run

From the repo root:

```bash
# Dry run (transform only, no indexing)
python article-01-csv-bulk-ingestion/pipeline.py --dry-run --limit 10000

# Full ingestion (default CSV path: article-01-csv-bulk-ingestion/data/amazon_products.csv)
python article-01-csv-bulk-ingestion/pipeline.py

# Custom CSV path
python article-01-csv-bulk-ingestion/pipeline.py --csv /path/to/amazon_products.csv
```

## Structure

```
article-01-csv-bulk-ingestion/
├── data/                           # gitignored — put amazon_products.csv here
├── mappings/
│   └── amazon_products_v1.json     # ES index mapping
├── transforms/
│   └── product.py                  # CSV row → ES document
└── pipeline.py                     # main entry point
```

## What the pipeline does

1. Creates the index from `mappings/amazon_products_v1.json` (skips if exists)
2. Optimizes index settings for bulk import (`refresh_interval: -1`, `replicas: 0`)
3. Reads the CSV in chunks of 10 000 rows with pandas
4. Transforms each row, skipping rows with no ASIN
5. Bulk-indexes with `chunk_size=2000` via `streaming_bulk`
6. Restores settings (`refresh_interval: 1s`, `replicas: 1`) and refreshes
7. Force merges to 1 segment per shard
