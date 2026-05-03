# Adding Semantic Search to 1.4 Million Products: Vector Ingestion in Elasticsearch with Ollama

*From keyword search to embeddings — building a vector ingestion pipeline that runs entirely on your machine*

---

In the [previous article](../article-01-csv-bulk-ingestion/ARTICLE.md), I indexed 1.4 million Amazon products into Elasticsearch in under 90 seconds and showed how `streaming_bulk`, refresh tuning, and force merge work together to make that possible. The result was a fast, classic full-text index — BM25 scoring, English analyzer, the works.

But classic full-text search has a fundamental limitation: it matches **words**, not **meaning**. A search for `"something to keep my coffee hot at the office"` won't find products titled `"Stainless Steel Insulated Travel Mug"` — there isn't a single word in common. To bridge that gap, we need **vector search**.

In this article, I'll extend the pipeline to add a 768-dimensional embedding to every product, generated at ingestion time by a local Ollama instance running `nomic-embed-text`. Same dataset, same Elasticsearch instance, but every document will now carry a `dense_vector` field — the foundation for semantic and hybrid search downstream.

We'll cover:

- What an embedding model actually is, and how it differs from an LLM
- Why we use a tokenizer and what it really does
- How to declare a `dense_vector` mapping in ES 9.x and what HNSW does under the hood
- Why batching the embedding step is non-negotiable
- How `int8` quantization keeps memory usage sane
- A concrete kNN query you can run in Kibana to test the result

The full code is in `article-02-vector-ingestion/`.

---

## 1. What is an embedding, exactly?

An **embedding** is a function that converts a piece of text into a fixed-size vector of floating-point numbers. The vector has no inherent meaning on its own — what matters is the **geometry** of the vector space. Texts with similar meanings end up close together; unrelated texts end up far apart.

```
"running shoes for men"     ──▶ [ 0.034, -0.218,  0.097, ...]   (768 numbers)
"athletic sneakers"         ──▶ [ 0.041, -0.205,  0.103, ...]   ← very close
"stainless steel blender"   ──▶ [-0.156,  0.421, -0.038, ...]   ← far away
```

The first two vectors differ by tiny amounts in most dimensions because the model has learned that "running shoes" and "athletic sneakers" describe the same concept. The third points in an entirely different direction in the 768-dimensional space.

Once every document in your corpus has been embedded, finding "products similar to X" becomes a **geometric** problem: find the vectors closest to X's vector. That's what kNN search does — but we'll get to that.

### Embedding models versus LLMs

It's tempting to lump everything modern into "AI", but embedding models and LLMs do very different jobs:

| | LLM (GPT, Claude) | Embedding model (nomic) |
|---|---|---|
| **Input** | text | text |
| **Output** | text, token by token | one vector of N floats |
| **Trained for** | predicting the next token | bringing similar texts close in vector space |
| **Typical size** | 100B+ parameters | 100M–1B parameters |
| **Use case** | generation, reasoning, dialogue | semantic similarity, retrieval |

Both architectures are **Transformers** — the same family of neural networks. But where an LLM **generates**, an embedding model **represents**. They're complementary: in a RAG system, the embedding model finds *what* to talk about (the relevant documents), and the LLM decides *how* to talk about it (the natural-language answer).

For our pipeline, we only need the embedding step. The query side and the LLM-driven response generation belong to a separate repository.

### How embedding models are trained: contrastive learning

Nobody hand-codes the rule "synonyms should have similar vectors". The model learns it from data, through **contrastive learning**:

```
Positive pairs (semantically related):
  ("how to fix a leaky faucet", "plumbing tutorial for dripping tap")
  ("best Italian restaurants NYC", "top pizza places in Manhattan")
  ("python list comprehension", "list comprehensions tutorial")

Negative pairs (unrelated):
  ("python list comprehension", "best running shoes for men")
  ...
```

During training, the model is shown millions of these pairs and adjusts its weights to **pull positives closer** and **push negatives apart** in the vector space. After hundreds of millions of pairs, the space organizes itself: regions emerge for cooking, electronics, sports, etc., without anyone explicitly defining them.

The crucial property that follows: once trained, the model is **deterministic**. The same text always produces the same vector. No temperature, no sampling, no randomness. That's what makes the operation cacheable, batchable, and predictable — exactly the properties you want in an ingestion pipeline.

---

