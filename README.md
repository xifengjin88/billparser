# billparse

Upload any cloud bill (AWS, Azure, GCP exports, reseller PDFs, custom Excel or JSON) and get
canonical line items, a validation verdict with confidence, resolved services, a spend summary,
a forecast range, and commitment-eligible spend. New layouts are learned once and reused
without LLM calls.

## Quick start

```bash
uv sync --extra dev                     # or: pip install -e ".[dev]"
python samples/make_samples.py          # 13 sample files in samples/files/
cp tests/fixtures/raw/*.json data/raw/  # tiny demo catalog; see "Service catalog" for the real one
python -m billparse parse samples/files/* --no-llm
python -m pytest -q                     # 41 tests, LLM routes use a fake LLM
```

With an API key, unknown layouts go to the LLM: `export ANTHROPIC_API_KEY=...`
(model via `BILLPARSE_MODEL`). API server: `uvicorn billparse.api:app`, then
`curl -F file=@invoice.pdf localhost:8000/parse`.

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

**Validation** (`validate.py`). Evidence that moves a confidence score:

| Check | What it catches |
|---|---|
| labels | cost with no service or description |
| line math | qty x unit price != amount (flags consistent ratios as unit or currency issues) |
| stated total | grand total anywhere (table row, header field, JSON metadata, any language via "one row equals the sum of the others"), or subtotals that sum to the lines |
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
4. Retrains the row classifier (decision tree) from rows of reconciled documents; installed only
   at 97%+ holdout agreement. Rules stay as fallback.
5. Writes a report with the metrics that should improve over time: first-route success,
   share with no LLM call, average LLM calls, reconciled share.

Every parse appends to `store/records.jsonl`; row labels go to `store/labels.jsonl`.

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

Everything is on disk under `store/` behind `store.py` (specs with versions, raw docs for
regression, results as JSON plus Parquet rows, JSONL logs). Swap for S3 and Postgres by
reimplementing that one class.

## Known limits

- Built-in column names come from public docs and are tested against synthetic files only.
  `azure-cost-analysis-export` and `gcp-cost-table` are marked unverified. Run real samples first.
- No OCR: scanned PDFs are detected and queued. Hook Azure Document Intelligence or Textract
  into `docmodel._read_pdf` to produce the same word-with-position rows.
- Commitment savings are indicative ranges; monthly bills hide hourly usage.
- Single process, synchronous. Stages are plain functions, so a queue can wrap them later.
