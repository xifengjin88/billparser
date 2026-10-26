---
type: quickstart
title: billparse quickstart
description: What billparse does, how to run and test it, and which wiki page to read for each kind of task.
tags: [quickstart, overview, routing-map]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-25T20:19:27.415Z
sources:
  - id: openwiki-source-32155ba535ca23dae2a10494
    resource: repo://billparse/pipeline.py
  - id: openwiki-source-b79fbbd921df689b4bbdc82f
    resource: repo://docker-compose.yml
  - id: openwiki-source-012f2c78e3b1446dfc35803f
    resource: repo://Makefile
  - id: openwiki-source-05ccef8d4cf1698187f20464
    resource: repo://pyproject.toml
  - id: openwiki-source-23775c3de52f3ab95a13cb8b
    resource: repo://README.md
generated: { by: "claude-code", at: "2026-10-25T20:19:27.415Z" }
---

# billparse quickstart

billparse parses any cloud bill or cost file (AWS, Azure and GCP exports, FOCUS, reseller and provider PDF invoices,
custom Excel, CSV or JSON, and CloudFormation/Terraform inventories) into canonical cost lines. Every value keeps a
cite to where it is printed, every result has a validation verdict (proved against the printed totals when possible),
services are resolved to a catalog, and a spend analysis is computed. New layouts are learned once as JSON specs and
then read with no LLM call.

Python ≥ 3.12 package `billparse` (with `svcres/` for service resolution), FastAPI API, React web app, SQLModel on
SQLite or PostgreSQL, optional Claude calls through the Anthropic API.

## Run it

```bash
cp example.env .env          # optional: ANTHROPIC_API_KEY, AGENT_ENABLED, AUTH, ...
make up                      # Docker: web app http://localhost:8090, API http://localhost:8010/parse
make dev                     # the same with hot reload
```

Without Docker:

```bash
uv sync --extra dev
python samples/make_samples.py
cp tests/fixtures/raw/*.json data/raw/
python -m billparse parse samples/files/* --no-llm
make api-local               # API on :8000 (SQLite)
make web-local               # web app on :5173
```

## Check a change

| Command | Use |
|---|---|
| `make test` | all tests; no network (fake LLM) |
| `make lint`, `make typecheck` | Ruff and oxlint; web TypeScript |
| `make web-types` | after any API response change |
| `make corpus-check` | compare real local files with the accepted baseline (customer data: never copy it into the repo) |

## Where to read next

| Task | Page |
|---|---|
| Understand the whole flow of one file and its entry points | [Architecture overview and parse flow](architecture/overview.md) |
| A file type is read wrongly (PDF, Excel, CSV, JSON, zip) | [Reading files into the document model](architecture/document-model.md) |
| Which spec reads a file, acceptance, the spec format, built-in specs | [Routing lanes and the spec interpreter](architecture/routing-and-specs.md) |
| PDF bills, subtotals, the exact total proof, account/service/region of a line | [Messy layouts, the tree of sums and line context](architecture/layout-and-tree.md) |
| Rule-based column mapping, fingerprints, instance type from text | [Rule mapper and layout fingerprints](architecture/rule-mapper.md) |
| LLM spec generation, LLM extraction, cost and models | [LLM spec generation and extract-then-derive](architecture/llm-and-induction.md) |
| The agent that writes specs, its jobs, limits and traces | [The spec-authoring agent](architecture/agent.md) |
| Why a file is reconciled, needs review or is contradicted | [Validation, completeness and confidence](concepts/validation-and-confidence.md) |
| The output document, charges, kinds, cites, totals and gaps | [Output contract](concepts/output-contract.md) |
| Service names and codes, categories, the analysis numbers | [Service resolution, attributes and analysis](concepts/services-and-analysis.md) |
| CloudFormation and Terraform files | [Infrastructure-as-code inventory](concepts/iac-inventory.md) |
| Spec versions, the regression gate, the learning run, review and approval | [Learning loop, spec lifecycle and human review](workflows/learning-and-review.md) |
| Endpoints, background parses, sign-in, the web app | [HTTP API, web app and sign-in](integrations/http-api-and-web-app.md) |
| Docker, Make targets, environment variables | [Local development and operations](operations/local-development.md) |
| Tables, files, Store API, migrations | [Storage, database and migrations](operations/storage-and-database.md) |
| Tests, samples, `EXPECTED`, the corpus check | [Tests, samples and the corpus check](testing/tests-and-corpus.md) |

## Planned work

`docs/new_direction/01…09-*.md` describe the next tasks: typed cost types and commitment coverage, attribute and
commitment columns in built-in specs, a price catalog and price matching, price-based checks, a durable job queue,
large-file reading, a learned header dictionary, and structured LLM output.