## 2. The tokenizer: how text becomes numbers

Before the model can process anything, the text has to be converted into integers. That's the job of the **tokenizer**.

A naive approach — one word = one ID — sounds clean but breaks down fast. Vocabularies explode (English alone has 1M+ words), unknown words become `<UNK>` tokens that destroy information, and morphological relatives like "run", "running", "runner" end up as completely unrelated IDs.

Modern models use **subword tokenization**: a token is a frequent chunk of text, smaller than a word but larger than a character. The tokenizer is built by analyzing a huge corpus and iteratively merging the most frequent character pairs. Common words become a single token; rare words break into pieces.

For nomic-embed-text (which uses WordPiece, with a vocabulary of about 30,000 tokens):

```
"wireless"          → ["wireless"]                     (1 token)
"headphones"        → ["head", "##phones"]             (2 tokens, ## marks a word continuation)
"unbelievable"      → ["un", "##believable"]           (2 tokens)
"TikTokify"         → ["tik", "##tok", "##ify"]        (3 tokens for an invented word)
"antidisestablishmentarianism" → 6 tokens
```

This solves several problems at once:

- **No out-of-vocabulary problem** — any text breaks down to characters at worst
- **Generalization across morphology** — "run", "runs", "running", "runner" all share the "run" subword
- **Multilingual support is feasible** — a shared vocabulary can cover several languages

For our pipeline, this matters in two practical ways. First, the model's "8192 token context" isn't 8192 words — English averages roughly 1.3 tokens per word, so it's about 6,000 words of capacity. More than enough for product titles. Second, the cost of embedding scales with token count (Transformer attention is O(n²) on sequence length), which is why short titles embed quickly and longer descriptions would be measurably slower.

### A quick aside on terminology

The word "embedding" is overloaded:

- The **embedding model** is the whole neural network
- The **embedding layer** is the first layer inside that network — a lookup table mapping each token ID to an initial vector
- The **embedding** (the noun, what we store in ES) is the final pooled output vector

Three different things, one name. It's a real source of confusion when reading papers — worth knowing.

---

## 3. Choosing the model: why `nomic-embed-text`

There are dozens of embedding models. Here's how the field looks today:

| Model | License | Dims | Context | MTEB | Notes |
|---|---|---|---|---|---|
| **nomic-embed-text v1.5** | Apache 2.0 | 768 | 8192 | ~62 | Open weights, open training data |
| OpenAI text-embedding-3-small | Closed API | 1536 | 8191 | ~62 | Per-token cost, not local |
| OpenAI text-embedding-3-large | Closed API | 3072 | 8191 | ~64 | Same, larger vectors |
| BGE-large-en-v1.5 | MIT | 1024 | 512 | ~64 | Higher quality, but 512-token limit |
| all-MiniLM-L6-v2 | Apache 2.0 | 384 | 256 | ~56 | Lightweight, older standard |
| Cohere embed-v3 | Closed API | 1024 | 512 | ~64 | Good quality, API cost |

For an educational, fully local, fully reproducible setup, nomic checks every box that matters here:

1. **Open weights and open data.** Apache 2.0 license, training data published, training code public. There's no black box to handwave around.
2. **Available via `ollama pull`.** No HuggingFace dance, no PyTorch install, no tokenizer setup. One command and the model is on disk.
3. **Trained for asymmetric retrieval** — short query, longer document. Exactly our case: a few-word user query against product titles.
4. **768 dimensions is the sweet spot.** Smaller (384) costs noticeable recall; larger (1536+) doubles or quadruples your storage and HNSW memory for marginal quality gains.
5. **8192-token context.** Overkill for titles, but means the same model can later handle `title + description` without migration.

OpenAI's models are slightly better in raw MTEB scores, but they impose a per-call cost (real money on 1.4M docs), require an API key, and send your data to a third party. Cohere and Voyage have similar trade-offs. BGE and E5 are excellent open-weights alternatives but require the full HuggingFace stack to run, which adds friction.

For this article, the decision was easy.

---

## 4. The mapping: declaring the `dense_vector` field

