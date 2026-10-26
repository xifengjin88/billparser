---
type: testing
title: Tests, samples and the corpus check
description: The billparse test suites and what each proves, the FakeLLM pattern that keeps tests off the network, the synthetic samples and the EXPECTED table, and the local corpus check that compares real files with an accepted baseline.
tags: [tests, pytest, samples, corpus, regression]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-25T20:19:27.415Z
sources:
  - id: openwiki-source-012f2c78e3b1446dfc35803f
    resource: repo://Makefile
  - id: openwiki-source-bb125fdf93a518cfb0df4791
    resource: repo://scripts/corpus.py
  - id: openwiki-source-f0a6e7dc03522b2682f88655
    resource: repo://tests/conftest.py
  - id: openwiki-source-763f5d4170665c7c2eddf81e
    resource: repo://tests/test_agent.py
  - id: openwiki-source-5df3e5ec1d3c549ddc9a97c9
    resource: repo://tests/test_pipeline.py
generated: { by: "claude-code", at: "2026-10-25T20:19:27.415Z" }
---

# Tests, samples and the corpus check

`make test` runs `uv run pytest -q`. No test calls the network: every LLM and agent call goes to a fake with scripted
responses.

## Test isolation (`tests/conftest.py`)

`ENV_FILE` points to a file that does not exist before any module loads settings, so tests never read the developer's
`.env` (keys, agent switch, folders). An autouse fixture removes `DATABASE_URL` and `AUTH`, so tests always use a
SQLite file in a temporary store with sign-in off. Pipeline fixtures copy a catalog into a temporary `data/raw` and
reset the shared resolver (`analyze._RESOLVER`).

## Suites

| File | Proves |
|---|---|
| `test_pipeline.py` (21) | every sample parses with the expected route and total and no LLM call; layout details of the reseller PDF; cache on a second upload; Cost Explorer unpivot; LLM spec with repair loop; learned spec reused without LLM; extract-then-derive; nightly repair and regression gate; candidate promotion; spec safety; number parsing; redaction; the API; unmapped columns kept; service codes; quantities from lines; per-block prices; months as periods; provider retry; fees keep their kind |
| `test_tree.py` (13) | the rounding proof and its failure; cells give region, quantity and description; header fields; tax lines; whole-label regions; each charge type its own line; region codes; two accounts with the same amount; exchange rates; Microsoft charges by period; multi-row headers; Microsoft short product names |
| `test_agent.py` (10) | the agent loop with a scripted fake (see [The spec-authoring agent](../architecture/agent.md)) |
| `test_svcres.py` (12) | resolver cascade: codes, aliases, product names, fuzzy, candidates not guesses, synonyms, dominant provider, cache and catalog rebuild, Marketplace sellers |
| `test_webapi.py` (7) | background parse then view, source viewer, review preview and approve, review queue and delete, specs/jobs/costs, mapping and aliases, model choices per upload |
| `test_iac.py` (3) | CloudFormation and Terraform inventories |
| `test_corpus_check.py` (3) | the corpus comparison rules |

### The fake LLM

`tests/test_pipeline.py` defines `FakeLLM.complete(system, user)`: it records each prompt and returns queued spec
responses (a missing response is an invalid spec) or a fixed extraction for the extraction prompt.
`tests/test_agent.py` defines a `FakeLLM.create(...)` that plays scripted tool-use turns. A pipeline built with
`llm=None` has the LLM routes off.

## Samples and `EXPECTED`

`samples/make_samples.py` (`make samples`) writes synthetic files into `samples/files/` (ignored by git): an AWS CUR
2.0 (gzip), an Azure EA usage file, a Partner Center invoice reconciliation, a FOCUS Parquet file, a GCP JSON export, a
Cost Explorer CSV, a messy CSV, a custom workbook, a month-pivot workbook, a reseller PDF, a summary PDF, a custom
JSON, and German invoices. `EXPECTED` in `tests/test_pipeline.py` lists, per sample, the route that must read it
(`builtin` or `mapper`) and its total when one is fixed. A change must not alter these; new formats add a new
sample and a new `EXPECTED` entry.

## The corpus check (`scripts/corpus.py`)

A local check over real files (by default `~/projects/cost_engine_inputs`, or `CORPUS_DIR`). The corpus is customer
data: it is read in place, parsed into a scratch store with the LLM off, and never copied into the repository.
Reports go to `store/corpus/<time>.json` (not in git).

| Target | Does |
|---|---|
| `make corpus` | parse every file and compare with the last report |
| `make corpus-check` | compare with the accepted baseline (`store/corpus/baseline.json`); exit 1 when any file is worse or changed |
| `make corpus-accept` (`ONLY=<text>`) | accept this run as the baseline, after a person checked the changes |
| `make corpus-agent` | also run the agent on files that are not proved (costs money; keeps a store so learned specs are reused) |

Per file, the report keeps status, completeness, total, line count, sums by kind and service, and the share of lines
with each attribute. The comparison calls a file **worse** when its completeness drops, an attribute is filled less
often, an error appears, or a hand-written expectation in `store/corpus/expected.json` fails; **better** when
completeness or attribute coverage rises; and **changed** when status, total, line count or a breakdown moves (even
the same totals from other printed rows) until a person accepts it.
