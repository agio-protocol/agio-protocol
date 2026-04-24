FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev curl && \
    rm -rf /var/lib/apt/lists/*

COPY . .
RUN pip install --no-cache-dir . gunicorn "uvicorn[standard]" solders solana || true

EXPOSE 8000

# WORKER_CMD env var selects what this container runs:
#   (empty/default) = API server
#   batch_worker    = Base batch worker
#   solana_worker   = Solana batch worker
#   reconciler      = Reconciliation service
#   oracle          = Oracle test loop
CMD sh -c '\
  if [ "$WORKER_CMD" = "batch_worker" ]; then \
    python -m src.workers.batch_worker_prod; \
  elif [ "$WORKER_CMD" = "solana_worker" ]; then \
    python -m src.workers.solana_batch_worker; \
  elif [ "$WORKER_CMD" = "reconciler" ]; then \
    python -m src.workers.reconciler_prod; \
  elif [ "$WORKER_CMD" = "oracle" ]; then \
    python -m src.workers.oracle_prod; \
  elif [ "$WORKER_CMD" = "arena" ] || [ "$WORKER_CMD" = "challenges" ]; then \
    python -m src.workers.challenges_worker; \
  elif [ "$WORKER_CMD" = "overseer" ]; then \
    python -m src.workers.overseer; \
  else \
    gunicorn src.main:app --worker-class uvicorn.workers.UvicornWorker --workers 2 --bind 0.0.0.0:${PORT:-8000} --timeout 120 --access-logfile - --error-logfile -; \
  fi'
