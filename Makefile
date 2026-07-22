# Interpréteur utilisé pour créer le venv. Le code exige 3.10+ (syntaxe `X | None`).
# Surcharger si le `python3` par défaut est trop ancien : make start PY=python3.12
PY    ?= python3

PYTHON = .venv/bin/python
PIP    = .venv/bin/pip
BLACK  = .venv/bin/black

COMPOSE_FILES = -f docker/docker-compose.yml
ifdef GPU
COMPOSE_FILES += -f docker/docker-compose.gpu.yml
endif
COMPOSE = docker compose $(COMPOSE_FILES) --env-file .env

PIPELINE = article-02-vector-ingestion/pipeline.py

.PHONY: start stop restart status logs clean up down venv env dry-run ingest format format-check

## Tout démarrer : .env + venv + dépendances + Docker (ES, Kibana, Ollama + modèle)
start: env venv
	$(COMPOSE) up -d --wait
	$(COMPOSE) --profile init up --force-recreate ollama-init
	@echo ""
	@echo "  Elasticsearch  http://localhost:9200"
	@echo "  Kibana         http://localhost:5601"
	@echo "  Ollama         http://localhost:11434"

## Tout arrêter (les données ES et les modèles Ollama sont conservés)
stop:
	$(COMPOSE) down

restart: stop start

status:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f

## Tout supprimer, y compris les volumes (index ES, modèles Ollama) et le venv
clean:
	$(COMPOSE) down -v
	rm -rf .venv

## Validation : transforme et embed 1000 docs sans rien indexer
dry-run: env venv
	$(PYTHON) $(PIPELINE) --dry-run --limit 1000

## Ingestion complète (1.4M docs). Surcharger : make ingest LIMIT=100000 EMBED_BATCH=256
ingest: env venv
	$(PYTHON) $(PIPELINE) $(if $(LIMIT),--limit $(LIMIT)) $(if $(EMBED_BATCH),--embed-batch $(EMBED_BATCH))

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

format:
	$(BLACK) . --exclude '\.venv|__pycache__'

format-check:
	$(BLACK) --check . --exclude '\.venv|__pycache__'
