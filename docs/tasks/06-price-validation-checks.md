# Task 06: Price-based validation checks

**Order:** after 05, and after the user has reviewed the match rates from task 05. **Needs:** 05.
**STOP gate:** none.
**Read first:** `billparse/validate.py` in full.

## In one sentence

Use the price columns from task 05 to catch parses that add up but are wrong: swapped columns, wrong units, invented
values.

## Where the values come from

The checks read the rows after `pricing.match_prices` has run: `price_match_level`, `price_ratio`,
`list_unit_price`, `coverage`, the cost fields, `quantity`, `unit`, `charge_start` / `charge_end`, `resource_id`.
They do not look anything up themselves.

## What exists today (verified)

- `validate.validate(ext, alt=None, resolved_share=None, kind="cost_lines")` builds a list of `Check(name, status,
  detail, weight)`. Confidence = 0.55 + the sum of the weights, clipped to 0..1.
- `CRITICAL` checks decide `needs_review`; `INTERNAL` checks decide `consistent` (`completeness()`).
- `feedback` (fail and warn details) flows into the LLM repair loop (`pipeline._llm_spec`) and the agent's evidence
  report (`agent.evidence_report`).
- `learn.regression_gate` compares rows, total and confidence on a spec's earlier files.
- After task 05, `pipeline._parse` calls `match_prices` before the final `validate`. Route attempts (`_try`) call
  `validate` **before** pricing. Step 4 decides how the checks reach them.

## Checks to add

Add one function per check in `validate.py`. Call them from `validate()`. Each returns `status="skip"` with weight 0
when its inputs are missing, so files without prices or attributes are never penalized.

| Name | Rule | Result (starting weights; tune on the corpus with the user) |
|---|---|---|
| `catalog_grounding` | share of priced usage cost at match levels 1–3 | ≥ 95%: pass +0.10 · 70–95%: warn 0 · < 70%: fail −0.15 |
| `implied_unit_price` | on lines with `coverage` `on_demand` or `unknown`, the share of cost with `price_ratio` between 0.5 and 1.05 | ≥ 90%: pass +0.10. Ratios clustered near 1000, 1/1000, 3600 or 1/3600 on > 5% of cost: fail −0.20, detail "unit or column mix-up: ratio ≈ X on rows ..." · otherwise warn 0 |
| `usage_hours` | lines with an hourly unit and a `resource_id`: quantity ≤ hours between `charge_start` and `charge_end` × 1.01 | any line over: fail −0.10, with the rows |
| `cost_order` | lines where at least two are printed: list ≥ contracted ≥ effective, with a 1% tolerance | more than 2% of lines break it: warn −0.05, detail "cost columns may be swapped" |
| `grounding_values` | route `llm_extract_direct` only: every `instance_type` and `region` value appears in `pipeline.doc_text(doc)` | any value not found: fail −0.15, with the values |

Notes:

- `implied_unit_price` ignores credits, negations, tax, fees and lines covered by commitments. Ratios between 0 and
  0.5 on on-demand lines are a warning, not a failure: large negotiated discounts exist, and task 05's
  `inferred_discount` explains them.
- `cost_order` warns rather than fails, because credits and a few edge cases legitimately break it.
- `grounding_values` needs the document text. Pass it in as a new optional parameter of `validate()`.

## Steps

1. Implement the five checks.
2. **Wiring:**
   - Add `catalog_grounding`, `implied_unit_price` and `usage_hours` to `INTERNAL`.
   - Do **not** add them to `CRITICAL` yet; the user decides that after seeing corpus results.
   - Include row numbers (from `_src`) and the ratio in the failure details, so the LLM repair loop and the agent can
     fix the right column.
3. **Learned-spec regression gate** (`learn.regression_gate`): after running the spec on each earlier file, also run
   `match_prices` and `validate`. Reject the new version if any of the new checks went from pass to fail on a file.
4. **Route attempts.** For the checks to steer route choice and LLM repair, run `match_prices` inside
   `pipeline._try` for `cost_lines` attempts as well, on the attempt's rows. It is cheap because it works on unique
   keys. Skip this for files over 100,000 rows until task 09 makes large files sample-based, and note that in a
   comment.

## Tests (`tests/test_price_checks.py`)

Build the frames directly, with the fixture catalog from task 04:

- a correct mapping → all new checks pass or skip, and confidence does not fall;
- quantity and unit price columns swapped → `implied_unit_price` fails with the mix-up detail;
- usage in seconds labeled as hours → fails with a ratio near 3600;
- 25 hours on one resource in a one-day window → `usage_hours` fails;
- list and effective columns swapped → `cost_order` warns;
- an LLM-extracted row with an instance type not in the text → `grounding_values` fails;
- no prices matched → every new check is `skip`;
- regression gate: a spec version that swaps two columns on a file it used to read correctly is rejected.

## Done when

- [ ] Five checks implemented, cleanly skipped when inputs are missing.
- [ ] Old samples keep their status and `EXPECTED` totals; confidence may rise on correct samples, never fall.
- [ ] Failure details reach the LLM feedback and the agent's evidence report.
- [ ] README validation table updated.
- [ ] The report lists confidence and status before and after for every sample file, and asks the user to run
      `make corpus-check`.
