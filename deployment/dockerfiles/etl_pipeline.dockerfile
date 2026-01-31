FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y build-essential git libpq-dev && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY etl_pipeline/pyproject.toml ./pyproject.toml
COPY etl_pipeline/src ./src
COPY etl_pipeline/config ./config
COPY shared ./shared

RUN pip install --no-cache-dir -e .

FROM python:3.11-slim

RUN useradd -m appuser \
    && mkdir -p /app \
    && chown -R appuser:appuser /app

WORKDIR /app

COPY --from=builder /app /app
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

USER appuser
ENV PYTHONPATH=/app

EXPOSE 4001
CMD ["dagster", "api", "grpc", "-h", "0.0.0.0", "-p", "4001", "-m", "src"]
