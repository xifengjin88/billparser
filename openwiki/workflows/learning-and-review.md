---
type: workflow
title: Learning loop, spec lifecycle and human review
description: How learned specs are created, versioned, verified, repaired and disabled; the regression gate every new version must pass; the learning run and its metrics; and how a person's review corrects a document, approves it and teaches the system.
tags: [learning, specs, regression-gate, review, synonyms]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-25T20:19:27.415Z
sources:
  - id: openwiki-source-adb9568b7bddb4c8bc2d2760
    resource: repo://billparse/api.py
  - id: openwiki-source-4fb9e3e35092ff9d2ea970b0
    resource: repo://billparse/learn.py
  - id: openwiki-source-1393cdedeb3210d0b08caead
    resource: repo://billparse/review.py
  - id: openwiki-source-6493e5e062ec6f788b766acc
    resource: repo://billparse/webapi.py
generated: { by: "claude-code", at: "2026-10-25T20:19:27.415Z" }
---

# Learning loop, spec lifecycle and human review

billparse improves in three ways: new layouts get specs (the mapper, the LLM routes, the agent), specs are repaired
when a file breaks them, and people correct results in review. Every change to a spec is guarded by the
**regression gate**.

## Spec lifecycle

Learned specs live in the database (`spec`, `spec_version`); every save is a new version and nothing is overwritten
([Storage, database and migrations](../operations/storage-and-database.md)).

| Trust (stored) | Status (API / web) | How a spec gets there |
|---|---|---|
| `builtin` | `trusted` | files in `specs/builtin/`; never changed automatically |
| `candidate` | `draft` | saved by the pipeline when a `mapper`, `llm_spec` or `llm_extract_derived` attempt is accepted; saved by the agent when it proves a file; saved by a person with `PUT /specs/{id}` without a status |
| `verified` | `trusted` | promotion after enough clean files, a review approval with no charge changed, or a person |
| `disabled` | `disabled` | a person (`PATCH /specs/{id}/status`); kept but never used |

Each accepted parse links the spec to the file (`Store.link`): the file, its row count, total and confidence join the
spec's regression set, and the layout fingerprint is recorded so the learned route finds the spec again.

Built-in specs cannot be edited through the API (save a copy under a new id); their status is kept in
`spec_status.json`. The CLI has `python -m billparse specs [list | show ID | approve ID | rollback ID VERSION | test ID]`.

## The regression gate (`learn.regression_gate`)

A new version of a spec runs on every stored file the spec was accepted on. It fails when, for any file:

- the row count differs by more than max(1, 1%);
- the total differs by more than max(0.05, 0.1%);
- the confidence drops by more than 0.05;
- the spec raises an error.

The gate is used by the learning run's repairs and by the agent's repairs. A rejected version is not saved; the old
version stays current.

## The learning run (`learn.run_nightly`)

Started by `python -m billparse nightly`, `make nightly` or `POST /nightly` (it is a manual or scheduled run; nothing
in the app schedules it):

1. For every open review item with a stored file: if a learned spec matched the file but failed, or (when no learned
   spec matched) the closest learned spec by fingerprint (similarity ≥ 0.5), ask the LLM to repair that spec with the
   validator's feedback (`repair_spec`). The repair must make the failing file pass and then pass the regression gate;
   only then is it saved as a new candidate version.
2. Parse the file again (`force=True`) with the larger spec library; the old review entries are replaced.
3. Promote candidates to verified: 3 or more linked files with confidence ≥ 0.75 (`promote_candidates`).
4. Write a report with `metrics` to `<data dir>/nightly/<time>.json`.

`metrics` (also `GET /metrics`) counts, over the latest record per file: accepted share, reconciled share, first-route
success, share with no LLM call, average LLM calls, and files per route. These should improve over time.

The agent is the other learning path; it runs inline during a parse
([The spec-authoring agent](../architecture/agent.md)).

## Human review (`billparse/review.py`, web Review pages)

Files with status `needs_review` or `unsupported` enter the `review` journal; `GET /review/queue` lists them, most
money at risk first.

A reviewer can edit charges (description, cost, kind, service code, counted or not), delete or add charges, and edit
header fields (document type, provider, currency, period). `POST /documents/{sha}/review/preview` applies the edits
and re-tests the totals (`contract.rereconcile`) without saving. `POST /documents/{sha}/review/approve` needs every
checklist item ticked (totals tie, unclaimed amounts resolved, repeated summary rows not counted, header right, three
lines spot-checked) and then:

1. saves each corrected service code as a **synonym** for the charge's label (with the document's provider), so the
   same label resolves next time;
2. stores the corrected document as the **gold** part and its charges as the approved rows; the document's status
   becomes `approved` and its view shows the gold version;
3. marks the candidate spec that read the file **verified** when no charge was deleted, added, re-priced, re-kinded or
   re-counted.

Service aliases can also be managed directly: the Mapping page (`/mapping`, `/aliases`), the CLI
(`python -m billparse synonym "VM Hrs" "azure:Virtual Machines"`) or `scripts/add_synonym.py`.
