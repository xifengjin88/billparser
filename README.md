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
volumes: `pgdata` (PostgreSQL, with the synonyms that review teaches) and `billparse-data` (uploads, result rows,
nightly reports, the service catalog).

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
3. **Rule mapper** (`mapper.py`): header words plus value profiles, month pivots, list-style lines. Not tried when
   a built-in spec recognized the file and read rows. Each column gets the field with the best score from:
   - **header rules** for every readable canonical field (costs, prices, IDs, product attributes, commitments;
     never `coverage`, which is derived), with negative words (`List Cost` never scores for `billed_cost`);
   - the **header dictionary** (`headers.py`): learned headers (table `header_synonym`, 3.5) first, then the seed of
     foreign and English headers (`data/header_synonyms.seed.json`: German, French, Spanish, Portuguese, Italian,
     Dutch, Japanese; 3.0). Case, accents and punctuation do not matter; a header two fields claim is no hit;
   - **value detectors** (`detect.py`, 3.0 when 80% of the sample values fit): instance types, regions, account
     IDs, currencies, usage types, operations, resource IDs, dates, Azure meter IDs, and SKUs. Instance types and
     SKUs are checked against the price catalog when one is loaded (half the distinct values must be products). A
     detector never proposes a cost or a quantity: those need a header or the dictionary. The weak ID patterns
     (12 digits, a GUID, a GCP project ID) propose an account ID only under a meaningless header (`Col_1`, none) or
     one that says account, subscription, project, tenant, payer or linked (the GCP project ID: project only).

   A pair rejected in the dictionary is never taken. Two unit-price columns (Azure's `UnitPrice` and
   `EffectivePrice`) are told apart by line math: the one where quantity x price = cost (within 1%) is `unit_price`,
   another is `list_unit_price` (its header says list, public or on demand) or `contracted_unit_price`. Columns a
   spec leaves out (built-in, learned or LLM) are still guessed with the rules of before, so those results do not
   change with the mapper.
4. **LLM spec** (`llm.py`): redacted preview plus the most similar stored specs as examples. When the mapper was not
   accepted but settled some columns (score 3.0 or more), the prompt lists them ("Settled columns: field <- header")
   and asks to map only the others; a spec that changes one is kept (the checks decide), with a warning. The answer is the
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
confident attempt wins; a built-in spec is). A built-in spec is judged without its failed price checks: one (a SKU
the catalog does not have: an old bill, a SKU newer than the last price download) lowers its confidence, and may make
it need review, but never hands the file to another route, whose reading would replace the built-in's. The regression
gate prices too and rejects a new spec version that fails a price check the accepted result passed.

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

The mapper learns column headers (`headers.py`, table `header_synonym`, shared by every worker):

- **Approved review** with no charge changed: the headers of the spec that read the file (source `review`; count at
  once).
- **Spec verified** (the nightly promotion, Specs page, `specs approve`): its headers (source `spec`; count from the
  second confirmation).
- **Spec repaired** (nightly repair, agent improve round) that reads a header as another field: that header is
  rejected for the old field.
- **Admin**: the Mapping page's "Column headers" tab, or `GET/POST /headers`, `POST /headers/reject`,
  `DELETE /headers/{header_norm}/{field}`. Unapproved or unverified results teach nothing.

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

Corrections: `python -m billparse synonym "VM Hrs" "azure:Virtual Machines"`, the Mapping page (`/aliases`), and
review approval (each corrected service code). They are synonyms in the table `service_synonym` (`synonyms.py`;
scope `*` or a provider, the normalized label, the service key, source review or admin, who and when), shared by the
API and every worker: one statement per change, so two approvals at once lose nothing. Each process reads the table
again when another one changed it (at most every 60 s; at once after its own change) and drops its cached picks of
the changed labels. The old file `CATALOG_DIR/synonyms.json` is imported once into an empty table on first use (a
`journal` entry, stream `synonym_import`, records it) and is never changed; `svcres` on its own (without billparse)
still keeps its synonyms in that file.

## Price catalog

`prices/` keeps the public AWS (and Azure: see "Azure" below) list prices in billparse's own database (tables `price_snapshot`, `price_product`,
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

### GCP prices

The GCP public list prices come from the Cloud Billing Catalog API (`services.list`, then `services.skus.list` per
service, by `nextPageToken`). The API needs an API key: set `GCP_BILLING_API_KEY` (a Google Cloud project with the
Cloud Billing API enabled). Without it the fetcher stops with a message, and the weekly job skips GCP.

