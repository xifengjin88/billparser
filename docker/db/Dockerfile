# PostgreSQL for billparse. The schema comes from the app's Alembic migrations (docker/entrypoint.sh),
# not from init scripts, so the same migrations run here and in any other PostgreSQL.
FROM postgres:17.6-alpine

ENV POSTGRES_USER=billparse \
    POSTGRES_DB=billparse

HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=10 \
    CMD pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" || exit 1
