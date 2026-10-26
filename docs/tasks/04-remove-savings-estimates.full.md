# Task 04: Remove savings estimates from the analysis (self-contained)

This file holds everything you need for this task: the project context first (parts A–E), then the task itself
(part F). You do not need any other task file.

**Order:** start after task 03 (`task/03-attribute-columns`) is merged into `main`, including its two review
follow-ups. **Needs:** task 01 (`coverage`, `commitment_status`), task 02 (the large path), task 03 (instance type
and the roll-up keys it added). **STOP gate:** none.

---

## A. What billparse is

A service that reads any cloud bill or cost file a customer uploads (AWS, Azure, GCP exports; PDF invoices; custom
Excel, CSV or JSON; Terraform) and returns **canonical line items** with proof that nothing is missing or counted
twice.

Other teams' products consume the result: savings estimating, cost estimating, forecasting. **billparse returns
facts, never conclusions.** It says what was used and what it cost. It never estimates savings. Savings, discount
rates and Archera's own rates (GRI, GSP) belong to the consumers. **This task enforces that rule in the analysis.**

The biggest business risk: **usage already covered by a Reserved Instance, Savings Plan or other commitment must
never look like on-demand usage**, or a consumer will promise savings the customer already has.

## B. How a file flows today (on `main` after tasks 01–03)

```
upload (webapi.save_upload streams to disk; webapi.start_parse -> small pool or large pool)
  -> pipeline.Pipeline.parse_path
     -> bigfile.classify: small (<= LARGE_FILE_MB uncompressed, default 100) or large
     small: parse_bytes -> _parse
        docmodel.load_bytes -> fingerprint -> _route (built-in, learned, mapper, LLM spec, LLM extract)
        each attempt = _try: spec.run -> validate.clean_rows -> attributes.normalize_regions
                             -> attributes.extract -> coverage.apply -> validate.validate
        analyze.resolve_services -> validate.validate -> _finish
     large: _parse_large
        bigfile.sample_doc (first rows) -> _route on the sample
        bigfile.scan: the accepted spec over the whole file in chunks; rows to Parquet; only sums are kept
          (validate.RowStats) and lines rolled up by contract.ROLLUP_KEYS + service, category and month
        validate.validate_stats -> _finish with the rolled-up lines
  _finish: analyze.analyze(rows) -> contract.build (schema.Document) -> store.save_result
```

**What this means for this task:** `analyze.analyze` runs on full lines for small files, and on **rolled-up lines**
for large files. On the normal path, `contract._rollup` also rolls up files with more than `CONTRACT_MAX_CHARGES`
(50,000) lines, but only for the contract, after the analysis. Your analysis must give the same answer on both
paths. That works only for fields that are roll-up keys (`contract.ROLLUP_KEYS`), which is why step 1 adds
`commitment_status` to them.

Key data shapes:

- **Rows DataFrame.** Columns are canonical fields (`transforms.CANONICAL`: `billed_cost`, `effective_cost`,
  `coverage`, `commitment_status`, `instance_type`, `charge_type`, `charge_start`, ...) plus columns the steps add
  (`service`, `category`, `provider`) and internal ones starting with `_` (`_lines` on rolled-up lines: how many
  lines each one stands for).
- **Charge types** (`charge_type`): `Usage`, `Purchase` (commitment fees, `UnusedReservation`), `Negation` (AWS
  `SavingsPlanNegation`), `Tax`, `Credit`, `Refund`, `Fee`, `Support`, `Adjustment`. `analyze.NON_USAGE` lists the
  types that are not usage.
- **Coverage** (`coverage`, task 01): `on_demand`, `reserved`, `savings_plan`, `committed_use`, `spot`, `unknown`
  on usage lines; empty on lines that are not usage.
- **Commitment status** (`commitment_status`): `used` or `unused` where the file says so (Azure unused lines, FOCUS
  `CommitmentDiscountStatus`).
- **Public contract** (`billparse/export.py`): `export.to_schema` projects the internal document onto a strict
  schema (`extra="forbid"`). The analysis is **not** part of it; it lives in the result summary
  (`result["analysis"]`) that `GET /results/{sha}`, the CLI and the web app read.

## C. What exists and what does not (after tasks 01–03)

| Area | State |
|---|---|
| Billed, list, contracted, effective cost | typed fields, from the file only (task 01) |
| Commitment coverage | `coverage` on every usage line (task 01) |
| Instance type, OS, IDs, commitment IDs | read from export columns (task 03) |
| Large files | chunked large path, bounded memory (task 02) |
| Prices | **none**: the price catalog is task 05 |
| **Savings** | `analyze._commitments` returns a hard-coded 20–40% "indicative savings": **this task removes it** |

## D. Where the numbers in this task come from

**Only from the parsed lines.** `billed_cost` or `effective_cost`, `category` (service resolution), `coverage`,
`commitment_status`, `instance_type`, `charge_type`, `charge_start`. Nothing is priced, looked up or assumed. If a
value cannot be known from the file, report `null` with a note. Never report 0 for "unknown".

## E. Rules for this task

1. **Start clean:** `git status` is clean, on an up-to-date `main`, and `make test` passes. Branch:
   `task/04-remove-savings-estimates`.
2. **Read before you write:** read `billparse/analyze.py`, `billparse/contract.py` (`ROLLUP_KEYS`, `_rollup`),
   `billparse/bigfile.py` (`ROLL_KEYS`, `roll`), `billparse/cli.py` (`_print_summary`) and the web pages that show
   the analysis. If the code differs from what this file says, stop and tell the user.
