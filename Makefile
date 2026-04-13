format:
	black . --exclude '\.venv|__pycache__'

format-check:
	black --check . --exclude '\.venv|__pycache__'
