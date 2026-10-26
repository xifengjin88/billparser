# Task 01: Cost columns and commitment coverage from the file

**Order:** first task. **Needs:** nothing. **STOP gate:** none.
**Read first:** `00-START-HERE.md`, especially section 4 ("Where every money value comes from").

## In one sentence

Read the cost columns that the uploaded file itself prints (billed, list, contracted, effective) into typed fields,
and record for every line whether its usage was on demand or covered by a commitment.

## Where the values come from (read this before anything else)

**Every value in this task comes from a column printed in the customer's file. This task does no pricing, no
lookup, and no calculation beyond simple arithmetic on printed columns of the same row.** There is no price data in
this repository; prices arrive in tasks 05–06. If a file does not print a cost type, that field stays empty.

| Format | billed cost | list cost | contracted cost | effective cost | line type / pricing model |
|---|---|---|---|---|---|
| AWS CUR legacy (`aws-cur-legacy.json`) | `lineItem/UnblendedCost` (already mapped) | `pricing/publicOnDemandCost` (already mapped) | not printed: leave empty | per line type, see rule E1 | `lineItem/LineItemType` (already mapped to `charge_type`) |
| AWS CUR 2.0 (`aws-cur2.json`) | `line_item_unblended_cost` (mapped) | `pricing_public_on_demand_cost` (mapped) | not printed: leave empty | per line type, see rule E1 | `line_item_line_item_type` (mapped) |
| Azure cost details (`azure-usage-details.json`) | `CostInBillingCurrency` / `Cost` / `PreTaxCost` (mapped) | `PayGPrice` × `Quantity` | `UnitPrice` × `Quantity` (only when the file has `UnitPrice`) | see rule E2 | `ChargeType` (mapped), `PricingModel` (new) |
| FOCUS (`focus.json`) | `BilledCost` (mapped) | `ListCost` (mapped) | `ContractedCost` (new) | `EffectiveCost` (mapped) | `ChargeCategory` (mapped), `PricingCategory`, `CommitmentDiscountType`, `CommitmentDiscountStatus` (new) |
| GCP billing export (`gcp-billing-export.json`) | `cost` (mapped) | `cost_at_list` if present (new) | not printed: leave empty | `cost + credits` (already a derive) | `cost_type` (mapped) |
| Every other format (PDF invoices, custom files, learned specs) | as today | empty unless the spec maps a column | empty | empty | as today |

Every provider column name above that is marked "new" must be verified against the provider's current
documentation (rule 10 in START-HERE).

**Rule E1 (CUR effective cost).** CUR has no single effective-cost column. Per row, by line type:
`Usage` → unblended cost; `DiscountedUsage` → `reservation/EffectiveCost` (2.0: `reservation_effective_cost`);
`SavingsPlanCoveredUsage` → `savingsPlan/SavingsPlanEffectiveCost` (2.0:
`savings_plan_savings_plan_effective_cost`); `SavingsPlanNegation`, `SavingsPlanRecurringFee`, `RIFee` → 0 (their
cost is already spread into the covered usage rows); `Tax`, `Credit`, `Refund`, `Fee` → unblended cost. **If the
file has neither the reservation nor the Savings Plan effective-cost column, leave `effective_cost` empty on every
row** and do not list `effective` in `Document.cost_types`, because without those columns the amortized cost
cannot be known.

**Rule E2 (Azure effective cost).** Azure produces two exports with the same columns: actual cost and amortized
cost. In the amortized export, usage rows with `PricingModel = Reservation` or `SavingsPlan` carry a non-zero cost
and there are no reservation `Purchase` rows; in the actual-cost export, those usage rows cost 0 and the purchases
appear as `Purchase` rows. Detect which one the file is. Amortized → copy the cost to `effective_cost` too and set
the document's cost basis to `amortized`. Actual → leave `effective_cost` empty and set `actual`. Cannot tell →
leave it empty and set `unknown`. `billed_cost` keeps the cost column in all cases (totals depend on it).

## What exists today (verified)

