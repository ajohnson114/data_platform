FROM python:3.11-slim

RUN useradd -m appuser \
    && mkdir -p /app \
    && chown -R appuser:appuser /app

WORKDIR /app

COPY kafka_producer/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY kafka_producer/producer.py ./producer.py

USER appuser

CMD ["python", "producer.py"]
