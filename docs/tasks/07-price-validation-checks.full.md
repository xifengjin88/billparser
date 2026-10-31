# Task 07: Price-based validation checks (self-contained)

This file holds everything you need for this task: the project context first (parts A–E), then the task itself
(part F). You do not need any other task file. It replaces the older `07-price-validation-checks.md`, which was written
before price matching existed.

**Order:** start after task 06 (`task/06-price-matching`) is merged into `main`, including its follow-ups (pricing
per distinct key, `assume_region`, the stored fallback date). If they are missing, stop and tell the user.
**Needs:** tasks 02 (large path), 05 (catalog), 06 (pricing). **STOP gate:** none, but the weights below are starting
values: **report the check results on every sample and ask the user before making any new check critical.**

---

## A. What billparse is

A service that reads any cloud bill or cost file a customer uploads and returns canonical line items with proof that
nothing is missing or counted twice. Other products consume the result; billparse returns facts, never savings.

Today the proof is about **completeness**: totals, the tree of sums, row coverage, a second independent reading, line
math. It cannot tell whether the values are the **right** ones. A spec that maps the quantity column into the unit
price, or reads seconds as hours, still adds up to the total. Task 06 now gives every AWS usage line a list price from
the provider's own catalog, and that is an independent reference: **a parse that maps real SKUs, gives sane unit
prices and agrees with the printed list prices is almost certainly right; one that does not is almost certainly
wrong.** This task turns that into checks.

## B. Where validation and pricing run today

```
small file  (pipeline._parse)
  routes: _try(spec) -> spec.run -> clean_rows -> attributes -> coverage -> validate(ext)       <- NOT priced yet
  accepted attempt -> resolve_services -> self._price(rows) [pricing.match_prices] -> validate(ext, alt, share)
large file  (pipeline._parse_large)
  routes on the sample (same _try) ; bigfile.scan: per chunk ... match_prices(chunk) -> RowStats.add(chunk)
  -> validate_stats(scan.stats, ...)
learned spec repair  (learn.regression_gate -> learn._check): run + clean_rows + validate   <- NOT priced
```

- `validate.validate(ext, ...)` and `validate.validate_stats(stats, ...)` build a list of
  `Check(name, status, detail, weight)`. Status is `pass`, `fail`, `warn` or `skip`. Confidence is 0.55 plus the sum
  of the weights, clipped to 0..1.
- `CRITICAL` checks decide `needs_review`; `INTERNAL` checks decide `consistent` (`validate.completeness`).
- Both functions read `validate.RowStats`, sums and counts that are built from a whole frame or merged chunk by chunk.
  That is how the large path is checked by the same functions. **Every new check must work from `RowStats`.**
- A failed or warned check's detail goes into `feedback`. That flows into the LLM spec repair
  (`pipeline._llm_spec`) and the agent's evidence report (`agent.evidence_report`).

## C. What pricing gives you (task 06, `billparse/pricing.py`)

Per usage line, after `match_prices`:

| Column | Meaning |
|---|---|
| `price_match_level` | `exact`, `attributes`, `assumed`, `ambiguous`, `unmatched`, `unit_mismatch`, `no_catalog` (Azure, GCP) |
| `price_missing` | for ambiguous or unmatched lines: the empty key columns, or `catalog_row` (no such product found) |
| `price_assumptions` | text inferences and request defaults used; also `tiered`, `before_history` |
| `list_unit_price`, `list_cost` | printed by the file, or filled from the catalog (`_list_unit_price_inferred`, `_list_cost_inferred`) |
| `implied_unit_price` | `effective_cost / quantity`, else `billed_cost / quantity` |
| `price_ratio` | `implied_unit_price / list_unit_price` |
| `catalog_sku_id` | the matched catalog SKU |
| `coverage` | from the file (task 01): `on_demand`, `reserved`, `savings_plan`, `committed_use`, `spot`, `unknown` |

**Gap this task fills:** when the file **prints** its own list unit price (CUR `pricing/publicOnDemandRate`, FOCUS
`ListUnitPrice`, Azure `PayGPrice`), pricing keeps the printed value and **discards the catalog's**, so nothing can
compare the two. Step 1 keeps it.

## D. Rules for this task

1. **Start clean:** `git status` is clean, on an up-to-date `main`, and `make test` passes. Branch:
   `task/07-price-checks`.
2. **Read before you write:** `billparse/validate.py` in full, `billparse/pricing.py` (`match_prices`, `_apply`,
   `_missing`, `PriceStats`), `prices/store.py`, `billparse/pipeline.py` (`_parse`, `_try`, `_price`, `_parse_large`),
   `billparse/bigfile.py` (`scan`), `billparse/learn.py` (`regression_gate`, `_check`), `billparse/agent.py`
   (`evidence_report`). If the code differs from this file, stop and tell the user.
