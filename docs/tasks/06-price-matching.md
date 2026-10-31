# Task 06: Price matching stage

**Order:** after 05. **Needs:** 01 (cost fields, coverage), 02 (the large path), 03 (attribute and ID fields), 05 (price catalog).
**STOP gate:** none, but **stop at the end for user review** (they check match rates on real files before task 07
tunes thresholds on them).
**Read first:** `00-START-HERE.md` section 4, task 05's answers file.

## In one sentence

After a file is parsed, match every usage line to the price catalog, record how the match was made, and fill the
list cost where the file did not print one, so consumers have the on-demand baseline for every line that can be
priced.

## Where the values come from

| Value | Source |
|---|---|
| `list_unit_price`, `catalog_sku_id`, `price_match_level` | price catalog (task 05), as of the line's date |
| `list_cost` when the file printed none | quantity (from the file) × `list_unit_price` (catalog); `origin = "inferred"` |
| `list_cost` when the file printed one | the file; never overwritten |
| `implied_unit_price` | `billed_cost / quantity`, or `effective_cost / quantity` when effective is printed (file values) |
| `contracted_cost` when the file printed none | only by rule R2 below; `origin = "inferred"` |
| `effective_cost` | **file only. This stage never fills it.** |
| `coverage` | file only (task 01). This stage never changes it. |

## What exists today

- `billparse/pipeline.py` `_parse`. After the accepted attempt, it runs `share = resolve_services(rows, ...)`, then
  `verdict = validate(best.ext, alt=alt, resolved_share=share)`, then `_finish`. Rows are the DataFrame
  `best.ext.rows`.
- `store.get_rows(sha)` reloads stored rows. `store.save_result(sha, summary, rows)` stores the summary and rows.
  `webapi` has `POST /documents/{sha256}/reparse`, which parses again.
- Task 05 provides `prices.store.PriceCatalog` with bulk `lookup(keys)`, and `prices.units.normalize`.

## Steps

### 1. Module `billparse/pricing.py`

```python
@dataclass
class PriceDefaults:          # opt-in assumptions from the request; all None = assume nothing
    operating_system: str | None = None   # e.g. "Linux"
    tenancy: str | None = None            # e.g. "Shared"

def match_prices(rows: pd.DataFrame, catalog: PriceCatalog, defaults: PriceDefaults) -> dict:
    """Adds price columns to rows in place; returns the document-level summary."""
```

### 2. Which lines are priced

Usage lines only: `charge_type` is empty or `Usage`, and `coverage` is anything (covered usage is priced too, so
consumers can see the on-demand baseline). Skip tax, credits, refunds, purchases, fees, negations and support.

### 3. Build keys and deduplicate

For each priced line, build its key from:

- `provider`;
- the IDs: `meter_id`, `sku_id`, or `usage_type` + `operation`;
- the attributes: `instance_type`, `region`, `operating_system`, `tenancy`, `license_model`, `database_engine`,
  `deployment_option`;
- the unit, normalized with `prices.units.normalize`;
- `as_of`: `charge_start`, else the billing period start, else today, with a warning.

Deduplicate the keys and give each unique key a `key_id`. **Call `catalog.lookup` once with the unique keys**, then
join the results back to the rows on `key_id`. Never loop over rows calling the catalog.

### 4. Match ladder

Run each level only on the keys that are still unmatched. Record the level on every priced line in
`price_match_level`.

| Level | Value | Matches on |
|---|---|---|
| 1 | `exact_id` | `meter_id` (Azure) or `sku_id` (GCP, AWS `product/sku`), or AWS `usage_type` + `operation` |
| 2 | `attributes` | instance type, region, OS and tenancy all known (plus license, engine and deployment where the service needs them) |
| 3 | `defaults` | as level 2, with missing attributes filled from `PriceDefaults`; only when the request set them |
| 4 | `service_only` | only the service is known; no unit price |
| 5 | `unmatched` | nothing matched |

- AWS `operation` codes give the OS and license (for example `RunInstances:0002` is Windows). Use
  `prices/aws_codes.py`, and cite the AWS documentation in a comment there.
- **Never assume an attribute silently.** A line missing a required attribute, with no default, stops at level 4.
  Record which attributes were missing in `price_missing`, a semicolon-separated list. Operating system matters
  most: Windows and SQL Server licensing can double a rate.
- A unit that `normalize` does not know is level 5, with `price_missing = "unit"`.

### 5. Columns this stage adds to the rows

| Column | Content |
|---|---|
| `price_match_level` | the level name |
| `price_missing` | missing attributes, or None |
| `price_assumptions` | defaults used, or None |
| `list_unit_price` | from the catalog **only when the file did not print one** (task 03 maps printed ones) |
| `catalog_sku_id` | the matched catalog record's SKU or meter |
| `implied_unit_price` | see the table at the top |
| `price_ratio` | `implied_unit_price / list_unit_price` |
| `list_cost` | filled only where empty: `quantity × factor × list_unit_price` |
| `_list_cost_inferred` | True where this stage filled `list_cost` |

`contract._charge` reads `_list_cost_inferred` and `_contracted_cost_inferred` to set `origin="inferred"`,
`source="price_catalog"`.

Fields on `schema.Charge`:

- `list_unit_price` already exists from task 03. Fill it from the catalog only when it is empty.
- `catalog_sku_id` **already exists** on `schema.Charge` and in `export.py`. Fill it with the matched catalog
  record's SKU or meter. (The table above uses this name too.)
