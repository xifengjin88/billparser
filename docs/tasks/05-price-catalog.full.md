# Task 05: Price catalog (self-contained)

This file holds everything you need for this task: the project context first (parts A–E), then the task itself
(part F). You do not need any other task file.

**Order:** start after task 04 (`task/04-remove-savings-estimates`) is merged into `main`, including its review
fixes. **Needs:** nothing in code (task 06 uses this catalog). **STOP gate: yes. Ask the user the step 0 questions
before writing any code.**

---

## A. What billparse is

A service that reads any cloud bill or cost file a customer uploads (AWS, Azure, GCP exports; PDF invoices; custom
Excel, CSV or JSON; Terraform) and returns **canonical line items** with proof that nothing is missing or counted
twice. Other teams' products consume the result (savings estimating, cost estimating, forecasting). **billparse
returns facts, never conclusions**: what was used, what it cost, and (after task 06) what it costs at the provider's
list price. Savings and Archera's own rates (GRI, GSP) belong to the consumers.

## B. Why this task exists

Today every money value comes from the uploaded file. Some files print the provider's on-demand list price (CUR
`pricing/publicOnDemandCost`, FOCUS `ListCost`, Azure `PayGPrice`); most do not (PDF invoices, custom spreadsheets,
Cost Explorer exports, reseller bills). Task 06 will fill the list price for those from a **price catalog**: the
provider's prices, looked up by SKU or by attributes, as of the line's date. This task builds that catalog. It does
not touch the parse pipeline.

The catalog's data must come from **the company's existing nightly provider price pull**, which lives outside this
repository. That is why this task starts by asking the user (step 0).

## C. What exists after tasks 01–04

| Area | State |
|---|---|
| Cost types | billed, list, contracted, effective as typed fields, from the file only (task 01) |
| Large files | chunked large path with bounded memory, `billparse/bigfile.py` (task 02) |
| Line attributes | read from export columns (task 03): `instance_type`, `operating_system`, `tenancy`, `license_model`, `database_engine`, `deployment_option`, `usage_type`, `operation`, `sku_id`, `sku_price_id`, `meter_id`, `region` (always a code), `unit`, `charge_start` |
| Analysis | facts about commitment-eligible usage, no savings (task 04) |
| Service catalog | `svcres/`: service names, codes and categories only. **No prices. Do not change it.** |
| **Prices** | **none anywhere in this repo: this task** |

The attribute fields in the table are what task 06 will look prices up with, so the catalog must be keyed by the
same fields, spelled the same way (see "Normalized values" in part F).

## D. Rules for this task

1. **Start clean:** `git status` is clean, on an up-to-date `main`, and `make test` passes. Branch:
   `task/05-price-catalog`.
2. **Step 0 first.** Ask the user the step 0 questions and wait. Record the answers in
   `docs/tasks/05-price-catalog-answers.md` on your branch.
3. **Read before you write:** `svcres/regions.py` (`lookup`), `svcres/catalog.py` and
   `scripts/refresh_catalog.sh` (the safe-refresh pattern), `analyze.get_resolver` (load once per process),
   `billparse/settings.py` (settings), `transforms.parse_block_unit` (Azure unit blocks), and the task 03 samples
   in `samples/make_samples.py` (the attribute values bills produce). If the code differs from this file, stop and
   tell the user.
4. **Do not wire the catalog into the pipeline.** That is task 06. `make test` must pass with no change to any
   parse result or `EXPECTED` entry.
5. **No network in tests.** Use the fixture source.
6. **Bulk only:** every lookup is one query over many keys, never one query per key.
7. **Docs:** add a "Price catalog" section to `README.md`; add new settings to `example.env` with comments.
8. **Finish with a report:** what changed, each "Done when" item and how you verified it, the step 0 answers, and
   anything you decided that this file does not specify.

Commands:

```bash
uv sync --extra dev
make test
make lint
uv run python scripts/refresh_prices.py --help
```

## E. Words used below

- **Snapshot:** one nightly import of all prices.
- **Record:** one price: provider, SKU or attributes, region, pricing model, unit, unit price, the dates it applied.
- **Key:** what a bill line is looked up by (task 06 builds keys from parsed lines).

---

## F. The task

### In one sentence

Build a price catalog that billparse can query in bulk: for a provider, SKU or attributes, region, unit and date,
return the on-demand list unit price (and native commitment prices where the source has them) that was in effect on
that date.

### Where the prices come from

**Not from this repository.** billparse has no price data today. `svcres/` is a service-*name* catalog (names, codes,
categories) and has no prices; do not confuse the two, and do not change `svcres/`.

The company already downloads the cloud providers' prices every night, in another system. **This catalog must read
that data.** Fetching prices from the providers directly is a fallback, only if the user says so.

### Step 0: STOP and ask the user (required)

Before writing code, ask the user these questions and wait for answers:

1. Where does the nightly provider price data live: a database, an S3 bucket or other object storage, files, an
   internal API? How does billparse get access (credentials, network)?
