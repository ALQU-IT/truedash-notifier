#!/bin/sh
set -e

# uvicorn loads the TLS cert/key before importing the app, so the cert must
# exist on disk *before* uvicorn starts — the app's lifespan handler runs too
# late. Generate it here on first boot (idempotent: skips if files exist).
python -c "import certgen; certgen.ensure_cert()"

exec uvicorn main:app \
    --host 0.0.0.0 \
    --port 7842 \
    --ssl-keyfile /data/key.pem \
    --ssl-certfile /data/cert.pem
