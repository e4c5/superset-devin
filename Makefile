.PHONY: install run simulate test lint docker-up docker-down

install:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements-dev.txt

run:
	.venv/bin/uvicorn app.webhook:app --host 0.0.0.0 --port $${PORT:-8080} --reload --env-file .env

simulate:
	.venv/bin/python -m scripts.simulate

test:
	.venv/bin/python -m pytest -q

lint:
	.venv/bin/ruff check app simulator scripts tests

docker-up:
	docker compose up --build

docker-down:
	docker compose down
