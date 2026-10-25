#!/bin/sh
# Bring the database schema up to date, then start the given command (the API by default).
# RUN_MIGRATIONS=0 skips the migrations. The service catalog lives in the data volume (CATALOG_DIR), so the
# synonyms that review teaches survive a rebuild: its raw inputs are copied there once from the image.
set -e
if [ -n "${CATALOG_DIR:-}" ] && [ ! -d "$CATALOG_DIR/raw" ]; then
  mkdir -p "$CATALOG_DIR"
  cp -r /app/data/raw "$CATALOG_DIR/raw"
fi
case "${DATABASE_URL:-}" in
  postgresql*)
    if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
      alembic upgrade head
    fi
    ;;
esac
exec "$@"
