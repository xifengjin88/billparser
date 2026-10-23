# Spec format

A spec is a JSON document that one trusted interpreter (`billparse/spec.py`) runs.
Built-in specs live here in `builtin/`. Learned specs are written to `store/specs/<id>/v<N>.json`.

| Key | Purpose |
|---|---|
| `id`, `version`, `trust` | `builtin`, `verified` or `candidate`. Built-ins never change automatically. |
| `detect` | `required` headers, `any_of` groups, `forbid`, `any_regex` `{pattern, min}`. Header matching ignores case and punctuation. |
| `meta` | `provider`, `kind` (`cost_lines`, `price_sheet`, `recommendations`), `cost_basis`, `format`, `notes`. |
| `source` | `frame` (CSV/JSON/Parquet), `grid` (Excel/PDF tables), `lines` (regex per text line), or `auto`. |
| `table` | Grid only: `sheet`, `start` / `end` regex anchors, `header_rows`, `multi_page`, `group_header.carry_to`, `continuation` (`merge`/`drop`), `stop_at_total`. |
| `columns` | `canonical_field: {"from": [headers], "t": [transforms], "coalesce": bool, "const": value, "header_regex": r}`. Special sources: `_group`, `_id`, `_var`, `_value`. |
| `derive` | `field: "expression"` with `+ - * /`, `coalesce`, `abs`, `round`, `fillna`. |
| `reshape` | `{"unpivot": {"id": header or "first", "exclude": regex, "total_row": regex}}` for month or service pivots. |
| `filters` | `drop_if_blank`, `drop_if_match {field, regex}`, `keep_if_in {field, values}`. |
| `fields` | Header values by anchor: `{"anchor": regex, "take": "right_of" or "below", "regex": capture}`. |
| `lines` | `[{"pattern": regex with named groups}]` for list-style PDFs. |
| `totals` | `{"from_fields": [field names]}`: which fields are stated totals. |

Transforms: `number`, `currency`, `date` (optional format), `strip`, `regex`, `map`, `charge_type`,
`json_sum`, `upper`, `strip_suffix`, `scale`. Add hand-written ones to `CUSTOM_TRANSFORMS` in
`transforms.py`; LLM-written specs can only use the registered list.

Canonical fields: see `CANONICAL` in `billparse/transforms.py`.

To add a provider format: drop a JSON file in `builtin/`, add a sample to `samples/`, add it to
`EXPECTED` in `tests/test_pipeline.py`.
