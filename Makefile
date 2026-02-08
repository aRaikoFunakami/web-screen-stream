.PHONY: setup up down build rebuild logs ps shell test clean

setup:
	@echo "==> Building and starting containers..."
	docker compose up -d --build
	@echo "==> Waiting for backend http://localhost:8200/api/healthz"
	@until curl -sf http://localhost:8200/api/healthz >/dev/null; do sleep 1; done
	@echo "==> Waiting for frontend http://localhost:3001/"
	@until curl -sf http://localhost:3001/ >/dev/null; do sleep 1; done
	@echo "==> Ready: http://localhost:3001/"

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

ps:
	docker compose ps

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
