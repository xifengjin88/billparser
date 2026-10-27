# billparse

Upload any cloud bill (AWS, Azure, GCP exports, reseller PDFs, custom Excel or JSON) and get
canonical line items, a validation verdict with confidence, resolved services, a spend summary,
a forecast range, and commitment-eligible spend. New layouts are learned once and reused
without LLM calls.

## Quick start

With Docker (PostgreSQL, the API, two workers and the web app):

```bash
cp example.env .env                     # optional: ANTHROPIC_API_KEY, AUTH, ...
make up                                 # web app http://localhost:8090, API http://localhost:8010/parse
make dev                                # the same with hot reload (API, workers and web app)
make logs-worker                        # what the workers do
make help                               # every target: logs, psql, migrate, nightly, backup-db, destroy, ...
```

The ports differ from invoice-parser's (8000/8080), so both stacks run side by side; `API_PORT` and `WEB_PORT`
change them. The API container runs the migrations (`alembic upgrade head`) before it starts. Data stays in two
volumes: `pgdata` (PostgreSQL) and `billparse-data` (uploads, result rows, nightly reports, the service catalog and
the synonyms that review teaches).

Without Docker (SQLite file in `store/` unless `DATABASE_URL` is set):

```bash
uv sync --extra dev
python samples/make_samples.py          # 13 sample files in samples/files/
cp tests/fixtures/raw/*.json data/raw/  # tiny demo catalog; see "Service catalog" for the real one
python -m billparse parse samples/files/* --no-llm
make api-local                          # API on :8000 (with SQLite it runs the jobs itself: INLINE_WORKER)
make web-local                          # web app on :5173 (cd web && pnpm install first)
make test                               # tests; LLM routes use a fake LLM
```

With an API key, unknown layouts go to the LLM: `ANTHROPIC_API_KEY=...` (model via `BILLPARSE_MODEL`;
`BILLPARSE_USE_LLM=0` turns it off).

## Web app and API

The web app (`web/`: React, Vite, shadcn/ui, react-query; nginx in Docker forwards `/api` to the API) has:

| Page | What it does |
|---|---|
| Upload | Drop files; each one is queued and parsed by a worker (`POST /parse/start`, polled at `GET /parse/tasks/{sha}`); a learning run shows apart, after the first result |
| Documents | Every file: status, confidence, total, route; a document's lines, checks, analysis and routes tried |
| Review | The queue (`needs_review`, `unsupported`, most money first). Edit or delete lines, add lines, map unresolved services to catalog services, **Check again** (validates and analyzes again, saves nothing), tick the checklist, **Approve** |
| Specs | Built-in and learned specs, every version, make one current, mark verified, the files each one parsed |
| Learning | Metrics, run the nightly loop in the background, nightly reports |

Approving keeps the corrected lines as the document's result (the parsed result stays beside it), closes the
review item, saves each service pick as a synonym, and marks a candidate spec verified when no line was edited.

API types for the web app come from the OpenAPI document: `make web-types`. `GET /parse` is the API page with
every endpoint. The endpoints of the CLI era stay: `POST /parse`, `GET /results/{sha}`, `/results/{sha}/rows`,
`/review`, `/specs/{id}`, `POST /specs/{id}/approve`, `/nightly`, `/metrics`, plus `/health`.

### Parse jobs (for other services)

```bash
curl -F file=@cur.csv.gz 'localhost:8010/parse/jobs?callback_url=https://hooks.example.com/billparse'
# 202 {"job_id": 41, "sha256": "...", "state": "queued", "status_url": "/parse/jobs/41", ...}
curl localhost:8010/parse/jobs/41      # state, progress, summary, learning; result_url once done
curl localhost:8010/documents/<sha256> # the document
```

- `POST /parse/jobs` queues a parse and answers at once (202). A file that is already queued or running answers its
  job (200). `GET /parse/jobs?sha256=&state=&limit=`, `GET /parse/jobs/{id}`, `POST /parse/jobs/{id}/stop`.
- `POST /parse` keeps its request and its `ParseResult`: it queues the parse and waits for it, up to
  `PARSE_WAIT_SECONDS` (300). A parse that takes longer (a large export) answers **202** with the job, to poll as
  above. The result is the first pass: learning runs after it.
