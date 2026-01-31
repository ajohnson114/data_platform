# -------------------------
# Stage 1: Builder
# -------------------------
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir \
    dagster==1.12.12 \
    dagit==1.12.12 \
    dagster-postgres==0.28.12 \
    dagster-duckdb==0.28.12
    
# Copy Dagster workspace
COPY deployment/workspace.yaml .

# -------------------------
# Stage 2: Runtime
# -------------------------
FROM python:3.11-slim

RUN useradd -m appuser \
    && mkdir -p \
        /app \
        /dagster_home \
        /duckdb_io_manager \
        /dagster_compute_logs \
    && chown -R appuser:appuser \
        /app \
        /dagster_home \
        /duckdb_io_manager \
        /dagster_compute_logs

WORKDIR /app

# Copy Python env + workspace
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app /app

# ✅ COPY dagster.yaml into DAGSTER_HOME
COPY deployment/dagster.yaml /dagster_home/dagster.yaml

RUN chown -R appuser:appuser /app /dagster_home

USER appuser
ENV PATH="/usr/local/bin:$PATH"

EXPOSE 3000

CMD ["dagster-webserver", "-h", "0.0.0.0", "-p", "3000", "-w", "/app/workspace.yaml"]
