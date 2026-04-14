# Indexing 1.4 Million Amazon Products into Elasticsearch in Under 90 Seconds with Python and pandas

*A practical guide to building a robust and performant CSV ingestion pipeline*

---

When you start working seriously with Elasticsearch, the question of data ingestion comes up quickly. Tutorials usually show a few dozen documents. But in production — or even for realistic experiments — you're dealing with hundreds of thousands, sometimes millions of records.

In this article, I'll show you how I indexed **1.4 million Amazon products** into Elasticsearch 9.x in **under 90 seconds**, using Python, pandas, and a few well-chosen optimizations. The code is clean, modular, and reusable for any CSV dataset.

---

## Context: why this project?

This pipeline is part of an ecosystem I call **search-lab** — a set of companion repositories for exploring Elasticsearch's capabilities through Medium articles. The idea: work with realistic data, not toy examples.

The chosen dataset is the **Amazon Products Dataset 2023** available on Kaggle: 1.4 million products, around 300 MB as a CSV. It contains exactly what you want to test a search engine — text titles, prices, ratings, categories.

Why Elasticsearch over a SQL database? Because full-text search, relevance scoring, and analytical aggregations are cases where ES excels natively. A `SELECT WHERE title LIKE '%coffee maker%'` will never give you the same quality of results as an ES search with the `english` analyzer.

---

## 1. Docker setup: simple and reproducible

First decision: run ES locally without friction. Docker is the obvious answer. Here's the `docker-compose.yml` I use:

```yaml
services:
  elasticsearch:
    image: elasticsearch:9.0.1
    environment:
      - discovery.type=single-node
      - xpack.security.enabled=false
      - xpack.security.http.ssl.enabled=false
      - ES_JAVA_OPTS=-Xms2g -Xmx2g
    ports:
      - "9200:9200"
    mem_limit: 4g
    healthcheck:
      test: curl -sf http://localhost:9200/_cluster/health | grep -q '"status":"green"\|"status":"yellow"'
      interval: 10s
      retries: 10

  kibana:
    image: kibana:9.0.1
    environment:
      - ELASTICSEARCH_HOSTS=http://elasticsearch:9200
    ports:
      - "5601:5601"
    depends_on:
      elasticsearch:
        condition: service_healthy
    mem_limit: 1g
```

A few deliberate choices:

- **`xpack.security.enabled=false`**: In local development, security adds complexity without value. Enable it in production, not for experiments.
- **`ES_JAVA_OPTS=-Xms2g -Xmx2g`**: We fix the JVM heap at 2 GB. The ES rule of thumb: never exceed 50% of available RAM for the heap, and stay under 32 GB.
- **`healthcheck` with condition**: Kibana only starts once ES is truly ready. This avoids boot race conditions.

```bash
docker compose -f docker/docker-compose.yml up -d
```

---

## 2. The mapping: defining the data contract

Before indexing anything, you need to define the **mapping** — the index schema. It's one of the most important decisions, and a hard one to change after the fact.

```json
{
  "settings": {
    "number_of_shards": 2,
    "number_of_replicas": 0
  },
  "mappings": {
    "dynamic": "strict",
    "properties": {
      "asin":        { "type": "keyword" },
      "title":       { "type": "text", "analyzer": "english" },
      "imgUrl":      { "type": "keyword", "index": false },
      "productURL":  { "type": "keyword", "index": false },
      "stars":       { "type": "float" },
      "price":       { "type": "float" },
      "isBestSeller":{ "type": "boolean" },
      "ingested_at": { "type": "date" }
    }
  }
}
```

Let's walk through each choice:

**`dynamic: strict`** — Any field not declared in the mapping causes an indexing error. In `dynamic: true` mode (the default), ES infers types automatically, which can lead to surprises — a numeric field inferred as `long` when you wanted `float`. With `strict`, you keep full control.

**`keyword` vs `text`** — The `title` field is `text` with the `english` analyzer: it will be tokenized, stop words removed, words reduced to their root (*stemming*). Perfect for full-text search. `asin`, on the other hand, is `keyword`: we want exact matches, no tokenization.

**`index: false` on URLs** — `imgUrl` and `productURL` will never be used for filtering or searching. We store them to retrieve in results, but we explicitly tell ES not to index them. This saves disk space and memory.

---

## 3. Data transformation: handling the reality of CSV files

A 300 MB CSV is rarely clean. Pandas loads empty cells as `NaN` (a Python float), numbers can be stored as strings, and some rows are incomplete. The transform function is the single place where all of this gets handled.

```python
import math

def _clean(value):
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value

def transform(row: dict) -> dict | None:
    asin = _clean(row.get("asin"))
    if not asin:
        return None  # invalid row, skip it

    return {
        "_id": str(asin),
        "asin": str(asin),
        "title": _clean(row.get("title")),
        "stars": _parse_float(row.get("stars")),
        "price": _parse_float(row.get("price")),
        "isBestSeller": bool(_clean(row.get("isBestSeller")) or False),
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }
```

Key points:

