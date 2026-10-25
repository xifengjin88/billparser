---
type: architecture
title: Reading files into the document model
description: How billparse/docmodel.py turns any upload (PDF, Excel, CSV, Parquet, JSON, text, Word, PowerPoint, gzip, zip) into one Document of grid regions and frame regions that every spec and check reads.
tags: [docmodel, reading, pdf, excel, csv, json]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-25T20:19:27.415Z
sources:
  - id: openwiki-source-8d355aae7ac62f2ce962afc3
    resource: repo://billparse/docmodel.py
  - id: openwiki-source-fa3b516cbac4d1d454de7725
    resource: repo://billparse/spec.py
  - id: openwiki-source-1aefa65cced5d5fd3d5971cf
    resource: repo://billparse/transforms.py
generated: { by: "claude-code", at: "2026-10-25T20:19:27.415Z" }
---

# Reading files into the document model

Every spec, check and tool in billparse works on one in-memory model, never on the raw file. `billparse/docmodel.py`
builds it with `load(path)` or `load_bytes(data, name)`. The document's `sha256` is the hash of the uploaded bytes,
and it is the key of the document everywhere else (store, API, cache).

## The model

| Type | Holds |
|---|---|
| `Document` | `name`, `file_type`, `sha256`, `regions`, `notes` (problems found while reading) |
| `Region` | `kind` (`sheet`, `page` or `frame`), `name`, and either `rows` (grid) or `df` (frame) |
| `Row` | `cells`, `index` (Excel row number or PDF line number), `top` (PDF y), `page`; derived `text`, `bold`, `first_x` |
| `Cell` | `value`, `x0`/`x1` position, `bold`, `indent`, `formula` |

There are two kinds of region:

- **Grid regions** (Excel sheets, PDF pages, text, Word, PowerPoint, JSON metadata) keep position and formatting.
  Messy layouts need them: group headers, subtotals, wrapped lines, tables anywhere on a sheet
  (see [Messy layouts and the tree of sums](layout-and-tree.md)).
- **Frame regions** (CSV, Parquet, JSON records, very large sheets) are a pandas DataFrame with string-like values and
  no per-cell objects, because native exports can have millions of rows.

`Region.preview(n)` returns the first rows as text. Fingerprints and LLM previews use it.

## How the file type is chosen

`_load_into` looks at the first bytes before the name:

1. gzip (`1f 8b`): decompress and read the inner file again.
2. `.docx` / `.pptx`: paragraphs and table rows as text lines, one region per document or slide.
3. Other zip archives (not `.xlsx`/`.xlsm`): read every member again, with the member path as the region name
   prefix; `__MACOSX` and `._` entries are skipped. A damaged archive adds a note and reads nothing.
4. `%PDF-`: PDF pages.
5. Zip with `.xlsx`/`.xlsm`: Excel.
6. `PAR1` or `.parquet`: one frame.
7. `.json`, or text that starts with `{` or `[`: JSON.
8. Text extensions (`.txt`, `.md`, `.log`, `.yaml`, `.yml`, `.tf`, `.hcl`) that are not a delimited table: one text
   page, with the indent as the x position.
9. Anything else: CSV.

Archives can hold several files, so one document can have regions from many members.

## CSV

`_read_csv` decodes the bytes (`utf-8-sig`, `utf-16`, `cp1252`, `latin-1`), sniffs the delimiter from the first
20,000 characters (`, ; tab |`), finds the header row among the first 40 rows with `detect_header_row`, and reads
everything as strings (`dtype=str`). Bad lines are skipped. Header names are made unique (`col_N` for empty names,
`name_1` for repeats).

The whole file is decoded into one string and read from a `StringIO`, so memory grows with the file size. Large-file
reading is an open task (`docs/new_direction/07-large-file-reading.md`).

## Excel

`_read_xlsx` (openpyxl) reads sheets with more than `MAX_GRID_ROWS` (20,000) rows as frames, with no formatting.
Smaller sheets become grids:

- merged ranges are filled with their top-left value;
- each cell keeps bold, indent and its formula text;
- formulas saved without a cached result (files written by programs) are computed by `_evaluate_formulas`: cell
  references, ranges, `SUM`, numbers and `+ - * /` only, walked with Python's syntax tree, never `eval`. Other
  formulas stay empty.

## PDF

`_read_pdf` reads words with pdfplumber. Pages are read in parallel worker processes when the file has at least 4
pages and `PDF_WORKERS` (default: CPU count, at most 8) is more than 1. The pool uses `spawn`, not `fork`, because
the API parses in threads. If the pool cannot start (for example in a sandbox), the pages are read in the current
process.

`_words_to_rows` groups words into lines by their `top` position (3 point tolerance) and merges words into phrases by
the gap between them. A word is bold when its font name contains `bold`. A page with no words but with images is
marked `scanned`; there is no OCR, so such files end as `unsupported`.

## JSON

`_read_json` accepts a JSON document or JSON lines. The largest list of objects anywhere in it is the line-item table
(`_find_records`). Nested objects are flattened into dotted column names; lists stay as JSON text (for example GCP
`credits`, which the `json_sum` transform reads). Every scalar outside the record list (invoice number, totals) goes
into a separate key/value grid region named `json-meta`, so specs can read header fields and stated totals from it.

## Header detection

`detect_header_row` scores the first rows with `header_score`: share of non-numeric cells, share of short cells, the
number of filled cells, and a bonus for header words (`cost`, `amount`, `service`, `date`, `quantity`, `region`,
`account`, `sku`, `currency`, ...). Rows with fewer than half the widest row's cells are skipped, and an earlier row
wins a near tie.

## Numbers

Values stay as text in the model. `billparse/transforms.py` turns them into numbers later: `parse_number` handles
currency symbols, `(12.00)` and `12.00-` as negatives, and both `1,234.50` and `1.234,50`; `t_number` applies it to a
column after `pd.to_numeric`. Specs choose transforms by name
(see [Routing and the spec interpreter](routing-and-specs.md)).

## Infrastructure as code

CloudFormation and Terraform files are read into the model like any other file (YAML, `.tf` and `.hcl` as text pages,
JSON as frames). The built-in spec with `"source": "iac"` then reads them again with `billparse/iac.py` as an
inventory of resources (see [Infrastructure-as-code inventory](../concepts/iac-inventory.md)).
