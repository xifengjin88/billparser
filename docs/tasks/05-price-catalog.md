# Task 05: Price catalog

**Order:** after 04. **Needs:** nothing in code, but it is scheduled here because task 06 needs it.
**STOP gate: yes. Ask the user before writing any code (step 0).**
**Read first:** `00-START-HERE.md` section 4.

## In one sentence

Build a price catalog that billparse can query in bulk: for a provider, SKU or attributes, region, unit and date,
return the on-demand list unit price (and native commitment prices where the source has them) that was in effect on
that date.

## Where the prices come from

**Not from this repository.** billparse has no price data today. `svcres/` is a service-*name* catalog (names, codes,
categories) and has no prices; do not confuse the two, and do not change `svcres/`.

The company already downloads the cloud providers' prices every night, in another system. **This catalog must read
that data.** Fetching prices from the providers directly is a fallback, only if the user says so.

## Step 0: STOP and ask the user (required)

Before writing code, ask the user these questions and wait for answers:

1. Where does the nightly provider price data live: a database, an S3 bucket or other object storage, files, an
   internal API? How does billparse get access (credentials, network)?
2. What does one price record look like? Ask for a sample or a schema, for AWS, Azure and GCP.
3. Which pricing models does it include: on demand only, or also Reserved Instance, Savings Plan and committed use
   prices? Which terms and payment options?
4. Does it keep history (old prices with dates), or only the latest prices?
5. How often is it refreshed, and when does the refresh finish?

Write the answers into `docs/tasks/05-price-catalog-answers.md` and commit them with your branch.

- If the user cannot answer now, build everything below against the fixture catalog only (step 5), with a
  `FixtureSource`. Stop before step 4's real adapter, and report that the adapter is waiting for the answers.
- If the existing data keeps no history, the catalog keeps its own (step 3), starting from the first import.

## What exists today

Nothing for prices. Related pieces you will reuse:

- `svcres/regions.py` `lookup(label)` turns region names into codes (`US East (N. Virginia)` → `us-east-1`).
  Normalize every catalog region with it.
- `scripts/refresh_catalog.sh` shows the pattern for a safe nightly refresh: build new, sanity check, swap
  atomically. Copy the pattern, not the code.
- `analyze.get_resolver` shows how a catalog is loaded once per process and shared under a lock.

## Design

### Package `prices/` (beside `svcres/`)

```
prices/
  __init__.py
  model.py        PriceKey, PriceRecord (dataclasses)
  units.py        unit normalization and conversion
  aws_codes.py    AWS operation codes -> operating system / license (used in task 06)
  store.py        PriceCatalog: storage, snapshots, bulk lookup
  sources/
    __init__.py   Source protocol: iter_records(snapshot_date) -> Iterator[PriceRecord]
    fixture.py    FixtureSource (tests)
    company.py    the adapter for the company's nightly data (after step 0)
scripts/refresh_prices.py
```

### PriceRecord fields

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
| `pricing_model` | `on_demand` | on_demand, reserved, savings_plan, committed_use, spot |
| `term`, `payment_option` | `1yr`, `no_upfront` | empty for on demand |
| `unit` | `hour` | normalized (see units) |
| `unit_price` | `Decimal("0.192")` | per one normalized unit |
| `currency` | `USD` | |
| `effective_from`, `effective_to` | `2026-09-01`, None | `effective_to` None while current |
| `snapshot_id` | `2026-10-25` | which import |

### Units (`prices/units.py`)

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

### Storage (`prices/store.py`)

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
      #       deployment_option, unit, pricing_model (default on_demand)
      # returns: key_id + matched columns (unit_price, unit, currency, record ids, effective_from) for the best match
      #          per key; keys without a match are absent
  ```

  `lookup` is one SQL join in DuckDB, never a query per key.
- Load once per process, like `analyze.get_resolver`, with a lock. Reload when `version` on disk changes.

### Nightly refresh (`scripts/refresh_prices.py`, `make prices`)

1. Read every record from the configured source into a new snapshot directory.
2. **Sanity check:** for each provider, the record count must not drop by more than 10% from the previous snapshot,
   and no provider present before may be missing. On failure, delete the new snapshot, exit non-zero, and keep the
   old snapshot current.
3. Apply history (close changed records), then mark the snapshot current atomically (write a `CURRENT` file with the
   snapshot ID, using a temp file and rename).

## Step 5: Fixture catalog and tests

`tests/fixtures/prices/`: a small source with two snapshots, `2026-03-01` and `2026-04-01`, where one price changes
in April. Include:

- AWS EC2 `m5.xlarge` in `us-east-1`, Linux and Windows, on demand;
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
- regions given as names are stored as codes.

## Done when

- [ ] Step 0 answers recorded (or the report says they are pending, and the real adapter is not written).
- [ ] `prices/` package, `scripts/refresh_prices.py`, `make prices` target, `PRICES_DIR` setting.
- [ ] History, as-of lookups, sanity check and atomic switch all tested.
- [ ] README has a "Price catalog" section: where prices come from (the company source), how to refresh, how
      history works.

## Do not

- Do not wire the catalog into the pipeline. That is task 06.
- Do not add Archera rates (GRI, GSP). Consumers own them.
- Do not change `svcres/`.
