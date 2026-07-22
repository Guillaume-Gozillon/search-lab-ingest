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

`make start` creates `.env` from `.env.example`, builds the venv, installs dependencies,
brings up the containers and pulls `nomic-embed-text` into the Ollama volume. There is no
manual venv step — every target depends on it.

| Command | Effect |
|---------|--------|
| `make start` | venv + dependencies + docker stack + model pull |
| `make stop` | stop the containers, **keep** the volumes (ES data, Ollama models) |
| `make clean` | stop and **delete** the volumes and the venv |
| `make status` / `make logs` | `compose ps` / follow the logs |

- Elasticsearch — `http://localhost:9200` (plain HTTP, security disabled: local lab, not a deployment)
- Kibana — `http://localhost:5601`
- Ollama — `http://localhost:11434`

If the default `python3` is older than 3.10, pass a newer one: `make start PY=python3.12`.

## GPU acceleration

Embedding is the bottleneck of article-02, and it is the one stage that cares whether Ollama
can see a GPU — roughly **6× end to end** on a full run. The Makefile decides on its own:

```makefile
GPU ?= $(shell command -v nvidia-smi ... && docker info ... | grep -q nvidia && echo 1 || echo 0)
```

Both halves must hold: an NVIDIA driver **and** the `nvidia` runtime registered in Docker by
the Container Toolkit. When they do, `docker/docker-compose.gpu.yml` is layered onto the base
compose file and the `ollama` container receives the device passthrough. Otherwise the
override is skipped and Ollama falls back to CPU.

| Command | Effect |
|---------|--------|
| `make start` | auto-detect — GPU on a Linux box with the toolkit, CPU on macOS |
| `make start GPU=1` | force the override on |
| `make start GPU=0` | force it off |

The **base** `docker/docker-compose.yml` carries no GPU configuration whatsoever — the
passthrough lives entirely in the override. This matters because starting without it fails
*silently*: the pipeline runs correctly, just several times slower.

### Verifying the GPU is really used

Auto-detection only proves the host *can* serve a GPU. These three checks prove Ollama *is*
using it — run them before committing to a long ingestion:

```bash
# 1. the container actually got the devices
docker inspect search-lab-ollama --format '{{json .HostConfig.DeviceRequests}}'
# → [{"Driver":"nvidia","Count":-1,...,"Capabilities":[["gpu"]]}]     null = CPU-only

# 2. the model is resident on the GPU (send any embed call first to load it)
docker exec search-lab-ollama ollama ps
# → PROCESSOR = 100% GPU                                              100% CPU = override missed

# 3. every layer was offloaded
docker logs search-lab-ollama 2>&1 | grep offloaded | tail -1
# → offloaded 13/13 layers to GPU                                     0/13 = CPU-only
```

`nvidia-smi` should also list `/usr/bin/ollama` under its compute apps once the model is
loaded. Note that `HostConfig.Runtime` stays `runc` even on GPU — the devices arrive through
`DeviceRequests`, so that field is not the signal to read.

Troubleshooting: `DeviceRequests: null` means the stack was started without the override —
`make stop && make start GPU=1`. If it is populated but the offload is still `0/13`, the
problem sits below Docker: check that `nvidia-container-toolkit` is installed and that
`docker info --format '{{json .Runtimes}}'` mentions `nvidia`.

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
│   ├── docker-compose.yml               # ES, Kibana, Ollama — no GPU config
│   └── docker-compose.gpu.yml           # NVIDIA passthrough, layered on when detected
├── Makefile                             # start/stop/ingest, GPU auto-detection
├── .env.example
└── requirements.txt
```

## Ecosystem

Part of the **search-lab** ecosystem — a hands-on companion for Medium articles on Elasticsearch.
