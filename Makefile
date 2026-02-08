.PHONY: up down build rebuild logs shell test clean

# Docker Compose commands
up:
	docker compose up -d

down:
	docker compose down

build:
	docker compose build

rebuild:
	docker compose up -d --build --force-recreate

logs:
	docker compose logs -f

shell:
	docker compose exec server bash

# Test
test:
	uv run pytest tests/ -v

test-docker:
	docker compose exec server uv run pytest tests/ -v

# Clean
clean:
	docker compose down --volumes --remove-orphans
