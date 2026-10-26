# Task 02: Large CUR and export files

**Order:** after 01 is merged, including its review fixes. **Needs:** 01 (`coverage.apply`, the cost fields).
**STOP gate:** none, but **stop at the end and report the benchmark numbers to the user.**
**Read first:** `00-START-HERE.md`, then these in full: `billparse/docmodel.py` (`load_bytes`, `_load_into`,
`_read_csv`, `_frame`), `billparse/pipeline.py` (`parse_bytes`, `_parse`, `_try`, `_finish`), `billparse/spec.py`
(`run`, `_frame`, `_map_columns`), `billparse/validate.py` (`clean_rows`, `validate`), `billparse/contract.py`
(`build`, `_rollup`), `billparse/webapi.py` (`read_upload`, `start_parse`), `billparse/store.py` (`save_doc`,
`load_doc`, `save_result`).

## In one sentence

Parse CUR, FOCUS, Azure and GCP exports of tens of millions of rows with bounded memory and reasonable time,
without overloading the API or other parses, and with the same results the current in-memory path gives on smaller
files.

## Why: measured on the current code

A synthetic CUR 2.0 file (17 columns, gzip) parsed through `Pipeline.parse_bytes`:

| Rows | Upload (.gz) | CSV | Time | Peak memory above baseline |
|---|---|---|---|---|
| 400,000 | 19 MB | ~110 MB | 24 s | ~0.8 GB |
| 1,200,000 | 56 MB | ~330 MB | 73 s | ~2.3 GB |

That is about **2 GB of memory per million rows**. Real CURs have tens of millions of rows, so a 20M-row file would
need about 40 GB. Reading alone takes about 5× the CSV size in memory, and CUR files compress about 6.5×, so the
upload limit (`MAX_UPLOAD_MB`, in compressed bytes) says little about the real size.

Note: the synthetic file failed the built-in spec's line-math check, so the mapper route also ran over the whole
file. The timings are therefore about 2× a clean CUR; the memory figure holds either way.

Where the memory goes:

- `webapi.read_upload` reads the whole upload into memory.
- `docmodel._load_into` decompresses gzip and zip in memory.
- `_read_csv` decodes the whole file into one string, then builds a pandas frame of Python strings
  (`dtype=str`).
- Every route attempt (`_try` → `spec.run`) processes the whole file, so a route that fails costs a full extra pass.

Where the time goes (cProfile, 400k rows):

| Function | Seconds | Why |
|---|---|---|
| `transforms.t_strip` | 9.1 | a Python lambda per cell |
| `validate.clean_rows` → `_provider_of` | 7.6 | 800k Python calls |
| `coverage.apply` → `_text_coverage` | 4.7 | regex per row |
| `transforms.t_charge_type` / `coverage_of` | 5.5 | Python per row |
| `spec._keep_unmapped` | 4.6 | Python lambda per cell |
| `analyze._kinds` | 2.9 | list comprehension per row |

## Targets

| Target | Value |
|---|---|
| Peak memory of one large parse | ≤ `LARGE_MEMORY_MB` above the process baseline (default 2,048), **whatever the row count** |
| Throughput, built-in CUR spec, one parse | ≥ 50,000 rows per second; today about 16,000 at best. Record the real number |
| Large parses at the same time | at most `LARGE_PARSE_WORKERS` (default 1); the rest wait with state `queued` |
| API while a large parse runs | `GET /health` answers in under 200 ms |
| Results | the same as the in-memory path on files both paths can handle (step 10) |
| Small files | unchanged path, unchanged `EXPECTED` |

## Design

```
upload ──stream to disk──> size check ──small──> today's path (bytes in memory)
                               │
                             large (tabular only)
                               │
            sample (first SAMPLE_ROWS rows) ──> routing as today, on the sample only
                               │ accepted spec (frame source, no reshape)
            stream the file in chunks ──> per chunk: spec.run, clean, attributes, coverage, services
                               │                     │
                    rows appended to Parquet     statistics accumulated
                               │                     │
                               └──> validate from statistics, analyze and contract from the rolled-up lines
```

## Steps

### 1. Upload to disk, never into memory

- New `webapi.save_upload(upload) -> (path, sha256, size)`. It copies the upload to a temp file in the data
  directory in 8 MB chunks, computing SHA-256 as it goes, and stops with 413 above `MAX_UPLOAD_MB`. Use it in
  `POST /parse/start` and `POST /parse` (`api.parse`) instead of `read_upload`.
- New `store.save_doc_path(sha, path, name)` moves the temp file into the documents folder. Keep `save_doc(sha,
  data, name)` for existing callers.
- New `Pipeline.parse_path(path, name, ...)`:
  - small files: read the bytes and call `parse_bytes` as today;
  - large files: take the large path below.
- `parse_bytes` stays the small-file entry point.
- `MAX_UPLOAD_MB` stays at 2,048, with `web/nginx.conf` matching.

### 2. Decide the path: `billparse/bigfile.py` `classify(path, name)`

