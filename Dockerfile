# billparse: the API (one image; the web app has its own, web/Dockerfile).
FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.8.15 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PARSER_DATA_DIR=/data/store \
    CATALOG_DIR=/data/catalog

WORKDIR /app

# dependencies first: this layer is rebuilt only when the lock file changes
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev \
    && useradd --create-home --uid 1000 billparse \
    && mkdir -p /data && chown billparse:billparse /data

USER billparse
EXPOSE 8000
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["uvicorn", "billparse.api:app", "--host", "0.0.0.0", "--port", "8000"]
