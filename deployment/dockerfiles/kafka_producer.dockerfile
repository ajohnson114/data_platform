FROM python:3.11-slim

# /app/state holds the Jetstream cursor so a restart resumes instead of skipping
# to the live edge. Mount a volume here to keep it across container re-creation.
RUN useradd -m appuser \
    && mkdir -p /app/state \
    && chown -R appuser:appuser /app

WORKDIR /app

# requirements.txt pulls in `websockets` for the Jetstream firehose alongside
# confluent-kafka; both ship manylinux wheels, so no build toolchain is needed.
COPY kafka_producer/requirements.txt ./requirements.txt
# Tolerate slow PyPI downloads during multi-arch builds
ENV PIP_DEFAULT_TIMEOUT=100 \
    PIP_RETRIES=5
RUN pip install --no-cache-dir -r requirements.txt

COPY kafka_producer/producer.py ./producer.py

USER appuser

CMD ["python", "producer.py"]
