.PHONY: up down migrate test test-local lint fmt shell gate prod-build prod-up prod-down backup restore

gate:
	python -m app.cli release-gate

up:
	docker compose up -d

down:
	docker compose down -v

migrate:
	docker compose run --rm migrate

test:
	docker compose -f docker-compose.test.yml run --rm test

# Run tests locally (requires a running DB at DATABASE_URL or TEST_DATABASE_URL)
test-local:
	pytest tests/ -v --tb=short

lint:
	ruff check app/ tests/

fmt:
	ruff format app/ tests/

shell:
	docker compose exec app bash

# Production deployment helpers
prod-build:
	docker compose -f deploy/docker-compose.prod.yml build

prod-up:
	docker compose -f deploy/docker-compose.prod.yml up -d

prod-down:
	docker compose -f deploy/docker-compose.prod.yml down

backup:
	bash deploy/backup_nightly.sh

restore:
	@echo "Usage: bash deploy/restore_backup.sh <path_to_backup.age> <path_to_private_key>"

