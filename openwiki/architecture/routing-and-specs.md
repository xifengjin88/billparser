---
type: architecture
title: Routing lanes and the spec interpreter
description: How billparse chooses a reading for a file (built-in spec, learned spec, rule mapper, LLM spec, LLM extract), when an attempt is accepted, and how the single spec interpreter runs a JSON spec (detect, sources, columns, transforms, derive, reshape, filters, fields, totals).
tags: [routing, specs, interpreter, transforms, builtin]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-25T20:19:27.415Z
sources:
  - id: openwiki-source-68d86a9228fc3b781e0f2cae
    resource: repo://billparse/expr.py
  - id: openwiki-source-32155ba535ca23dae2a10494
    resource: repo://billparse/pipeline.py
  - id: openwiki-source-fa3b516cbac4d1d454de7725
    resource: repo://billparse/spec.py
  - id: openwiki-source-c9b0d4adb896b06d8a75bc74
    resource: repo://billparse/store.py
  - id: openwiki-source-1aefa65cced5d5fd3d5971cf
    resource: repo://billparse/transforms.py
generated: { by: "claude-code", at: "2026-10-25T20:19:27.415Z" }
---

# Routing lanes and the spec interpreter

Every reading of a file is a **spec**: a JSON document that one trusted interpreter (`billparse/spec.py`) runs. No
generated code ever runs. Specs come from four places: built-in files, learned specs saved in the database, the rule
mapper, and the LLM (or the agent). The pipeline tries them in a fixed order.

## The routing ladder

`Pipeline._parse` (`billparse/pipeline.py`) tries routes cheapest first. Each route is a generator of `Attempt`s, and
the first accepted attempt ends the ladder:

| Order | Route | Where | What it tries |
|---|---|---|---|
| 1 | `builtin` | `_builtin` | the built-in spec with the highest `detect_score` against the file's headers and text (if above 0) |
| 2 | `learned` | `_learned` | up to 3 learned specs: header/text detect matches, the exact layout fingerprint, or fingerprint similarity ≥ `reuse_similarity` (0.8) |
| 3 | `mapper` | `_mapper` | every rule-based proposal from `mapper.propose_all` ([Rule mapper](rule-mapper.md)) |
| 4 | `llm_spec` | `_llm_spec` | LLM-written specs with repair rounds |
| 5 | `llm_extract` | `_llm_extract` | LLM rows, then a derived spec or the rows directly ([LLM routes](llm-and-induction.md)) |

Disabled specs are never used: a learned spec with `trust: disabled` is skipped, and a built-in can be disabled by a
person (`spec_status.json` in the data directory).

If no attempt is accepted, the attempt with the highest confidence is kept, and validation decides its status
(usually `needs_review`).

### One attempt (`Pipeline._try`)

1. `spec.run(spec, doc)` extracts rows.
2. For non-tree specs, totals found by earlier routes can be added as `document_total`s, so a mapper or LLM reading is
   tested against the total the tree of sums found.
3. `validate.clean_rows` removes total and blank rows; `attributes.normalize_regions` sets region codes.
4. `attributes.extract` fills attributes from line text (only for files up to 100,000 rows, `ATTRIBUTE_ROWS`).
5. `validate.validate` gives the verdict, unless the spec's `meta.kind` is not `cost_lines` (price sheets,
   recommendations, inventories), which are `auxiliary`.

Any exception becomes an `error` attempt with its message, so one bad spec never stops the ladder.

### Acceptance (`Pipeline._accept`)

An attempt is accepted when it produced rows, its status is not `error` or `needs_review`, and its confidence is at
least `builtin_accept` (0.5) for a built-in or `accept_confidence` (0.7) for any other route. An `auxiliary` attempt
is accepted when it has rows.

## Detection

`detect_score(spec, headers, text)` is 0 when the spec cannot apply, else higher for a better match:

- `detect.text`: a regex that must match the text of the first pages (for PDFs and text, which have no table header);
  alone it scores 2.0;