- `billparse/transforms.py`: `CANONICAL` has `billed_cost`, `effective_cost`, `list_cost`. No `contracted_cost`,
  no `coverage`, no `line_type_raw`. `NUMERIC` lists the money fields.
- `transforms.CHARGE_TYPE_MAP` / `t_charge_type`: maps provider line types to canonical charge types. It maps
  `savingsplancoveredusage` and `discountedusage` to `"Usage"`, so coverage is lost here. `SavingsPlanNegation` has
  no entry, so its raw text is kept as the charge type.
- `billparse/schema.py`: `Charge` has one money field, `cost`, and `basis` (`billed | estimated | priced`).
  `commitment_status` already exists. `SCHEMA_VERSION = "1.3"`.
- `billparse/contract.py`:
  - `_charge` sets `c.cost` from `billed_cost` and always sets `basis = "billed"`.
  - It copies `effective_cost` and `list_cost` into the untyped `attributes` dict.
  - `KIND_OF_CHARGE_TYPE` and `kind_of` map a charge type to a `Kind`.
  - `ROLLUP_KEYS` and `_rollup` sum only `billed_cost` and `quantity`, so other money columns are dropped in a
    roll-up.
- `billparse/spec.py`: `NEVER_ADDED = {"billed_cost", "tax", "unit_price"}` stops `_keep_unmapped` from guessing
  these from headers.
- `billparse/analyze.py`: `NON_USAGE` and `TYPE_OF_CHARGE` classify charge types for the analysis.
- `billparse/validate.py`: `_plausibility` warns about negative lines not marked Credit/Refund/Adjustment.
- `scripts/corpus.py`: `KIND` maps charge types for the corpus report.
- `billparse/webapi.py`: `ChargeRow` (what the document page shows per charge) has `cost` and `basis` only.
- Samples: `samples/make_samples.py` `aws_cur2()` writes only `Usage`, `Tax` and `Credit` rows, with no commitment
  rows and no amortized columns.

Observed on `samples/files/focus_gcp.parquet`: every charge has `basis: "billed"`, and `effective_cost` and
`list_cost` exist only inside `attributes`.

## Steps

1. **Canonical fields** (`transforms.py`):
   - Add `contracted_cost` to `CANONICAL` (description "Cost after negotiated discounts, before commitments") and
     to `NUMERIC`.
   - Add `coverage` ("How the usage was priced: on_demand, reserved, savings_plan, committed_use, spot, unknown"),
     `line_type_raw` ("The provider's own line type, unchanged") and `commitment_status` ("used or unused") to
     `CANONICAL`.
   - Add `contracted_cost`, `effective_cost` and `list_cost` to `spec.NEVER_ADDED`.

2. **Keep the line type and derive coverage** (`transforms.py`):
   - Add an entry `"savingsplannegation": "Negation"` to `CHARGE_TYPE_MAP`.
   - Add a new transform `coverage` (registered in `TRANSFORMS`, so specs and the LLM can use it). It reads a
     provider line type or pricing model and returns a coverage value:

     | Input (normalized, case and separators ignored) | coverage |
     |---|---|
     | `usage`, `ondemand`, `standard` | `on_demand` |
     | `discountedusage`, `reservation`, `reserved`, `rifee` | `reserved` |
     | `savingsplancoveredusage`, `savingsplannegation`, `savingsplan`, `savingsplanrecurringfee`, `savingsplanupfrontfee` | `savings_plan` |
     | `committed` (FOCUS `PricingCategory`), `committedusediscount`, `cud` | `committed_use` |
     | `spot`, `dynamic` | `spot` |
     | anything else | `unknown` |

   - In each built-in spec, map the line-type column a second time, to `line_type_raw` with `["strip"]` and to
     `coverage` with `["strip", "coverage"]`, in addition to the existing `charge_type` mapping.
   - Azure: map `PricingModel` to `coverage`. Its values are OnDemand, Reservation, SavingsPlan and Spot; when it
     is missing, fall back to `ChargeType`.
   - Map `UnusedReservation` and `UnusedSavingsPlan` to `commitment_status = "unused"`. Map covered usage to
     `used`.
   - FOCUS: map `CommitmentDiscountStatus` to `commitment_status`. Set `coverage` from `PricingCategory`, refined
     by `CommitmentDiscountType`: a Savings Plan type gives `savings_plan`, a reservation type gives `reserved`.
   - Mapping one source column to several fields already works: `spec._map_columns` looks up each field's `from`
     column on its own, so `charge_type`, `line_type_raw` and `coverage` can all read `line_item_line_item_type`.
     No interpreter change is needed. Add a test that proves it.
   - A row whose line type marks a commitment fee (`RIFee`, `SavingsPlanRecurringFee`, `SavingsPlanUpfrontFee`)
     keeps `charge_type = "Purchase"`, as today, and also gets `coverage`.