3. **Every check works on both paths:** it reads `RowStats` (extend it with new counters, and merge them), so
   `validate()` and `validate_stats()` give the same check for the same file.
4. **A check skips cleanly** (status `skip`, weight 0) when its inputs are missing: no catalog, no priced lines,
   Azure or GCP only, no quantities. A file without prices must get exactly the verdict it gets today.
5. **Do not change existing samples or `EXPECTED`.** On every correct sample, confidence may rise and never fall, and
   the status may not get worse. Report every change.
6. **Do not add the new checks to `CRITICAL`** in this task. The user decides that after the corpus run.
7. **Schema:** step 1 adds a contract field. Bump `SCHEMA_VERSION` to `"1.9"`, in `schema.py`, `export.py` and the
   web types.
8. **No network in tests.** Use the task 05 fixture catalog.
9. **Finish with a report:** what changed; every sample's new checks (status, detail) and confidence before and
   after; each "Done when" item; anything you decided that this file does not specify.

## E. Words

- **Priced line:** an AWS usage line whose `price_match_level` is `exact`, `attributes` or `assumed`.
- **Ratio clusters:** a share of cost whose `price_ratio` sits near one value. 1000, 1/1000, 3600, 1/3600, 24, 1/24,
  730 and 1/730 mean a unit or column mix-up (GB vs MB, seconds vs hours, days vs hours, months vs hours).

---

## F. The task

### In one sentence

Use the catalog's prices to catch parses that add up but are wrong (swapped columns, wrong units, invented or
impossible values) and feed what they find into confidence, the LLM repair loop and the regression gate.

### 1. Keep the catalog's price, and say why a line is unmatched

- **`catalog_unit_price`:** in `pricing._apply`, set it on every priced line to the catalog price **in the line's
  unit** (`unit_price × factor`), whether or not the file printed a list price. Add it to `PRICE_COLUMNS`, to
  `bigfile.STEP_COLUMNS`, to the roll-ups (the shared value of the group, like `UNIT_PRICES`), to `schema.Charge` and
  `export.Charge` (`origin="inferred"`, source `price_catalog`), and to the web charge detail. Schema `1.9`.
- **Unmatched reasons:** today an unmatched line with a SKU, usage type or instance type says only `catalog_row`.
  Split it:
  - `service_not_loaded`: the catalog has no prices for the line's `service_code` at all. Add
    `PriceCatalog.services() -> set[str]`, cached per catalog version. This is a catalog gap, not a parse error.
  - `no_such_product`: the service is loaded, but no product has this instance type in this region (any OS, any
    tenancy), or no product has this SKU or usage type. Add `PriceCatalog.exists(keys)`, one query for many keys,
    like `lookup`. **This is a strong sign of a bad parse:** the instance type, region or usage type was read from
    the wrong column.
  - Keep `catalog_row` only when neither applies.

### 2. `RowStats` counters (mergeable across chunks)

Add the counters each check below needs, filled in `RowStats.add` from the priced rows, and summed in `merge`. Keep
**up to 5 example source rows** (`_src`) and their values per check, for the feedback. Keep the examples from the
first chunks; never grow without bound.

### 3. The checks

Add one function per check in `validate.py`, called from **both** `validate()` and `validate_stats()`.

