.PHONY: up down migrate test lint fmt shell

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
