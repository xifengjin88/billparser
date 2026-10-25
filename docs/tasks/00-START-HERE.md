# START HERE: billparse improvement tasks (for Claude Code)

You are working in the `billparse` repository. This folder holds 10 tasks. Read this whole file before you touch any
task. It tells you what the system does today, what exists and what does not, where every number comes from, the
exact order of the tasks, and the rules for every change.

**Work on one task at a time, in file-number order (01, then 02, then 03 ...).** Every task depends only on tasks
with a lower number. Never start a task before the one before it is merged. Some tasks have a **STOP gate**: a
question you must ask the user and get answered before you write code.

---

## 1. What billparse is

A service that reads any cloud bill or cost file a customer uploads (AWS, Azure, GCP exports; PDF invoices; custom
Excel, CSV or JSON; Terraform) and returns **canonical line items** with proof that nothing is missing or counted
twice.

Other teams' products consume the result: savings estimating, cost estimating, forecasting. **billparse returns
facts, never conclusions.** It says what was used, what it cost, and (after task 05) what it costs at the provider's
list price. It never estimates savings. Savings and Archera's own rates (GRI, GSP) belong to the consumers.

The biggest business risk this work removes: **usage already covered by a Reserved Instance, Savings Plan or other
commitment must never look like on-demand usage.** If it does, a consumer will promise savings the customer already
has.

## 2. How a file flows today (read these files before task 01)

```
upload (webapi.start_parse -> thread pool)
  -> pipeline.Pipeline.parse_bytes
     -> _parse:
        docmodel.load_bytes            file -> Document of regions (frames for CSV/Parquet/JSON, grids for Excel/PDF)
        fingerprint.fingerprint         layout fingerprint
        routes, cheapest first, stop at the first accepted attempt:
          _builtin   specs/builtin/*.json chosen by spec.detect_score on the headers
          _learned   learned specs from the store (header match or fingerprint similarity)
          _mapper    mapper.propose_all (header rules + value profiles)
          _llm_spec  llm.generate_spec, up to Settings.max_repairs rounds
          _llm_extract  llm.extract_rows, then induce.derive_spec
        each attempt = _try: spec.run -> validate.clean_rows -> attributes.normalize_regions
                             -> attributes.extract (only if <= ATTRIBUTE_ROWS rows) -> validate.validate
        analyze.resolve_services        service names -> catalog services (svcres/)
        validate.validate(... alt=, resolved_share=)  final verdict
        _finish: analyze.analyze -> contract.build (schema.Document) -> store.save_result
     -> if agent.needs_agent(result): agent.run_job runs INLINE (learns a spec with the LLM)
```

Key data shapes:

- **Rows DataFrame.** `spec.run` returns an `Extraction` whose `rows` is a pandas DataFrame. Its columns are the
  **canonical fields** listed in `transforms.CANONICAL` (`billed_cost`, `list_cost`, `effective_cost`,
  `instance_type`, ...), plus internal columns starting with `_` (`_src` = where the row came from). A spec maps
  a source column to a canonical field in its `columns` section (`spec._map_columns`). Columns a spec does not map
  are guessed by `spec._keep_unmapped` (header rules) except the money fields in `spec.NEVER_ADDED`, or are kept as
  text in `_extra`.
- **Output contract.** `contract.build` turns the rows into `schema.Document` with a list of `schema.Charge`. Each
  value is a `CitedValue` (value + `Cite` saying where it is printed + `origin`: `printed` or `inferred`).
  `contract._charge` does the copying, field by field. Fields it does not know go into `Charge.attributes` (an
  untyped dict). Over `CONTRACT_MAX_CHARGES` (50,000) rows, `contract._rollup` merges lines that are alike in every
  key of `contract.ROLLUP_KEYS`.
- **Public contract: what consumers receive** (`billparse/export.py`). `export.to_schema(doc)` projects the
  internal `schema.Document` onto a **strict** copy of the schema (`extra="forbid"`) and validates it.
  `POST /parse` returns this (`api._contract`). Two consequences you must remember:
  1. A field added to `schema.py` but not to `export.py` (its model **and** the field list in `to_schema`)
     **never reaches consumers**.
  2. A new `Literal` value (for example a new charge `kind`) added to `schema.py` but not to the matching `Literal`
     in `export.py` makes `to_schema` **raise**, which breaks `POST /parse`.

  The output is billparse's own schema, not FOCUS. Field names follow FOCUS where one exists (`list_cost` ≈ FOCUS
  `ListCost`, `list_unit_price` ≈ `ListUnitPrice`); keep that mapping in the field comments.