3. **Cost columns per format** (built-in specs): add the mappings and `derive` expressions in the table above. Use
   `derive` for the arithmetic (`PayGPrice × Quantity`) and for rule E1. Check that `expr.py` supports what you
   need; if rule E1 needs a conditional the expression language does not have, add a small hand-written transform
   in `CUSTOM_TRANSFORMS` (see `transforms.py`) rather than extending the expression grammar.

4. **Azure actual vs amortized** (rule E2): detect it after the spec runs, in a small function in `spec.py` or a new
   module, called for the `azure-usage-details` spec only. Record the result in the extraction's fields as
   `cost_basis`.

5. **Schema** (`schema.py`), `SCHEMA_VERSION = "1.4"`:
   - `Charge`: add `list_cost`, `contracted_cost`, `effective_cost` (`Optional[CitedValue[Decimal]]`), and
     `coverage` and `line_type_raw` (`Optional[CitedValue[str]]`). Keep `cost` as the billed cost (no change).
   - `Kind`: add `"commitment_negation"`.
   - `Document`: add `cost_types: list[str]` (which of `billed`, `list`, `contracted`, `effective` at least one
     usage line has, printed) and `cost_basis: Optional[str]` (`actual`, `amortized`, `unknown`, or None when not
     applicable).

6. **Contract** (`contract.py`):
   - `_charge`: fill the new typed fields from the row with `origin="printed"`. Values computed by a `derive` from
     printed columns (Azure `PayGPrice × Quantity`) get `origin="inferred"` and `source="derived"`. To know which,
     have the spec runner record the derived field names (for example in the extraction, passed through the
     summary).
   - Keep writing `effective_cost` and `list_cost` into `attributes` as well, for one version. Mark the line with
     a comment: `# deprecated in 1.4: use the typed fields`.
   - `KIND_OF_CHARGE_TYPE`: add `"Negation": "commitment_negation"`.
   - `ROLLUP_KEYS`: add `coverage` and `line_type_raw`. `_rollup`: sum `list_cost`, `contracted_cost` and
     `effective_cost` when present, with `min_count=1` so an all-empty group stays empty instead of becoming 0.
   - `build`: fill `Document.cost_types` and `Document.cost_basis`.

   **Public contract** (`export.py`). This is what consumers receive from `POST /parse`, and it is strict, so do
   all of this or the new fields never leave billparse and the new kind makes `to_schema` raise:
   - `export.Charge`: add `list_cost`, `contracted_cost`, `effective_cost`, `coverage`, `line_type_raw`, with the
     same types as in `schema.py`. Add a comment with each one's FOCUS name: `ListCost`, `ContractedCost`,
     `EffectiveCost`; `coverage` comes from `PricingCategory` / `CommitmentDiscountType`.
   - `export.Charge.kind`: add `"commitment_negation"` to its `Literal`.
   - `export.Document`: add `cost_types` and `cost_basis`.
   - `export.to_schema`: add the five charge fields to the tuple of names copied with `_cv`, and copy
     `cost_types` and `cost_basis` into `out`.

7. **Analysis and validation:**
   - `analyze.NON_USAGE`: add `"Negation"`. `TYPE_OF_CHARGE`: add `"Negation": "commitment_negation"`.
   - Negation lines are reported on their own in `non_usage`. They are not usage and not credits.
   - `validate._plausibility`: treat `Negation` as an explained negative.
   - `scripts/corpus.py` `KIND`: add `"Negation"`.