3. **Do not change existing samples or their `EXPECTED` entries** in `tests/test_pipeline.py`. Use the existing task
   01 samples for tests; add new generators only if you need new data.
4. **Schema version:** bump `schema.SCHEMA_VERSION` to `"1.6"` with a comment.
5. **Large path:** anything you add must give the same result on the large path (`LARGE_FILE_MB` set very low in a
   test) as on the normal path. Per-row Python is allowed only as a mapping over unique values.
6. **No network in tests.**
7. **API changes:** if a response model changes, run `make web-types`, then `make typecheck` and `make lint`.
8. **Docs:** update `README.md` ("Analysis" and "Known limits") in the same branch.
9. **Finish with a report:** what changed, each "Done when" item and how you verified it, and anything you decided
   that this file does not specify.

Commands:

```bash
uv sync --extra dev
uv run python samples/make_samples.py
make test
make lint && make typecheck
make web-types
BILLPARSE_USE_LLM=0 uv run python -m billparse parse samples/files/<file> --no-llm
```

---

## F. The task

### In one sentence

Replace the hard-coded "indicative savings" in the analysis with facts about commitment-eligible usage: how much
is already covered, how much is on demand, how much is unused.

### Where the values come from

From the parsed lines only: `billed_cost` or `effective_cost`, `category` (from service resolution), and `coverage`
and `commitment_status` (task 01). No prices, no discount assumptions.

### What exists today (verified)

- `billparse/analyze.py`, `_commitments(usage, by_cat, monthly, conf)`: returns `commitment_opportunity` with
  `eligible_monthly_spend`, `suggested_baseline_coverage`, `coverable_monthly_spend`,
  `assumed_discount_range: [0.2, 0.4]` and `indicative_monthly_savings`. It is called from `analyze()` and stored
  under `analysis["commitment_opportunity"]`. Eligible categories: `COMMITTABLE = {"Compute", "Databases",
  "Containers", "Analytics"}`.
- It sums `billed_cost`, so usage already covered by a commitment counts as eligible.
- `billparse/cli.py` `_print_summary` reads `commitment_opportunity` and prints the savings range.
- The web app does not read `commitment_opportunity` (no match in `web/src`). Check again before you start.

### Steps

1. **New function** `analyze._eligible_usage(df)` returning `commitment_eligible_usage`:
   - `cost_type`: `"effective"` when every usage line in the eligible categories has `effective_cost`, else
     `"billed"`. Use that column for all the sums below, and say which one in the output.
   - `by_category`: for each category in `COMMITTABLE`, the spend split by `coverage`
     (`on_demand`, `reserved`, `savings_plan`, `committed_use`, `spot`, `unknown`).
   - `by_group`: the same split by provider, region and instance family when known. The instance family is the part
     of `instance_type` before the size: `m5` from `m5.xlarge`, `Standard_D` + series for Azure, `n2` for GCP. Keep
     the top 50 groups by spend, and roll the rest into `"other"`.
   - `unused_commitment_spend`: commitment capacity paid for but not used, by commitment type, from what each format
     prints:
     - **Azure:** lines with `commitment_status = unused` (`UnusedReservation`, `UnusedSavingsPlan`);
     - **FOCUS:** lines with `commitment_status = unused` (`CommitmentDiscountStatus = Unused`);
     - **AWS CUR:** the `effective_cost` of `RIFee` and `SavingsPlanRecurringFee` lines. Task 02's fix made
       `cur_effective` put the unused part of the commitment there. When the file has no amortized columns, report
       `null` with a note, not 0.
   - Keep unused spend **out of** `by_category` and `by_group`: a FOCUS unused line is `ChargeCategory = Usage` with
     `coverage = savings_plan`, so without this it would be counted as covered usage too.
   - `monthly`: the eligible on-demand spend per month, when the file spans more than one month.
   - `note`: one sentence saying these are facts from the file and that billparse does not estimate savings.

   **Roll-ups.** Every real CUR or FOCUS file is analyzed from rolled-up lines: on the large path, and on the normal
   path above `CONTRACT_MAX_CHARGES`. `commitment_status` is **not** in `contract.ROLLUP_KEYS` today, so unused FOCUS
   and Azure lines would merge with used ones and the unused amount would come out as 0. Add `commitment_status` to
   `ROLLUP_KEYS` (it has three values, so groups barely grow). Every other field this step reads (`coverage`,
   `category`, `provider`, `region`, `instance_type`, `line_type_raw`, the month) is already a roll-up key.

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

### Tests

In `tests/test_cost_types.py` or a new file, using the task 01 sample `aws_cur2_commitments.csv.gz`:

- covered spend appears under `reserved` and `savings_plan`, not under `on_demand`;
- `unused_commitment_spend` matches the unused lines on the CUR sample, `azure_actual.csv` and
  `focus_commitments.parquet`, and is `null` on `aws_cur2_no_amortized.csv.gz`;
- the same three files give the same `commitment_eligible_usage` on the large path (`LARGE_FILE_MB` set very low,
  small `CHUNK_ROWS`) and with `CONTRACT_MAX_CHARGES` set low on the normal path, so unused spend survives roll-ups;
- the coverage splits add up to the eligible usage spend;
- no key anywhere in `analysis` contains `discount` or `savings`, except the deprecated object's
  `deprecated` and `replaced_by` keys;
- the forecast's base excludes negations.

### Done when

- [ ] Tests, lint and typecheck pass; `EXPECTED` unchanged.
- [ ] No savings or discount assumption is produced anywhere.
- [ ] README "Analysis" section updated, and the "Known limits" line about indicative commitment savings removed.
