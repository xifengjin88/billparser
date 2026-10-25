# Task 10: Header dictionary and value-first column detection

**Order:** last. **Needs:** 01 and 02 (fields), 04 (price catalog, to confirm instance types and SKUs).
**STOP gate:** none.
**Read first:** `billparse/mapper.py` in full, `spec._keep_unmapped`, `svcres/resolver.py` (how service
synonyms are learned and stored), `billparse/review.py`, and the review approve endpoint in `webapi.py`.

## In one sentence

Let the rule mapper read new layouts, including non-English ones, without the LLM more often, by learning column
headers from approved results and by recognizing columns from their values.

## What exists today (verified)

- `mapper.HEADER_RULES`: hard-coded English regexes per canonical field. `header_score(field, header)` checks them.
- `mapper.map_columns`: a column is considered for a field only when its header matches a rule
  (`if hs <= 0: continue`). `value_score` only adjusts the score afterwards and never proposes a field on its own.
  So a column of `m5.xlarge` values under an unknown or foreign header is never mapped.
- No header rules for `effective_cost`, `list_cost` or `contracted_cost`. The header `effective price` matches
  `unit_price`.
- Learning exists for **service labels** only: `svcres.Resolver.add_synonym` writes `CATALOG_DIR/synonyms.json`.
  Approving a review saves service picks as synonyms (see `webapi` review approve). Nothing learns column headers.
- `samples/files/german_invoice.xlsx` without the LLM: the mapper route gives `needs_review` (confidence 0.6,
  4 rows). The tests make it pass only with an LLM spec (`GERMAN_SPEC` in `tests/test_pipeline.py`).

## Steps

1. **Seed dictionary** `data/header_synonyms.seed.json` (in git):
   `{"<canonical field>": {"<language>": ["header", ...]}}` for `billed_cost`, `quantity`, `unit`, `unit_price`,
   `service_raw`, `description`, `region`, `account_id`, `currency`, `charge_start`, `charge_end`, `tax`,
   `invoice_id`, `list_cost`, `effective_cost`, `contracted_cost`.
   - Languages: German, French, Spanish, Portuguese, Italian, Dutch and Japanese, plus English variants the rules
     miss.
   - Write the entries carefully; list in the report any you are unsure of, for the user to check.
   - Normalize headers with `layout.norm_h`.

2. **Learned dictionary** `CATALOG_DIR/header_synonyms.json` (not in git), the same pattern as
   `svcres` `synonyms.json`. Structure: `{"<norm header>": {"field": f, "count": n, "source": "spec|review",
   "rejected": [fields]}}`. Write it through a small class `billparse/headers.py` `HeaderDictionary` with
   `lookup(header) -> field | None`, `learn(header, field, source)` and `reject(header, field)`. Use a lock and an
   atomic write, as `Resolver.flush` does.

3. **Scoring** (`mapper.header_score`):
   - Check the learned dictionary first, then the seed, then `HEADER_RULES`.
   - A dictionary hit scores 3.5, above every rule, unless that field is in the header's `rejected` list.
   - Add rules for the cost types: `effective_cost` ← `amortized cost|effective cost`, `list_cost` ←
     `list cost|on demand cost|public cost`, `contracted_cost` ← `contracted cost`.
   - Change `effective price` to map to a new canonical field `effective_unit_price` (add it to `CANONICAL`,
     `NUMERIC` and `spec.NEVER_ADDED`), not to `unit_price`, when the file also has a plain unit price column.

4. **Value-first detection** (`mapper.py`): add a set of value detectors that may propose a field even when the
   header matches nothing. Each runs on the 200 sample values `_samples_frame` already takes. A detector that matches
   at least 80% of non-empty values proposes its field with score 3.0.

   | Field | Detector |
   |---|---|
   | `instance_type` | `attributes.instance_type(v)` is not None; when the price catalog is loaded, at least half the distinct values exist in it |
   | `region` | `svcres.regions.lookup(v)` is not None |
   | `account_id` | 12-digit number (AWS), GUID (Azure subscription), or GCP project ID pattern `^[a-z][a-z0-9-]{4,28}[a-z0-9]$` together with a header that is not a rule match for another field |
   | `currency` | ISO 4217 three-letter code (keep a set in the module) |
   | `usage_type` | `attributes.USAGE_TYPE` matches |
   | `meter_id` | GUID, when the provider is Azure and the column is not `account_id` |
   | `sku_id` | when the price catalog is loaded, at least half the distinct values are catalog SKUs |

   Where a header rule and a detector disagree, the higher score wins. Ties go to the header.

5. **Learning.**
   - When a learned spec is promoted to verified (`learn.promote_candidates`), or a review is approved with no
     edited lines, call `HeaderDictionary.learn` for each `columns[field]["from"][0]` header of the spec, with source
     `spec` or `review`.
   - When a reviewer changes a line's field mapping, record the old pair with `reject`.
   - Add `GET /headers`, `POST /headers` and `DELETE /headers/{norm}` in `webapi.py`, and a section on the Mapping
     page that lists, adds and deletes entries, like the `/aliases` endpoints.

6. **LLM only for the leftovers** (`pipeline._llm_spec`, `llm.generate_spec`): pass the columns the mapper already
   settled with high scores (≥ 3.0) as fixed, and tell the model to map only the others. Keep the full-spec path
   when the mapper settled nothing.

## Tests (`tests/test_headers.py`)

- With the seed loaded, `german_invoice.xlsx` parses through the **mapper** with zero LLM calls and status
  `reconciled` or `unreconciled`. Add it to `EXPECTED` with route `mapper` and its total.
- A CSV with headers `Col_1` … `Col_8` holding an AWS account ID, a region, an instance type, a date, a currency, a
  quantity, a cost and a description maps the account, region, instance type, date and currency columns with no
  header rule.
- Approving a review learns its headers; a second file with the same unusual headers then maps through the
  dictionary.
- `reject` stops a header from mapping to the rejected field.
- A file with both `UnitPrice` and `EffectivePrice` maps them to different fields.
- The LLM prompt lists the settled columns (check with `FakeLLM.calls`).
- All other `EXPECTED` entries unchanged.

## Done when

- [ ] Seed and learned dictionaries, scoring, value detectors, learning hooks and admin endpoints and UI work.
- [ ] The German sample needs no LLM.
- [ ] The nightly report metric "share with no LLM call" (`learn.metrics`) does not fall on the corpus. Ask the user
      to run `make corpus-check`.
- [ ] README "Routing" and "Learning loop" sections updated.