- Webhooks: `callback_url` must start with one of `CALLBACK_ALLOWLIST` (empty: webhooks are off and a URL gets 400),
  https only (http://localhost in development). The worker POSTs `{"event", "job_id", "sha256", "state", "summary",
  "result_url", "sent_at"}` (never the rows) with `X-Billparse-Signature: sha256=<HMAC-SHA256 of the body with
  CALLBACK_SECRET>`, `X-Billparse-Event` and `X-Billparse-Job`; 10 s timeout, no redirects. Events: `parse.done`,
  `parse.error`, `parse.stopped`, and `result.updated` (learning proved a spec and the stored result changed), always
  after the job's `parse.*` event, never instead of it. A
  non-2xx answer is tried again after 30 s, 2 min, 10 min, 1 h and 6 h, then the callback is `failed`.
- `POST /nightly` queues the nightly loop (202); `GET /jobs/{id}` shows any job with its result (the nightly report,
  the price load). `GET /jobs/schedules`: each schedule's next run and last job. `GET /jobs/stats`: jobs per queue
  and state.

## Jobs and workers

Every parse, reprice, learning run, price refresh and nightly loop is a row of the `job` table (`jobs.py`;
PostgreSQL, migration `78fc8de19bf8`). The API only queues; worker processes run the jobs
(`python -m billparse worker --queues small:2,learn:1`):

| Queue | Jobs | Served by (docker compose) | Threads |
|---|---|---|---|
| `small` | parse and reprice of files under `LARGE_FILE_MB` | `worker` | `PARSE_WORKERS` (2) |
| `learn` | learning runs (the agent; they cost money) | `worker` | 1 |
| `large` | parse and reprice of large files | `worker-large` | `LARGE_PARSE_WORKERS` (1) |
| `maintenance` | the weekly price refresh, the nightly loop | `worker-large` | 1 |

- A worker claims a job with `FOR UPDATE SKIP LOCKED` (two workers never take the same job) and holds it with a
  60 s lease that it renews every 15 s. A worker that dies loses its lease: another worker takes the job again
  (at most 2 attempts; a learning run 1, because a retry costs money). A job never lives only in memory, so an API
  or worker restart loses nothing; `GET /parse/tasks/{sha}` works after a restart.
- At most one parse or reprice per file is queued or running (a partial unique index): a second upload of the same
  file gets the same job.
- Stop (`POST /parse/jobs/{id}/stop`, or Stop on a document) and the deadline per queue (`JOB_DEADLINE_SMALL` 1800 s,
  `_LEARN` 3600, `_LARGE` 7200, `_MAINTENANCE` 7200) end a job at the parse's next step: between two chunks of a
  large file.
- Learning is a job of its own: the parse answers with its first-pass result, and a learning run that proves a spec
  replaces the stored result later (webhook `result.updated`). Small files never wait for large files or learning.
- SIGTERM: a worker claims nothing more and lets its jobs finish (`stop_grace_period`: 2 min for `worker`, 10 min
  for `worker-large`); what is left comes back through its lease (a parse is idempotent per file).
- Schedules, from every worker (a `dedupe_key` per run, so two workers make one job): the AWS price refresh every
  `PRICES_REFRESH_DAY` (sun) at `PRICES_REFRESH_HOUR_UTC` (10), the nightly loop at `NIGHTLY_HOUR_UTC` (11). An
  empty value turns one off. The price refresh downloads into `PRICE_FILES_DIR.incoming-<date>` and replaces the
  previous files only when the download is complete; then it loads them (a load that loses more than 10% of a price
  list's rates is refused: the job ends `error` and the current snapshot stays). Stored documents are not repriced.
- More API or worker containers: `docker compose up --scale api=2 --scale worker=3` (behind a load balancer for the
  API). Their state is in PostgreSQL and the shared `billparse-data` volume.
- Connections to PostgreSQL: a worker opens at most its threads + 4 (`DB_POOL_SIZE` = threads + 2, overflow 2), the
  API at most 15. The default compose (api, worker with 3 threads, worker-large with 2) uses at most 15 + 7 + 6 = 28
  of PostgreSQL's 100.
- Local runs with SQLite (`make api-local`, the tests): `INLINE_WORKER=1` (the default with SQLite) runs a worker in
  the API process. With PostgreSQL outside Docker: `make api-local` and `make worker`.

Sign-in is optional and works as in invoice-parser: `AUTH=google` with a Google OAuth client, one email domain,
and an invite code the first time (see `example.env`). The redirect URI is `<address>/api/auth/callback`.

## How a file flows

```
intake (sha256, cache) -> read into document model -> route -> extract -> clean -> validate
  -> resolve services -> analyze -> store (result, rows, spec, learning records, review queue)