```bash
uv run python scripts/gcp/fetch_gcp_prices.py --out ~/gcp-price-files       # every Google Cloud service
uv run python scripts/gcp/fetch_gcp_prices.py --out ~/gcp-price-files --service "Compute Engine"
make prices ARGS="--provider gcp"                                          # load GCP_PRICE_FILES_DIR
make prices ARGS="--provider gcp --only 6F81-5844-456A"                    # one service; the others stay
```

The download is `skus/<service ID>.jsonl.gz` (one SKU per line, as the API returns it) and `manifest.json`
(`"provider": "gcp"`). It loads into the same tables (provider `gcp`), with the same history and sanity rules; each
GCP service is one price list, named `gcp:<service ID>`:

- **Product** = `skuId`. `service_code` is the service ID (what a GCP export prints as `service.id`),
  `product_family` the resource family, `usage_type` the SKU's usage type (OnDemand, Preemptible, Commit1Yr, ...),
  `region` the SKU's one service region (none when it lists several); the description, resource group, every
  region and the units are in `attributes`.
- **Rates**: one per tier of the latest `pricingInfo` (rate code `<skuId>.t<startUsageAmount>`), price = `units` +
  `nanos`, in the usage unit (`h`, `GiBy.mo`, normalized to hour, gb_month, ...), from `effectiveTime`. The term:
  `on_demand`, `spot` (Preemptible) or `committed_use` with its lease (Commit1Yr, Commit3Yr).
- **One snapshot for both.** A load compares and changes only its own provider's price lists: a full AWS load never
  calls the GCP price lists missing, nor closes their rates, and the reverse. A GCP load on the day of the current
  snapshot adds to it (`price_snapshot.source` lists both).
- The weekly job loads GCP after AWS when `PRICE_PROVIDERS` includes `gcp` (default `aws`: nothing changes until it
  is turned on) and the key is set.

Database size: 9.8 GB. Lookup of 10,000 distinct keys: by rate code 0.5–0.8 s, by SKU 0.3–0.4 s, by usage type and
operation 1.3–5.3 s (15 s without the operation: an EC2 usage type then fits every OS, and most keys are ambiguous), by
attributes 2.6 s (11 s when the OS is not given: then every key is ambiguous), Savings Plans 0.2–1.3 s. A usage type
with the `USE1-` prefix also matches the price list's us-east-1 form without it.

### Azure

The Azure retail prices go into the same tables (provider `azure`; migration `a7d2e9c41b05` adds
`price_product.meter_name`, `product_name`, `sku_name` and an index on the meter name). The source is the Azure Retail
Prices API (`https://prices.azure.com/api/retail/prices`): public, no key, no cost.

```bash
python scripts/azure/fetch_azure_retail_prices.py --out ~/azure-price-files               # ~20 most billed services
python scripts/azure/fetch_azure_retail_prices.py --out ~/azure-price-files --service "Virtual Machines" --region eastus
make prices-azure                             # load AZURE_PRICE_FILES_DIR as one snapshot
make prices-azure ARGS='--only "Virtual Machines"'
```

