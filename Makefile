# Interpréteur utilisé pour créer le venv. Le code exige 3.10+ (syntaxe `X | None`).
# Surcharger si le `python3` par défaut est trop ancien : make start PY=python3.12
PY    ?= python3

PYTHON = .venv/bin/python
PIP    = .venv/bin/pip
BLACK  = .venv/bin/black

# Le passthrough GPU n'est ajouté que si la machine sait le servir : pilote NVIDIA
# présent ET runtime nvidia enregistré dans Docker. Sur le Mac les deux manquent, donc
# l'override est ignoré. Forcer avec GPU=1, désactiver avec GPU=0.
GPU ?= $(shell command -v nvidia-smi >/dev/null 2>&1 && docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia && echo 1 || echo 0)

COMPOSE_FILES = -f docker/docker-compose.yml
ifeq ($(GPU),1)
COMPOSE_FILES += -f docker/docker-compose.gpu.yml
endif

# text-embeddings est sous profil : il ne démarre qu'avec TEI=1. Le démarrer n'implique
# rien côté pipeline — c'est EMBED_BACKEND (ou `make ingest BACKEND=tei`) qui décide qui
# calcule les vecteurs.
TEI ?= 0
ifeq ($(TEI),1)
COMPOSE_PROFILES = --profile tei
endif

COMPOSE = docker compose $(COMPOSE_FILES) $(COMPOSE_PROFILES) --env-file .env

PIPELINE = article-02-vector-ingestion/pipeline.py

.PHONY: start stop restart status logs clean up down venv env dry-run ingest compare format format-check

## Tout démarrer : .env + venv + dépendances + Docker (ES, Kibana, Ollama + modèle).
## TEI=1 ajoute le service text-embeddings.
start: env venv
	$(COMPOSE) up -d --wait
	$(COMPOSE) --profile init up --force-recreate ollama-init
	@echo ""
	@echo "  Elasticsearch  http://localhost:9200"
	@echo "  Kibana         http://localhost:5601"
	@echo "  Ollama         http://localhost:11434"
ifeq ($(TEI),1)
	@echo "  TEI            http://localhost:8080"
	@echo ""
	@echo "  Le premier démarrage télécharge nomic-embed-text-v1.5 depuis HuggingFace."
	@echo "  Attendre « Ready » : docker logs -f search-lab-tei"
endif

## Tout arrêter (les données ES et les modèles Ollama sont conservés)
stop:
	$(COMPOSE) down

restart: stop start

status:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f

## Tout supprimer, y compris les volumes (index ES, modèles Ollama, cache HF) et le venv
clean:
	$(COMPOSE) down -v
	rm -rf .venv

## Validation : transforme et embed 1000 docs sans rien indexer
dry-run: env venv
	$(PYTHON) $(PIPELINE) --dry-run --limit 1000 $(if $(BACKEND),--backend $(BACKEND)) $(if $(WORKERS),--workers $(WORKERS))

## Ingestion complète (1.4M docs) dans un index versionné neuf, alias basculé à la fin.
## Options : LIMIT=100000 pour un sous-ensemble, EMBED_BATCH=512 pour des lots plus gros,
## BACKEND=tei pour changer de moteur, WORKERS=8 pour la concurrence,
## INCREMENTAL=1 pour réécrire dans l'index en service au lieu d'en construire un neuf.
ingest: env venv
	$(PYTHON) $(PIPELINE) $(if $(INCREMENTAL),,--recreate) $(if $(LIMIT),--limit $(LIMIT)) $(if $(EMBED_BATCH),--embed-batch $(EMBED_BATCH)) $(if $(BACKEND),--backend $(BACKEND)) $(if $(WORKERS),--workers $(WORKERS))

## Compare les vecteurs des deux moteurs sur les mêmes titres. Exige Ollama ET TEI
## démarrés : make start TEI=1. Options : LIMIT=1000.
compare: env venv
	$(PYTHON) tools/compare_embeddings.py $(if $(LIMIT),--limit $(LIMIT))

env:
	@test -f .env || (cp .env.example .env && echo "→ .env créé depuis .env.example")

venv: .venv/.installed

.venv/.installed: requirements.txt
	@$(PY) -c 'import sys; sys.exit(sys.version_info < (3, 10))' || \
		(echo "$(PY) est en $$($(PY) -V | cut -d' ' -f2), or le projet exige 3.10+." && \
		 echo "Relance avec un interpréteur plus récent : make $(MAKECMDGOALS) PY=python3.12" && exit 1)
	@test -d .venv || $(PY) -m venv .venv
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet -r requirements.txt
	@touch $@

# Alias historiques
up: start
down: stop

format: venv
	$(BLACK) . --exclude '\.venv|__pycache__'

format-check: venv
	$(BLACK) --check . --exclude '\.venv|__pycache__'