- **Store.** `store.Store` saves the summary in the `documents` table, the rows as Parquet, extra parts with
  `save_part(sha, name, text)` / `part(sha, name)`.
- **Web app.** `billparse/webapi.py` builds `DocumentView` and `ChargeRow` for the React app in `web/`. Its
  TypeScript types are generated: `make web-types`.

## 3. What exists and what does not

| Area | Exists today | Missing (task) |
|---|---|---|
| Reading files | CSV, Parquet, JSON, Excel, PDF (text), zip/gzip, Terraform | streaming of very large files (09) |
| Routing and learning | built-in, learned, mapper, LLM spec with repair, LLM extract, agent, nightly loop, regression gate, versions | structured LLM output, parallel candidates (07) |
| Completeness proof | stated totals, tree of sums, row coverage, dual extraction, line math, plausibility | price-based checks (06) |
| Billed cost | yes: `billed_cost` → `Charge.cost` | |
| List cost | mapped only in the CUR specs and FOCUS; ends up untyped in `Charge.attributes` | typed field (01) |
| Contracted cost | nowhere | typed field from files that print it (01) |
| Effective (amortized) cost | FOCUS and a GCP derive only; untyped in `attributes` | typed field, CUR and Azure mapping (01) |
| Commitment coverage | **lost**: `transforms.CHARGE_TYPE_MAP` maps `SavingsPlanCoveredUsage` and `DiscountedUsage` to plain `Usage` | `coverage` field (01) |
| Instance type, OS, usage type | regex on description text (`attributes.py`); skipped above 100k rows; export columns not mapped | map export columns (02) |
| Commitment IDs, meter/SKU IDs | fields exist on `Charge`; no built-in spec fills them | map them (02) |
| Service names | `svcres/` catalog: names, codes, categories. **No prices.** | |
| **Prices** | **none. There is no price data anywhere in this repo.** | price catalog (04), matching (05) |
| Savings | `analyze._commitments` returns a hard-coded 20–40% "indicative savings" | remove (03) |
| Jobs | in-memory thread pool, lost on restart; agent runs inline in the parse | durable queue (08) |
| Header learning | English regex rules (`mapper.HEADER_RULES`); service-label synonyms only | header dictionary (10) |

## 4. Where every money value comes from (read this twice)

There are two different sources, and they must never be confused.

**A. Printed in the uploaded file (tasks 01–02).** The value is in a column or line of the customer's file. A spec
maps that column to a canonical field. Nothing is computed, nothing is looked up. `origin = "printed"`.

**B. Computed from the price catalog (tasks 04–05).** The value is computed by billparse: quantity × a unit price
from the price catalog. `origin = "inferred"`. The price catalog does not exist until task 04, and its data comes
from **the company's nightly provider price pull, which lives outside this repository.** Task 04 starts with a STOP
gate to ask the user where that data is.

| Value | Source in tasks 01–03 | Source after task 05 |
|---|---|---|
| billed cost | file (always; every bill prints it) | file |
| list cost | file, only if it prints one (CUR `pricing/publicOnDemandCost`, FOCUS `ListCost`, Azure `PayGPrice` × quantity, GCP `cost_at_list`) | file if printed, else quantity × catalog on-demand price |
| contracted cost | file, only if it prints one (FOCUS `ContractedCost`, Azure `UnitPrice` × quantity) | file if printed, else inferred from a steady discount ratio (task 05 rules) |
| effective cost | file, only if it prints amortized columns (CUR reservation/Savings Plan effective cost, Azure amortized export, FOCUS `EffectiveCost`, GCP cost + credits) | **file only. Never computed.** |
| coverage (on demand vs commitment) | file: line type / pricing model columns | file; task 05 may add a warning, never a value |
| list unit price, match level | not available | catalog |

**Rule: when a file does not print a value, leave it empty.** Empty is a correct answer. A guessed value is a wrong
answer that a consumer will trust. `Document.cost_types` (task 01) tells consumers which cost types the file
printed.

## 5. Task order