2. What does one price record look like? Ask for a sample or a schema, for AWS, Azure and GCP.
3. Which pricing models does it include: on demand only, or also Reserved Instance, Savings Plan and committed use
   prices? Which terms and payment options?
4. Does it keep history (old prices with dates), or only the latest prices?
5. How often is it refreshed, and when does the refresh finish?

Write the answers into `docs/tasks/05-price-catalog-answers.md` and commit them with your branch.

- If the user cannot answer now, build everything below against the fixture catalog only (see "Fixture catalog and tests"), with a
  `FixtureSource`. Do not write `sources/company.py`; report that the adapter is waiting for the answers.
- If the existing data keeps no history, the catalog keeps its own (see "History" under Storage),
  starting from the first import.

### What exists today

Nothing for prices. Related pieces you will reuse:

- `svcres/regions.py` `lookup(label)` turns region names into codes (`US East (N. Virginia)` → `us-east-1`).
  Normalize every catalog region with it.
- `scripts/refresh_catalog.sh` shows the pattern for a safe nightly refresh: build new, sanity check, swap
  atomically. Copy the pattern, not the code.
- `analyze.get_resolver` shows how a catalog is loaded once per process and shared under a lock.

### Design

#### Package `prices/` (beside `svcres/`)

```
prices/
  __init__.py
  model.py        PriceKey, PriceRecord (dataclasses)
  normalize.py    attribute values spelled one way (OS, tenancy, license model), for the catalog and task 06
  units.py        unit normalization and conversion
  aws_codes.py    AWS operation codes -> operating system / license (used in task 06)
  store.py        PriceCatalog: storage, snapshots, bulk lookup
  sources/
    __init__.py   Source protocol: iter_records(snapshot_date) -> Iterator[PriceRecord]
    fixture.py    FixtureSource (tests)
    company.py    the adapter for the company's nightly data (written after step 0's answers)
scripts/refresh_prices.py
```

#### PriceRecord fields

| Field | Example | Notes |
|---|---|---|
| `provider` | `aws` | aws, azure, gcp |
| `service_code` | `AmazonEC2`, `Virtual Machines`, `Compute Engine` | same codes as `svcres` uses, where possible |
| `sku_id` | AWS `product/sku`, GCP SKU ID | |
| `meter_id` | Azure meter ID | |
| `sku_price_id` | | when the source has one |
| `region` | `us-east-1`, `eastus`, `us-central1` | always the code (normalize with `svcres.regions.lookup`) |
| `instance_type` | `m5.xlarge`, `Standard_D4s_v5`, `n2-standard-4` | empty when not applicable |
| `operating_system`, `tenancy`, `license_model`, `database_engine`, `deployment_option` | | empty when not applicable |
| `usage_type`, `operation` | `USE1-BoxUsage:m5.xlarge`, `RunInstances` | AWS |
| `pre_installed_sw` | `NA`, `SQL Std`, `SQL Web`, `SQL Ent` | AWS EC2: software in the price (SQL Server licensing) |
| `capacity_status` | `Used` | AWS EC2: the price list has several on-demand prices per instance (`Used`, `UnusedCapacityReservation`, `AllocatedCapacityReservation`); a bill line is `Used` |
| `pricing_model` | `on_demand` | on_demand, reserved, savings_plan, committed_use, spot |
| `term`, `payment_option` | `1yr`, `no_upfront` | empty for on demand |
| `unit` | `hour` | normalized (see units) |
| `unit_price` | `Decimal("0.192")` | per one normalized unit |
| `currency` | `USD` | |
| `effective_from`, `effective_to` | `2026-09-01`, None | `effective_to` None while current |
| `snapshot_id` | `2026-10-25` | which import |

#### Normalized values (`prices/normalize.py`)

Task 06 matches bill lines to catalog records by attribute **values**, so both sides must spell them the same way.
Write one normalizer used by the catalog import here and by task 06 on the parsed lines:

| Field | Parsed lines today (tasks 01–03) | Normalized |
|---|---|---|
| `operating_system` | `Linux`, `Windows`, `RHEL`, `SUSE` (AWS `product/operatingSystem`, Azure from the meter) | `Linux`, `Windows`, `RHEL`, `SUSE`, `Red Hat Enterprise Linux with HA`, `Ubuntu Pro`: the AWS price list names |
| `tenancy` | `Shared`, `Dedicated`, `Host` | same; `default` → `Shared`, `dedicated` → `Dedicated` |
| `license_model` | `No License required`, `No license required`, `License included`, `Bring your own license` | case-insensitive: `No License required`, `License Included`, `Bring your own license` |
| `instance_type` | `m5.xlarge`, `db.r6g.large`, `Standard_D4s_v5`, `n2-standard-4` | as printed, trimmed; Azure sizes keep `Standard_` |
| `region` | codes (`svcres.regions`) | codes |

Normalize case and spacing, map known synonyms, and keep unknown values as they are, never empty them. Test that the
values in every task 03 sample normalize to the same strings as the fixture catalog's records.