| Name | Rule | Result (starting weights) |
|---|---|---|
| `catalog_grounding` | Among AWS usage cost whose service the catalog has: share priced vs share `no_such_product`. Ambiguous, unit-mismatch and `service_not_loaded` lines count in neither. | `no_such_product` < 2% of that cost: pass +0.10 · 2–10%: warn 0 · > 10%: fail −0.15. The detail names the values not found (instance types, regions, usage types) and example rows. |
| `printed_list_price` | Lines where the file **printed** `list_unit_price` and the match is `exact`, not tiered, not `before_history`: printed / `catalog_unit_price` within ±1% | ≥ 95% of that cost agrees: pass +0.10 · else, ratios clustered (section E): fail −0.20 "printed list prices disagree with the catalog by ×N: unit or column mix-up" · else warn −0.05 |
| `implied_unit_price` | Priced lines with `coverage` `on_demand` or `unknown`, not tiered: share of cost with `price_ratio` in 0.5–1.05 | ≥ 90%: pass +0.10 · more than 5% of that cost clustered near a mix-up ratio: fail −0.20, detail "unit or column mix-up: ratio ≈ X on rows ..." · else warn 0 (large negotiated discounts exist; task 06's `inferred_discount` explains them) |
| `price_units` | Share of AWS usage cost at `unit_mismatch` | > 5%: warn −0.05 "the unit column may be wrong" with examples · else pass 0 |
| `usage_hours` | Lines with an hourly unit, a `resource_id` and both `charge_start` and `charge_end`: quantity ≤ hours in that window × 1.01 | any line over: fail −0.10, with the rows · none tested: skip |
| `cost_order` | Usage lines where at least two of list, contracted, effective cost are **printed**: list ≥ contracted ≥ effective, 1% tolerance | > 2% of those lines break it: warn −0.05 "cost columns may be swapped" |
| `grounding_values` | Route `llm_extract_direct` only: every `instance_type` and `region` value appears in the document text (`pipeline.doc_text`) | any value not found: fail −0.15, listing the values · other routes: skip |

Notes:
- The ratio checks ignore commitment-covered lines, credits, negations, tax, fees and Spot.
- Never compare against prices filled by the catalog itself. `printed_list_price` uses printed values only, and
  `implied_unit_price` uses billed and effective cost, which are always from the file.
- `cost_order` warns rather than fails: credits and a few edge cases legitimately break it.

### 4. Wiring

- **Completeness:** add `catalog_grounding`, `printed_list_price`, `implied_unit_price` and `usage_hours` to
  `INTERNAL`, so passing them can make a file `consistent`. Leave `CRITICAL` unchanged (rule 6).
- **Route choice and LLM repair.** For the checks to steer which route wins and what the LLM fixes, price the
  attempts:
  - In `_try`, for `cost_lines` attempts, call `match_prices` with **one `KeyCache` per parse**, shared by every
    attempt and by the final `_price`, so no key is looked up twice.
  - On the large path, attempts already run on the sample: the same.
  - Keep `_try` from failing on a pricing error, as `_price` does.
- **Feedback:** failure and warning details include up to 5 example rows (`_src`) and the values or ratios, so the
  LLM repair (`_llm_spec`) and the agent (`evidence_report`, `agent.check`) can fix the right column. Add the new
  checks to the agent's evidence report.
- **Regression gate:** `learn._check` must price (catalog and a `KeyCache`) before validating, on both paths. In
  `regression_gate`, reject a new spec version when any of the new checks goes from `pass` on the accepted result to
  `fail`. Store the accepted check statuses with the spec's documents (`store.link` accepted dict) so the gate can
  compare.

### 5. Results and the web app

The checks appear in `result["checks"]` like the others. On the document page, show the new checks with their
examples. If a response model changes, run `make web-types`.

### Tests (`tests/test_price_checks.py`)

Build the frames directly or as small CSV files, priced against the fixture catalog. Run every case **on the normal
path and on the large path** (`LARGE_FILE_MB` very low, small `CHUNK_ROWS`).

- **A correct CUR:** every new check passes or skips, and confidence does not fall.
- **Column mix-ups:**
  - quantity and unit price swapped: `implied_unit_price` fails, with the mix-up detail;
  - usage in seconds labeled as hours: fails with a ratio near 3600;
  - a printed list unit price per month on hourly lines: `printed_list_price` fails near 730.
- **Impossible values:** an instance type read from the wrong column, or an `m5.xlarge` in a region the fixture does
  not offer, gives `no_such_product` and fails `catalog_grounding`. A service the fixture does not load gives
  `service_not_loaded` and the check skips.
- **`usage_hours`:** 25 hours for one resource in a one-day window fails.
- **`cost_order`:** list and effective columns swapped gives a warning.
- **`grounding_values`:** an LLM-extracted row with an instance type not in the text fails.
- **Skips:** no catalog, or an Azure-only file: every new check skips, and the verdict equals today's.
- **Route choice:** with a fake LLM giving two candidate specs, one with swapped columns, the correct one wins
  because of the new checks.
- **Regression gate:** a spec version that swaps two columns on a file it read correctly before is rejected.
- **Contract:** `catalog_unit_price` appears in `export.to_schema` and `POST /parse`.
- **Samples:** `EXPECTED` unchanged, and no sample's status gets worse.

### Done when

- [ ] Seven checks, each skipping cleanly without inputs, identical on both paths.
- [ ] `catalog_unit_price` and the split unmatched reasons, in the contract (schema 1.9).
- [ ] Attempts are priced with one cache per parse; failures reach the LLM repair, the agent and the regression gate.
- [ ] README validation table updated.
- [ ] **Report to the user:** every sample's new check results and confidence before and after. Ask them to run
      `make corpus-check` on real files before any weight changes or any check becomes critical.

### Do not

- Do not make any new check critical.
- Do not let catalog-filled values validate themselves.
- Do not change pricing results, except adding `catalog_unit_price` and splitting the unmatched reasons.