- **`_clean` handles pandas NaN**: `math.isnan()` only works on floats, hence the `isinstance` guard.
- **`transform` returns `None`** for rows without an ASIN — the pipeline skips them silently.
- **`_id` = ASIN**: We use the business identifier as the ES `_id`. This makes re-ingestions idempotent — re-indexing the same document overwrites the previous one instead of creating a duplicate.
- **Pure functions with no side effects**: `transform` touches nothing external. This makes unit testing and debugging straightforward.

---

## 4. The pipeline: streaming without blowing up memory

300 MB of CSV is manageable in RAM. But 3 GB? 30 GB? The fundamental principle: **never load the entire file into memory**.

Pandas supports chunk reading with `read_csv(..., chunksize=10_000)`. Combined with a Python generator, you get a document stream that never loads more than 10,000 rows at a time:

```python
def document_stream(csv_path, limit=None):
    for chunk in pd.read_csv(csv_path, chunksize=10_000, low_memory=False):
        for row in chunk.to_dict("records"):
            if limit is not None and emitted >= limit:
                return
            doc = transform(row)
            if doc is None:
                continue
            doc["_index"] = config.ES_INDEX
            yield doc
```

The `--limit` flag is invaluable in development: testing with 1,000 documents before running the full ingestion saves a lot of painful surprises.

---

## 5. Bulk optimizations: where the real performance gains happen

This is the most interesting part. Without optimization, ES indexes each document in real time: it writes the segment, the refresh makes the doc visible, replicas sync. Multiplied by 1.4 million documents, this is catastrophically slow.

Before starting the ingestion:

```python
def optimize_for_import(client, index):
    client.indices.put_settings(
        index=index,
        settings={"index": {"refresh_interval": "-1", "number_of_replicas": 0}},
    )
```

**`refresh_interval: -1`** — By default, ES refreshes the index every second to make new documents visible to searches. This is expensive: it forces the creation of new Lucene segments. By completely disabling refresh during ingestion, ES can write to memory and flush to disk far more efficiently.

**`number_of_replicas: 0`** — Each replica receives a copy of every indexed document. During a bulk import, replicas are extra work with no immediate benefit. We restore them to 1 afterwards.

After ingestion, we restore normal settings:

```python
def restore_after_import(client, index):
    client.indices.put_settings(
        index=index,
        settings={"index": {"refresh_interval": "1s", "number_of_replicas": 1}},
    )
    client.indices.refresh(index=index)
```

---

## 6. `streaming_bulk`: why not classic bulk?

The `elasticsearch-py` library offers two approaches for mass indexing:

- **`bulk`**: takes a complete list of documents, serializes it, sends it in one request.
- **`streaming_bulk`**: consumes an iterator, sends batches on the fly.

With 1.4 million documents, classic `bulk` would mean building the entire list in memory before sending anything. `streaming_bulk` consumes the generator chunk by chunk (`chunk_size=2000`): memory usage stays constant regardless of dataset size.

```python
for ok, result in streaming_bulk(
    client, actions,
    chunk_size=2_000,
    raise_on_error=False
):
    if not ok:
        errors.append(result)
```

`raise_on_error=False` lets us collect errors without interrupting the pipeline. We log them at the end rather than stopping everything for one malformed document.

---

## 7. Force merge: the step everyone forgets

After a bulk ingestion, the underlying Lucene index can contain hundreds of segments — each flush during ingestion creates new ones. Too many segments = slower searches.

```python
client.indices.forcemerge(index=index, max_num_segments=1, request_timeout=300)
```

`max_num_segments=1` merges all segments into one per shard. This is the optimum for a read-heavy index: searches become maximally efficient. **Warning**: this is an I/O-intensive operation, to be done once after ingestion — never continuously.

---

## 8. Results

```
Bulk complete — 1,426,337 indexed, 0 errors in 84.9s
Total: 1,426,337 documents
Segments before merge: 312 → after merge: 2
```

**~16,800 documents/second**, zero errors. A quick verification via aggregations: average price **$43.37**, average rating **4.0/5**, **8,520 best sellers**. And full-text search works — `"coffee makers"` also returns "coffee maker" and "making coffee" thanks to the `english` analyzer's stemming.

---

## Conclusion: key takeaways

1. **`dynamic: strict` first** — Defining your mapping explicitly avoids surprises in production.
2. **`keyword` vs `text` is fundamental** — Getting this wrong means broken searches or wasted disk space.
3. **The two optimizations that change everything** — `refresh_interval: -1` and `number_of_replicas: 0` during ingestion. Without them, the same pipeline would run 5 to 10 times slower.
4. **`streaming_bulk` + generator** — The combination that lets you ingest datasets of any size with a constant memory footprint.
5. **Force merge is not optional** — If your index is primarily read-heavy, force merge is the final optimization step. Don't skip it.
6. **Test with `--limit`** — Validating the pipeline on 1,000 documents before running on 1.4 million saves time and frustration.

*The full code is available on GitHub. The next article in the series will cover advanced full-text queries, aggregations, and relevance scoring — using this same dataset as a playground.*
