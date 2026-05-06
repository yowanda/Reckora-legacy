# Hugging Face Spaces / single-image deployment Dockerfile.
#
# Builds the frontend with pnpm, then copies the resulting bundle into
# the backend's static dir so a single FastAPI process serves both the
# API and the SPA on one origin. HF Spaces sets PORT=7860 at runtime;
# the entrypoint honours it via ${PORT:-8080}.
#
# Build context = repo root. Locally: `docker build -t cacadoxx .`
# Then: `docker run --rm -p 7860:7860 -e PORT=7860 \
#         -e CACADOXX_ADMIN_USERNAME=admin \
#         -e CACADOXX_ADMIN_PASSWORD=changeme cacadoxx`

# ---- Stage 1: build the React frontend ----
FROM node:20-alpine AS frontend
WORKDIR /src
RUN corepack enable
COPY frontend/package.json frontend/pnpm-lock.yaml* ./
RUN pnpm install --frozen-lockfile || pnpm install
COPY frontend/ ./
RUN pnpm build


# ---- Stage 2: install Python deps ----
FROM python:3.11-slim AS pybuilder
WORKDIR /app
ENV PIP_NO_CACHE_DIR=1
RUN pip install --upgrade pip && \
    pip install --prefix=/install \
      "fastapi[standard]>=0.115.0" "uvicorn[standard]>=0.30.0" \
      "httpx[http2]>=0.27.0" "pydantic>=2.8.0" "pydantic-settings>=2.4.0" \
      "slowapi>=0.1.9" "orjson>=3.10.0" "anyio>=4.4.0"


# ---- Stage 3: runtime ----
FROM python:3.11-slim
WORKDIR /app
COPY --from=pybuilder /install /usr/local
COPY backend/app ./app
COPY --from=frontend /src/dist ./app/static

ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    CACADOXX_DB_PATH=/data/cacadoxx.db

# /data is writable on Hugging Face Spaces persistent volumes; falls back
# to /tmp at runtime if /data isn't writable.
RUN mkdir -p /data && chmod 777 /data

EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
