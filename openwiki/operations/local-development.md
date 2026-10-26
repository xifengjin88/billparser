---
type: operations
title: Local development and operations
description: How to run billparse locally (Docker compose stack of PostgreSQL, API and web app, hot reload, or plain processes with SQLite), the Make targets, the environment variables, the service catalog refresh, and the known operational limits.
tags: [operations, docker, make, environment, local]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-25T20:19:27.415Z
sources:
  - id: openwiki-source-dfa1b0df3ce19bd99b4c6c11
    resource: repo://billparse/settings.py
  - id: openwiki-source-0c106052b3286e779bed85e7
    resource: repo://docker-compose.dev.yml
  - id: openwiki-source-b79fbbd921df689b4bbdc82f
    resource: repo://docker-compose.yml
  - id: openwiki-source-2f4855371b154cd2f4d70da2
    resource: repo://docker/db/Dockerfile
  - id: openwiki-source-b39c2a237ec6484b934d931f
    resource: repo://docker/entrypoint.sh
  - id: openwiki-source-bb1ebe868e35e9e500714501
    resource: repo://Dockerfile
  - id: openwiki-source-0756aa10ba9e55148108b47d
    resource: repo://example.env
  - id: openwiki-source-012f2c78e3b1446dfc35803f
    resource: repo://Makefile
  - id: openwiki-source-6f8cce9cd1948d0615350074
    resource: repo://scripts/refresh_catalog.sh
generated: { by: "claude-code", at: "2026-10-25T20:19:27.415Z" }
---

# Local development and operations

## The Docker stack

`docker-compose.yml` (project name `billparse`) runs three services; the ports differ from invoice-parser's so both
stacks run side by side:

| Service | Image | Port | Notes |
|---|---|---|---|
| `db` | `docker/db` (PostgreSQL 17.6, schema from the app's migrations) | `127.0.0.1:5434` → 5432 | volume `pgdata`; health check with `pg_isready` |
| `api` | `billparse:local` (built here, never pulled) | `${API_PORT:-8010}` → 8000 | `DATABASE_URL` to `db`; volume `billparse-data` at `/data` (uploads, rows, reports, catalog, synonyms); `.env` loaded when present |
| `web` | `billparse-web:local` (nginx) | `${WEB_PORT:-8090}` → 80 | starts when the API is healthy; `/api/` goes to the API |

`docker/entrypoint.sh` copies the raw catalog inputs into `CATALOG_DIR` once (so synonyms that review teaches
survive a rebuild), runs `alembic upgrade head` when `DATABASE_URL` is PostgreSQL (unless `RUN_MIGRATIONS=0`), then
starts uvicorn. The API image is Python 3.13 slim with dependencies installed by `uv sync --frozen --no-dev`, running
as user `billparse`.

`make dev` adds `docker-compose.dev.yml`: the code is mounted, uvicorn reloads on changes under `billparse/` and
`svcres/` (polling, because file events from macOS do not always reach the container), the web container runs the
Vite dev server, and a change to `pyproject.toml`, `uv.lock` or `web/package.json` rebuilds the image.

## Without Docker

```bash
uv sync --extra dev
python samples/make_samples.py          # synthetic samples in samples/files/
cp tests/fixtures/raw/*.json data/raw/  # a tiny demo catalog
make api-local                          # API on :8000, SQLite under PARSER_DATA_DIR unless DATABASE_URL is set
make web-local                          # web app on :5173 (cd web && pnpm install first)
```

The CLI works on the same store: `python -m billparse parse FILES --no-llm`.

## Make targets

| Target | Does |
|---|---|
| `up`, `down`, `start`, `stop`, `restart`, `build`, `logs`, `ps` | manage the compose stack (`SERVICE=api` for one) |
| `dev` | the stack with hot reload |
| `psql`, `migrate`, `shell` | psql in `db`; migrations in `api`; a shell |
| `backup-db` | `pg_dump` to `backups/db-<time>/` (the uploads volume is not included) |
| `reset-db`, `destroy` | empty the database / remove containers and volumes (both ask for `yes`) |
| `nightly` | the learning loop in the API container |
| `test`, `lint`, `lint-fix`, `typecheck`, `web-types` | pytest; Ruff (on a fixed file list) and oxlint; Ruff fixes; tsc; regenerate web API types |
| `samples`, `corpus`, `corpus-check`, `corpus-accept`, `corpus-agent`, `agent-jobs` | samples and the corpus check ([Tests, samples and the corpus check](../testing/tests-and-corpus.md)) |

## Environment

Settings are read from the process environment, then `.env` (`billparse/settings.py`; `ENV_FILE` points to another
file). `example.env` lists them all:

| Area | Variables |
|---|---|
| LLM routes | `ANTHROPIC_API_KEY`, `BILLPARSE_MODEL`, `BILLPARSE_EFFORT`, `BILLPARSE_MAX_TOKENS`, `BILLPARSE_LLM_TIMEOUT`, `LLM_CHOICES`, `BILLPARSE_USE_LLM` |
| Agent | `AGENT_ENABLED` (0 by default: learning costs money), `BILLPARSE_AGENT_MODEL`, `AGENT_EFFORT`, `AGENT_MAX_ROUNDS`, `AGENT_TOOL_CALLS`, `AGENT_MAX_COST_USD`, `AGENT_MAX_SECONDS`, `AGENT_MAX_TOKENS` |
| Storage | `DATABASE_URL`, `PARSER_DATA_DIR` (default `store`), `CATALOG_DIR` (default `data`) |
| Server | `MAX_UPLOAD_MB`, `PARSE_WORKERS`, `PDF_WORKERS`, `CONTRACT_MAX_CHARGES`, `LOG_LEVEL` |
| Sign-in | `AUTH`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `SESSION_SECRET`, `ALLOWED_EMAIL_DOMAIN`, `INVITE_CODES`, `PUBLIC_URL`, `SESSION_HTTPS_ONLY` |

## Service catalog refresh

`scripts/refresh_catalog.sh` fetches the Azure and AWS service lists (`fetch_azure.py`, `fetch_aws.py`) and the GCP
list only when `GCP_API_KEY` is set, builds `data/catalog.msgpack.new`, refuses it when its code count dropped by more
than 10%, and then swaps it in. See [Service resolution, attributes and analysis](../concepts/services-and-analysis.md).

## Known limits (from the README)

- Built-in column names come from public docs and were tested on synthetic files only.
- No OCR: scanned PDFs end as `unsupported`.
- Commitment savings in the analysis are indicative ranges.
- Parses run in a thread pool in the API process; a running task is in memory and is lost on restart, so only one
  API container can run.
