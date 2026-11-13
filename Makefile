# GrowwBuddy task runner. Run from the project root.
# Facts-only. No investment advice.

PYTHON ?= python
BACKEND := backend

.PHONY: install lint format test ingest serve eval eval-free eval-cutoff linkcheck audit pii-patterns \n	frontend-install frontend-dev frontend-test frontend-build

install:
	$(PYTHON) -m pip install -e "$(BACKEND)[dev]"

lint:
	cd $(BACKEND) && $(PYTHON) -m ruff check . && $(PYTHON) -m ruff format --check . && $(PYTHON) -m mypy

format:
	cd $(BACKEND) && $(PYTHON) -m ruff format . && $(PYTHON) -m ruff check --fix .

test:
	cd $(BACKEND) && $(PYTHON) -m pytest

# Available from Phase 1.
ingest:
	cd $(BACKEND) && $(PYTHON) -m growwbuddy.ingest

# One worker only: per-thread locks live in the process (EC-THR-08). Uvicorn's access log
# is off because it prints raw paths; the app logs the route template instead (EC-API-09).
serve:
	cd $(BACKEND) && $(PYTHON) -m uvicorn growwbuddy.main:app --reload --no-access-log

# Phase 6. Uses the live Groq API (costs usage: a full run is ~180K tokens, most of a
# free-tier day, so it may need `--resume` the next day); never run in CI.
eval:
	cd $(BACKEND) && $(PYTHON) -m growwbuddy.eval

# Only the golden items the rules handle (PII, advisory, NAV...): no API usage.
eval-free:
	cd $(BACKEND) && $(PYTHON) -m growwbuddy.eval --no-llm

# Tune the retrieval low-confidence cutoff offline (task 6.3); local embedder, no Groq.
eval-cutoff:
	cd $(BACKEND) && $(PYTHON) -m growwbuddy.eval.cutoff

# The five Groww URLs still resolve and show the right scheme (task 6.8). Fetches 5 pages.
linkcheck:
	cd $(BACKEND) && $(PYTHON) -m growwbuddy.ingest.linkcheck

# Dependency audit (task 6.10, EV-6-13).
audit:
	cd $(BACKEND) && $(PYTHON) -m pip_audit --skip-editable
	cd frontend && npm audit --omit=dev

# ---------------------------------------------------------------- web UI (Phase 5)
# Needs Node.js 22.12+. In development run `make serve` and `make frontend-dev` side by
# side and open http://localhost:5173 (Vite proxies /api to the backend). For production,
# `make frontend-build` and `make serve` alone serve the UI at http://localhost:8000.

frontend-install:
	cd frontend && npm ci

frontend-dev:
	cd frontend && npm run dev

frontend-test:
	cd frontend && npm run typecheck && npm test

frontend-build:
	cd frontend && npm run build

# Regenerate the UI's copy of the PII regexes after changing pipeline/pii_guard.py (EV-5-07).
pii-patterns:
	cd $(BACKEND) && $(PYTHON) -m growwbuddy.pipeline.pii_export
