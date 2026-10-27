# Task 05: price catalog, step 0 answers

Recorded 2026-10-26 from the user's answers (2026-10-25 and 2026-10-26); updated 2026-10-27.

| # | Question | Answer |
|---|---|---|
| 1 | Where does the provider price data live, and how does billparse get access? | **The AWS Price List bulk files** (public, no login, no cost), downloaded by `scripts/aws/fetch_aws_price_files.py`. Not the Archera database or API: billparse is built in isolation. AWS first; Azure and GCP later. |
| 2 | What does one price record look like? | A product (`price_product`: one per SKU, the typed attributes the lookup matches on, every other attribute as JSON), its rates (`price_rate`: one per rate code: on demand or reserved, one tier, the unit normalized) and Savings Plans rates (`price_sp_plan` once per plan, `price_sp_rate` per plan and covered SKU). |
| 3 | Which pricing models? | All in the files: on demand, Reserved Instances (term, payment option, offering class, tiers, the upfront fee as its own rate) and Savings Plans (Compute, EC2 Instance, Database, Machine Learning). |
| 4 | Does it keep history? | Yes, the catalog's own: a rate is closed (`effective_to`) only when its price changes or it is gone. One version is downloaded (the current one, or `--as-of DATE`). |
| 5 | How often is it refreshed? | Weekly is planned (`make prices`), downloading only the price lists that have a new version (not built yet). |

Decisions:

- **Storage: PostgreSQL in billparse's own database** (user, 2026-10-25 and 2026-10-26: "save the pricing information in
  db and nightly run update and set up index so that query is faster"), not DuckDB over Parquet. SQLite in tests and
  the CLI, as the rest of the store.
- **Refresh: scheduled** (`make prices`, `scripts/refresh_prices.py`): not runtime work.
- **Source: the providers' public price files** (the user chose them over the Archera database and the AWS query API).
- **Lines without a SKU need their EC2 software, license and tenancy.** One m5.xlarge Linux in one region has six
  prices (plain and three SQL Server editions; Shared, Dedicated and Host), Windows three license models. The parser
  fills `pre_installed_sw`, `license_model` and `tenancy` from the CUR columns, the operation code, or the line's words,
  else by rule (NA software, Shared tenancy), marked inferred.
