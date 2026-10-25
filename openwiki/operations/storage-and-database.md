---
type: operations
title: Storage, database and migrations
description: Where billparse keeps its state (database tables through SQLModel, large parts as files under the data directory), the Store API that wraps both, PostgreSQL versus SQLite, Alembic migrations, and how settings choose the locations.
tags: [storage, database, sqlmodel, alembic, migrations]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-25T20:19:27.415Z
sources:
  - id: openwiki-source-eff137aeca3927e0406190e1
    resource: repo://billparse/db.py
  - id: openwiki-source-c9b0d4adb896b06d8a75bc74
    resource: repo://billparse/store.py
  - id: openwiki-source-a41ded727f97fd17b9db4917
    resource: repo://migrations/env.py
generated: { by: "claude-code", at: "2026-10-25T20:19:27.415Z" }
---

# Storage, database and migrations

`billparse/store.py` (`Store`) is the one place that reads and writes state. Rows go to a database through SQLModel
(`billparse/db.py`); large parts stay files under the data directory.

## Where things are

| Setting | Default | Holds |
|---|---|---|
| `DATABASE_URL` | `sqlite:///<data dir>/billparse.db` | the tables below |
| `PARSER_DATA_DIR` (data dir) | `store` (`/data/store` in Docker) | uploads, rows, contract parts, traces, reports |
| `CATALOG_DIR` | `data` (`/data/catalog` in Docker) | `catalog.msgpack`, `synonyms.json`, `resolution_cache.json`, `raw/` |

`settings.data_dir()` and `settings.catalog_dir()` read them; `settings.load_env()` loads `.env` first at the API and
CLI entry points (a variable already set in the shell wins).

## Database tables (`billparse/db.py`)

| Table | Row | Notes |
|---|---|---|
| `documents` | one per uploaded file (key `sha256`) | latest summary (JSON), status, route, spec id/version, totals, upload and review fields, the `approved` result after review |
| `spec` | one per learned spec | which version is current, its trust |
| `spec_version` | every saved version of a learned spec | never overwritten: history and rollback |
| `spec_doc` | (spec, file) | the files a spec was accepted on, with rows, total and confidence: the spec's **regression set** |
| `spec_fingerprint` | (spec, layout hash) | layouts a spec parsed; the learned route finds specs by these |
| `journal` | append-only log entries by `stream` | `records` (one per parse), `labels`, `review` (the queue), `runs` (one per parse run, for the jobs page) |
| `agent_job` | one per layout key | the agent's learning jobs ([The spec-authoring agent](../architecture/agent.md)) |
| `app_users` | one per admitted person | Google sign-in ([HTTP API, web app and sign-in](../integrations/http-api-and-web-app.md)) |

Built-in specs are files in `specs/builtin/` and never rows; a person's enable/disable choice for them is
`spec_status.json` in the data directory.

JSON columns are `jsonb` on PostgreSQL and JSON text on SQLite. `store.jsonable` makes values safe to store (numpy and
dates as text, NaN and infinity as null).

## Files under the data directory

| Path | Content |
|---|---|
| `docs/<sha>.<ext>` | raw uploads, kept for parsing again and for regression tests (`retain_docs`) |
| `results/<sha>.parquet` | canonical rows; `<sha>.approved.parquet` after review |
| `parts/<sha>.<part>.json` | the contract document (`doc`, `gold` after review), its `meta`, the source `bundle` |
| `agent/<job>.jsonl` | agent traces |
| `spec_status.json` | built-in spec status |
| `models/`, `nightly/` | the row classifier and the learning reports |

## The Store API (selection)

| Area | Methods |
|---|---|
| Specs | `builtin_specs`, `learned_specs`, `get_spec`, `save_spec` (new version, optionally current), `set_current`, `versions`, `spec_history`, `link` (regression set + fingerprint), `spec_docs`, `fingerprints` |
| Files and results | `save_doc`, `load_doc`, `save_result`, `get_result`, `get_rows`, `save_part`, `part`, `save_approved`, `delete_document` |
| Documents | `document`, `list_documents`, `update_document` |
| Logs | `append`, `read`, `add_run`, `update_run`, `runs` |
| Agent jobs | `enqueue_agent_job`, `agent_job`, `agent_jobs`, `update_agent_job`, `agent_job_for`, `recover_agent_jobs`, `retry_agent_job` |

Each method opens its own short session (`Store.session()`, `expire_on_commit=False`) and commits it. Callers do not
pass sessions. `delete_document` removes the result, rows, journal entries and the upload, but keeps specs and their
regression links.

## PostgreSQL or SQLite

`db.engine_for(url)` keeps one engine per URL. With SQLite (the CLI, tests and `make api-local`), tables are created
with `create_all` on first use. With PostgreSQL (Docker), the schema comes **only** from the Alembic migrations, which
the API container runs at start (`docker/entrypoint.sh`).

## Migrations

`migrations/env.py` takes the URL from `alembic.ini` or `DATABASE_URL` (else the default SQLite file) and the schema
from the SQLModel tables; SQLite uses batch mode. Versions: `bf0c37c441f3_initial_schema` and
`ce9c3045992b_agent_jobs`.

To change the schema: change `billparse/db.py`, then

```bash
DATABASE_URL=... uv run alembic revision --autogenerate -m "..."
```

and review the generated file. `make migrate` runs `alembic upgrade head` in the API container.
