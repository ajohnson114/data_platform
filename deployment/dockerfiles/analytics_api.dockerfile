FROM python:3.11-slim

RUN useradd -m appuser && mkdir -p /app && chown -R appuser:appuser /app

WORKDIR /app

COPY requirements.txt .
# Tolerate slow PyPI downloads during multi-arch builds
ENV PIP_DEFAULT_TIMEOUT=100 \
    PIP_RETRIES=5
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

USER appuser

EXPOSE 8000 7860

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port 8000 & python gradio_app.py"]
