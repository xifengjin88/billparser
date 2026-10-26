# billparse

Upload any cloud bill (AWS, Azure, GCP exports, reseller PDFs, custom Excel or JSON) and get
canonical line items, a validation verdict with confidence, resolved services, a spend summary,
a forecast range, and commitment-eligible spend. New layouts are learned once and reused
without LLM calls.

## Quick start

With Docker (PostgreSQL, the API and the web app):

```bash
cp example.env .env                     # optional: ANTHROPIC_API_KEY, AUTH, ...
make up                                 # web app http://localhost:8090, API http://localhost:8010/parse
make dev                                # the same with hot reload (API and web app)
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
make api-local                          # API on :8000
make web-local                          # web app on :5173 (cd web && pnpm install first)
make test                               # tests; LLM routes use a fake LLM
```

With an API key, unknown layouts go to the LLM: `ANTHROPIC_API_KEY=...` (model via `BILLPARSE_MODEL`;
`BILLPARSE_USE_LLM=0` turns it off).

## Web app and API

The web app (`web/`: React, Vite, shadcn/ui, react-query; nginx in Docker forwards `/api` to the API) has:

| Page | What it does |
|---|---|
| Upload | Drop files; each one is parsed in the background (`POST /parse/start`, polled at `GET /parse/tasks/{sha}`) |
| Documents | Every file: status, confidence, total, route; a document's lines, checks, analysis and routes tried |
| Review | The queue (`needs_review`, `unsupported`, most money first). Edit or delete lines, add lines, map unresolved services to catalog services, **Check again** (validates and analyzes again, saves nothing), tick the checklist, **Approve** |
| Specs | Built-in and learned specs, every version, make one current, mark verified, the files each one parsed |
| Learning | Metrics, run the nightly loop in the background, nightly reports |

Approving keeps the corrected lines as the document's result (the parsed result stays beside it), closes the
review item, saves each service pick as a synonym, and marks a candidate spec verified when no line was edited.

API types for the web app come from the OpenAPI document: `make web-types`. `GET /parse` is the API page with
every endpoint. The endpoints of the CLI era stay: `POST /parse`, `GET /results/{sha}`, `/results/{sha}/rows`,
`/review`, `/specs/{id}`, `POST /specs/{id}/approve`, `/nightly`, `/metrics`, plus `/health`.

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
4. **LLM spec** (`llm.py`): redacted preview plus the most similar stored specs as examples.
   Validator problems are fed back for up to 3 repair rounds.
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
Lines that are not usage (tax, credit, fee) have no coverage. A Savings Plan negation line (it cancels covered usage
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

Status: `reconciled` (total matched), `unreconciled` (no total, other checks fine; forecast
ranges widen), `needs_review` (goes to the review queue), `auxiliary` (price sheets,
recommendations), `unsupported` (scanned PDF).

**Analysis** (`analyze.py`). Spend by service, category and provider, tax/credits/purchases
separated, period detection, next-month and next-quarter forecast with a range that widens
with lower confidence and shorter history, and indicative commitment-eligible spend.

## Learning loop

Run nightly: `python -m billparse nightly` (or `POST /nightly`).

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
- Commitment savings are indicative ranges; monthly bills hide hourly usage.
- Parses run in a thread pool in the API process (`PARSE_WORKERS`). A task is in memory: an API restart forgets
  running tasks (never stored results). One API container only.