The mapping for article-02 is the article-01 mapping plus one new field:

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
      "stars":       { "type": "float" },
      "price":       { "type": "float" },
      "category_id": { "type": "keyword" },
      "isBestSeller":{ "type": "boolean" },
      "ingested_at": { "type": "date" },
      "embedding":   {
        "type": "dense_vector",
        "dims": 768,
        "index": true,
        "similarity": "cosine"
      }
    }
  }
}
```

Three things matter on that `embedding` field:

**`dims: 768`** must match the model's output dimension exactly. Nomic outputs 768 floats; if you set 512 or 1024, indexing fails. This is one of the rare hard constraints in ES mappings.

**`index: true`** tells ES to build an HNSW graph on this field — without it, the vectors are stored but cannot be searched with kNN. This isn't free: HNSW construction is the slowest part of the indexing process and is what makes vector ingestion noticeably heavier than article-01.

**`similarity: cosine`** chooses the metric used to compare vectors. Three options exist (`cosine`, `dot_product`, `l2_norm`). For text embeddings produced by modern models, cosine is almost always right — it measures the **angle** between vectors, which is what captures semantic similarity. Use `dot_product` only if your vectors are already L2-normalized and you want a marginal speedup.

In ES 9.x, when you set `index: true`, `dense_vector` defaults to `int8_hnsw` — which we'll cover in a moment.

---

## 5. The Docker setup: adding Ollama as a service

To keep the project self-contained and reproducible, the Ollama runtime ships in the same `docker-compose.yml`:

```yaml
  ollama:
    image: ollama/ollama:latest
    container_name: search-lab-ollama
    ports:
      - "11434:11434"
    volumes:
      - ollamadata:/root/.ollama
    healthcheck:
      test: ["CMD-SHELL", "ollama list >/dev/null 2>&1 || exit 1"]
      interval: 10s
      retries: 10
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]

  ollama-init:
    image: ollama/ollama:latest
    depends_on:
      ollama:
        condition: service_healthy
    environment:
      - OLLAMA_HOST=ollama:11434
    entrypoint: ["sh", "-c", "ollama pull nomic-embed-text"]
    restart: "no"

volumes:
  esdata:
  ollamadata:
```

A few points worth highlighting:

**The `deploy.resources.reservations.devices` block** is what enables GPU passthrough on Linux with the NVIDIA Container Toolkit. Without it, Ollama runs on CPU — which works but is roughly an order of magnitude slower on this workload.

**The `ollama-init` service** runs once at startup, pulls `nomic-embed-text` (~270 MB), and exits. It depends on Ollama being healthy. The `restart: "no"` is important — we don't want it looping forever.

**The `ollamadata` named volume** persists the model across container restarts. The pull runs once, not every time you `docker compose up`.

---

## 6. The pipeline: where to plug in the embedding step

The article-01 pipeline was a clean linear stream:

```
CSV ──▶ transform ──▶ streaming_bulk ──▶ ES
```

Article-02 inserts a single new stage:

```
CSV ──▶ transform ──▶ embed (batched) ──▶ streaming_bulk ──▶ ES
```

Everything else stays identical: same chunked CSV reader, same `optimize_for_import` / `restore_after_import`, same force merge at the end. The new stage is a Python generator that buffers documents until it has 128 of them, sends all 128 titles to Ollama in a single API call, attaches each returned vector back onto its document, and yields them downstream:

```python
def embed_stream(docs, client, model, batch_size, text_field="title"):
    buffer = []

    def flush(batch):
        if not batch:
            return
        texts = [doc[text_field] for doc in batch]
        response = client.embed(model=model, input=texts)
        vectors = response["embeddings"]
        for doc, vector in zip(batch, vectors):
            doc["embedding"] = vector
            yield doc

    for doc in docs:
        buffer.append(doc)
        if len(buffer) >= batch_size:
            yield from flush(buffer)
            buffer = []

    yield from flush(buffer)
