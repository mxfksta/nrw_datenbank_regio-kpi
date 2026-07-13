# Cloud Run JOB (kein Service): Batch-Lauf ohne HTTP-Endpoint.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Non-root User (Cloud Run Best Practice)
RUN useradd --create-home --uid 1000 appuser

WORKDIR /app

# Dependencies zuerst → Docker-Layer-Cache bei Code-Änderungen
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Fachliche Konfiguration + Code
COPY kpi_spec.yaml regionen.csv ./
COPY src/ ./src/

USER appuser

ENTRYPOINT ["python", "-m", "src.main"]
