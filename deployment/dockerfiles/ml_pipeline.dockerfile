FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y build-essential git libpq-dev && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY basic_ml_pipeline/pyproject.toml ./pyproject.toml
COPY basic_ml_pipeline/src ./src
COPY basic_ml_pipeline/config ./config
COPY shared ./shared

# Tolerate slow PyPI downloads during multi-arch builds
ENV PIP_DEFAULT_TIMEOUT=100 \
    PIP_RETRIES=5
RUN pip install --no-cache-dir -e .

FROM python:3.11-slim

RUN useradd -m appuser \
    && mkdir -p /app \
    && chown -R appuser:appuser /app

WORKDIR /app

COPY --from=builder /app /app
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# save_model writes trained models to the landing volume, shared with the etl
# location. Docker creates a named volume's mountpoint root-owned when the path
# does not already exist in the image, and this container runs as appuser -- so
# without this the first model write fails with EACCES, and worse, whichever of
# the two containers starts first decides the ownership for both. Creating it
# here (owned by appuser) makes Docker seed the volume from it, exactly as the
# etl image does.
RUN mkdir -p /app/landing && chown appuser:appuser /app/landing

USER appuser
ENV PYTHONPATH=/app

EXPOSE 4000
CMD ["dagster", "api", "grpc", "-h", "0.0.0.0", "-p", "4000", "-m", "src"]
