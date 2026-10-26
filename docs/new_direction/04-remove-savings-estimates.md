# Task 04: Remove savings estimates from the analysis

Replace the hard-coded "indicative savings" in the analysis (`analyze._commitments`: a 20–40% discount on
commitment-eligible spend) with facts about commitment-eligible usage: how much is already covered (reserved, savings
plan, committed use), how much is on demand or Spot, and how much commitment was paid for but not used. From the
parsed lines only: `billed_cost` or `effective_cost`, `category`, `coverage`, `commitment_status`. No prices, no
discount assumptions; unknown is `null` with a note, never 0.

Steps (as given to the agent on 2026-10-26):

1. `analyze._eligible_usage(df)` → `analysis["commitment_eligible_usage"]`: `cost_type` (effective when every
   eligible usage line has it, else billed), `by_category` and `by_group` (provider, region, instance family; top 50,
   the rest "other") split by coverage, `unused_commitment_spend` (Azure/FOCUS `commitment_status = unused`, AWS CUR
   RIFee and SavingsPlanRecurringFee effective cost; null when the file does not print the amount), kept out of the
   splits, `monthly_on_demand`, and a `note`. `commitment_status` becomes a roll-up key so unused lines survive
   roll-ups (large path, and the contract above `CONTRACT_MAX_CHARGES`).
2. `commitment_opportunity` stays one schema version with `deprecated: true`, `replaced_by:
   "commitment_eligible_usage"`, and no discount, coverage or savings fields. `SCHEMA_VERSION = "1.6"`; removed in
   1.7.
3. The forecast's usage base excludes negations (test).
4. `cli._print_summary` prints the eligible-usage split, not savings.
5. The web app shows no analysis section: no change.

Tests: covered spend under its commitment, unused spend per format (null when unknown), the same result on the
large path and with roll-ups, splits that add up, no savings or discount key (the coverage name `savings_plan` is a
commitment kind, not an estimate), the forecast without negations.