#### Units (`prices/units.py`)

One function, `normalize(unit: str) -> tuple[str, float]`, returns the normalized unit and the factor that converts
one input unit into it. It covers at least these units:

| Input forms | Normalized unit | Factor |
|---|---|---|
| `Hrs`, `Hours`, `1 Hour`, `hour`, `Hr` | `hour` | 1 |
| `seconds`, `s` | `hour` | 1/3600 |
| `GB-Mo`, `GB-Month`, `1 GB/Month` | `gb_month` | 1 |
| `GB`, `1 GB` | `gb` | 1 |
| `Requests`, `1M requests`, `10K` | `request` | 1, 1,000,000 or 10,000 |

Azure units carry a block size (`10 Hours`, `100 GB`); use `transforms.parse_block_unit`. An unknown unit returns
`None`. Task 06 reports it.

#### Storage (`prices/store.py`)

- **DuckDB over Parquet**, partitioned as `<PRICES_DIR>/provider=<p>/snapshot=<id>/*.parquet`. `PRICES_DIR` is a new
  env var, default `data/prices`; add it to `example.env` and `billparse/settings.py`.
  - If the user's answers show the data already lives in PostgreSQL in a usable shape, a PostgreSQL view or table
    can replace this. Note the choice in the report.
- **History:** a new snapshot closes the previous record's `effective_to` only when the price changed. Otherwise the
  old record stays current. Lookups by date read the record whose `[effective_from, effective_to)` contains the
  date.
- **`PriceCatalog` API:**

  ```python
  class PriceCatalog:
      version: str                                   # latest snapshot id
      @classmethod
      def load(cls, prices_dir: Path) -> "PriceCatalog": ...
      def lookup(self, keys: pd.DataFrame) -> pd.DataFrame: ...
      # keys: one row per UNIQUE key, columns: key_id, provider, as_of, and any of sku_id, meter_id, usage_type,
      #       operation, instance_type, region, operating_system, tenancy, license_model, database_engine,
      #       deployment_option, pre_installed_sw, unit, pricing_model (default on_demand);
      #       capacity_status defaults to Used for AWS EC2
      # returns: key_id + matched columns (unit_price, unit, currency, record ids, effective_from) for the best match
      #          per key; keys without a match are absent
  ```

  `lookup` is one SQL join in DuckDB, never a query per key.
- Load once per process, like `analyze.get_resolver`, with a lock. Reload when `version` on disk changes.

#### Nightly refresh (`scripts/refresh_prices.py`, `make prices`)

1. Read every record from the configured source into a new snapshot directory.
2. **Sanity check:** for each provider, the record count must not drop by more than 10% from the previous snapshot,
   and no provider present before may be missing. On failure, delete the new snapshot, exit non-zero, and keep the
   old snapshot current.
3. Apply history (close changed records), then mark the snapshot current atomically (write a `CURRENT` file with the
   snapshot ID, using a temp file and rename).

### Fixture catalog and tests

`tests/fixtures/prices/`: a small source with two snapshots, `2026-03-01` and `2026-04-01`, where one price changes
in April. Include:

- AWS EC2 `m5.xlarge` in `us-east-1`, Linux and Windows, on demand, with a `Used` and an
  `UnusedCapacityReservation` price (the lookup must return `Used`), and a Windows with `SQL Std` price;
- AWS RDS `db.r6g.large` MySQL, Single-AZ;
- Azure `Standard_D4s_v5` in `eastus`, by meter ID;
- GCP `n2-standard-4` in `us-central1`, by SKU ID;
- one AWS Savings Plan rate, if the source shape includes commitment prices.

Tests in a new file, `tests/test_prices.py`:

- lookup by exact ID (Azure meter, GCP SKU), and by attributes (EC2: type, region, OS, tenancy);
- a key dated in March returns the March price, and a key dated in April returns the new price;
- bulk lookup of 10,000 keys on the fixture runs in under 1 second; record the real time;
- units: an hourly price matches usage given in seconds after conversion; an unknown unit returns None;
- refresh: a snapshot with 20% fewer records is refused and the old one stays current;
- regions given as names are stored as codes;
- attribute values from the task 03 samples normalize to the fixture's values (`prices/normalize.py`);
- an EC2 attribute lookup returns the `Used` price, never a capacity-reservation price.

### Done when

- [ ] Step 0 answers recorded (or the report says they are pending, and the real adapter is not written).
- [ ] `prices/` package (with `normalize.py`), `scripts/refresh_prices.py`, `make prices` target, `PRICES_DIR` setting.
- [ ] History, as-of lookups, sanity check and atomic switch all tested.
- [ ] README has a "Price catalog" section: where prices come from (the company source), how to refresh, how
      history works.

### Do not

- Do not wire the catalog into the pipeline. That is task 06.
- Do not add Archera rates (GRI, GSP). Consumers own them.
- Do not change `svcres/`.
