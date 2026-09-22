#!/bin/sh
set -e

# uvicorn loads the TLS cert/key before importing the app, so the cert must
# exist on disk *before* uvicorn starts — the app's lifespan handler runs too
# late. Generate it here on first boot (idempotent: skips if files exist).
python -c "import certgen; certgen.ensure_cert()"

# Single worker only: dedup state and diagnostics live in this process's
# memory (backed by /data/state.json). Do not add --workers > 1 without
# moving that state into shared storage.
exec uvicorn main:app \
    --host 0.0.0.0 \
    --port 7842 \
    --workers 1 \
    --ssl-keyfile /data/key.pem \
    --ssl-certfile /data/cert.pem