- **Uncompressed size:**
  - zip: the sum of `ZipInfo.file_size` over its members;
  - gzip: stream-decompress and count, stopping as soon as the count passes `LARGE_FILE_MB`, so the cost is
    bounded;
  - plain files: the file size.
- **Large** = more than `LARGE_FILE_MB` uncompressed (default 300).
- **Too large** = more than `MAX_UNCOMPRESSED_GB` (default 50). Refuse with a clear message that says how to
  split the export.
- The large path supports CSV, CSV in gzip or zip, and Parquet, including Parquet inside zip. A large Excel or PDF
  file is refused with the message "Export this report as CSV to parse a file this large"; Excel cannot hold more
  than about 1M rows anyway.
- Read CSV dialect and the header row exactly as `_read_csv` does today (`csv.Sniffer`, `detect_header_row` on the
  first `HEADER_SCAN_ROWS` lines), from the first 20 KB.

### 3. Route on a sample only

- Build a `docmodel.Document` from the header plus the first `SAMPLE_ROWS` rows (default 50,000). Use it for the
  fingerprint and run the routes on it: built-in, learned, mapper and the LLM routes. The LLM routes already work
  on a preview.
- The accepted spec must have `source` frame or auto, and no `reshape`. Otherwise the file ends `unsupported`, with
  the reason in the feedback.
- The agent (`agent.run_job`) never runs inline for a large file. If it is needed, queue its job as today and let
  it work on the sample document.

### 4. Stream the file in chunks

- Read with `pyarrow.csv.open_csv`, with every column as a string so values match today's `dtype=str`, or with
  `pyarrow.parquet.ParquetFile.iter_batches`.
- Chunk size: start at `CHUNK_ROWS` (default 250,000). After the first chunk, measure memory per row and adjust so
  that one chunk plus the accumulators stay within `LARGE_MEMORY_MB`.
- Cap pyarrow's threads at `LARGE_IO_THREADS` (default 2), so one parse cannot take every core.
- For each chunk:
  1. Make a frame `Region` and a one-region `Document`. Set `header_row` and a row offset so `_src` row numbers
     stay correct across chunks. Today `spec._frame` uses `header_row + i + 2`; add the chunk's starting row.
  2. Run `spec.run(spec, chunk_doc)`, `validate.clean_rows`, `attributes.normalize_regions`,
     `attributes.extract`, `coverage.apply` and `analyze.resolve_services`. The resolver caches what it resolved,
     so repeated values are cheap.
  3. **Chunk mode for `clean_rows`.** Its safety net ("one row equals the sum of all the others" becomes a total)
     must be off in chunk mode: it can misfire on a chunk. Add a parameter.
  4. Append the rows to one Parquet file with `pyarrow.parquet.ParquetWriter`. Fix the column set from the spec's
     columns, its derives and the columns the steps add. A column missing from a chunk is written as null, and an
     unexpected new column is dropped with a warning. Cast text columns to string.
  5. Update the statistics (step 5).
- Report progress through the existing `on_step` callback, for example "reading: 3.2M of ~20M rows", estimated from
  the bytes read.

### 5. Statistics accumulated per chunk (`bigfile.Stats`)

Everything validation, analysis and the contract need, so the full rows never have to be in memory:

- rows and source rows;
- billed cost by charge type, by service, by category, by provider and by month, with currencies;
- the minimum and maximum charge dates;
- line-math counters (lines checked, lines matching), and unexplained negative lines;
- labels coverage (cost with a service or description);
- service-resolution share (cost with a resolved service);
- cost-type presence (task 01: list, contracted and effective present on how many usage lines);
- spend by coverage;
- **rolled-up lines**: per chunk, `groupby` over `contract.ROLLUP_KEYS` plus the month of `charge_start`, summing
  `billed_cost`, `quantity`, the task 01 cost fields and a `_lines` count, then merged into the running result.
  Re-group every 10 chunks. When the groups pass `CONTRACT_MAX_CHARGES`, keep the largest by cost and merge the
  rest into one "other" line per service, with a warning.

### 6. Validate from statistics

- Add `validate.validate_stats(stats) -> verdict`. It produces the same check names, statuses and weights as
  `validate()` for the checks that apply to exports (`rows`, `labels`, `line_math`, `stated_total`, which is usually
  skipped for exports, `service_resolution`, and the plausibility checks).
- Share helper functions with `validate()` so the two cannot drift. Add a test that both give the same verdict on
  the same file (step 10).

### 7. Analysis and contract from the rolled-up lines

- `analyze.analyze` runs on the rolled-up frame. It has every column the analysis reads (`billed_cost`,
  `charge_type`, `service`, `category`, `provider`, `currency`, `charge_start`), and its sums by service,
  category, provider and month are exact, because those are roll-up keys.
- `contract.build` receives the rolled-up frame (it already supports `_lines`) and the **sample** document for the
  source viewer bundle.
- `store.save_result` gets the summary plus the path of the Parquet file written in step 4, instead of a frame.

### 8. Remove per-row Python from the hot paths (all files benefit)