The fetcher follows `NextPageLink` (at most 1,000 items, about 590 KB of JSON, per request), waits and retries when
the API answers 429, and writes each service and region as gzip JSON Lines (the API's items unchanged) with a
`manifest.json` (provider `azure`). Set `PRICE_PROVIDERS=aws,azure` and the weekly refresh downloads and loads Azure
after AWS (`AZURE_PRICE_SERVICES`, `AZURE_PRICE_REGIONS`; default `aws` only, so nothing changes until it is set).

- A product is one meter of one SKU: `sku` = meterId/skuId/meterName, `usage_type` = the meter ID (a line's `MeterId`),
  `service_code` = serviceName, `region` = armRegionName, `instance_type` = armSkuName, `meter_name`, `product_name`,
  `sku_name`; a VM meter's OS from its product name ("... Series Windows" is the Windows meter, else Linux).
- A rate is one price: `on_demand` (Consumption), `dev_test` (DevTestConsumption) or `reserved` (Reservation, 1yr /
  3yr; the API prints the whole term's price, stored per hour). Tiers: `begin_range` = tierMinimumUnits. The unit of
  measure's block is normalized: "10K" is per request (price / 10,000), "1 GB/Month" per GB-month, "1/Month" per
  count-month. `effective_from` is effectiveStartDate.
- Same history and sanity check as AWS; a price list is `azure:<service name>`. A full load compares only its own
  provider's price lists and keeps the other provider's: AWS and Azure loaded on the same day are one snapshot.
- A full download is not measured. Estimate (2026-10-27): the API answers 400 past `$skip=1,000,000`, and the
  unfiltered list still had a full page at 999,000, so it has more than 1M items: at least 1,000 requests, about 600 MB
  of JSON (about 50 MB gzip), 20 to 40 minutes at the API's rate limit. Each query is capped at 1M items, so the fetcher
  asks per service (and per region when given); a query that reaches the cap is reported as an error.

## Price matching

After a parse, `billparse/pricing.py` looks every AWS usage line (and every Azure or GCP usage line, once that
provider's prices are loaded) up in the price catalog at the public on-demand list price, as of the line's date (else
the printed billing period, else today, with a warning). It runs on the
normal path after the service resolution, and on the large path per chunk, with one key cache for the file. The
catalog is called once per file with the distinct keys, never per line, and the work that depends only on the key
(cleaning, assumptions, what is missing) is done once per key: 500,000 lines with 2,000 keys take about 1.5 s. Covered usage gets its list price too (the consumer's on-demand baseline); `coverage` and `effective_cost` are
never changed, and no savings are estimated.

What pricing adds to a parse (task 14; a 300,000-row synthetic CUR from `scripts/bench_large.py`, the test fixture
catalog, best of 3, the time with prices less the time with no catalog): in memory 2.2 s (8.7 s against 6.5 s), was
3.5 s; on the large path 1.2 s (7.1 s against 6.0 s), was 2.0 s. The catalog is not the cost (its lookups take about
0.02 s per file): the time is work per line. Task 14 removed copies of every column of the priced lines, flag columns
read with a Python call per line (pricing, the checks and the contract now call it once per distinct value), and text
conversions of lines that cannot have a price conflict. The results are byte-identical. The accepted attempt and the
final pass both price the lines: the final pass runs after the service resolution, which sets `service_code` and
`provider` (the keys and R2 read them), so it cannot reuse the attempt's prices.

Each line gets `price_match_level`:

| Level | When |
|---|---|
| `exact` | rate code (`pricing/RateCode`), SKU, or usage type + operation; Azure: meter ID: one price |
| `attributes` | instance type, region, OS, tenancy, license, software, engine, deployment; Azure: meter name, region, (VM) OS: one price |
| `assumed` | as `attributes`, with an assumption in `price_assumptions` |
| `ambiguous` | several prices fit; `price_missing` names the empty columns that tell them apart |
| `unmatched` | no price; `price_missing` names what is missing (`instance_type`, `region`, `sku_id`, or `catalog_row`) |
| `unit_mismatch` | the line's unit cannot be converted to the price's unit |
| `no_catalog` | an Azure or GCP line while the catalog has no prices of its provider |

**GCP lines** are matched by their SKU ID only (`exact`): a GCP export prints it (`sku.id`, FOCUS `SkuId`). The price
is the SKU's own, whatever its term: a Spot SKU gives the spot price, a commitment SKU (the commitment fee) its
committed price. Committed-use covered usage prints the on-demand SKU and gets its list price, like AWS covered
usage; `coverage` stays as parsed. Of a tiered SKU the first paid tier is the price (a free first tier is an
allowance), noted `tiered`. The line's unit is converted to the price's: `hour`, `gibibyte month` as the export's
`usage.pricing_unit` prints them, and the SKU's base unit (`usage.unit`: `seconds`, `byte-seconds`) with the SKU's
`baseUnitConversionFactor`. A line without a SKU ID is `unmatched` with `price_missing` `sku_id`; a SKU no catalog row
has is `no_such_product` (or `service_not_loaded` when the catalog has no SKU of its service). The price checks
(`catalog_grounding`, `implied_unit_price`, ...) then cover GCP lines too.

**Azure lines.** Exact by the line's `MeterId` (any region: the meter is the price; a printed region or size that is
not the meter's is a `price_conflict`). Else by the meter name (`MeterName`) in the line's region, within its service
(`MeterCategory` = the retail serviceName) and, for VM meters, the meter's OS: Windows with the license included is
the Windows meter; Linux, RHEL, SUSE and Windows with Azure Hybrid Benefit (BYOL) are billed on the Linux meter. A VM
line whose OS is unknown (no `MeterSubCategory`) is `ambiguous` with `price_missing=operating_system`, until the request
says `assume_os`. The price's unit block is converted to the line's ("1 Hour" price on a "10 Hours" line: × 10).
Spot and Low Priority lines are their own meters (their price); reservation and savings plan usage prints the on-demand
meter, so it gets the on-demand list price, as AWS's covered usage does. An unmatched Azure line is
`service_not_loaded` or `catalog_row`, never `no_such_product` (a meter missing from a partial download is a catalog
gap). Azure exports print `PayGPrice`, so R1 leaves their list price as printed; `catalog_unit_price` is beside it and
the price checks compare them.

- **No silent assumptions.** An EC2 invoice line (no operation, no SKU) that names an OS and no SQL Server is looked
  up with `pre_installed_sw=NA (text)`; one that does not say Dedicated or Host with `tenancy=Shared (text)`. The
  request can add defaults (`assume_os`, `assume_tenancy`, `assume_software`, `assume_license`, `assume_region` on
  `POST /parse` and `/parse/start`): `operating_system=Linux (request)`, `region=us-east-1 (request)` (reseller
  invoices often print no region). Each is in `price_assumptions`; the line's own columns stay
  as parsed. A first-tier price (`tiered`) and a date before the catalog's oldest price (`before_history`) are noted
  there too.
- **Defaults, said.** A line matched by its attributes (AWS: instance type; Azure: meter name) that prints no region
  or OS, when the request gives none, is priced with the file's own most common value (`region=us-east-1 (file)`),
  else a setting: `PRICE_DEFAULT_REGION_AWS` (`us-east-1`), `PRICE_DEFAULT_REGION_AZURE` (`eastus`),
  `PRICE_DEFAULT_OS` (`Linux`; an empty value: none). Its level is `assumed`, the value is in `price_assumptions`,
  and `pricing.defaults_used` (the "Defaults used" table of the Prices tab) lists each one with its source, lines and
  cost. A line with its own SKU, rate code, usage type or meter ID assumes nothing; GCP lines never do.
  `PRICE_AUTO_DEFAULTS=0` keeps only the request's.
- **Currency.** The catalog's prices are in USD. A line that prints another currency is priced in its own currency
  with the rate it prints (`currency_conversion_rate`: units of its currency per USD; GCP exports print
  `currency_conversion_rate`, FOCUS `x_CurrencyConversionRate`, Azure cost details `ExchangeRatePricingToBilling`,
  Partner Center reconciliation `PCToBCExchangeRate`), noted in `price_assumptions`
  (`currency=EUR at 0.92 per USD (printed rate)`): `list_unit_price`, `list_cost` and `catalog_unit_price` are then in
  the line's currency, so the price checks compare one currency. A line with another currency and no rate is not
  priced (`unmatched`, `price_missing` `currency`). A line that prints no currency is taken to be in USD. AWS, Azure
  and GCP alike.
- **R1.** `list_unit_price` and `list_cost` are filled only where the file printed none (inferred, source
  `price_catalog`); `Document.cost_types` still lists only what the file gives.
- **R2.** When at least 20 on-demand lines matched `exact` or `attributes`, across at least 3 services, have the same
  ratio r of paid to list price (within 1%, 0.5 ≤ r < 0.99), `contracted_cost = list_cost × r` is filled where empty
  and `pricing.inferred_discount` is 1 − r, labeled an estimate.
- **R3.** Usage with unknown coverage far below list price (ratio < 0.5, or cost 0) gives one warning.
- `implied_unit_price` (effective, else billed cost / quantity) and `price_ratio` (/ list unit price) are on each
  priced line, recomputed from the sums on rolled-up lines.

`Document.pricing` has the catalog version, the match rate (share of priced AWS, Azure and GCP usage cost at `exact` and
`attributes`, and at `assumed`; also by lines), the lines and cost per level, the most common missing columns, the
inferred discount, the defaults used (`defaults_used`) and warnings, and `undated_as_of`: the date of lines that have none (the printed billing period,
else the day of the first pricing; a reprice uses it again, so it gives the same prices on any day). No catalog, an empty one or a database error: the parse goes on, unpriced, with a
warning. `POST /documents/{sha256}/reprice` (the web app's "Price again") prices a stored result again with the
current catalog, never parsing the file again (a large file: its stored rows, in batches); its optional body gives new
defaults, else the earlier ones stay.

## Storage

`store.py` keeps rows in the database (`db.py`): documents, every version of every learned spec, each spec's
regression set (`spec_doc`, `spec_fingerprint`), the logs (`journal`: records, labels, review), the learned
column headers (`header_synonym`) and service synonyms (`service_synonym`), and the admitted users. PostgreSQL with `DATABASE_URL`, else a SQLite file in `PARSER_DATA_DIR`. Large parts are files in
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
