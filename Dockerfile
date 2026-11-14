# syntax=docker/dockerfile:1
# GrowwBuddy: one container, FastAPI serving the built UI. Facts-only. No investment advice.
# Deploy notes: docs/deployment-plan.md. Built for Render's free tier (512 MB): the
# embedding model runs on ONNX Runtime, so torch is not installed.

# ---- UI build ----
FROM node:24-slim AS web
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- app ----
FROM python:3.12-slim
ARG EMBED_MODEL=intfloat/e5-small-v2
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/hf \
    GROWWBUDDY_EMBED_MODEL=${EMBED_MODEL} \
    GROWWBUDDY_EMBED_RUNTIME=onnx \
    GROWWBUDDY_EMBED_THREADS=1
WORKDIR /app

# setuptools pinned past the advisories CI found (PYSEC-2026-3447, GHSA-xmj3-9gf7-h52w).
RUN python -m pip install --upgrade pip "setuptools>=84.1.0"

# Editable install with the source tree at /app: config.py, llm/prompts.py and
# ingest/manifest.py find data/, backend/prompts/ and corpus/ relative to their own files.
# No [torch] extra: the image has no torch.
COPY backend/pyproject.toml backend/
COPY backend/growwbuddy backend/growwbuddy
COPY backend/prompts backend/prompts
RUN python -m pip install -e backend

# Bake the ONNX model and tokenizer into the image; at runtime the Hub is never contacted.
RUN python -c "import os; from huggingface_hub import hf_hub_download as d; m = os.environ['GROWWBUDDY_EMBED_MODEL']; [d(m, f) for f in ('model.onnx', 'tokenizer.json')]"
ENV HF_HUB_OFFLINE=1

COPY corpus/sources.yaml corpus/retrieval_headers.yaml corpus/
COPY --from=web /app/frontend/dist frontend/dist
RUN mkdir -p data

EXPOSE 10000
# One worker only (EC-THR-08). Shell form so $PORT expands. --no-access-log: the app logs
# route templates, not raw paths (EC-API-09). --proxy-headers: the client IP comes from
# Render's X-Forwarded-For (routes.py); see the plan's note on trusting it.
CMD cd backend && exec uvicorn growwbuddy.main:app \
    --host 0.0.0.0 --port "${PORT:-10000}" --workers 1 \
    --no-access-log --proxy-headers --forwarded-allow-ips="*"
