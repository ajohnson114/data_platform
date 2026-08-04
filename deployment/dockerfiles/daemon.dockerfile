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

# Tolerate slow PyPI downloads during multi-arch builds
ENV PIP_DEFAULT_TIMEOUT=100 \
    PIP_RETRIES=5

RUN pip install --no-cache-dir \
    dagster==1.13.6 \
    dagster-postgres==0.29.6 \
    dagster-aws==0.29.6

# Copy Dagster workspace
COPY deployment/workspace.yaml .

# -------------------------
# Stage 2: Runtime
# -------------------------
FROM python:3.11-slim

# Create user and required directories
RUN useradd -m appuser \
    && mkdir -p \
        /app \
        /dagster_home \
        /dagster_compute_logs \
    && chown -R appuser:appuser \
        /app \
        /dagster_home \
        /dagster_compute_logs

WORKDIR /app

# Copy Python env + workspace
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app /app

# ✅ COPY dagster.yaml into DAGSTER_HOME
COPY deployment/dagster.yaml /dagster_home/dagster.yaml

# Ensure ownership
RUN chown -R appuser:appuser /app /dagster_home

USER appuser
ENV PATH="/usr/local/bin:$PATH"

CMD ["dagster-daemon", "run", "-w", "/app/workspace.yaml"]