Rule: per-row Python is allowed only as a mapping over **unique values** (compute once per distinct value, then
`Series.map` the result).

| Function | Change |
|---|---|
| `transforms.t_strip` | `s.astype("string").str.strip()`, with blanks, `"nan"` and `"None"` turned into missing values |
| `transforms.t_charge_type`, `coverage_of` / `t_coverage`, `t_commitment_credit` | map over unique values |
| `validate.clean_rows` `_provider_of` | map over unique `service_raw` values |
| `coverage.apply` `_text_coverage` | run on unique texts only |
| `spec._keep_unmapped` `_extra` building | vectorized string concatenation |
| `attributes.extract` | run on unique texts (build a key per row, extract once per key, map back), and **delete `pipeline.ATTRIBUTE_ROWS`** and the 100,000-row cutoffs in `pipeline._try` and `agent.check` |
| `analyze._kinds` | vectorized with `numpy.select`, or a map over unique `(charge_type, category)` pairs |

Add a guard: when a column has more than `MAX_UNIQUE_TEXTS` distinct values (default 500,000), skip the text step
for that column and add a warning. One example is a `resource_id` used as a description.

### 9. Protect the API and other parses

- `webapi`: large parses run in a separate `ThreadPoolExecutor` with `LARGE_PARSE_WORKERS` threads (default 1),
  apart from the small-file pool (`PARSE_WORKERS`). A large parse waiting for a slot shows `progress = "queued"`.
  Task 09 later moves the large pool into its own worker process; keep the code that runs a large parse in
  `bigfile.py`, so task 09 only has to call it.
- `GET /health` must stay fast during a large parse. Test it (step 10).
- Re-parse, the nightly loop (`learn.run_nightly`), the regression gate (`learn.regression_gate`) and
  `repair_spec` load stored files with `store.load_doc` (bytes) and `load_bytes`. For large documents, give them a
  file path and the large path:
  - the regression gate compares totals and row counts from the statistics;
  - spec repair works on the sample document.
- The source viewer endpoints (`/documents/{sha}/source/...`) read the region tables. For large documents, they
  read the first `MAX_TABLE_ROWS` rows from the stored file.

### 10. Benchmark script and tests

Benchmark script `scripts/bench_large.py`:

- It generates a CUR 2.0 file in a temp directory. Use realistic data: cost equal to quantity × rate, several
  services and instance types, and a few commitment rows as in task 01's sample.
- It parses the file through `Pipeline.parse_path`, in a subprocess, and reports rows, compressed and uncompressed
  size, time, rows per second and peak RSS (`resource.getrusage`).
- Run it for 1M and 5M rows, and put the numbers in the report.

Tests (`tests/test_large_files.py`). Never commit generated large files. Force the large path on small files by
setting `LARGE_FILE_MB` very low:

- **Same results:** a 300,000-row file through both paths gives the same row count, the same billed totals by charge
  type and by service, the same coverage split and the same verdict status.
- **Chunk boundaries:**
  - `_src` row numbers are right in the second and later chunks;
  - a column that is empty in the first chunk and filled later still arrives;
  - the total-row safety net never fires inside a chunk.
- **Memory:** a 2,000,000-row file parsed in a subprocess with `CHUNK_ROWS=100000` stays under a set peak RSS.
  Choose the bound from a first run, and record it in the test with a comment.
- **Guards:**
  - a gzip that decompresses past `MAX_UNCOMPRESSED_GB` (set tiny in the test) is refused with a clear message;
  - a large `.xlsx` gets the "export as CSV" message.
- **API:** while a large parse runs in the test client, `GET /health` returns in under 200 ms, and a second large
  upload shows `progress = "queued"`.
- **Hot paths:** `t_strip`, `t_charge_type`, `_provider_of` and `coverage.apply` on 1,000,000 rows each finish in
  under a set time; record the before and after times in the report.
- `EXPECTED` unchanged; all existing tests pass.

## Settings (add to `example.env` with comments)

`LARGE_FILE_MB=300`, `MAX_UNCOMPRESSED_GB=50`, `SAMPLE_ROWS=50000`, `CHUNK_ROWS=250000`, `LARGE_MEMORY_MB=2048`,
`LARGE_PARSE_WORKERS=1`, `LARGE_IO_THREADS=2`, `MAX_UNIQUE_TEXTS=500000`.

## Done when

- [ ] Benchmark numbers for 1M and 5M rows, before and after, are in the report. Peak memory at 5M rows is within
      `LARGE_MEMORY_MB`, and throughput meets the target or the report explains the gap.
- [ ] The same-results test passes; small files are unchanged.
- [ ] `ATTRIBUTE_ROWS` and the 100k cutoffs are gone.
- [ ] Large parses are limited by `LARGE_PARSE_WORKERS`, and the API stays responsive.
- [ ] README "Known limits" updated, and the new settings documented.

## Do not

- Do not add a database or Redis for this; the durable queue is task 09.
- Do not change the small-file path's results.
- Do not load a whole large file into a pandas frame anywhere, including in the nightly loop, the regression gate
  and review.
