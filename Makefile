PYTHON = .venv/bin/python
PIP    = .venv/bin/pip
BLACK  = .venv/bin/black

COMPOSE_FILES = -f docker/docker-compose.yml
ifdef GPU
COMPOSE_FILES += -f docker/docker-compose.gpu.yml
endif
COMPOSE = docker compose $(COMPOSE_FILES) --env-file .env

.PHONY: start stop restart status logs clean up down venv env format format-check

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

env:
	@test -f .env || (cp .env.example .env && echo "→ .env créé depuis .env.example")

venv: .venv/.installed

.venv/.installed: requirements.txt
	@test -d .venv || python3 -m venv .venv
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