- `required`: every header must be present (matching ignores case and punctuation via `layout.norm_h`);
- `any_of`: each group needs one of its headers; `forbid`: none of these may be present;
- `any_regex`: at least `min` headers match a pattern.

More required headers score higher, so the more specific spec wins a tie.

## Running a spec (`spec.run`)

| `source` | Reader |
|---|---|
| `tree` | the tree of sums plus line context ([Messy layouts and the tree of sums](layout-and-tree.md)) |
| `iac` | CloudFormation / Terraform inventory ([IaC inventory](../concepts/iac-inventory.md)) |
| `lines` | a regex per text line, with named groups as fields |
| `grid` | a located table in sheets or PDF pages (`layout.extract_table`); `meta.kind: inventory` reads every row under the header |
| `frame` (default) | the frame regions whose headers match the spec best, concatenated, each row with its `_src` |

After extraction, a frame/grid/lines spec goes through:

1. **`reshape.unpivot`**: a month or service pivot becomes long rows (`_id`, `_var`, `_value`); a `total_row` regex
   turns total rows into stated totals.
2. **`columns`**: each canonical field reads the first matching header in `from` (or `header_regex`), or a `const`;
   `coalesce` takes the first non-blank of several columns. Transforms in `t` run in order; without `t`, the defaults
   apply (`number` for money and quantities, `date` for dates, `strip` + `charge_type` for charge types, `strip`
   otherwise). A missing `required` column adds a warning.
3. **Unmapped columns are kept**: a header the mapper's rules know fills a field the spec left empty (never a money
   column: `billed_cost`, `tax`, `unit_price`); every other non-empty column goes into the line's `_extra` text as
   `Header: value | ...`.
4. **`derive`**: expressions in `billparse/expr.py` (`+ - * /`, unary minus, `coalesce`, `abs`, `round`, `fillna`,
   `neg`), parsed with `ast` and walked by hand, never `eval`.
5. **`filters`**: `drop_if_blank`, `drop_if_match {field, regex}`, `keep_if_in {field, values}`.
6. **`meta`**: `meta.provider` fills `provider` when the spec does not map it; `meta.currency` fills an empty
   currency column.
7. **Totals**: `fields` reads header values by anchor (`right_of` or `below`); `totals.from_fields` makes them stated
   totals. If a spec states no total, `_auto_totals` looks for a label such as "Total due" followed by an amount
   anywhere in the grid regions (including JSON metadata).

## Checking untrusted specs (`check_spec`)

Specs from the LLM or the agent are checked before they run: only known top-level keys, only canonical fields
(`transforms.CANONICAL`), only registered transforms (`transforms.TRANSFORMS`; hand-written ones go in
`CUSTOM_TRANSFORMS` and the LLM cannot add any), valid derive expressions, compilable regexes, valid `tree` and
`context` options, and at least one mapped column (or a `tree`/`iac` source).

## Built-in specs

`specs/builtin/*.json` (loaded by the store at start, `trust: builtin`, never changed automatically):

| Spec | Kind | Detected by |
|---|---|---|
| `aws-cur2`, `aws-cur-legacy` | cost lines | required CUR headers |
| `aws-cost-explorer-csv` | cost lines | header regex |
| `aws-invoice-pdf`, `azure-mca-invoice-pdf`, `azure-product-invoice-pdf` | cost lines (`tree` source) | text regex |
| `azure-usage-details`, `azure-cost-analysis-export`, `azure-pc-daily-rated-usage`, `azure-pc-invoice-recon` | cost lines | headers |
| `focus` | cost lines (any provider) | FOCUS headers |
| `gcp-billing-export`, `gcp-cost-table` | cost lines | headers |
| `azure-price-sheet` | price sheet | headers |
| `aws-cost-optimization-recommendations`, `azure-reservation-recommendations` | recommendations | headers |
| `iac` | inventory (`iac` source) | text regex |

The README notes that built-in column names come from public documentation and are tested on synthetic files only.
To add a provider format: add a JSON file here, a sample generator in `samples/make_samples.py`, and an entry in
`EXPECTED` in `tests/test_pipeline.py`. The full key table is in `specs/README.md`.
