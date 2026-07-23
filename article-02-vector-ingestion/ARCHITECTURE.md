# Architecture — vector ingestion pipeline

## 1. Overview

A chain of Python generators with no intermediate materialization: the 1.4M-row CSV never
sits in memory. Each stage pulls from the previous one on demand — except the embedding
stage, which runs a small pool underneath so the GPU and Elasticsearch stop waiting on each
other.

```mermaid
flowchart LR
    CSV[("amazon_products.csv<br/>~1.4M rows")]
    subgraph PY["pipeline.py — Python process"]
        direction LR
        READ["pandas.read_csv<br/>chunksize=10,000"]
        TRANSFORM["transform<br/>row dict → ES doc"]
        EMBED["embed_stream<br/>batch=128, workers=4"]
        TAP["ProbeReservoir.tap<br/>uniform sample of vectors"]
        BULK["bulk_index<br/>chunk=2,000"]
        READ --> TRANSFORM --> EMBED --> TAP --> BULK
    end
    BACKEND["EMBED_BACKEND<br/>ollama :11434<br/>or tei :8080"]
    ES[("Elasticsearch<br/>amazon_products_embeddings<br/>:9200")]

    CSV --> READ
    EMBED <-->|"N titles → N vectors, 768d"| BACKEND
    BULK -->|"_bulk"| ES
```

## 2. The embedding stage in detail

`embeddings/stream.py` — the main thread reads documents and hands batches of *text* to a
pool; it never lets a worker touch the generator or the documents. Two invariants make that
safe to drop into an ingestion pipeline.

```mermaid
flowchart TD
    IN["incoming doc"] --> BUF["batch.append(doc)"]
    BUF --> FULL{"len(batch) >= batch_size ?"}
    FULL -->|no| IN
    FULL -->|yes| SUB["executor.submit(backend.embed, texts)<br/>push (batch, future) onto the deque"]
    SUB --> WIN{"len(inflight) >= workers * 2 ?"}
    WIN -->|no| IN
    WIN -->|yes| POP["popleft() — the OLDEST batch<br/>future.result() blocks"]
    POP --> CHECK{"len(vectors) == len(batch) ?"}
    CHECK -->|no| ERR["RuntimeError — refuse to zip<br/>misaligned vectors onto docs"]
    CHECK -->|yes| ZIP["zip(batch, vectors)<br/>doc['embedding'] = vector"]
    ZIP --> OUT["yield enriched docs"]
    OUT --> IN
    IN -.->|"stream exhausted"| DRAIN["submit the ragged batch,<br/>drain the deque in order"]

    style SUB fill:#4a7ebb,color:#fff
    style POP fill:#4a7ebb,color:#fff
    style ERR fill:#c0392b,color:#fff
```

**Order is preserved.** Batches complete out of order — that is the point of the pool — but
they are *consumed* in submission order, oldest first. A document leaves the stage at the
same position it entered.

**Memory is bounded.** `popleft()` only happens once the window is full, so the upstream
generator stalls instead of reading ahead: at most `workers * 2` batches are in flight plus
the one being assembled, whatever the worker count.

`future.result()` re-raises whatever the worker raised, in the main thread, at that point in
the stream — a backend failure stops the run rather than quietly leaving a hole in the index.
The `len(vectors) != len(batch)` check is the same guardrail as before: raise instead of
attaching the wrong vector to the wrong product.

## 3. Backends

`embeddings/backends/` — both expose `embed(texts) -> vectors`, chosen by `EMBED_BACKEND`.

```mermaid
flowchart TD
    CFG["EMBED_BACKEND"] --> GB["get_backend()"]
    GB -->|ollama| OB["OllamaBackend<br/>POST /api/embed"]
    GB -->|tei| TB["TeiBackend<br/>POST /embed"]
    OB --> OLL["ollama :11434<br/>OLLAMA_NUM_PARALLEL slots"]
    TB --> TEI["text-embeddings-inference :8080<br/>dynamic batching"]
    CMP["tools/compare_embeddings.py"] -.->|"cosine per title"| OB
    CMP -.->|"cosine per title"| TB

    style CMP fill:#4a7ebb,color:#fff
```

Ollama is a token-by-token generation engine pressed into embedding service: it pads
28-token titles into a large context, and it serves `OLLAMA_NUM_PARALLEL` requests at a
time — at 1 it serialises everything the pool pushes at it. TEI is built for this one job.

Same model on both sides, so the vectors *should* be interchangeable. `ollama show` cannot
confirm it — it reports 137M parameters for both nomic v1 and v1.5 — which is what
`compare_embeddings.py` is for.

## 4. Configuration and infrastructure

```mermaid
flowchart TD
    ENV[".env / environment variables"] --> CFG["shared/config.py"]
    CFG -->|EMBED_BACKEND| C0["get_backend()"]
    CFG -->|"OLLAMA_URL / TEI_URL"| C1["backend endpoint"]
    CFG -->|"EMBED_WORKERS<br/>4"| C2["embed_stream(workers=...)"]
    CFG -->|"EMBED_DOC_PREFIX<br/>empty"| C4["embed_stream(prefix=...)"]
    CFG -->|"EMBED_QUERY_PREFIX<br/>empty"| C5["no consumer in this repo<br/>— the search side must apply it"]
    CFG -->|"OLLAMA_EMBED_DIMS<br/>768"| C3["not read by the mapping"]

    subgraph DOCKER["docker/"]
        OL["docker-compose.yml<br/>es + kibana + ollama<br/>+ text-embeddings (profile tei)<br/>no GPU configuration"]
        GPUF["docker-compose.gpu.yml<br/>NVIDIA passthrough<br/>ollama + text-embeddings"]
        INIT["ollama-init service<br/>ollama pull nomic-embed-text"]
        GPUF -.->|"layered on by the Makefile<br/>when a usable GPU is detected"| OL
        OL -->|"condition: service_healthy"| INIT
    end

    MAP["mappings/amazon_products_embeddings_v1.json<br/>dims: 768 hardcoded"] --> C3

    style C3 fill:#e67e22,color:#fff
    style C5 fill:#e67e22,color:#fff
    style GPUF fill:#e67e22,color:#fff
```

