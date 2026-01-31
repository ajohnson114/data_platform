FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y bash && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir duckdb==1.4.4

FROM python:3.11-slim

RUN useradd -m appuser \
    && mkdir -p /app /duckdb_io_manager \
    && chown -R appuser:appuser /app /duckdb_io_manager

USER appuser
WORKDIR /app

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Initialize DuckDB file
RUN python -c "import duckdb; duckdb.connect('/duckdb_io_manager/my_data.duckdb').close()"

CMD ["bash", "-c", "while true; do sleep 3600; done"]
