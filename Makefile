format:
	black . --exclude '\.venv|__pycache__'

format-check:
	black --check . --exclude '\.venv|__pycache__'

up:
	docker compose -f docker/docker-compose.yml --env-file .env up -d

down:
	docker compose -f docker/docker-compose.yml --env-file .env down
