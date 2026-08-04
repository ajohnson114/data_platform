FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y build-essential git libpq-dev && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY etl_pipeline/pyproject.toml ./pyproject.toml
COPY etl_pipeline/src ./src
COPY etl_pipeline/config ./config
COPY etl_pipeline/dbt ./dbt
COPY shared ./shared

# Tolerate slow PyPI downloads during multi-arch builds
ENV PIP_DEFAULT_TIMEOUT=100 \
    PIP_RETRIES=5
RUN pip install --no-cache-dir -e .

# Build the dbt manifest here, not at import time.
#
# dagster-dbt defines one Dagster asset per dbt node, and it reads that list out
# of target/manifest.json. Generating it when the code server starts would make
# loading the definitions depend on dbt parsing successfully inside a live
# container -- so a typo in a model would present as a dead code location taking
# every unrelated asset down with it, rather than as a build failure.
#
# `dbt parse` compiles the project without touching the warehouse. The dummy
# password satisfies profiles.yml's env_var lookup; nothing connects.
RUN cd /app/dbt \
    && DBT_CLICKHOUSE_PASSWORD=build-time-noop \
       dbt parse --profiles-dir /app/dbt --project-dir /app/dbt --no-version-check \
    && test -f /app/dbt/target/manifest.json

FROM python:3.11-slim

RUN useradd -m appuser \
    && mkdir -p /app \
    && chown -R appuser:appuser /app

WORKDIR /app

COPY --from=builder /app /app
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# The landing zone is a mounted volume. Docker creates a named volume's
# mountpoint root-owned when the path doesn't already exist in the image, and
# this container runs as appuser -- so without this the first parquet write
# fails with EACCES. Creating it here (owned by appuser) makes Docker seed the
# volume from it and inherit the ownership.
RUN mkdir -p /app/landing && chown appuser:appuser /app/landing

# Same root cause, different directory: the chown above the COPY only covers
# what existed then, and `COPY --from=builder` lands root-owned. dbt needs
# target/ writable at RUN time -- dagster-dbt creates a per-invocation
# subdirectory there so concurrent runs don't share partial_parse state -- and
# without this every dbt build dies with
#   PermissionError: [Errno 13] Permission denied: '/app/dbt/target/...'
RUN chown -R appuser:appuser /app/dbt

USER appuser
ENV PYTHONPATH=/app

EXPOSE 4001
CMD ["dagster", "api", "grpc", "-h", "0.0.0.0", "-p", "4001", "-m", "src"]