| # | Task | Needs | STOP gate |
|---|---|---|---|
| 01 | [Cost columns and commitment coverage from the file](01-cost-columns-and-coverage.md) | none | no |
| 02 | [Attribute, ID and commitment columns from export files](02-attribute-and-id-columns.md) | 01 | no |
| 03 | [Remove savings estimates from the analysis](03-remove-savings-estimates.md) | 01 | no |
| 04 | [Price catalog](04-price-catalog.md) | none (scheduled here because 05 needs it) | **yes: where do the nightly prices live?** |
| 05 | [Price matching stage](05-price-matching.md) | 01, 02, 04 | no |
| 06 | [Price-based validation checks](06-price-validation-checks.md) | 05 | no |
| 07 | [Structured LLM output and parallel spec candidates](07-structured-llm-output.md) | none | no |
| 08 | [Durable job queue](08-durable-job-queue.md) | none | **yes: PostgreSQL as the queue, or Redis?** |
| 09 | [Large-file reading](09-large-file-reading.md) | 08 | no |
| 10 | [Header dictionary and value-first column detection](10-header-dictionary.md) | 01, 02, 04 | no |

Why this order: 01–03 fix what the service already returns (correctness first, and 03 is small). 04–06 add pricing,
each needing the one before. 07 is small and independent, placed after the pricing chain so it does not interrupt
it. 08–09 are infrastructure; 09 uses the `large` queue that 08 creates. 10 is last because its value detectors use
the price catalog (04) to confirm instance types and SKUs.

**Review points.** After 02 and after 05, stop and tell the user. They will run the corpus check
(`make corpus-check`) on real customer files, which are not in this repo, before you continue.

## 6. Rules for every task

1. **Start clean.** `git status` is clean, you are on an up-to-date `main`, and `make test` passes before you start.
   Create a branch `task/NN-short-name`.
2. **Read before you write.** Every task names the files and functions to change. Open and read each one fully
   before editing. If what you find differs from what the task says, stop and tell the user what differs.
3. **Do not change existing samples or their expected results.** `EXPECTED` in `tests/test_pipeline.py` (routes and
   totals per sample file) must stay as it is. New behavior gets **new** sample generators in
   `samples/make_samples.py`, new files in `samples/files/`, and new entries in `EXPECTED`. Run
   `uv run python samples/make_samples.py` after adding a generator.
4. **Every schema change goes into three places:** `billparse/schema.py` (internal), `billparse/export.py` (the
   public contract: model, `Literal` values and the field list in `to_schema`), and `billparse/webapi.py` if the
   web app shows it. Add a test that `export.to_schema` validates on the new sample files and that `POST /parse`
   returns the new fields.
   **Schema changes are additive.** Never rename or remove a field in `billparse/schema.py` or a response model in
   `billparse/webapi.py`. Add fields; bump `schema.SCHEMA_VERSION` (one minor step per task that changes it) and
   explain the bump in the comment beside it.
5. **Money columns are special.** Every new money field (any `*_cost`) goes into `transforms.NUMERIC` and
   `spec.NEVER_ADDED`, and must be summed (not dropped) in `contract._rollup`.
6. **No network in tests.** LLM routes use `FakeLLM` (`tests/test_pipeline.py`). Price tests use a fixture catalog.
7. **Database changes** go through Alembic: edit `billparse/db.py`, then
   `uv run alembic revision --autogenerate -m "..."`, and check the generated migration by hand.
8. **API changes:** run `make web-types`, then `make typecheck` and `make lint`.
9. **Docs:** update `README.md` (and `specs/README.md` for spec-format changes) in the same branch.
10. **Provider column names must be verified.** Every provider column name in these tasks is a candidate taken from
    public documentation. Check each one against the provider's current documentation before you use it. If you
    cannot verify a name, use it but list it in your report as unverified.
11. **Finish with a report** to the user: what changed (files), each acceptance criterion and how you verified it,
    anything unverified, anything you decided that the task did not specify.

## 7. Commands

```bash
uv sync --extra dev                      # install
uv run python samples/make_samples.py    # regenerate sample files (after editing generators)
make test                                # all tests (uv run pytest -q)
make lint && make typecheck              # backend + web
make web-types                           # regenerate web/src/api/schema.d.ts after API changes
BILLPARSE_USE_LLM=0 uv run python -m billparse parse samples/files/<file> --no-llm   # parse one file
make corpus-check                        # real-file regression check (user's machine; corpus not in repo)
```