8. **Web:**
   - `webapi.ChargeRow`: add `list_cost`, `contracted_cost`, `effective_cost`, `coverage`.
   - `DocumentView`: add `cost_types` and `cost_basis`.
   - Fill them where `ChargeRow(...)` is built (around `webapi.py` line 484).
   - Run `make web-types`, and show the four costs and the coverage in the charge detail panel
     (`web/src/components/app/charges.tsx`, `ChargeDetailPanel`).
   - Run `make typecheck`.

## New samples and tests

Add generators to `samples/make_samples.py`. Do not change the existing ones.

1. **`aws_cur2_commitments()` → `aws_cur2_commitments.csv.gz`.** Over 3 days, one account, `us-east-1`:
   - on-demand `Usage` rows;
   - `DiscountedUsage` rows, with `reservation_reservation_a_r_n` and `reservation_effective_cost` filled;
   - `SavingsPlanCoveredUsage` rows priced at the on-demand rate, with `savings_plan_savings_plan_a_r_n` and
     `savings_plan_savings_plan_effective_cost` filled;
   - one `SavingsPlanNegation` row per covered row, at minus its unblended cost;
   - one `SavingsPlanRecurringFee` row, one `RIFee` row, one `Tax` row and one `Credit` row.

   Compute the expected total in the generator and add the file to `EXPECTED` with route `builtin`.
2. **`aws_cur2_no_amortized()`.** The same rows without the two effective-cost columns.
3. **`azure_amortized()` and `azure_actual()`.** The same usage, in the two Azure export styles (rule E2), with
   `PricingModel` and `PayGPrice`.
4. **`focus_commitments()`.** A FOCUS file with `ListCost`, `ContractedCost`, `EffectiveCost`, `PricingCategory`,
   `CommitmentDiscountId`, `CommitmentDiscountType` and `CommitmentDiscountStatus`.

New tests, in a new file `tests/test_cost_types.py`, using the `make_pipe` fixture pattern from
`tests/test_pipeline.py`:

- no `DiscountedUsage` or `SavingsPlanCoveredUsage` line has `coverage = on_demand`;
- negation lines exist, their kind is `commitment_negation`, and the file still reconciles to its expected total;
- `analysis.usage_spend` does not include negations, and `non_usage` lists them on their own;
- the CUR file with amortized columns has `effective_cost` on every usage line and `"effective"` in `cost_types`,
  and the one without them has no `effective_cost` and no `"effective"`;
- Azure: the amortized file gives `cost_basis = "amortized"` and the actual file gives `"actual"`; `list_cost`
  equals `PayGPrice × Quantity` with origin `inferred`;
- FOCUS: all four cost types are typed with origin `printed`, and the coverage and commitment status values are
  right;
- roll-up: with `CONTRACT_MAX_CHARGES` set low (monkeypatch), covered and on-demand lines are not merged, and
  `effective_cost` is summed;
- `export.to_schema` validates for every new sample. `POST /parse` (`tests/test_webapi.py` client) on
  `aws_cur2_commitments.csv.gz` returns charges with `list_cost`, `effective_cost` and `coverage`, and a negation
  charge with kind `commitment_negation`;
- `SCHEMA_VERSION == "1.4"`.

## Done when

- [ ] `make test`, `make lint`, `make typecheck` pass; `EXPECTED` entries for old samples unchanged.
- [ ] New samples parse by the built-in route with zero LLM calls.
- [ ] No covered usage reaches the output as `on_demand`.
- [ ] Cost fields are typed; `effective_cost` is never filled where the file does not print amortized data.
- [ ] README "How a file flows" mentions the cost types and coverage; `specs/README.md` documents the `coverage`
      transform.
- [ ] Report lists every provider column used and whether you verified its name.

## Do not

- Do not look up or compute any price. Prices are tasks 05–06.
- Do not change what `billed_cost` holds for any format; totals reconciliation depends on it.
- Do not drop negation lines.
