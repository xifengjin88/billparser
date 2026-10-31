# Task 06: Price matching (self-contained)

This file holds everything you need for this task: the project context first (parts A–E), then the task itself
(part F). You do not need any other task file. It replaces the older `06-price-matching.md`, which was written before
the price catalog existed.

**Order:** start after task 05 (`task/05-price-catalog`) is merged into `main`, **including its review fixes**:
`pre_installed_sw` as a parsed field, and `PriceCatalog.lookup` returning the same types (float prices, bool flags)
on PostgreSQL and SQLite. If either is missing, stop and tell the user. **Needs:** tasks 01 (cost fields, coverage),
02 (large path), 03 (attributes), 05 (catalog). **STOP gate:** none, but **stop at the end and report the match
rates** (part F, "Done when").

---

## A. What billparse is

A service that reads any cloud bill or cost file a customer uploads and returns **canonical line items** with proof
that nothing is missing or counted twice. Other teams' products consume the result (savings estimating, cost
estimating, forecasting). **billparse returns facts, never conclusions.** After this task it also says what each
usage line would cost at the provider's **public on-demand list price**, and how sure that match is. It never
estimates savings, and never uses Archera's own rates.

The biggest business risk: usage already covered by a commitment must never look like on-demand usage. This task
prices covered usage at list price too (the consumer's on-demand baseline) but **never changes `coverage`**.

## B. How a file flows today, and where this task plugs in

```
upload -> pipeline.Pipeline.parse_path -> bigfile.classify
  small: parse_bytes -> _parse
     docmodel.load_bytes -> _route (built-in, learned, mapper, LLM)
     _try: spec.run -> clean_rows -> normalize_regions -> attributes.extract -> coverage.apply -> validate
     analyze.resolve_services            (adds service, service_code such as AmazonEC2, category, provider)
     >>> THIS TASK: pricing.match_prices(rows, ...)   (new, before the final validate)
     validate.validate -> _finish (analyze.analyze -> contract.build -> store.save_result)
  large: _parse_large
     _route on the sample, then bigfile.scan in chunks; per chunk:
        spec.run -> clean_rows -> normalize_regions -> attributes.extract -> coverage.apply -> resolve_service_sums
        >>> THIS TASK: pricing.match_prices(chunk rows, ...) with a key cache shared across chunks
        -> RowStats.add -> Parquet rows -> roll()
     validate_stats -> _finish with rolled-up lines
```

Large files are analyzed and turned into the contract from **rolled-up lines** (`contract.ROLLUP_KEYS`), and the
normal path also rolls up above `CONTRACT_MAX_CHARGES` (50,000). Price columns must survive that (step 7).

## C. What exists after tasks 01–05

| Area | State |
|---|---|
| Cost types | `billed_cost`, `list_cost`, `contracted_cost`, `effective_cost` typed, from the file only (task 01) |
| Coverage | `coverage` on every usage line (task 01) |
| Line attributes | from export columns or text (task 03): `instance_type`, `operating_system`, `tenancy`, `license_model`, `pre_installed_sw` (task 05 fix), `database_engine`, `deployment_option`, `usage_type`, `operation`, `sku_id` (AWS `product/sku`), `sku_price_id` (AWS `pricing/RateCode`), `meter_id`, `region` (a code), `unit`, `quantity`, `charge_start`, `list_unit_price` (when printed). Each value's `origin` says printed or inferred. |
| Price catalog | `prices/` (task 05), **AWS only**, in billparse's own database (`DATABASE_URL`; SQLite in tests): `prices.store.get_catalog()`, `PriceCatalog.lookup(keys)`, `PriceCatalog.version`, `prices.normalize`, `prices.units.normalize` |
| Prices for Azure and GCP | **none yet**: their lines are reported as `no_catalog`, never as unmatched |

How `PriceCatalog.lookup` works (read `prices/store.py` in full):

- **Input:** a DataFrame, one row per **distinct** key: `key_id`, `as_of`, and any of `provider` (default aws),
  `rate_code`, `sku`, `service_code`, `usage_type`, `operation`, `region`, `instance_type`, `operating_system`,
  `tenancy`, `license_model`, `pre_installed_sw`, `database_engine`, `deployment_option`, `term_type` (default
  `on_demand`). It normalizes values itself (`prices.normalize`).
- **It matches in this order**, and the first way that matches wins: `rate_code` (exact price), then `sku`, then
  `usage_type` + `operation`, then `attributes`. It reports which one in `matched_by`.
- **Output:** one row per key that found something: `key_id`, `matched_by`, `candidates`, `unit_price` (empty when
  `candidates > 1`), `unit` (normalized: `hour`, `gb_month`, `request`, ...), `currency`, `sku`, `rate_code`,
  `effective_from`, `before_history` (the date is older than the catalog's oldest price), `tiered`. Keys that found
  nothing are absent.
- **It never guesses:** several fitting prices give `candidates > 1` and no price.

## D. Where every value in this task comes from

| Value | Source |
|---|---|
| `list_unit_price` | the file when printed (task 03); else the catalog, as of the line's date |
| `list_cost` | the file when printed; else `quantity × unit factor × list_unit_price` (catalog), `origin = "inferred"`, source `price_catalog` |
| `implied_unit_price` | `effective_cost / quantity` when effective is printed, else `billed_cost / quantity`: file values |
| `price_ratio` | `implied_unit_price / list_unit_price` |
| `contracted_cost` | the file when printed; else only by rule R2, `origin = "inferred"` |
| `effective_cost`, `coverage` | **file only. This task never fills or changes them.** |

**Never assume an attribute silently.** A value the line does not give stays empty and the price stays unmatched,
unless the request passed an explicit default (step 4) or the line's own text supports it (step 3), and either is
recorded in `price_assumptions`.

## E. Rules for this task

1. **Start clean:** `git status` is clean, on an up-to-date `main`, and `make test` passes. Branch:
   `task/06-price-matching`.
2. **Read before you write:** `prices/store.py`, `prices/normalize.py`, `prices/units.py`, `billparse/pipeline.py`
   (`_parse`, `_finish`), `billparse/bigfile.py` (`scan`, `roll`, `ROLL_KEYS`, `STEP_COLUMNS`),
   `billparse/contract.py` (`_charge`, `ROLLUP_KEYS`, `_rollup`, `UNIT_PRICES`, `shared_value`),
   `billparse/schema.py`, `billparse/export.py`, `billparse/validate.py` (`RowStats`). If the code differs from this
   file, stop and tell the user.
3. **Do not change existing samples or `EXPECTED`.** Pricing never changes `billed_cost`, the route or the status of
   an existing sample.
4. **Every schema change goes into `schema.py`, `export.py` (model and the field list in `to_schema`) and
   `webapi.py`** where the web app shows it. Bump `SCHEMA_VERSION` to `"1.8"` (task 05 used 1.7 for `pre_installed_sw`), and in the same bump remove the
   deprecated `analysis.commitment_opportunity` (task 05 moved this removal to 1.8).
5. **Large path:** everything here must give the same result on the large path (`LARGE_FILE_MB` set very low in a
   test) as on the normal path. Per-row Python only as a mapping over unique values.
6. **Bulk only:** the catalog is called with unique keys, once per file (or once per chunk with a cache), never per
   row.
7. **No network in tests.** Use the task 05 fixture catalog (`tests/fixtures/prices/`), loaded into a SQLite file.
8. **The parse never fails because of pricing.** No catalog, an empty catalog or a database error: skip with a
   warning.
9. **API changes:** `make web-types`, `make typecheck`, `make lint`.
10. **Docs:** README section "Price matching".
11. **Finish with a report:** what changed, each "Done when" item and how you verified it, the match rates of every
    sample file (part F), and anything you decided that this file does not specify.

---

## F. The task

### In one sentence

After a file is parsed, look every usage line up in the price catalog, record how it matched and how sure that is,
and fill the list price and list cost where the file did not print them, so consumers have the on-demand baseline
for every line that can be priced.

### 1. Module `billparse/pricing.py`

```python
@dataclass
class PriceDefaults:              # explicit, per request; all None = assume nothing
    operating_system: str | None = None    # "Linux"
    tenancy: str | None = None             # "Shared"
    pre_installed_sw: str | None = None    # "NA"
    license_model: str | None = None

class KeyCache:                   # key tuple -> lookup result, shared across the chunks of one large file

def match_prices(rows: pd.DataFrame, catalog: PriceCatalog | None, defaults: PriceDefaults,
                 cache: KeyCache | None = None) -> PriceStats:
    """Adds the price columns to rows in place; returns counters for the document summary (mergeable across
    chunks)."""
```

### 2. Which lines are priced

- **Priced:** usage lines (`charge_type` empty or `Usage`) of provider `aws`, **whatever their coverage**. Covered
  usage gets its list price too: that is the consumer's on-demand baseline.
- **Not priced:** tax, credits, refunds, purchases (commitment fees), negations, fees, support, and lines with no
  quantity. Their `price_match_level` is empty.
- **Azure and GCP usage lines:** `price_match_level = "no_catalog"`. The catalog has no prices for them yet. Do not
  count them as unmatched.

### 3. Build the keys

For each priced line, map the parsed fields to catalog key columns:

| Key column | From the line |
|---|---|
| `rate_code` | `sku_price_id` (CUR `pricing/RateCode`) |
| `sku` | `sku_id` (CUR `product/sku`), only for AWS lines |
| `service_code` | `product_code` when it is an AWS service code (CUR `line_item_product_code`, such as `AmazonEC2`), else the resolved `service_code` |
| `usage_type`, `operation`, `region`, `instance_type`, `operating_system`, `tenancy`, `license_model`, `pre_installed_sw`, `database_engine`, `deployment_option` | the same-named fields |
| `as_of` | `charge_start`; else the document's billing period start; else today, with a warning |
| `term_type` | always `on_demand`: list prices only |

Then:

- **Inferences from the line's own words.** Only for AWS EC2 lines with no `operation` and no `sku`, typically
  invoices:
  - When the line names an OS and no SQL Server, and `pre_installed_sw` is empty, use `NA`.
  - When the line does not say "Dedicated" or "Host", and `tenancy` is empty, use `Shared`.

  Record each one in `price_assumptions` (for example `pre_installed_sw=NA (text)`). Do nothing else of this kind.
- **Explicit defaults** (`PriceDefaults`) fill attributes that are still empty, also recorded in
  `price_assumptions` (for example `operating_system=Linux (request)`).
- **Deduplicate** the keys, give each a `key_id`, and call `catalog.lookup` **once** with the unique keys (on the
  large path: only the keys the cache does not have). Join the results back to the rows.

### 4. Match levels (`price_match_level`)

| Level | When |
|---|---|
| `exact` | `matched_by` is `rate_code`, `sku` or `usage`, with one candidate |
| `attributes` | `matched_by` is `attributes`, with one candidate, and no assumptions were needed |
| `assumed` | as `attributes`, but `price_assumptions` is not empty (text inferences or request defaults) |
| `ambiguous` | `candidates > 1`; `price_missing` lists the key columns that were empty and could tell the candidates apart (OS, tenancy, license, software, engine, deployment) |
| `unmatched` | no catalog row; `price_missing` names the missing key columns (no instance type, no region, ...) |
| `unit_mismatch` | a price matched but the line's unit cannot be converted to the price's unit; `price_missing = "unit"` |
| `no_catalog` | Azure or GCP line (step 2) |

### 5. Units and the list cost

- Normalize the line's `unit` with `prices.units.normalize`. It must equal the catalog row's normalized `unit`;
  otherwise the level is `unit_mismatch`.
- A line with no unit takes the price's unit only when `matched_by` is `rate_code`, `sku` or `usage`. Those identify
  the exact product.
- `list_unit_price` is filled from the catalog **only where the file printed none**, in the line's own unit (catalog
  price × factor).
- `list_cost = quantity × list_unit_price`, filled **only where empty**. Mark it `_list_cost_inferred = True`.
- `tiered` prices (S3 storage, data transfer): use the first tier and add `tiered` to `price_assumptions`. Task 07
  will flag large tiered lines.
- `before_history`: keep the price, and add `before_history` to `price_assumptions` so consumers see that an older
  bill was priced at the oldest known price.

### 6. Rules for inferred values

- **R1.** `list_cost` and `list_unit_price` are filled only where the file left them empty. A printed value always
  wins.
- **R2, contracted cost.** Consider only lines at level `exact` or `attributes`, with `coverage` `on_demand` or
  `unknown`. If at least 20 such lines, across at least 3 services, have `price_ratio` within ±1% of one value r,
  with 0.5 ≤ r < 0.99, then r is a steady negotiated discount:
  - fill `contracted_cost = list_cost × r` where it is empty, with `_contracted_cost_inferred = True`;
  - report `inferred_discount = 1 − r` in the summary, labeled an estimate.

  Otherwise infer nothing. On the large path, collect the ratio statistics across chunks and apply R2 when building
  the result from rolled-up lines.
- **R3.** Usage lines with `coverage = unknown` and a `price_ratio` below 0.5 (or a cost of 0) get one summary
  warning: "N lines cost far below list price and the file does not say why (possible commitment coverage)".
  **Never change `coverage`.**
- **R4.** `effective_cost` is never filled here.

### 7. Columns, schema and roll-ups

New row columns: `price_match_level`, `price_missing`, `price_assumptions`, `implied_unit_price`, `price_ratio`,
`_list_cost_inferred`, `_contracted_cost_inferred`; `catalog_sku_id` gets the matched catalog SKU.

- **`schema.Charge` and `export.Charge`:** add `price_match_level`, `price_missing`, `price_assumptions`
  (`CitedValue[str]`), and `implied_unit_price`, `price_ratio` (`CitedValue[Decimal]`), all with
  `origin="inferred"`, source `price_catalog`. Fill `catalog_sku_id`, which already exists. `contract._charge` sets
  `list_cost` and `contracted_cost` to inferred and `price_catalog` where the `_..._inferred` flags are set.
- **`schema.Document` and `export.Document`:** add `pricing`: `catalog_version`, `match_rate` (share of priced AWS
  usage cost at `exact` and `attributes`, and separately at `assumed`), `levels` (lines and cost per level),
  `missing` (the most common missing key columns, with cost), `inferred_discount`, `warnings`.
- **Roll-ups:**
  - `price_match_level` and `price_assumptions` become roll-up keys (`contract.ROLLUP_KEYS`, which `bigfile.ROLL_KEYS`
    uses).
  - `price_missing` is kept when all lines of a group agree.
  - The `_..._inferred` flags are rolled as "any line".
  - `list_cost` and `contracted_cost` are summed (they already are).
  - `implied_unit_price` and `price_ratio` are recomputed from the rolled sums.
  - Add them to `bigfile.STEP_COLUMNS` so the Parquet rows keep them.
- `SCHEMA_VERSION = "1.8"`. Remove `analysis.commitment_opportunity`.

### 8. Pipeline wiring

- `pipeline.Settings`: add `price_defaults: PriceDefaults`.
- **Normal path:** in `_parse`, after `resolve_services` and before the final `validate`, call `match_prices` when
  the accepted attempt's kind is `cost_lines`. Use `prices.store.get_catalog()`, and treat `version is None` as
  "no catalog".
- **Large path:** in `bigfile.scan`, per chunk, after `resolve_service_sums`, with one `KeyCache` for the whole
  file. Merge the `PriceStats`.
- Put the summary in the result as `pricing`, through `_finish`, and in `Document.pricing`.
- A pricing error never fails the parse. Catch it, set every priced line's level empty, and add a warning.

### 9. Request options and reprice

- `POST /parse/start` and `POST /parse`: optional query parameters `assume_os`, `assume_tenancy`, `assume_software`
  and `assume_license`, passed to `PriceDefaults` and stored in the summary.
- **`POST /documents/{sha256}/reprice`** (optional body: new defaults):
  - **normal path:** load the stored rows (`store.get_rows`), drop the price columns, and drop the values the
    `_..._inferred` flags mark. Run `match_prices` with the current catalog, then `validate`, `analyze` and
    `contract.build`, and save. Never re-parse.
  - **large path:** read the stored Parquet rows in chunks, and rebuild the rolled-up lines and statistics the same
    way. Reuse `bigfile`.
  - It returns the same task shape as reparse.
- The web app: show the match rate and the level counts on the document page, and the level, assumptions and
  missing fields in the charge detail. Run `make web-types`.

### Tests (`tests/test_pricing.py`)

Load the task 05 fixture into a SQLite catalog, plus a copy with Linux SQL Server variants (as in the task 05 review
test).

- **Levels:**
  - a CUR line with `pricing/RateCode` matches `exact` by rate code; one with only usage type and operation matches
    `exact` by usage;
  - an invoice-style line ("$0.192 per On Demand Linux m5.xlarge Instance Hour", region from its heading) matches
    `assumed`, with `pre_installed_sw=NA (text)` and `tenancy=Shared (text)`;
  - the same line with "Windows" and no license information is `ambiguous`, with `license_model` in
    `price_missing`;
  - a line naming no OS is `ambiguous` or `unmatched` with `operating_system` in `price_missing`, and with
    `assume_os=Linux` it is `assumed`;
  - Azure and GCP sample lines are `no_catalog`.
- **Covered usage:** a `SavingsPlanCoveredUsage` line gets a list price, and its `coverage` is unchanged.
- **Units:** usage in seconds matches an hourly price; GB-Mo matches `gb_month`; a mismatch gives `unit_mismatch`.
- **Dates and history:** a line dated before the catalog's oldest price is priced, with `before_history` in its
  assumptions. Add a second fixture snapshot with a changed price, and a line dated between the snapshots gets the
  older price.
- **R1:** a printed `list_cost` or `list_unit_price` is never overwritten.
- **R2:** a synthetic file with a steady 10% discount gives `inferred_discount ≈ 0.10` and inferred
  `contracted_cost`; random ratios infer nothing.
- **R3:** the warning, and `coverage` unchanged.
- **No catalog:** the parse succeeds with a warning, and nothing is priced.
- **Same results everywhere:** a CUR sample priced on the normal path, on the large path (`LARGE_FILE_MB` very low,
  small `CHUNK_ROWS`) and with `CONTRACT_MAX_CHARGES` set low gives the same `pricing` summary and the same
  `list_cost` total.
- **Reprice:** change the fixture catalog's price, reprice, and `list_cost` changes while the route and row count do
  not, on both paths.
- **Contract:** `export.to_schema` validates on every sample, and `POST /parse` returns the new fields.
- **Performance:** 500,000 rows with about 2,000 distinct keys are priced in under 10 seconds. Record the real time.
- `EXPECTED` unchanged.

### Done when

- [ ] Every AWS usage line has a `price_match_level`; Azure and GCP lines say `no_catalog`; nothing is dropped.
- [ ] No silent assumptions: every inference and default is in `price_assumptions`. `effective_cost` and `coverage`
      are never touched.
- [ ] The same results on the normal path, the large path and after roll-ups.
- [ ] Reprice works without re-parsing, on both paths.
- [ ] Schema 1.8 in `schema.py`, `export.py` and the web types; `commitment_opportunity` removed.
- [ ] README "Price matching" section.
- [ ] **Stop and report to the user:** the `pricing` summary (match rate, levels, top missing fields) of every
      sample file. The user will check match rates on real files before task 07.

### Do not

- Do not estimate savings or use Archera rates.
- Do not look up Savings Plans or Reserved Instance rates for lines. Those are consumers' work; the catalog keeps
  them for later.
- Do not change `coverage` or `effective_cost`.
