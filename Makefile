PYTHON = .venv/bin/python
BLACK = .venv/bin/black

format:
	$(BLACK) . --exclude '\.venv|__pycache__'

format-check:
	$(BLACK) --check . --exclude '\.venv|__pycache__'

up:
	docker compose -f docker/docker-compose.yml --env-file .env up -d

down:
	docker compose -f docker/docker-compose.yml --env-file .env down