```

**Document model** (`docmodel.py`). Excel sheets and PDF pages become grids of cells with
position, bold, indent and formulas (merged cells filled). CSV, Parquet, JSON records and huge
sheets become DataFrames. gzip and zip are unpacked. JSON values outside the record list
(invoice number, totals) become a key/value grid.

**Routing** (`pipeline.py`), cheapest first, stops at the first acceptable result:

1. **Built-in spec**: known provider export, detected by headers (`specs/builtin/`).
2. **Learned spec**: header match or fingerprint similarity to a stored layout.
3. **Rule mapper** (`mapper.py`): header words plus value profiles, month pivots, list-style lines.
4. **LLM spec** (`llm.py`): redacted preview plus the most similar stored specs as examples. The answer is the
   input of an `answer` tool whose schema is a spec's (`spec_schema.py`, as loose as `check_spec`): always a whole
   JSON object, never cut-off text (an LLM without that falls back to reading JSON from its text). The first round
   asks for `BILLPARSE_SPEC_CANDIDATES` specs (default 2) at once, the second told to read the layout on its own;
   each is run and checked in the parse's own thread (price checks included), and the best one wins: accepted, else
   no failed critical check, else the fewest failed price checks, else the most confident. Every candidate is in the
   attempts log. Two that read the same rows, total and column values add `candidates_agree` (+0.05); where they
   differ, the next round is told the column. Validator problems are fed back for up to 3 rounds in all (round 1 has
   the candidates, each later round one call).
5. **LLM extract, then derive** (`induce.py`): the LLM reads the rows; the system searches for a
   deterministic spec that reproduces them. If found, it's saved, so the next file needs no LLM.
   If not, the LLM rows are returned but capped below auto-accept.

Specs are JSON run by one interpreter (`spec.py`); see `specs/README.md`. No generated code runs.

**Messy layouts** (`layout.py`, `rowclass.py`). Tables are found by header text and position,
anywhere on a sheet or across PDF pages. Every row gets a label: item, group header,
continuation, subtotal, total, header repeat, note. Group headers carry down as the service
(or as the provider when they name one). Wrapped description lines merge into the line above.
Subtotals with SUM formulas over rows above are recognized; row-wise SUMs are not.

**Context of each line** (`context.py`, `svcres/regions.py`). Headings above a line give its account, service
and region (a heading that is a whole AWS, Azure or GCP region name or code). The region is the code
(`ca-central-1`, `eastus2`), for every route; the printed name stays in `region_name`. A PDF row
printed in cells gives its quantity and unit ("588 Hrs") and keeps its other amounts by column name ("Pre-tax
charges"). Header rows over value rows ("Billing period | Account ID") are header fields. Each line has one type:
usage, tax, credit or commitment fee. They are never merged, and each keeps its service; the analysis shows each
service's money by type (`by_service_and_type`).

**Cost types and coverage** (`spec.py`, `costs.py`, `coverage.py`). Each line keeps the cost types its file prints,
as typed fields on the charge: billed (`cost`), `list_cost`, `contracted_cost` and `effective_cost`. Nothing is
priced or looked up: a value is printed in the file, or simple arithmetic on printed columns of the same row (Azure
`PayGPrice` × quantity, marked `inferred` with source `derived`). A cost type the file does not print stays empty;
`Document.cost_types` lists the ones it gives for every usage line (a type that only some lines have is
not listed, so a consumer never sums a partial column as a total). Effective (amortized) cost comes only from amortized data: AWS CUR
reservation and Savings Plan effective-cost columns by line type, Azure rows by `PricingModel` (on-demand and Spot
rows always; reservation and savings plan rows only in an amortized export, told apart in `Document.cost_basis`),
FOCUS `EffectiveCost`, GCP cost + credits.

Every usage line also says how it was priced (`coverage`): `on_demand`, `reserved`, `savings_plan`,
`committed_use`, `spot`, or `unknown` when the file does not say. It comes from the provider's line type or pricing
column (CUR `DiscountedUsage` → reserved, Azure `PricingModel`, FOCUS `PricingCategory` with
`CommitmentDiscountType`, GCP committed use discount credits, CUR `SpotUsage` usage types), and for files with no such
column from the line's words ("Savings Plan", "Reserved", "Spot", "On Demand"; words that say the usage was billed
at on-demand rates, such as "pay-as-you-go" or "not applied", win), marked `inferred` with source `text`.
Lines that are not usage (tax, credit, fee) have no coverage. In a CUR, the RI and Savings Plan fee lines carry
the commitment that was not used (`reservation/UnusedRecurringFee` + unused upfront fee; Savings Plan total commitment
− used commitment): that is their effective cost.

**AWS CUR** (`specs/builtin/aws-cur-legacy.json`, `aws-cur2.json`). A fixed format: every field is read from the
column of its exact name (`exact_columns`), legacy (`product/instanceType`) or 2.0 (`product_instance_type`, and the
`product` map for engine, OS and deployment). Columns the spec does not name are kept as extra text, never guessed.
The SKU (`product/sku`) and its exact price (`pricing/RateCode`) are `sku_id` and `sku_price_id`.

**Attribute and ID columns** (schema 1.5). Every built-in export reads a line's details from its own columns, and
text extraction (`attributes.py`) only fills what no column gave: instance type, OS, engine, deployment, tenancy,
license model, usage type, operation, pricing term and purchase option (CUR), meter, resource, reservation or savings
plan ID and the list and contracted unit prices (Azure: `MeterId`, `ResourceId`, `ReservationId`/`BenefitId`,
`PayGPrice`, `UnitPrice`, the VM size from `AdditionalInfo`), SKU, SKU price, resource and commitment IDs and unit
prices (FOCUS), SKU, resource and machine type from the system labels (GCP). An Azure VM meter name that is a size
(`D4s v5`) gives `Standard_D4s_v5`; a Spot or Low Priority suffix goes to the pricing model. A Savings Plan negation line (it cancels covered usage
printed at the on-demand rate) is its own kind, `commitment_negation`: not usage and not a credit.

**Validation** (`validate.py`). Evidence that moves a confidence score:

| Check | What it catches |
|---|---|
| labels | cost with no service or description |
| line math | qty x unit price != amount (flags consistent ratios as unit or currency issues) |
| stated total | grand total anywhere (table row, header field, JSON metadata, any language via "one row equals the sum of the others"), or subtotals that sum to the lines. For a tree of totals (PDF bills) the proof is exact: printed total = lines + the rounding at each printed subtotal, to the cent, and no subtotal may differ from its parts by more than half a cent per part (0.005 × (parts + 1)). Anything else is money counted twice or not read |
| row coverage | rows with amounts that didn't make it into line items |
| dual extraction | learned or LLM spec vs an independent rule-based extraction |
| plausibility | dates, mixed currencies, unexplained negatives |
| service resolution | share of usage cost mapped to a known service |
| catalog grounding | AWS usage naming products no catalog row has (`no_such_product`): a type, region or usage type read from a wrong column. Pass < 2% of the cost (+0.10), warn 2–10%, fail > 10% (−0.15) |
| printed list price | the file's own list unit prices against the catalog's (exact matches, first tier, inside the catalog's history): ≥ 95% within 1% pass (+0.10); clustered near a mix-up ratio fail (−0.20); else warn (−0.05) |
| implied unit price | what on-demand lines paid per unit against the list price: ≥ 90% of the cost at 0.5–1.05 pass (+0.10); > 5% near a mix-up ratio (×1000, ×3600, ×24, ×730, ×30.4 and the inverses) or more than 10× off fail (−0.20); else warn (a negotiated discount) |
| price units | > 5% of the AWS usage cost in a unit the price's cannot be converted to: warn (−0.05) |
| usage hours | one resource used more hours than its line's window has: fail (−0.10) |
| cost order | list ≥ contracted ≥ effective broken on > 2% of the lines that print two of them: warn (−0.05) |
| grounding values | rows an LLM wrote itself name an instance type or region the document does not print: fail (−0.15) |

The seven price checks read the catalog's list prices (`pricing.py`) and skip when no line was priced against it (no
catalog, Azure or GCP only): such a file gets the verdict it got before. A catalog value never checks itself: the
checks compare printed list prices and the file's own costs. None is critical yet; `catalog_grounding`,
`printed_list_price`, `implied_unit_price` and `usage_hours` count as internal checks (a pass can make a file
`consistent`). The attempts of a parse are priced too (one key cache per parse), so a failure lowers a wrong route,
goes to the LLM's repair and the agent, and a learned spec's attempt that fails one is not accepted at once (the most
confident attempt wins; a built-in spec is). The regression gate prices too and rejects a new spec version that fails
a price check the accepted result passed.

Status: `reconciled` (total matched), `unreconciled` (no total, other checks fine; forecast
ranges widen), `needs_review` (goes to the review queue), `auxiliary` (price sheets,
recommendations), `unsupported` (scanned PDF).

**Large files** (`bigfile.py`). A file of more than `LARGE_FILE_MB` uncompressed (CSV, CSV in gzip or zip,
Parquet, Parquet in zip) is never read whole. The upload is copied to disk in 8 MB parts. The routes run on its first
rows (`SAMPLE_ROWS`, fewer for a wide file: `SAMPLE_CELLS`); the accepted spec then reads the whole file in chunks
(`CHUNK_ROWS`, fewer for a wide file: `CHUNK_CELLS`), each chunk through the same steps as a small file. Only the
columns the spec names are read, and a line keeps no extra text of the other columns. A row with another number of
fields than the header cannot be read: the row coverage check fails. A byte that is not valid in the file's encoding
is replaced (with a warning). Learning runs of large files, as of small files, are jobs of the `learn` queue. The rows go to one Parquet file. Only sums stay in memory: the statistics the checks
read (the same checks, names and weights as a small file) and the lines rolled up by service, region, account,
charge type, coverage, month and the other keys of a charge. The analysis and the contract come from the rolled-up
lines, so their sums are exact; a charge counts its lines in its `lines` attribute. The source viewer shows the
first rows. Large parses run on their own queue (`large`, `LARGE_PARSE_WORKERS` threads); a waiting one shows `queued`.
`scripts/bench_large.py` measures one: 5M CUR rows (1.5 GB CSV) in 76 s with 1.7 GB of memory, against 118 s and
11 GB in memory.

**Analysis** (`analyze.py`). Spend by service, category and provider, tax/credits/purchases
separated, period detection, and a next-month and next-quarter forecast with a range that widens with lower
confidence and shorter history (commitment negations are not usage). `commitment_eligible_usage` gives facts, never
savings: the compute, database, container and analytics usage split by how it was priced (`coverage`: on demand,
reserved, savings plan, committed use, Spot, unknown), by category and by provider, region and instance family (top
50), each split also given as list cost and quantity per unit, so usage billed at 0 (reserved usage in an
actual-cost export) still shows its size; the commitment paid for but not used (Azure and FOCUS unused lines, the
unused part of AWS RI and Savings Plan fees), `null` with a note when the file does not print its amount (a CUR
without the unused-commitment columns), never 0; and the on-demand part by month. It uses the effective cost when
every eligible line has one, else the billed cost, and says which. Each commitment shows its term, payment option
and unused amount. billparse estimates no
savings and assumes no discount: that belongs to its consumers. `commitment_opportunity` was deprecated in schema
1.6 and is removed in 1.8.

## Learning loop

The workers run it every day at `NIGHTLY_HOUR_UTC` (11). By hand: `python -m billparse nightly`, or `POST /nightly`
(queued). It queues a learning job for every agent job that waits (with the agent on); it removes temporary uploads
older than a day that no job uses.

1. Retries every open review item with the current spec library and the LLM.
2. If a learned spec failed on a file (including when a renamed column stopped it matching),
   asks the LLM to repair that spec, then runs the **regression gate**: the new version must
   reproduce every file the spec parsed before (row count, total, confidence). Pass: promoted
   as a new version. Fail: rejected, old version stays.
3. Promotes candidate specs to verified after 3 clean files.
4. Writes a report with the metrics that should improve over time: first-route success,
   share with no LLM call, average LLM calls, reconciled share.

Every parse appends a record (route, status, checks) to the log.

Manage specs: `python -m billparse specs [list | show ID | approve ID | rollback ID VERSION | test ID]`.

## Service catalog

`svcres/` resolves product codes and labels to canonical services (code, synonym, exact name,
word prefix, fuzzy, optional LLM). Build the real catalog:

```bash
python scripts/fetch_azure.py && python scripts/fetch_aws.py
GCP_API_KEY=... python scripts/fetch_gcp.py
python scripts/build_catalog.py --raw-dir data/raw --out data/catalog.msgpack
```

Corrections: `python -m billparse synonym "VM Hrs" "azure:Virtual Machines"`.

## Price catalog

`prices/` keeps the public AWS list prices in billparse's own database (tables `price_snapshot`, `price_product`,
`price_rate`, `price_sp_plan`, `price_sp_rate`; migration `efba3358d2c4`). The source is the AWS Price List bulk files:
public, no login, no cost.

```bash
python scripts/aws/fetch_aws_price_files.py --all-services --savings-plans --out ~/aws-price-files   # ~1 GB gz
make prices                                   # load PRICE_FILES_DIR into DATABASE_URL as one snapshot
make prices ARGS="--only AmazonRDS"           # one price list; the others stay as they are
```

In Docker the workers do both every week (`PRICES_REFRESH_DAY`, `PRICES_REFRESH_HOUR_UTC`; see "Jobs and workers").

- **History, written only on change.** A rate row has `effective_from`/`effective_to`. A load closes the rates whose
  price changed or that are gone and inserts the new ones; an unchanged rate is not written again. AWS prints a recent
  `EffectiveDate` for most rates, so the oldest version of a rate (`first_version`) also answers earlier dates, and
  the lookup says so (`before_history`).
- **Sanity check.** A load that loses more than 10% of a price list's rates, or (a full load) misses a price list, is
  refused; the current snapshot stays.
- **Lookup** (`prices.store.get_catalog().lookup(keys)`): one SQL statement for many keys, by rate code, SKU, usage
  type + operation, or attributes (instance type, region, OS, tenancy, license, software, engine, deployment; give
  `service_code`: many services share instance types). When several prices fit, none is chosen (`candidates`).
  `lookup_savings_plans(keys)` gives the Savings Plans rates of a covered SKU.

Measured on a laptop (Docker PostgreSQL 17), all AWS price lists of 2026-10-27:

| | rows | load |
|---|---|---|
| AmazonEC2 | 2.2M products, 7.7M rates | 5 min 48 s |
| AWSComputeSavingsPlan | 16.3M rates | 1 min 56 s |
| the other 272 lists | 1.0M products, 1.6M rates, 0.2M Savings Plans rates | 2 min 56 s |
| a second load of unchanged files (RDS, 0.6M rates) | 0 rows written | 18 s |

Peak memory of a load: 1.9 GB (the EC2 files, read in 32 MB batches; 3.2 GB when each file was read whole). The
largest file is EC2 us-east-1: 18 MB gzip, about 400k rows of 90 columns.

Database size: 9.8 GB. Lookup of 10,000 distinct keys: by rate code 0.5–0.8 s, by SKU 0.3–0.4 s, by usage type and
operation 1.3–5.3 s (15 s without the operation: an EC2 usage type then fits every OS, and most keys are ambiguous), by
attributes 2.6 s (11 s when the OS is not given: then every key is ambiguous), Savings Plans 0.2–1.3 s. A usage type
with the `USE1-` prefix also matches the price list's us-east-1 form without it.

## Price matching

After a parse, `billparse/pricing.py` looks every AWS usage line up in the price catalog at the public on-demand
list price, as of the line's date (else the printed billing period, else today, with a warning). It runs on the
normal path after the service resolution, and on the large path per chunk, with one key cache for the file. The
catalog is called once per file with the distinct keys, never per line, and the work that depends only on the key
(cleaning, assumptions, what is missing) is done once per key: 500,000 lines with 2,000 keys take about 1.5 s. Covered usage gets its list price too (the consumer's on-demand baseline); `coverage` and `effective_cost` are
never changed, and no savings are estimated.

Each line gets `price_match_level`:

| Level | When |
|---|---|
| `exact` | rate code (`pricing/RateCode`), SKU, or usage type + operation: one price |
| `attributes` | instance type, region, OS, tenancy, license, software, engine, deployment: one price |
| `assumed` | as `attributes`, with an assumption in `price_assumptions` |
| `ambiguous` | several prices fit; `price_missing` names the empty columns that tell them apart |
| `unmatched` | no price; `price_missing` names what is missing (`instance_type`, `region`, or `catalog_row`) |
| `unit_mismatch` | the line's unit cannot be converted to the price's unit |
| `no_catalog` | an Azure or GCP line: the catalog has no prices for them yet |

- **No silent assumptions.** An EC2 invoice line (no operation, no SKU) that names an OS and no SQL Server is looked
  up with `pre_installed_sw=NA (text)`; one that does not say Dedicated or Host with `tenancy=Shared (text)`. The
  request can add defaults (`assume_os`, `assume_tenancy`, `assume_software`, `assume_license`, `assume_region` on
  `POST /parse` and `/parse/start`): `operating_system=Linux (request)`, `region=us-east-1 (request)` (reseller
  invoices often print no region). Each is in `price_assumptions`; the line's own columns stay
  as parsed. A first-tier price (`tiered`) and a date before the catalog's oldest price (`before_history`) are noted
  there too.
- **R1.** `list_unit_price` and `list_cost` are filled only where the file printed none (inferred, source
  `price_catalog`); `Document.cost_types` still lists only what the file gives.
- **R2.** When at least 20 on-demand lines matched `exact` or `attributes`, across at least 3 services, have the same
  ratio r of paid to list price (within 1%, 0.5 ≤ r < 0.99), `contracted_cost = list_cost × r` is filled where empty
  and `pricing.inferred_discount` is 1 − r, labeled an estimate.
- **R3.** Usage with unknown coverage far below list price (ratio < 0.5, or cost 0) gives one warning.
- `implied_unit_price` (effective, else billed cost / quantity) and `price_ratio` (/ list unit price) are on each
  priced line, recomputed from the sums on rolled-up lines.

`Document.pricing` has the catalog version, the match rate (share of priced AWS usage cost at `exact` and
`attributes`, and at `assumed`; also by lines), the lines and cost per level, the most common missing columns, the
inferred discount and warnings, and `undated_as_of`: the date of lines that have none (the printed billing period,
else the day of the first pricing; a reprice uses it again, so it gives the same prices on any day). No catalog, an empty one or a database error: the parse goes on, unpriced, with a
warning. `POST /documents/{sha256}/reprice` (the web app's "Price again") prices a stored result again with the
current catalog, never parsing the file again (a large file: its stored rows, in batches); its optional body gives new
defaults, else the earlier ones stay.

## Storage

`store.py` keeps rows in the database (`db.py`): documents, every version of every learned spec, each spec's
regression set (`spec_doc`, `spec_fingerprint`), the logs (`journal`: records, labels, review) and the admitted
users. PostgreSQL with `DATABASE_URL`, else a SQLite file in `PARSER_DATA_DIR`. Large parts are files in
`PARSER_DATA_DIR`: uploads (`docs/`), rows as Parquet (`results/`) and nightly reports.

Schema changes go through Alembic: change `billparse/db.py`, then
`DATABASE_URL=... uv run alembic revision --autogenerate -m "..."`.

## Known limits

- Built-in column names come from public docs and are tested against synthetic files only.
  `azure-cost-analysis-export` and `gcp-cost-table` are marked unverified. Run real samples first.
- No OCR: scanned PDFs are detected and queued. Hook Azure Document Intelligence or Textract
  into `docmodel._read_pdf` to produce the same word-with-position rows.
- The API and the workers share the `billparse-data` volume (uploads, result rows), so they run on one host. Files
  go through `Store`: object storage (S3) would change that one place.
- Webhooks go out from the workers: a receiver must be reachable from them, and on `CALLBACK_ALLOWLIST`.
- Large files (more than `LARGE_FILE_MB` uncompressed, default 100 MB): CSV and Parquet only, plain, in gzip, or as
  the one file of a zip. A large Excel, PDF or JSON file is refused ("Export this report as CSV"), and so is a file
  of more than `MAX_UNCOMPRESSED_GB` (default 50) uncompressed. A layout whose spec reshapes the table (a month per
  column) or reads a grid cannot be read in chunks. The agent works on a large file's first rows, in a learning job, as for
  a small file. A CSV row with another number of fields than the
  header is not read and the row coverage check fails; a small file keeps a row with fewer fields.
- Files just under `LARGE_FILE_MB` use the in-memory path, about 10 to 18 times the CSV size in memory (a 100 MB
  CUR: about 1 to 2 GB). Set `LARGE_FILE_MB` from the memory a parse may use.
- A large file's Azure cost basis is told chunk by chunk; chunks that disagree make it `unknown`, with a warning.
  More than `CONTRACT_MAX_CHARGES` rolled-up lines: the smallest become one "other" line per service, charge type,
  coverage and month.
