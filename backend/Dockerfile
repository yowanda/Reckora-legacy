FROM python:3.11-slim AS builder
WORKDIR /app
ENV PIP_NO_CACHE_DIR=1
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install --prefix=/install \
    "fastapi[standard]>=0.115.0" "uvicorn[standard]>=0.30.0" "httpx[http2]>=0.27.0" \
    "pydantic>=2.8.0" "pydantic-settings>=2.4.0" "slowapi>=0.1.9" "orjson>=3.10.0" \
    "anyio>=4.4.0"

FROM python:3.11-slim
WORKDIR /app
COPY --from=builder /install /usr/local
COPY app ./app
ENV PYTHONUNBUFFERED=1 \
    PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
