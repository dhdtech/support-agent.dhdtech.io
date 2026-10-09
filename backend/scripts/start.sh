#!/bin/bash
set -e

# Set environment variable to prevent tokenizer deadlock warnings
export TOKENIZERS_PARALLELISM=false

# Wait for PostgreSQL to be ready.
# The host comes from DATABASE_URL, not a hardcoded `db`: the upstream compose
# ships a `db` service, but a managed datastore is reached under another name and
# a literal here blocks the container forever with no useful error.
DB_HOST=$(python3 -c "import os,urllib.parse as u;print(u.urlparse(os.environ.get('DATABASE_URL','').replace('+psycopg','')).hostname or 'db')")
DB_PORT=$(python3 -c "import os,urllib.parse as u;print(u.urlparse(os.environ.get('DATABASE_URL','').replace('+psycopg','')).port or 5432)")
echo "Waiting for PostgreSQL at ${DB_HOST}:${DB_PORT}..."
tries=0
until nc -z "$DB_HOST" "$DB_PORT"; do
  tries=$((tries + 1))
  if [ "$tries" -gt 120 ]; then
    echo "PostgreSQL at ${DB_HOST}:${DB_PORT} did not become reachable after 60s" >&2
    exit 1
  fi
  sleep 0.5
done
echo "PostgreSQL is ready!"

# Check if Firebase credentials exist, but don't create them if missing
if [ ! -z "$FIREBASE_CREDENTIALS" ] && [ ! -f "$FIREBASE_CREDENTIALS" ]; then
    echo "Warning: Firebase credentials file not found at $FIREBASE_CREDENTIALS. Continuing without Firebase credentials..."
fi



# Preload embedding models to avoid runtime issues
echo "Preloading embedding models..."
python scripts/preload_models.py
if [ $? -eq 0 ]; then
    echo "Models preloaded successfully!"
else
    echo "Warning: Model preloading failed. Continuing anyway..."
fi

# Run migrations
echo "Running database migrations..."
alembic upgrade head

# Use only 2 workers to reduce resource usage and database connection issues
WORKERS=1

# Start the application with Gunicorn
echo "Starting FastAPI application with Gunicorn ($WORKERS workers)..."
gunicorn app.main:app \
    --workers $WORKERS \
    --worker-class uvicorn.workers.UvicornWorker \
    --bind 0.0.0.0:8000 \
    --timeout 120 \
    --keep-alive 5 \
    --log-level info \
    --access-logfile - \
    --error-logfile - \
    --preload