- New typed fields: `price_match_level`, `implied_unit_price`, `price_ratio`, `price_missing`, `price_assumptions`.

**Public contract** (`export.py`): add the five new fields to `export.Charge` and to the field list in
`export.to_schema`. `list_unit_price` was added in task 03, and `catalog_sku_id` is already copied. Consumers read
the on-demand list price from `list_unit_price` and `list_cost` (FOCUS `ListUnitPrice`, `ListCost`), with
`origin` saying whether the file printed it or the catalog supplied it. Add a document-level `pricing` object to
`export.Document` (match rate, catalog version, inferred discount, warnings) and copy it in `to_schema`.

Bump `SCHEMA_VERSION` to `"1.7"`. Do the 1.7 removal of `commitment_opportunity` promised in task 04 in the same
bump.

### 6. Rules for inferred values

- **R1.** `list_cost` is filled only where the file left it empty.
- **R2 (contracted cost).** Consider only lines with `coverage` `on_demand` or `unknown` and `price_ratio` present.
  If at least 20 such lines, across at least 3 services, have ratios within ±1% of one value r, with
  0.5 ≤ r < 0.99, then r is a steady negotiated discount:
  - fill `contracted_cost = list_cost × r` where it is empty, with `_contracted_cost_inferred = True`;
  - report `inferred_discount = 1 − r` at the document level, labeled an estimate.

  Otherwise infer nothing.
- **R3.** Lines with `coverage` `unknown` and a `price_ratio` below 0.5 or equal to 0 get a document warning: "N
  lines cost far below list and the file does not say why (possible commitment coverage)". **Do not change
  `coverage`.**
- **R4.** `effective_cost` is never filled by this stage.

### 7. Document-level summary (returned by `match_prices`, stored in the summary as `pricing`)

- `catalog_version`;
- `match_rate`: the share of priced usage cost at levels 1–2, and at level 3, separately;
- `levels`: line count and cost per level;
- `missing`: the top missing attributes, with cost;
- `inferred_discount`, if R2 found one;
- `warnings`.

Add `pricing` to `DocumentView` in `webapi.py`, show the match rate on the document page, and run
`make web-types`.

### 8. Pipeline wiring (`pipeline.py`)

- `Settings`: add `price_defaults: PriceDefaults` and `prices_dir` (from `PRICES_DIR`).
- In `_parse`, after `resolve_services` and before the final `validate`, call `match_prices` when the accepted
  attempt's kind is `cost_lines`. Load the catalog once per process.
- If the catalog is missing or empty, skip with a warning ("price catalog not available"). The parse must never
  fail because of pricing.
- Put the returned summary in the result as `pricing`, through `_finish`.

### 9. Request options and reprice

- `POST /parse/start` and `POST /parse`: add optional query parameters `assume_os` and `assume_tenancy`, passed
  through to `PriceDefaults`. Store them in the summary.
- New endpoint `POST /documents/{sha256}/reprice` (optional body: new defaults):
  - load the stored rows with `store.get_rows`;
  - drop the price columns and the inferred cost values (where `_list_cost_inferred` or
    `_contracted_cost_inferred` is set);
  - run `match_prices` again with the current catalog, then `validate` and `contract.build`, and save.
  - Never re-parse the file. It returns the same task shape as reparse.

## Tests (`tests/test_pricing.py`, using the task 05 fixture catalog)

- An Azure file matches at `exact_id` by meter; EC2 and RDS lines from a CUR match at `attributes`.
- A line without an OS and with no default is `service_only`, with `price_missing` containing `operating_system`.
  With `assume_os=Linux` it is `defaults`, with the assumption listed.
- Usage in seconds matches an hourly price.
- A March bill uses March prices; an April bill uses April prices.
- A synthetic file with a steady 10% discount gives `inferred_discount ≈ 0.10` and inferred `contracted_cost`.
  The same file with random ratios infers nothing.
- A file without amortized columns ends with `effective_cost` empty on every line after matching.
- A printed `list_cost` is never overwritten.
- Reprice after changing the fixture catalog changes `list_cost`, while the route and the row count stay the same.
- No catalog: the parse succeeds with a warning.
- Performance: 500,000 rows with about 2,000 unique keys are priced in under 10 seconds. Record the real time.
- Large path (task 02): `match_prices` runs per chunk inside `bigfile.py`, looking up each chunk's unique keys (with a
  cache across chunks), and its document summary is built from accumulated counters in `bigfile.Stats`. Test that a
  file priced through the large path (`LARGE_FILE_MB` set very low) gives the same match rate and `list_cost` total as
  the small path.
- `EXPECTED` routes and totals unchanged: pricing never changes `billed_cost`.

## Done when

- [ ] Every usage line has a `price_match_level`; unmatched lines are counted, never dropped.
- [ ] No silent defaults; `effective_cost` and `coverage` never inferred.
- [ ] Reprice works without re-parsing.
- [ ] Match rate visible in the result and on the document page.
- [ ] README describes the stage, the ladder, the defaults, R1–R4 and reprice.
- [ ] **Stop and report to the user with the match rates on all sample files.** They will check match rates on real
      files before task 07.

## Do not

- Do not estimate savings or use Archera rates.
- Do not let pricing failures fail a parse.
