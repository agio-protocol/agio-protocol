FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev curl && \
    rm -rf /var/lib/apt/lists/*

COPY . .
RUN pip install --no-cache-dir . gunicorn uvicorn[standard]

EXPOSE 8000

CMD gunicorn src.main:app --worker-class uvicorn.workers.UvicornWorker --workers 2 --bind 0.0.0.0:${PORT:-8000} --timeout 120 --access-logfile - --error-logfile -
