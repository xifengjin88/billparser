---
type: concept
title: "Output contract: documents, charges and cites"
description: The Document schema billparse returns (charges, sections, stated totals, commitments, facts, gaps, completeness, confidence; every value with a cite), how contract.build fills it from parsed rows, how totals are re-tested, and the strict export projection.
tags: [schema, contract, cites, charges, completeness]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-25T20:19:27.415Z
sources:
  - id: openwiki-source-a70f766b6fd9cb05cb1f46aa
    resource: repo://billparse/contract.py
  - id: openwiki-source-615a3d6f51c128c10b367f2e
    resource: repo://billparse/export.py
  - id: openwiki-source-32155ba535ca23dae2a10494
    resource: repo://billparse/pipeline.py
  - id: openwiki-source-1b42dcf932567d92960679e8
    resource: repo://billparse/schema.py
generated: { by: "claude-code", at: "2026-10-25T20:19:27.415Z" }
---

# Output contract: documents, charges and cites

Every parse is also saved as a **contract document**: the output format of invoice-parser, so the same web app and
clients work. The model is in `billparse/schema.py` (`SCHEMA_VERSION = "1.3"`), the builder in
`billparse/contract.py`, and the strict public projection in `billparse/export.py`.

## Cites: every value keeps its proof

A value is a `CitedValue[T]`: `value`, `cite`, `origin` (`printed` or `inferred`), and optional `confidence` and
`source` (`rule`, `mapping:<method>`, …). A `Cite` says where the value is printed: `page` + `line` for PDFs and text,
`table` + `row` (+ `column`) for sheets and exports, `path` for JSON or Terraform, and the printed `text` for review.

`contract.Locator` turns a row's `_src` (`page3:12`, `Sheet1:15`, `a.zip:b.xlsx:Sheet1:15`) into a cite: a page region
gives a page number and line (and the line's text), any other region gives a table and row.

## The Document

| Field | Content |
|---|---|
| `source_name`, `sha256`, `page_count` | the file |
| `doc_type` | `invoice` (PDF, docx, text), `cost_report`, `estimate` (price sheet), `inventory`, `other` (recommendations) — inferred |
| `provider`, `currency` | the most common value on the lines (or the currency the tree found) |
| `billing_period_start` / `_end` | min and max line dates, else a printed "Billing period" header field |
| `exchange_rate` | a printed rate such as `1 USD = 1.41 CAD` |
| `charges` | the lines (below) |
| `sections` | the headings above lines (`_path` "A > B > C") as nested sections |
| `stated_totals` | printed totals with level `grand`, `section` or `subtotal`, status `match` / `mismatch` / `untested` |
| `commitments` | charges grouped by `commitment_id` (type inferred from the text: savings plan, reserved instance, commitment) |
| `stated_facts` | amounts printed in another currency, and the `rounding` fact of a tree proof |
| `gaps` | `unclaimed_amount`, `unreconciled_total`, `missing_match_field`, … |
| `completeness` | `reconciled`, `consistent`, `unchecked`, `contradicted` |
| `confidence` | `high`, `medium` or `low`, with reasons |

## Charges

`contract._charge` turns one row into a `Charge`:

- `description` (prefixed with its heading when the line sits under a service: "AWS Lambda - Charges"), `cite`,
  `section_id`;
- `cost` = `billed_cost` with `basis: "billed"` for every charge;
- `kind` from the charge type (`kind_of`): `Purchase` → `commitment_fee` (or `marketplace_fee`), `Tax` → `tax`,
  `Credit`/`Refund` → `discount_or_credit`, `Support` → `support_fee`, `Fee`/`Adjustment` → `other`, anything else →
  `usage` (or `marketplace_usage` when the text names a Marketplace);
- `context`: service (printed, or inferred from the catalog mapping), region (the code; inferred when it came from a
  printed name), account;
- typed fields: `service_code`, `pricing_model`, `commitment_id`, `instance_type`, `operating_system`,
  `database_engine`, `deployment_option`, `usage_type`, `unit`, `quantity`, `unit_price`, `period_start`/`_end`,
  `sku_id` (from `product_code`);
- `attributes`: everything else (`service_raw`, `category`, `charge_type`, `region_name`, `effective_cost`,
  `list_cost`, `credits`, `tax`, `invoice_id`, the unmapped `_extra` columns, the heading path as `under`, …).

Only one cost type is typed today; list and effective cost survive only as untyped attributes. Typed cost types and
commitment coverage are planned in `docs/new_direction/01-cost-types-and-coverage.md`.

### Roll-up of very large files

When a file has more than `CONTRACT_MAX_CHARGES` (default 50,000) lines, lines alike in every `ROLLUP_KEYS`
dimension (service, region, account, charge type, pricing model, instance type, OS, engine, deployment, usage type,
unit, commitment id, currency, provider) become one charge: costs and quantities summed, earliest start, latest end,
and an `attributes.lines` count. A flag on the document says so.

## Totals, gaps and the re-test

`_totals` adds each printed total a route found (not another route's reading, and never the LLM's own sum), the tree's
failed subtotals (`mismatch`), the subtotals two breakdowns agree on (`match`), and the subtotals the lines were read
under. If the tree proved the lines, a `rounding` stated fact records the cents between the printed total and the
lines. `_gaps` adds unexplained printed amounts, mismatched subtotals, and usage charges with a non-zero cost and no
service code (at most 500).

`rereconcile(doc)` tests the grand totals against the counted charges:

- with a tree proof: the total must equal lines + rounding within half a cent;
- otherwise: the total may equal all charges, the charges without tax, or the charges plus the printed tax, within
  `max(0.05, 0.05% of the sum)` but never more than half a cent per charge;
- with no grand total, subtotals that together equal the non-tax charges also prove the document.

Then completeness is `contradicted` if any total mismatches, `reconciled` if one matched, else the parse's own
completeness (`consistent` / `unchecked`). `build` never gives a better verdict than the parse: if the parse said
`contradicted`, the document stays `contradicted` with low confidence. `confidence_for` gives `high` only for a
reconciled document with no gaps, `medium` for a consistent one whose only problems are missing service codes, else
`low`. Review calls `rereconcile` again after a person's edits.

`money_at_risk` sums gap amounts and the counted charges that gaps point at.

## Meta and source bundle

`build` also returns `meta` (lane, the spec used with version, timings, flags, notes from failed and warned checks,
attempts) and the source **bundle** (each PDF page's lines with boxes, each table's first 5,000 rows) that the
source viewer reads. The pipeline saves all three as store parts `doc`, `meta` and `bundle`.

## Strict export

`export.to_schema(doc)` projects the internal document onto the agreed contract with `extra="forbid"` models: no
internal fields (confidence and source of each value, total status, gap refs) leak. Those go into `export.sidecar`
for review. `POST /parse` returns this strict form. `export.json_schema()` gives its JSON Schema.

## Versioning

`SCHEMA_VERSION` is written on every document (1.2 added `database_engine` and `deployment_option`; 1.3 added
`confidence`). Changes so far only add fields and bump the version, and the comment beside the constant lists them.
