.PHONY: dev backend frontend install test lint seed migrate reset-demo

PYTHON ?= python3
NPM ?= npm

install:
	$(PYTHON) -m pip install -r backend/requirements.txt
	cd frontend && $(NPM) install

backend:
	PYTHONPATH=. $(PYTHON) -m uvicorn backend.app:app --reload --reload-dir backend --reload-exclude 'backend/data/*' --reload-exclude '**/data/**' --host 0.0.0.0 --port 8000

frontend:
	cd frontend && $(NPM) run dev -- --host 0.0.0.0 --port 5173

dev:
	@PYTHONPATH=. $(PYTHON) -m uvicorn backend.app:app --host 0.0.0.0 --port 8000 & backend_pid=$$!; trap 'kill $$backend_pid' EXIT INT TERM; cd frontend && $(NPM) run dev -- --host 0.0.0.0 --port 5173

test:
	PYTHONPATH=. $(PYTHON) -m pytest -q

lint:
	PYTHONPATH=. $(PYTHON) -m compileall -q backend
	cd frontend && $(NPM) run build

seed:
	PYTHONPATH=. $(PYTHON) -m backend.seed seed

migrate:
	@echo "SQLite demo mode uses automatic table creation; PostgreSQL deployments should run Alembic migrations."

reset-demo:
	DEMO_MODE=true PYTHONPATH=. $(PYTHON) -m backend.seed reset
