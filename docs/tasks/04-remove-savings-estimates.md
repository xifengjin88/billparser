# Task 04: Remove savings estimates from the analysis

**Order:** after 03. **Needs:** 01 (uses `coverage` and `commitment_status`). **STOP gate:** none.
**Read first:** `00-START-HERE.md` section 1 (billparse returns facts, never savings).

## In one sentence

Replace the hard-coded "indicative savings" in the analysis with facts about commitment-eligible usage: how much
is already covered, how much is on demand, how much is unused.

## Where the values come from

From the parsed lines only: `billed_cost` or `effective_cost`, `category` (from service resolution), and `coverage`
and `commitment_status` (task 01). No prices, no discount assumptions.

## What exists today (verified)

- `billparse/analyze.py`, `_commitments(usage, by_cat, monthly, conf)`: returns `commitment_opportunity` with
  `eligible_monthly_spend`, `suggested_baseline_coverage`, `coverable_monthly_spend`,
  `assumed_discount_range: [0.2, 0.4]` and `indicative_monthly_savings`. It is called from `analyze()` and stored
  under `analysis["commitment_opportunity"]`. Eligible categories: `COMMITTABLE = {"Compute", "Databases",
  "Containers", "Analytics"}`.
- It sums `billed_cost`, so usage already covered by a commitment counts as eligible.
- `billparse/cli.py` `_print_summary` reads `commitment_opportunity` and prints the savings range.
- The web app does not read `commitment_opportunity` (no match in `web/src`). Check again before you start.

## Steps

1. **New function** `analyze._eligible_usage(df)` returning `commitment_eligible_usage`:
   - `cost_type`: `"effective"` when every usage line in the eligible categories has `effective_cost`, else
     `"billed"`. Use that column for all the sums below, and say which one in the output.
   - `by_category`: for each category in `COMMITTABLE`, the spend split by `coverage`
     (`on_demand`, `reserved`, `savings_plan`, `committed_use`, `spot`, `unknown`).
   - `by_group`: the same split by provider, region and instance family when known. The instance family is the part
     of `instance_type` before the size: `m5` from `m5.xlarge`, `Standard_D` + series for Azure, `n2` for GCP. Keep
     the top 50 groups by spend, and roll the rest into `"other"`.
   - `unused_commitment_spend`: the sum over lines with `commitment_status = unused`.
   - `monthly`: the eligible on-demand spend per month, when the file spans more than one month.
   - `note`: one sentence saying these are facts from the file and that billparse does not estimate savings.

2. **Deprecate the old field** for one schema version:
   - Keep `commitment_opportunity` in the analysis, but add `"deprecated": true` and
     `"replaced_by": "commitment_eligible_usage"`, and remove `assumed_discount_range`,
     `suggested_baseline_coverage`, `coverable_monthly_spend` and `indicative_monthly_savings` from it.
   - Bump `SCHEMA_VERSION` to `"1.6"`, with a comment that `commitment_opportunity` is removed in 1.7.

3. **Forecast** (`analyze._forecast`, `_monthly`): check that usage spend excludes `Negation` lines (task 01 added
   them to `NON_USAGE`). Add a test. Do not change the forecast method.

4. **CLI:** `cli._print_summary` prints the eligible-usage split instead of savings.

5. **Web** (only if the document page shows an analysis section): show the coverage split. Run `make web-types` if
   a response model changed.

## Tests

In `tests/test_cost_types.py` or a new file, using the task 01 sample `aws_cur2_commitments.csv.gz`:

- covered spend appears under `reserved` and `savings_plan`, not under `on_demand`;
- `unused_commitment_spend` matches the unused lines;
- the coverage splits add up to the eligible usage spend;
- no key anywhere in `analysis` contains `discount` or `savings`, except the deprecated object's
  `deprecated` and `replaced_by` keys;
- the forecast's base excludes negations.

## Done when

- [ ] Tests, lint and typecheck pass; `EXPECTED` unchanged.
- [ ] No savings or discount assumption is produced anywhere.
- [ ] README "Analysis" section updated, and the "Known limits" line about indicative commitment savings removed.
