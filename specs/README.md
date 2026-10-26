# Spec format

A spec is a JSON document that one trusted interpreter (`billparse/spec.py`) runs.
Built-in specs live here in `builtin/`. Learned specs are written to `store/specs/<id>/v<N>.json`.

| Key | Purpose |
|---|---|
| `id`, `version`, `trust` | `builtin`, `verified` or `candidate`. Built-ins never change automatically. |
| `detect` | `required` headers, `any_of` groups, `forbid`, `any_regex` `{pattern, min}`. Header matching ignores case and punctuation. |
| `meta` | `provider`, `kind` (`cost_lines`, `price_sheet`, `recommendations`), `cost_basis`, `format`, `notes`, `post` (steps run after the columns and derives: `azure_amortization`, see `billparse/costs.py`). |
| `source` | `frame` (CSV/JSON/Parquet), `grid` (Excel/PDF tables), `lines` (regex per text line), or `auto`. |
| `table` | Grid only: `sheet`, `start` / `end` regex anchors, `header_rows`, `multi_page`, `group_header.carry_to`, `continuation` (`merge`/`drop`), `stop_at_total`. |
| `columns` | `canonical_field: {"from": [headers], "t": [transforms], "coalesce": bool, "const": value, "header_regex": r}`. Special sources: `_group`, `_id`, `_var`, `_value`. |
| `derive` | `field: "expression"` with `+ - * /`, `coalesce`, `abs`, `round`, `fillna`, `neg`, `col("Header")` (a column of the file itself, for headers that are not names, e.g. `col("reservation/EffectiveCost")`), and `cur_effective(line_type_raw, billed_cost, ri_effective, sp_effective)` (AWS CUR effective cost by line type). A derived value is recorded as computed (`inferred`, source `derived`) unless the expression left a printed value as it was or selected a printed column. |
| `reshape` | `{"unpivot": {"id": header or "first", "exclude": regex, "total_row": regex}}` for month or service pivots. |
| `filters` | `drop_if_blank`, `drop_if_match {field, regex}`, `keep_if_in {field, values}`. |
| `fields` | Header values by anchor: `{"anchor": regex, "take": "right_of" or "below", "regex": capture}`. |
| `lines` | `[{"pattern": regex with named groups}]` for list-style PDFs. |
| `totals` | `{"from_fields": [field names]}`: which fields are stated totals. |

Transforms: `number`, `currency`, `date` (optional format), `strip`, `regex`, `map`, `charge_type`,
`json_sum`, `upper`, `strip_suffix`, `scale`, `coverage` (a line type or pricing model as `on_demand`, `reserved`,
`savings_plan`, `committed_use`, `spot` or `unknown`; tax, credit, fee and other non-usage types give nothing),
`commitment_credit` (a GCP credits list: `committed_use` when a credit is a committed use discount). Add hand-written ones to `CUSTOM_TRANSFORMS` in
`transforms.py`; LLM-written specs can only use the registered list.

Canonical fields: see `CANONICAL` in `billparse/transforms.py`. Cost types: `billed_cost`, `list_cost`,
`contracted_cost`, `effective_cost`. Commitments: `coverage`, `line_type_raw` (the provider's line type, unchanged),
`commitment_type`, `commitment_status`, `commitment_id`. One source column can feed several fields (the CUR line type
feeds `charge_type`, `line_type_raw` and `coverage`).

To add a provider format: drop a JSON file in `builtin/`, add a sample to `samples/`, add it to
`EXPECTED` in `tests/test_pipeline.py`.
