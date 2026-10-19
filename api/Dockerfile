FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates postgresql-client tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*
RUN addgroup --system --gid 10001 app \
    && adduser --system --uid 10001 --ingroup app --home /home/app app \
    && mkdir -p /app/data \
    && chmod 700 /app/data \
    && chown -R app:app /app /home/app
COPY api/requirements.txt /app/api/requirements.txt
RUN pip install --no-cache-dir -r /app/api/requirements.txt
COPY alembic.ini /app/alembic.ini
COPY --chown=app:app api /app/api
COPY --chown=app:app scripts /app/scripts
COPY --chown=app:app skills /app/skills
USER app
ENV HOME=/home/app
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/ready', timeout=3)"
CMD ["python", "-m", "api.entrypoint"]
