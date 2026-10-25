---
type: architecture
title: Rule mapper and layout fingerprints
description: How billparse proposes specs without an LLM (header rules plus value profiles, month pivots, inventories, list-style PDF lines, the tree of sums), how layout fingerprints route files to learned specs, and how attributes such as instance type are read from line text.
tags: [mapper, rules, fingerprint, attributes, routing]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-25T20:19:27.415Z
sources:
  - id: openwiki-source-728b85873bb98f7903805e06
    resource: repo://billparse/attributes.py
  - id: openwiki-source-818848ac6ce1a1a404e45508
    resource: repo://billparse/fingerprint.py
  - id: openwiki-source-2a8fc8a35e3d97e85c94f33f
    resource: repo://billparse/mapper.py
  - id: openwiki-source-32155ba535ca23dae2a10494
    resource: repo://billparse/pipeline.py
generated: { by: "claude-code", at: "2026-10-25T20:19:27.415Z" }
---

# Rule mapper, layout fingerprints and attribute extraction

The rule mapper (`billparse/mapper.py`) is route 3 in the ladder (see
[Routing and the spec interpreter](routing-and-specs.md)). It proposes specs from rules only, before any LLM call.
Its output is an ordinary spec, so an accepted proposal is saved and reused like a learned spec.

## Proposals (`propose_all`)

1. If the document has PDF or text pages, the first proposal is the **tree of sums** spec
   (`{"source": "tree"}`, confidence 0.7), which reads totals, subtotals and lines from their amounts
   ([Messy layouts and the tree of sums](layout-and-tree.md)).
2. Then `propose(doc)`: the best spec over all regions. For each frame or grid table it tries, in order:
   - a **month pivot**: 3 or more month-like headers (`Jan`, `2026-01`, `01/2026`) and a text label column become an
     `unpivot` spec (services × months to long rows, the month in `charge_start`), confidence 0.8;
   - a **column mapping** with `map_columns` (below);
   - an **inventory**: no cost column but instance type, OS or engine columns: `meta.kind: inventory`,
     confidence 0.6.

   Between near-equal candidates (within 0.05), the table that fills more fields, then has more rows, wins (the
   detail, not a pivot of it). Grid samples skip total rows and bold group rows so they do not skew the profiles.
3. For PDFs where the best confidence is below 0.6, **list-style lines**: four `LINE_PATTERNS` (label + dots +
   amount, label + quantity + unit + unit price + amount, label + currency amount, label + amount + currency); the
   pattern that matches most money lines wins, with confidence `0.45 + 0.4 × coverage` (at most 0.85), and the
   invoice total is read from a "Total due / Amount due / Grand total" anchor.

## Column mapping (`map_columns`)

Each (column, canonical field) pair is scored from two kinds of evidence:

- **Header words** (`HEADER_RULES`): weighted regexes per field (for example `unit price|unit cost|effective price`
  for `unit_price`, `reservation id|savings plan arn|benefit id` for `commitment_id`). CamelCase headers are split
  (`ChargeType` reads as `charge type`). `NEGATIVE` regexes lower a score (a header with `unit` or `rate` is not
  `billed_cost`).
- **Value profile** (`profile`): share of numbers, currency-looking values, dates, codes, letters, distinct values
  and average length over up to 200 sample values. `value_score` rewards a fitting profile and strongly penalizes a
  wrong one (text in a money field, numbers in a text field).

Only pairs whose header score is above 0 are considered, so a column is proposed only when its header matches a
rule. Pairs are taken greedily from the best score (minimum 2.0), one field per column. If no cost column is found,
the rightmost money-looking column is used. Confidence is 0.5 with a cost column, plus 0.25 with a service or
description column, plus up to 0.25 from the average score.

`_spec_from_mapping` builds the spec: one `from` header per field, `detect.required` from the cost and label headers,
a `drop_if_blank: billed_cost` filter, and for grids a carried group header that becomes `service_raw` when the table
has no service column.

The same scorer also fills empty fields from columns a spec did not map (`spec._keep_unmapped`).

## Layout fingerprints (`billparse/fingerprint.py`)

`fingerprint(doc)` returns:

| Key | Content |
|---|---|
| `hash` | first 16 hex characters of SHA-1 over file type, region kinds and the sorted normalized table headers |
| `headers` | sorted normalized headers of up to 4 regions (generated `col N` names left out) |
| `words` | words of 4+ letters in the first 8 rows of the first grid region (not numbers) |
| `file_type` | the document's file type |

`similarity(a, b)` is 0 between a PDF and a tabular file; otherwise `0.75 × Jaccard(headers) + 0.25 ×
Jaccard(words)` (only headers or only words when one side is missing). The fingerprint is used twice:

- **routing**: an exact hash match or a similarity ≥ 0.8 sends a file to a learned spec (`Pipeline._learned`);
- **examples**: the most similar saved specs are shown to the LLM and the agent.

Each accepted spec is linked to the files it read together with their fingerprints (`Store.link`), which is also the
spec's regression set.

## Attributes from line text (`billparse/attributes.py`)

Bills often print details inside descriptions ("$0.7424 per RDS db.m7g.xlarge Multi-AZ instance hour running MySQL
1,176 Hrs"). `attributes.extract(rows)` reads them from each line's `description`, `service_raw`, `product_code` and
context path, and **only fills fields that are empty**, so a column in the file always wins:

| Field | Read by |
|---|---|
| `instance_type` | AWS types (with `db.`, `cache.`, `search.` prefixes), Azure sizes (`Standard_…`/`Basic_…`), GCP machine types |
| `database_engine`, `operating_system`, `deployment_option`, `pricing_model` | ordered word lists |
| `usage_type` | AWS usage types with a region prefix (`USE1-BoxUsage:m5.xlarge`) |
| `quantity`, `unit` | a number and a known unit at the end of a text (`4,216.07 Hrs`) |
| `unit_price`, `price_per` | "$0.05 per …" with its block size ("per 1,000 requests") |

Tax lines get no quantity or price. `normalize_regions` turns printed region names into codes and keeps the printed
name in `region_name`.

Extraction runs only for files up to 100,000 rows (`pipeline.ATTRIBUTE_ROWS`), on the assumption that large exports
have their own attribute columns. The Azure pattern needs the `Standard_` prefix, so meter names such as `D4s v5` do
not match. Both are open items in `docs/new_direction/02-builtin-attribute-columns.md`.
