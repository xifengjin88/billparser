---
type: "Reference"
title: "Messy layouts, the tree of sums and line context"
openwiki_generated: true
verified:
  - by: openwiki/0.7.2
    at: 2026-10-25T20:19:27.415Z
sources:
  - id: openwiki-source-fdd994828a8651fa97f6f3f5
    resource: repo://billparse/context.py
  - id: openwiki-source-3224c0cff3784229a39099b4
    resource: repo://billparse/layout.py
  - id: openwiki-source-563a25b42138de7299047821
    resource: repo://billparse/rowclass.py
  - id: openwiki-source-fa3b516cbac4d1d454de7725
    resource: repo://billparse/spec.py
  - id: openwiki-source-3881d0b3bca99a09bce49fa0
    resource: repo://billparse/tree.py
  - id: openwiki-source-1d8b98301b0c20838bd752a1
    resource: repo://svcres/regions.py
generated: { by: "claude-code", at: "2026-10-25T20:19:27.415Z" }
---


# Messy layouts, the tree of sums and line context

Frame regions (CSV, Parquet, JSON) are read column by column. Grid regions (Excel sheets, PDF pages, text) need more
work, because bills print tables anywhere, with group headers, wrapped lines and subtotals. Two spec sources read
grids:

| Spec source | Module | Used for |
|---|---|---|
| `grid` | `layout.py` + `rowclass.py` | a table with a header row (Excel sheets, PDF tables) |
| `tree` | `tree.py` + `context.py` | PDF and text bills read by their amounts, with no fixed table |

The spec interpreter (`spec.py`) calls them; see [Routing and the spec interpreter](routing-and-specs.md).

## Grid tables (`layout.py`)

`extract_table` finds a table with a spec's `table` options (`sheet`, `start`/`end` anchors, `header_rows`,
`multi_page`, `group_header.carry_to`, `continuation`, `stop_at_total`):

- **Header row.** `find_header` takes the row that matches an anchor regex, or the most header-like row among the
  first 80 rows (scored by `docmodel.header_score`, plus 3 per expected header name).
- **Columns by position.** `build_columns` makes one column per header cell, with its x range. Two-row headers append
  sub-labels; repeated names get suffixes; on PDF pages the column ranges widen to the midpoints between columns,
  and a cell can join the nearest column within 25 points. Columns are never fixed indexes, so a moved or added
  column does not break a spec.
- **Repeated headers.** A header found again on a later PDF page starts the rows again; on a sheet, a gap of more than
  3 rows ends the table unless the same header repeats below.
- **Row labels.** Each row gets a label from `rowclass.classify` and the features it read are kept for learning.

`rowclass.classify` is rules only:

| Label | Rule (simplified) |
|---|---|
| `blank` | no cells |
| `header_repeat` | 60% or more of the header texts again |
| `subtotal` | an amount and a subtotal word, or a vertical `SUM` formula over rows above |
| `total` | an amount and a total word, not a tax |
| `item` | an amount and a label (or an amount that is not all numbers) |
| `group_header` | no amount, and bold, ending in a colon, out-dented, or a single cell after a header or subtotal |
| `continuation` | no amount, short, after an item: merged into the item's label |
| `note` | anything else |

Items become records; a group header's text is carried down into each following item (`_group` by default);
subtotals and totals become stated totals, and the first total ends the table unless `stop_at_total` is false.

## The tree of sums (`tree.py`)

Many bills print one structure several times (a summary, a detail, an allocation by account), in layouts no rule was
written for. `tree.read(regions, options)` finds the structure **from the amounts**, not from words like "Total":

1. **Records.** Each row with an amount is a record. Amounts are found by `money_in`: an optional currency or symbol,
   a number with exactly two decimals, a sign or brackets; a bare number between words (a version such as `22.04`) is
   not an amount. Text rows next to an amount row join it (a product name above its numbers, a period below). Text
   repeated at the top or bottom of many pages is page furniture and is dropped.
2. **Forests.** A text row when no group is open starts a section. A row is a child of the open row above it when it
   is indented the same or deeper and its amount fits in what that row still lacks. A row whose children add up to it
   is a subtotal; a group that cannot be completed keeps its children and gets a difference line.
3. **Classes.** Rows that stand for the same money are one class: the same label and amount printed twice, a total
   over one row, a section that adds up to an earlier row (its breakdown).
4. **Lines.** From the largest class down, each class is broken down by its breakdown with the most leaves. The
   leaves are the line items; the classes above them are each line's path (its context).
5. **Evidence.** What adds up, what does not, classes with two breakdowns that agree, and amounts nothing explains.

Everything is deterministic. A spec's `tree` key sets `TreeOptions` (`ignore`, `skip_from`, `section`,
`repeated_header`, `informational`, `trailer`, `currency`, `amount`, furniture limits, heading rules, `min_total`,
`link_runs`, `max_combo`, `blocks`). `check_options` rejects unknown options, wrong types and bad regexes, so the
agent sees the error.

### The exact proof

`_rounding` accounts for every cent between a group's printed total and its lines: at each printed total, the
difference from the sum of its direct parts is its rounding. `bound(n)` is the most that `n` printed parts can differ
by rounding alone (`0.005 × (n + 1)`). `_reconcile` then states:

```
printed total = sum of lines + rounding at each printed total in between
```

The proof holds (`proved`) only when the rest (`unexplained`) is below half a cent, no total exceeds its rounding
bound, and the printed total has a source row. Any other difference is money counted twice or not read. With several
groups (for example two sellers), the printed amount that equals their sum is the total; if none exists, the groups
together are used and the proof cannot hold.

The evidence also lists subtotals that do not add up (`subtotals_failed`), amounts nothing explains
(`unexplained`, excluding amounts equal to a known amount, sums of rows with the same label, and rows that match the
`informational` option), rows printed in two currencies (`converted`), and a printed row used as a line more than once
(`duplicate_lines`). These feed the validation checks (see
[Validation, completeness and confidence](../concepts/validation-and-confidence.md)).

## Line context (`context.py`)

`context.lines_frame` turns the tree's lines into rows. Each line's path of labels ("Acme Prod (123456789012)" >
"Amazon Elastic Compute Cloud" > "Charges") gives its context. A spec's `context` options select which label is
which; the defaults are neutral:

- **account**: a label with a 12-digit AWS account id or an Azure GUID (option `account`, a regex with groups `id`
  and `name`);
- **region**: a label that is a whole region name or code (`svcres/regions.py`, matched ignoring case and
  punctuation). The line keeps the code (`ca-central-1`) as `region` and the printed name as `region_name`;
- **service**: the nearest label that the service catalog resolves by code, synonym, name or prefix (labels matching
  `not_service`, accounts and regions are skipped);
- **charge type**: the first `charge_types` regex that matches the line's label, then the labels above it. By default:
  `Tax` when the amount's column or a label names a tax (not "pre-tax"), `Credit` when the amount is negative and a
  credit, refund or discount is named, `Purchase` when a savings plan, reservation or commitment fee is named;
  otherwise usage. Each type stays its own line with its service;
- **cells**: a PDF row printed in cells gives a description, a quantity and unit ("588 Hrs"), and its other amounts by
  column name;
- **label_split**: a separator for labels printed in two languages (" / " by default when most labels have it).

`provider_of` picks AWS, Azure or GCP by counting provider words in the text.
