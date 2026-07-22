# Architecture — vector ingestion pipeline

## 1. Overview

A single chain of Python generators, with no intermediate materialization: the 1.4M-row
CSV never sits in memory. Each stage pulls from the previous one on demand.

```mermaid
flowchart LR
    CSV[("amazon_products.csv<br/>~1.4M rows")]
    subgraph PY["pipeline.py — Python process"]
        direction LR
        READ["pandas.read_csv<br/>chunksize=10,000"]
        TRANSFORM["transform<br/>row dict → ES doc"]
        EMBED["embed_stream<br/>batch=128"]
        BULK["bulk_index<br/>chunk=2,000"]
        READ --> TRANSFORM --> EMBED --> BULK
    end
    OLLAMA["Ollama<br/>nomic-embed-text<br/>:11434"]
    ES[("Elasticsearch<br/>amazon_products_embeddings<br/>:9200")]

    CSV --> READ
    EMBED <-->|"POST /api/embed<br/>128 titles → 128 vectors, 768d"| OLLAMA
    BULK -->|"_bulk"| ES
```

## 2. The embedding stage in detail

`embeddings/ollama.py` — a generator that **buffers**, calls Ollama **once per batch**,
then zips the vectors back onto the documents.

```mermaid
flowchart TD
    IN["incoming doc"] --> BUF["buffer.append(doc)"]
    BUF --> FULL{"len(buffer) >= 128 ?"}
    FULL -->|no| IN
    FULL -->|yes| TEXTS["texts = [doc['title'] for doc in batch]"]
    TEXTS --> CALL["client.embed(model, input=texts)<br/>1 HTTP call for 128 titles"]
    CALL --> CHECK{"len(vectors) == len(batch) ?"}
    CHECK -->|no| ERR["RuntimeError — refuse to zip<br/>misaligned vectors onto docs"]
    CHECK -->|yes| ZIP["zip(batch, vectors)<br/>doc['embedding'] = vector"]
    ZIP --> OUT["yield enriched doc"]
    OUT --> IN
    IN -.->|"stream exhausted"| FLUSH["flush remaining buffer"]
    FLUSH --> TEXTS

    style CALL fill:#4a7ebb,color:#fff
    style ERR fill:#c0392b,color:#fff
```

**Why batching is the whole point**: `client.embed` takes a *list* of strings and returns
a list of vectors **in the same order**. That ordering guarantee is what makes the `zip`
valid. One call per document would mean one HTTP round-trip per document — batching 128
amortizes the network latency and gives the GPU a real workload.

The `len(vectors) != len(batch)` check is the guardrail: if the ordering/cardinality
assumption is ever violated, we raise instead of silently attaching the wrong vector to
the wrong product.

## 3. Configuration and infrastructure

```mermaid
flowchart TD
    ENV[".env / environment variables"] --> CFG["shared/config.py"]
    CFG -->|OLLAMA_URL| C1["Client(host=...)"]
    CFG -->|"OLLAMA_EMBED_MODEL<br/>nomic-embed-text"| C2["embed_stream(model=...)"]
    CFG -->|"OLLAMA_EMBED_DIMS<br/>768"| C3["not read by the mapping"]

    subgraph DOCKER["docker/docker-compose.yml"]
        OL["ollama service<br/>NVIDIA passthrough + healthcheck"]
        INIT["ollama-init service<br/>ollama pull nomic-embed-text"]
        OL -->|"condition: service_healthy"| INIT
    end

    MAP["mappings/amazon_products_embeddings_v1.json<br/>dims: 768 hardcoded"] --> C3

    style C3 fill:#e67e22,color:#fff
```

The mapping's `dims: 768` and `OLLAMA_EMBED_DIMS` are two independent sources of truth:
switching embedding model without updating the JSON fails indexing (`dynamic: strict`
plus a dimension mismatch).

## 4. Lifecycle of a full run

```mermaid
sequenceDiagram
    participant P as pipeline.py
    participant ES as Elasticsearch
    participant O as Ollama

    P->>ES: ensure_index — create if absent
    P->>ES: optimize_for_import<br/>refresh_interval=-1, replicas=0
    Note over P,ES: import mode: no refresh, no replication

    loop per batch of 128 docs
        P->>O: embed(128 titles)
        O-->>P: 128 vectors, 768d
    end
    Note over P,ES: interleaved with the stream, in chunks of 2,000
    P->>ES: _bulk

    P->>ES: restore_after_import<br/>refresh_interval=1s, replicas=1
    P->>ES: refresh + count
    P->>ES: forcemerge max_num_segments=1<br/>300s timeout
```

`--dry-run` short-circuits everything touching Elasticsearch: it transforms, embeds, and
logs the vector length plus the first 5 dimensions of the first document. That is the
validation to run before committing to 1.4M rows.

## 5. The resulting document

```mermaid
flowchart LR
    subgraph DOC["indexed document"]
        direction TB
        K["asin — keyword, used as _id"]
        T["title — text, english analyzer"]
        N["stars, reviews, price, listPrice"]
        E["embedding — dense_vector<br/>768 dims, cosine, index true"]
    end
    T -.->|"embedding source"| E
    T --> BM25["lexical BM25 search"]
    E --> KNN["vector kNN search"]
```

The title is used twice: indexed as `text` for BM25, and vectorized for kNN. Only the
title is embedded — `embed_stream` takes a configurable `text_field`, but the pipeline
uses the default.

## 6. Known limits

```mermaid
flowchart TD
    L1["Sequential embedding<br/>one Ollama call at a time"] --> I1["main bottleneck<br/>→ worker pool or Ollama replicas"]
    L2["No retry, no checkpoint"] --> I2["an Ollama hiccup kills the whole run<br/>→ no resume, restart from scratch"]
    L3["dims duplicated across config and mapping"] --> I3["silent divergence when switching model"]

    style L1 fill:#e67e22,color:#fff
    style L2 fill:#e67e22,color:#fff
    style L3 fill:#e67e22,color:#fff
```
