# search-lab-ingest

Data engineering pipelines for ingesting product data into Elasticsearch 9.x.
Sibling of [search-lab](../search-lab) (Node.js / TypeScript — queries & aggregations).

Each article in this repo explores a different ingestion approach or technique.

## Prerequisites

- Docker
- Python 3.10+ (the code uses `X | None` syntax)
- Optional: an NVIDIA GPU with the Container Toolkit — see [GPU acceleration](#gpu-acceleration)

## Start the cluster

```bash
git clone <repo-url> && cd search-lab-ingest
make start
```

`make start` creates `.env` from `.env.example`, builds the venv, installs dependencies and
brings up the containers. There is no manual venv step — every target depends on it.

| Command | Effect |
|---------|--------|
| `make start` | venv + dependencies + Elasticsearch, Kibana, text-embeddings |
| `make stop` | stop the containers, **keep** the volumes (ES data, HF model cache) |
| `make clean` | stop and **delete** the volumes and the venv |
| `make status` / `make logs` | `compose ps` / follow the logs |

- Elasticsearch — `http://localhost:9200` (plain HTTP, security disabled: local lab, not a deployment)
- Kibana — `http://localhost:5601`
- text-embeddings — `http://localhost:8080`

The first `make start` downloads `nomic-embed-text-v1.5` from HuggingFace into the `hfcache`
volume (~550 MB). `up --wait` only waits for the container to be running, not for the model
to be resident, so follow `docker logs -f search-lab-tei` until *Ready* before ingesting.

If the default `python3` is older than 3.10, pass a newer one: `make start PY=python3.12`.

Elasticsearch runs with a 6 GB heap in a 12 GB container. The gap is deliberate: Lucene files
are mmapped, so their page cache is charged to the container's cgroup, and a limit set at the
heap gets the JVM OOM-killed mid-merge. Override the heap with `ES_HEAP` in `.env`, and move
`mem_limit` with it.

## GPU acceleration

Embedding is the bottleneck of article-02, and it is the one stage that cares whether the
model server can see a GPU — roughly **6× end to end** on a full run. The Makefile decides
on its own:

```makefile
GPU ?= $(shell command -v nvidia-smi ... && docker info ... | grep -q nvidia && echo 1 || echo 0)
```

Both halves must hold: an NVIDIA driver **and** the `nvidia` runtime registered in Docker by
the Container Toolkit. When they do, `docker/docker-compose.gpu.yml` is layered onto the base
compose file and the `text-embeddings` container receives the device passthrough. Otherwise
the override is skipped.

| Command | Effect |
|---------|--------|
| `make start` | auto-detect — GPU on a Linux box with the toolkit, CPU on macOS |
| `make start GPU=1` | force the override on |
| `make start GPU=0` | force it off |

The **base** `docker/docker-compose.yml` carries no GPU configuration whatsoever — the
passthrough lives entirely in the override. This matters because starting without it fails
*silently*: the pipeline runs correctly, just several times slower.

### Verifying the GPU is really used

Auto-detection only proves the host *can* serve a GPU. Run this before committing to a long
ingestion:

```bash
docker inspect search-lab-tei --format '{{json .HostConfig.DeviceRequests}}'
# → [{"Driver":"nvidia","Count":-1,...,"Capabilities":[["gpu"]]}]     null = no passthrough
```

Note that `HostConfig.Runtime` stays `runc` even on GPU — the devices arrive through
`DeviceRequests`, so that field is not the signal to read.

There is a second, blunter signal: the `120-*` image is a CUDA build, so a container that
reaches *Ready* at all has the device. This stack has no silent CPU fallback — the model
server either gets the GPU or refuses to start.

Troubleshooting: `DeviceRequests: null` means the stack was started without the override —
`make stop && make start GPU=1`. If it is populated and the container still will not run, the
problem sits below Docker: check that `nvidia-container-toolkit` is installed and that
`docker info --format '{{json .Runtimes}}'` mentions `nvidia`.

## Articles

| Folder | Topic |
|--------|-------|
| [article-01-csv-bulk-ingestion](./article-01-csv-bulk-ingestion) | Bulk-index a 1.4M-row Kaggle CSV into ES with pandas |
| [article-02-vector-ingestion](./article-02-vector-ingestion) | Add 768-dim `dense_vector` embeddings at ingestion time, via text-embeddings-inference |

## Project structure

```
search-lab-ingest/
├── shared/                              # shared across all articles
│   ├── config.py                        # central config (env vars)
│   └── es/
│       ├── client.py                    # ES client factory + index helpers
│       ├── bulk.py                      # bulk_index, update_alias, optimize_for_import, forcemerge
│       └── verify.py                    # recall gate + ProbeReservoir — blocks a bad alias swap
├── article-01-csv-bulk-ingestion/
│   ├── data/                            # gitignored — put CSV here
│   ├── mappings/                        # ES index mappings
│   ├── transforms/                      # raw row → ES document
│   └── pipeline.py                      # main entry point
├── article-02-vector-ingestion/
│   ├── data/                            # gitignored — put CSV here
│   ├── mappings/                        # ES index mapping with dense_vector
│   ├── transforms/                      # raw row → ES document
│   ├── embeddings/
│   │   ├── stream.py                    # concurrent, order-preserving embed stage
│   │   ├── preflight.py                 # is the engine producing meaningful vectors?
│   │   └── backends/                    # tei, selected by EMBED_BACKEND
│   └── pipeline.py                      # main entry point
├── docker/
│   ├── docker-compose.yml               # ES, Kibana, text-embeddings — no GPU config
│   └── docker-compose.gpu.yml           # NVIDIA passthrough, layered on when detected
├── Makefile                             # start/stop/ingest, GPU auto-detection
├── .env.example
└── requirements.txt
```

## Ecosystem

Part of the **search-lab** ecosystem — a hands-on companion for Medium articles on Elasticsearch.