```

The function is small, but two design choices matter.

**It's a generator, not a list.** The whole pipeline is built on the generator pattern: at no point do we hold more than a few thousand documents in memory, regardless of dataset size. If `embed_stream` returned a list, the whole thing would collapse into a giant in-memory blob.

**It batches before calling Ollama.** This is the single most important optimization in the entire article — if you skip it, the pipeline simply doesn't finish.

---

## 7. Why batching is the difference between minutes and hours

A single Ollama embedding call takes roughly 5–20 ms, dominated by network round-trip, tokenization, and a forward pass through the model. On a corpus of 1.4M documents, a naive one-call-per-document loop is:

```
1,400,000 docs × 10 ms = 14,000 seconds ≈ 4 hours
```

Batched at 128 documents per call:

```
1,400,000 / 128 = 10,937 calls
10,937 × ~50 ms (a batch is slower than a single, but not 128× slower)
≈ 545 seconds ≈ 9 minutes
```

The reason batching works is purely about how GPUs (and modern CPUs) handle matrix operations. A Transformer forward pass on 128 inputs uses the **same** GPU kernels as a forward pass on 1 input — it just makes the matrices wider. The bottleneck shifts from per-call overhead (Python, HTTP, tokenization startup) to actual compute, which the GPU is built for.

Ollama exposes this via the `input` parameter on its `/api/embed` endpoint — pass an array, get back an array of vectors in the same order. The Python client wraps it as `client.embed(model=..., input=[t1, t2, ...])`.

The batch size is a tunable. On the test rig (RTX 5060 Ti, 16 GB VRAM), 128 was a comfortable starting point. Higher (256, 512) speeds things up further on this GPU but starts hitting diminishing returns and consumes more VRAM. The pipeline exposes `--embed-batch` so it can be tuned without touching the code.

---

## 8. HNSW and int8 quantization: what ES does under the hood

Once embeddings are in ES, kNN search relies on two structures the user never sees but can't ignore.

### HNSW: navigable small-world graphs

A naive nearest-neighbor search over 1.4M vectors means computing 1.4M cosine similarities per query — about a billion floating-point operations. Doable in seconds, not in milliseconds.

**HNSW** (Hierarchical Navigable Small World) is the algorithm ES uses to make this fast. It's a multi-layer graph built at indexing time:

```
Layer 2  (sparse, long-range)        •─────────────•
                                     │             │
Layer 1  (medium density)       •──•─•─•──•──•─•──•
                                   │           │
Layer 0  (every node)         •─•─•─•─•─•─•─•─•─•─•─•
```

Every vector is a node. Each node has a few neighbors at the dense bottom layer, fewer (but longer-range) at higher layers. A search starts at the top, hops to the closest neighbor, drops a layer, and repeats — converging on the query's neighborhood logarithmically.

In practice this means a kNN query examines ~20 vectors instead of 1.4M, while still finding 95%+ of the true top-k. The trade-off is memory and indexing time: the graph is built incrementally as documents are indexed, and it lives in memory for fast traversal.

Two parameters control the trade-off:

- `ef_construction` (indexing): how many candidates to explore when building the graph. Higher = better graph quality, slower indexing.
- `num_candidates` (query time): how many candidates to explore during search. Higher = better recall, slower query.

ES's defaults are sensible for most use cases. Tuning these is a separate exercise that belongs in the query-side article.

### int8 quantization: 4× less memory for negligible recall loss

A 768-dimensional float32 vector is 3 KB. Across 1.4M documents, that's 4.3 GB of raw vectors — before counting the HNSW graph itself.

ES 9.x defaults `dense_vector` (when `index: true`) to **`int8_hnsw`** — the same HNSW graph, but with each float32 quantized to a single int8 byte. The dequantization happens on the fly during search.

```
float32 storage: 1,400,000 × 768 × 4 bytes ≈ 4.3 GB
int8 storage:    1,400,000 × 768 × 1 byte  ≈ 1.1 GB
```

The cost: roughly 1–2% recall drop in benchmarks. The benefit: the entire vector index fits comfortably in RAM on a developer machine, and queries run faster because there's less data to move through cache. For most production workloads, this trade is dramatically worth it. ES makes it the default precisely because it's the right answer in 95% of cases.

If you ever need exact float32 precision for some reason — research, compliance, or extreme recall sensitivity — you can opt out by setting `"index_options": { "type": "hnsw" }` explicitly in the mapping.

---

## 9. Results

Running the full pipeline on the 1.4M-document dataset:

```
$ python article-02-vector-ingestion/pipeline.py

Reading CSV: data/amazon_products.csv
Created index 'amazon_products_embeddings'
Index optimized for bulk import (refresh off, replicas=0)
  20480 docs embedded — 17.4s elapsed (1177 docs/s)
  ...
