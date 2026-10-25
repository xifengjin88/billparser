---
type: architecture
title: Architecture overview and parse flow
description: The end-to-end path of one uploaded cloud bill through billparse (intake by sha256, reading, routing, validation, service resolution, analysis, contract build, storage) and the entry points that start it (CLI, HTTP API, web API background tasks, agent).
tags: [architecture, pipeline, flow, entrypoints]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-25T20:19:27.415Z
sources:
  - id: openwiki-source-adb9568b7bddb4c8bc2d2760
    resource: repo://billparse/api.py
  - id: openwiki-source-d16efab9655dc24078395eda
    resource: repo://billparse/cli.py
  - id: openwiki-source-a70f766b6fd9cb05cb1f46aa
    resource: repo://billparse/contract.py
  - id: openwiki-source-32155ba535ca23dae2a10494
    resource: repo://billparse/pipeline.py
  - id: openwiki-source-6493e5e062ec6f788b766acc
    resource: repo://billparse/webapi.py
generated: { by: "claude-code", at: "2026-10-25T20:19:27.415Z" }
---

# Architecture overview and parse flow

billparse turns any cloud bill or cost file (AWS, Azure, GCP exports, reseller PDFs, custom Excel or JSON) into
canonical cost lines with proof of where each value is printed, a validation verdict, resolved services and a spend
analysis. New layouts are learned once as JSON specs and reused with no LLM call.

## Packages

| Package / folder | Role |
|---|---|
| `billparse/` | the pipeline, readers, spec interpreter, checks, contract, store, API, agent |
| `svcres/` | service catalog and resolver (labels and codes to canonical services), region names |
| `specs/builtin/` | built-in specs for known provider exports and invoices |
| `web/` | React web app (upload, documents, review, specs, jobs, costs, mapping) |
| `migrations/` | Alembic migrations for PostgreSQL |
| `scripts/` | catalog fetch/build, corpus check, OpenAPI export |
| `tests/`, `samples/` | tests with synthetic samples and a fake LLM |

## The flow of one file

`Pipeline.parse_bytes(data, name)` in `billparse/pipeline.py` runs everything for one file:

```
intake (sha256, cache) -> read (docmodel) -> route + extract (specs) -> clean -> validate
  -> resolve services -> validate again -> analyze -> build contract -> store -> [agent step] -> record run
```

1. **Intake.** `docmodel.load_bytes` reads the file and hashes it. If a result for that sha256 exists and `force` is
   false, the stored result is returned with `cached: true`. Otherwise the upload is saved (`Store.save_doc`).
2. **Unsupported files.** If every region is a scanned page or empty, the result is `unsupported` at once (no OCR).
3. **Route.** The routes are tried cheapest first: `builtin`, `learned`, `mapper`, `llm_spec`, `llm_extract`. Each
   attempt runs a spec through the one interpreter, cleans the rows, adds attributes and validates. The first
   accepted attempt wins; if none is accepted, the attempt with the highest confidence is used. See
   [Routing and the spec interpreter](routing-and-specs.md).
4. **Final pass.** Services are resolved on the chosen rows (`analyze.resolve_services`), a second independent
   extraction may be made for the dual-extraction check, and `validate` runs again with the resolution share. See
   [Validation, completeness and confidence](../concepts/validation-and-confidence.md).
5. **Finish** (`Pipeline._finish`): an accepted spec from the mapper or the LLM is saved as a candidate; an accepted
   spec is linked to the file (its regression set); cost lines get an analysis; the result summary is built; the
   contract document is built and saved (`contract.build`, parts `doc`, `meta`, `bundle`); rows and summary are
   saved; a `records` journal entry is written; `needs_review` and `unsupported` files go to the `review` journal.
6. **Agent step.** When the agent is on and the result is contradicted or unchecked, a job writes a spec for the
   layout and the file is parsed again with it. This runs inline. See [The spec-authoring agent](agent.md).
7. **Run record.** One run row (timings, model calls, lane, status, costs, user) is stored for the jobs page.

## Result statuses

| Status | Meaning |
|---|---|
| `reconciled` | a printed total matched the lines |
| `unreconciled` | no total to test; other checks are fine |
| `needs_review` | goes to the review queue |
| `auxiliary` | not cost lines (an inventory, a price sheet, recommendations) |
| `unsupported` | scanned PDF |
| `approved` | a person approved the corrected result (a document status set by review) |

The API and web app show these as invoice-parser's statuses (`contract.status_of`): `reconciled`, `unreconciled` and
`auxiliary` become `auto_accepted`; `needs_review` and `unsupported` become `needs_review`. The contract adds `completeness` (`reconciled`, `consistent`, `unchecked`, `contradicted`) and a confidence level.
See [Output contract](../concepts/output-contract.md).

## Entry points

| Entry | What it does |
|---|---|
| `python -m billparse parse FILES [--json] [--no-llm] [--force]` (`cli.py`) | parses files in the foreground; other commands: `nightly`, `specs`, `review`, `metrics`, `synonym`, `agent` |
| `POST /parse` (`api.py`) | parses one upload in the request and answers with the contract document and costs |
| `POST /parse/start` (`webapi.py`) | starts a background parse and answers at once; `GET /parse/tasks/{sha256}` reports progress |
| `POST /documents/{sha256}/reparse` | parses the stored file again and lets its agent job run again |
| `POST /documents/{sha256}/parse-current` | parses again with today's specs, without the agent or the LLM |

Background parses run in an in-process thread pool (`PARSE_WORKERS`, default 2). The task registry `_tasks` is in
memory: one task per sha256 (a second upload of a file being parsed joins it), and an API restart forgets running
tasks (stored results stay). This is why only one API container can run; see
`docs/new_direction/06-durable-job-queue.md` for the planned durable queue.

`webapi._pipeline` keeps one `Pipeline` per choice of models: the upload page can choose the spec route's model and
effort (`backend`) and the agent's (`fallback`), or `none` to turn them off.

## Configuration

Settings come from the environment, then `.env` (`settings.load_env`, called at the API and CLI entry points). The
main ones: `DATABASE_URL`, `PARSER_DATA_DIR`, `CATALOG_DIR`, `ANTHROPIC_API_KEY`, `BILLPARSE_MODEL`,
`BILLPARSE_USE_LLM`, `AGENT_ENABLED`, `PARSE_WORKERS`, `AUTH`. See
[Local development and operations](../operations/local-development.md) and
[Storage, database and migrations](../operations/storage-and-database.md).
