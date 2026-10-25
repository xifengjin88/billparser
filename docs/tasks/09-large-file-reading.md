# Task 09: Large-file reading

**Order:** after 08. **Needs:** 08 (large files run on the `large` queue). **STOP gate:** none.
**Read first:** `billparse/docmodel.py` (`load_bytes`, `_load_into`, `_read_csv`, `_frame`),
`pipeline._parse` and `_try`, `webapi.read_upload`, `store.save_doc`.

## In one sentence

Parse a multi-GB CUR or FOCUS export with bounded memory and the same result as today.

## What exists today (verified)

- `webapi.read_upload` reads the whole upload into memory (up to `MAX_UPLOAD_MB`, default 500).
- `docmodel.load_bytes(data, name)` takes the whole file as bytes. `_load_into` decompresses gzip and zip members
  fully in memory. `_read_csv` decodes the whole file into one string, then runs `pd.read_csv(io.StringIO(text),
  dtype=str)`. So the file is held as bytes, as a decoded string and as a frame of Python strings at the same time.
- `pipeline._parse` runs every route attempt (`_try` → `spec.run`) on the whole document. A large file is processed
  in full by every route tried, including routes that fail.
- Rows are stored as Parquet by `store.save_result`.
- `contract._rollup` already rolls up more than `CONTRACT_MAX_CHARGES` rows for the output contract.

## Steps

1. **Uploads to disk.** In the parse API, stream uploads above `LARGE_FILE_MB` (task 08) to the data directory in
   chunks, instead of `read_upload`'s single read. Compute the SHA-256 while streaming. `store.save_doc` gets a
   variant that takes a path.

2. **A path-based loader.**
   - Add `docmodel.load_path(path, name)` for large files.
   - Decompress gzip and zip members to temporary files in the data directory.
   - For CSV and Parquet, create a frame region that is **lazy**: it reads a sample on demand and the full table
     on demand. Use DuckDB (`read_csv` / `read_parquet`) or PyArrow.
   - Keep the current header-row detection (`detect_header_row` on the first `HEADER_SCAN_ROWS` lines) and
     delimiter sniffing (on the first 20 KB).
   - Read every column as a string, as `pd.read_csv(dtype=str)` does today, so the transforms behave the same.

3. **Sample first, then run in full** (`pipeline._parse`, large frames only):
   - Route attempts run on a sample: the first 10,000 rows plus a random 10,000 rows.
   - Only the accepted spec runs on the whole file.
   - Checks that need the whole file (stated totals, row coverage) run after the full run.
   - If the full run fails validation where the sample passed, continue with the next route as today, and add the
     warning "spec passed on a sample but failed on the full file".

4. **Chunked runs.** For `source: frame` specs without `reshape` and without cross-row logic, run `spec.run` per
   chunk of 250,000 rows and concatenate. Write the Parquet result from the chunks. Specs that need the whole table
   (any `reshape`, or `group_header` carry) keep the current path; document which ones.

5. **Pricing and attributes** already work on unique keys (tasks 02 and 05), so they need no change. Remove the
   100,000-row skip from task 06 step 4 now that attempts run on samples.

## Tests (`tests/test_large_files.py`; generate data in a temp dir, never commit it)

- A 1,000,000-row CUR-like CSV parses with peak memory below a set limit (measure with `tracemalloc` or
  `resource.getrusage`; choose the limit from a first run and record it).
- Its result equals the current reader's result on the same data: row count, totals, and 1,000 sampled rows
  compared field by field.
- Sample-first: a file whose sample passes but whose row 900,000 breaks the spec ends with the warning, and the
  next route is tried.
- A gzip upload is decompressed to disk, not in memory.
- `EXPECTED` unchanged.

## Done when

- [ ] Peak memory for large frames is bounded by the chunk size, not the file size.
- [ ] Identical results on the test data.
- [ ] Route attempts on large files use the sample; only the chosen spec runs in full.
- [ ] Timings for the 1M-row file, before and after, are in the report.
- [ ] README "Known limits" updated.