Embedding complete — 1,426,337 docs in 11,143 batches
Bulk complete — 1,426,337 indexed, 0 errors
Total documents in 'amazon_products_embeddings': 1,426,337
Segments before merge: 287 → after merge: 2
```

Every document now carries a `title` and an `embedding`. A quick spot check in Kibana DevTools:

```json
GET amazon_products_embeddings/_search
{
  "size": 1,
  "_source": ["asin", "title", "embedding"]
}
```

returns the expected shape — a 768-element array under `embedding`.

---

## 10. Testing the result: a kNN query

Before declaring victory, it's worth running a real semantic query to confirm the pipeline produced useful vectors.

The query side is technically out of scope for this article (it belongs in the sibling search-lab repo), but a one-shot test takes thirty seconds.

**Step 1** — embed the query text by calling Ollama from your terminal:

```bash
curl -s http://localhost:11434/api/embed \
  -d '{"model":"nomic-embed-text","input":"wireless headphones for running"}' \
  | jq -c '.embeddings[0]'
```

This prints a 768-element array. Copy it.

**Step 2** — paste it into a kNN query in Kibana DevTools:

```json
GET amazon_products_embeddings/_search
{
  "knn": {
    "field": "embedding",
    "query_vector": [PASTE_VECTOR_HERE],
    "k": 10,
    "num_candidates": 100
  },
  "_source": ["asin", "title", "stars", "price"]
}
```

The top results should be Bluetooth running earbuds, sport headphones, wireless workout headsets — even when the title doesn't contain the literal phrase "wireless headphones for running". That's the entire point of vector search: **conceptual matching, not keyword matching**.

A few queries worth trying that highlight the contrast with BM25:

- `"something to keep my coffee hot at the office"` → insulated mugs, thermal flasks
- `"gift for a 10 year old who likes science"` → chemistry kits, microscopes, telescopes
- `"bluetooth speaker for the shower"` → waterproof speakers (the literal word "shower" rarely appears in titles)

A keyword-only search on these queries returns mostly noise. The vector search returns relevant products immediately.

---

## 11. Performance: what slows things down, and why

Comparing article-01 and article-02 on the same machine:

| | Article 01 (BM25 only) | Article 02 (with embeddings) |
|---|---|---|
| Pipeline time (1.4M docs) | ~85s | ~12 min |
| Bottleneck | streaming_bulk → ES | Ollama batch embedding |
| Index size | ~700 MB | ~2.0 GB (incl. HNSW + int8) |

The 8× slowdown comes almost entirely from the embedding stage. Bulk indexing into ES with HNSW is moderately slower than without (HNSW construction has a cost), but it's a small fraction of the total. On a CPU-only machine, expect the embedding stage to dominate even more — easily 30–60 minutes for the full corpus.

Two practical levers if you need to go faster:

1. **Larger embedding batch size.** On a 16 GB GPU, 256 or 512 is reasonable. Watch VRAM usage with `nvidia-smi` and back off if it pressures the model.
2. **Subset the corpus.** For an article or a demo, `--limit 100000` produces a perfectly usable semantic index in about a minute.

---

## Conclusion: key takeaways

1. **An embedding is just a function** — text in, fixed-size vector out — but the vector space is structured so that geometric proximity reflects semantic similarity.
2. **Embedding models and LLMs are siblings, not replacements.** Both are Transformers; one represents, the other generates. They work together in RAG systems.
3. **The tokenizer matters.** It's how text becomes the integers a neural network can process. Modern subword tokenizers (BPE, WordPiece) avoid the vocabulary explosion of word-level approaches and the inefficiency of character-level approaches.
4. **`dense_vector` with `index: true` builds an HNSW graph.** That's what makes kNN queries fast. The `int8_hnsw` default reduces vector storage by 4× with negligible recall loss.
5. **Batch your embedding calls or your pipeline won't finish.** This is the single most important performance lever in the whole pipeline. The GPU's parallelism is wasted on one-doc-at-a-time calls.
6. **Open-weights local embeddings are viable.** Nomic-embed-text via Ollama means no API key, no per-call cost, no third-party data exposure — and quality competitive with closed alternatives.
7. **Embedding at ingest is one strategy among several.** You can also embed at query time and store no vectors, or precompute and cache only popular queries. We chose ingest-time because it pairs naturally with ES's HNSW indexing and gives the fastest queries downstream.

The full code is on GitHub. The next article in the series will live in the sibling **search-lab** repository and cover the query side: pure kNN, hybrid BM25+vector search with reciprocal rank fusion, and a small RAG endpoint that uses these embeddings to ground LLM responses on real product data.