The mapping's `dims: 768` and `OLLAMA_EMBED_DIMS` are two independent sources of truth:
switching embedding model without updating the JSON fails indexing (`dynamic: strict` plus
a dimension mismatch).

The GPU passthrough is a **separate compose file**, not part of the base stack. Whether it
gets layered on is decided at `make start` time, and nothing downstream reports the outcome:
a CPU-only run indexes exactly the same documents, only far slower. That makes it a silent
failure mode worth checking explicitly — see
[GPU acceleration](../README.md#gpu-acceleration).

## 5. Lifecycle of a full run

```mermaid
sequenceDiagram
    participant P as pipeline.py
    participant ES as Elasticsearch
    participant B as embedding backend

    P->>ES: ensure_index — create if absent
    P->>ES: optimize_for_import<br/>refresh_interval=-1, replicas=0
    Note over P,ES: import mode: no refresh, no replication

    loop EMBED_WORKERS batches in flight
        P->>B: embed(batch_size titles)
        B-->>P: batch_size vectors, 768d
    end
    Note over P,ES: interleaved with the stream, in chunks of 2,000
    P->>ES: _bulk
    Note over P: ProbeReservoir keeps VERIFY_PROBES vectors on the way past

    P->>ES: restore_after_import<br/>refresh_interval=1s, replicas=ES_REPLICAS
    P->>ES: refresh + count
    P->>ES: verify_vector_index — exact scan vs kNN
    alt recall below the floor
        P->>P: SystemExit — the alias does NOT move
    else
        P->>ES: update_alias → products_embeddings
    end
```

No force merge: `VECTOR_MAX_SEGMENTS` is `None`, for the reasons in
[the README](README.md#recall--is-the-index-actually-searchable).

`--dry-run` short-circuits everything touching Elasticsearch: it transforms, embeds, and
logs the vector length plus the first 5 dimensions of the first document. That is the
validation to run before committing to 1.4M rows.

## 6. The resulting document

```mermaid
flowchart LR
    subgraph DOC["indexed document"]
        direction TB
        K["asin — keyword, used as _id"]
        T["title — text, english analyzer"]
        N["stars, reviews, price, listPrice"]
        E["embedding — dense_vector<br/>768 dims, cosine, index true<br/>EXCLUDED from _source"]
    end
    T -.->|"embedding source"| E
    T --> BM25["lexical BM25 search"]
    E --> KNN["vector kNN search"]

    style E fill:#e67e22,color:#fff
```

The title is used twice: indexed as `text` for BM25, and vectorized for kNN. Only the title
is embedded — `embed_stream` takes a configurable `text_field`, but the pipeline uses the
default.

The vector is searchable but **not retrievable**: `_source` excludes it, which divides
storage by ~3. The consequences are real and listed in
[the README](README.md#the-vector-is-not-in-_source).

## 7. Known limits

```mermaid
flowchart TD
    L1["Ollama serialises at OLLAMA_NUM_PARALLEL"] --> I1["client concurrency only helps<br/>up to the server's slot count"]
    L2["No retry, no checkpoint"] --> I2["a backend hiccup kills the whole run<br/>→ no resume, restart from scratch"]
    L3["dims duplicated across config and mapping"] --> I3["silent divergence when switching model"]
    L4["GPU passthrough is opt-in at startup"] --> I4["a CPU-only run is ~6× slower<br/>and reports nothing"]
    L5["EMBED_QUERY_PREFIX has no consumer here"] --> I5["enabling prefixes only does half the job<br/>the search side must match"]
    L6["--recreate re-embeds all 1.4M titles"] --> I6["iterating on the mapping costs a full<br/>embedding run → an embedding cache would fix it"]

    style L1 fill:#e67e22,color:#fff
    style L2 fill:#e67e22,color:#fff
    style L3 fill:#e67e22,color:#fff
    style L4 fill:#e67e22,color:#fff
    style L5 fill:#c0392b,color:#fff
    style L6 fill:#e67e22,color:#fff
```

**On L1 —** the single-thread stall that used to cap the pipeline at ~500 docs/s is gone:
`embed_stream` now keeps `EMBED_WORKERS` requests in flight, so CSV parsing and the bulk call
overlap with the GPU instead of blocking it. What remains is a server-side limit — Ollama
answers `OLLAMA_NUM_PARALLEL` requests at a time and queues the rest, so raising
`EMBED_WORKERS` past that buys nothing. TEI has no such ceiling.

**On L5 —** this is the one that fails silently and expensively. Setting `EMBED_DOC_PREFIX`
without applying `EMBED_QUERY_PREFIX` at query time produces an index that still answers,
still ranks, and is measurably worse: cos(prefixed, bare) = 0.684 on this dataset. Nothing
raises.
