# Plan: capture every detail, prove it, and let an LLM review and improve the specs

Status: proposal, 2026-10-23. Nothing in this plan is built yet.

## 1. Goal

For every file (PDF, Excel, CSV, Parquet, JSON, text, ZIP), billparse extracts everything that the file prints
about cost:

- every line item, with its full context (account, service, region, usage type);
- every attribute in the line text (instance type, database engine, OS, deployment, quantity, unit, unit price,
  period, pricing model, commitment);
- the header fields (provider, invoice number, billing period, currency, account IDs).

Each result carries **proof** that nothing is missing or counted twice. A total is one kind of proof. Many files
have no total, so the system uses the strongest proof that the file allows (section 3).

**Review.** An LLM reviews each result that is not proved, finds the cause, and changes the **spec** (not the
lines) until the deterministic checks prove the result. A person reviews only what the LLM cannot prove, and can
check any result. Each night, the system refines all the specs.

## 2. The principle: the numbers add up

Totals, subtotals and line items form a **tree of sums**. If we find the tree:

- the leaves are the line items;
- the parents give each leaf its context (service, region, usage type);
- the sums prove that the lines are complete.

The tree does not need words like "Total" or Excel formulas. So it works for layouts that we did not see before.

## 3. Proof of completeness (evidence)

Each check passes, fails or does not apply. A failed check names the rows that caused it.

| Check | Proves | Example |
|---|---|---|
| Stated total | The lines sum to a printed total | "Total pre-tax" = sum of lines |
| Subtotals add up | No line missing or double inside each group | Each service total = sum of its regions |
| Every amount explained | No amount skipped | Each number is a line, a subtotal, a total, a tax or a repeat |
| Two views agree | The same money matches elsewhere in the file | Summary by service = detail pages; "Taxes by service" = tax lines |
| Printed counts | No group skipped | "(38)" services = 38 services read |
| All pages read | No page skipped | "Page n of 15" for each page |
| Line math | Each line read correctly | 588 Hrs × 0.035 = 20.58 |
| Two methods agree | The parse is consistent | An independent extraction gives the same lines and sum |
| Manifest | A flat export is complete | Row count and parts match the manifest; no "next page" left |
| Other files agree | Files agree with each other | Invoice PDF = CUR export for the same account and month |
| Plausibility | Values make sense | Dates in period, one currency, negatives are credits |

Status (as in invoice-parser):

| Status | Meaning | Automatic |
|---|---|---|
| `reconciled` | A printed number proves the result (total, subtotals or count) | Accepted. Training data |
| `consistent` | Nothing printed to test, but all internal checks pass | Accepted. Wider forecast range |
| `unchecked` | Nothing could be tested | LLM review, then person |
| `contradicted` | A check failed | LLM review, then person |

## 4. Formats and document kinds

All formats go into one document model: pages, sheets, tables and text lines, and for each row its cells, x
position, font, bold, indent, and (Excel) formula, outline level, hidden state.

| Format | Method | Main proof |
|---|---|---|
| PDF | Rows → levels (x, font, bold) → tree of sums | Totals, subtotals, amounts explained, views, pages |
| Excel | As PDF, plus formulas (exact subtotals), grouping, merged cells, hidden rows and sheets | Formulas, totals, summary sheet vs detail sheets |
| CSV, Parquet | Flat table; built-in or learned spec | Manifest, row count, no duplicates, other files |
| JSON | Record lists → tables; other values → header fields | Metadata totals, no leftover "next page" |
| Text | As PDF; levels from indent | As PDF |
| ZIP, GZ | Parse each member; keep members together as one **bundle** | Each member, plus the bundle (manifest parts, invoice vs detail files) |

The first step for every file classifies its **kind**:

| Kind | Extract | Proof |
|---|---|---|
| Bill, invoice | Billed lines, tax, credits | Section 3 |
| Cost export | Lines with all attributes | Manifest, row count, invoice match |
| Estimate | Priced lines | Line math, its total |
| Inventory | Resources with attributes, no cost | Resource count, no duplicates |
| Recommendation, analysis | Proposed savings and their items | Its own totals |
| Memo, text | Stated facts (commitments, amounts, dates) | Each fact cites its line |

Open decision: billparse cannot read PPTX, DOCX, YAML, Terraform, CloudFormation or images today. invoice-parser
reads all of these except images. If billparse must support them, take the readers from invoice-parser.

## 5. The LLM review loop (per file)

### Rules

1. **The LLM changes specs, never lines.** A result is always what a spec gives when the interpreter runs it.
   So every result can be reproduced, tested again and checked by the regression gate.
2. **Only deterministic checks prove a result.** The LLM's opinion never makes a result `reconciled`.
3. **A changed spec must not break other files.** It must still read every file that the spec read before (the
   regression gate).
4. **Limits on every job:** rounds, estimated cost, time, tool calls; and a daily budget. A limit stops the job
   with the best result so far.
5. **Everything is visible:** each job keeps a trace (what the LLM read, each spec it tried, each check result,
   the cost). The web app shows it.

### Trigger

A parse ends with `unchecked` or `contradicted`, or with `consistent` and low confidence. The file goes to a job
queue (table `agent_job`, one job for each layout fingerprint and spec version, as `learn_job` in
invoice-parser). The parse result stays in use until the job ends.

### Steps

```
evidence report ──> reviewer ──> author (tools) ──> verify ──> regression gate ──> decide
      ^                                               │                              │
      └──────────────────── findings ─────────────────┘                              ├─ proved: save spec
                                                                                     ├─ cannot fix: person
                                                                                     └─ limit: best so far
```

1. **Evidence report** (code, no LLM). The failed checks; the rows that do not add up, with their amounts and
   levels; the unexplained amounts; samples of each **row shape** in the whole file (not the first 40 rows).
2. **Reviewer** (LLM). Classifies the cause:
   - the spec reads the layout wrong (fix the spec);
   - the reader is wrong, for example a wrong level or a merged line (report a code issue; a person fixes it);
   - the document itself does not add up, for example a printed total is wrong (note it, send to a person);
   - wrong kind of document, for example an estimate read as a bill (change the kind).
3. **Author** (LLM with tools). Changes the spec, or writes a new one. Tools, as in invoice-parser's
   `specs/harness.py`: list tables, read rows, line statistics with x positions, find lines, test a regex, find a
   catalog service, **check spec** (runs the real interpreter and all checks on the whole file), submit.
4. **Verify** (code). Runs the spec on the whole file with all checks.
5. **Exact fixes** (code, no LLM). A fix that the checks can state exactly (for example "row 812 is a subtotal")
   is applied without a model call.
6. **Regression gate** (code). The changed spec must give the same rows, totals and status on every file that
   the spec read before.
7. **Decide:**
   - proved and gate passed → save the spec version; parse the file again with it;
   - the reviewer found a cause the spec cannot fix → the review queue, with the LLM's explanation and the
     rows involved;
   - a limit reached → keep the best spec only if it is better and passes the gate; else the review queue.

### Person review

A person sees only what the LLM could not prove, plus any file they choose. The review page shows the tree of
sums, the failed checks with their rows, the LLM's explanation and trace. An approval:

- saves the corrected lines as the approved result;
- adds the file to the **evaluation set** of the LLM reviewer (did the LLM find the same cause and fix?);
- trusts the spec, when no line was edited.

## 6. The nightly run (all specs)

1. **Run every spec again** on its regression set with the current code. A spec that now fails is demoted and
   gets an LLM repair job.
2. **Retry the review queue** with the current specs and the LLM loop.
3. **Group the files that fail by layout.** One job for each layout, with up to 5 failing files of that layout,
   so the author fixes the layout and not one file.
4. **Merge similar specs.** Specs for small variants of one layout (a renamed column, an added section) are
   merged into one spec when the merged spec passes the regression sets of both.
5. **Promote and demote** by evidence: candidate → verified after 3 proved files from different documents;
   verified → candidate after a failure on a new file.
6. (Removed 2026-10-24: no row classifier; the rules and the tree of sums label the rows.)
7. **Measure the LLM reviewer** against person approvals: agreement on cause, share of jobs proved, cost per
   proved layout.
8. **Report:** proved share by format and kind, LLM calls per file, cost, specs changed, regressions caught.

## 7. Phases

| Phase | Work | Done when |
|---|---|---|
| 0. Harness | Run all files in `~/projects/cost_engine_inputs` (local only); report kind, checks, status, attribute coverage. Expected results by hand for 5–10 files. Synthetic copies of the structures for unit tests | Every later change shows its effect on real files |
| 1. Reader | One document model: x position, font, bold, indent for PDF and text; formulas, grouping, hidden rows for Excel; ZIP bundles | The harness shows levels for every row |
| 2. Tree and evidence | The tree-of-sums solver; the checks of section 3; the 4 statuses | Most PDFs and Excel files are `reconciled` or name the rows that fail |
| 3. Attributes | Add `instance_type`, `database_engine`, `operating_system`, `deployment_option`, `usage_type`, `pricing_model`, `commitment_id`; extractors for AWS, Azure, GCP; the source of each value | Lines carry these fields when the file prints them; the web app shows them |
| 4. Spec format | `levels`, `subtotals: by_amount`, `carry` per level, views to ignore, document kinds; the mapper tries level choices and keeps the one that adds up | The mapper reads new layouts without the LLM |
| 5. LLM loop | Section 5: job table, reviewer, author with tools, verify, exact fixes, gate, limits, traces; Jobs page in the web app | Unproved files get a proved spec or a clear reason, with no person |
| 6. Nightly | Section 6 | Specs improve and merge each night; regressions are caught |
| 7. Built-ins | Built-in specs for the most common layouts in the harness (likely AWS console bill, AWS invoice, Microsoft invoice) | These layouts never need the LLM |
| 8. Person review | Tree view, LLM explanation, labels and evaluation set from approvals | Approvals measure the LLM |

## 8. What to reuse from invoice-parser

| Need | invoice-parser part |
|---|---|
| LLM loop (author → verify → review → fix) with LangGraph | `invoiceparse/specs/learn.py` |
| Author tools on the real file | `invoiceparse/specs/harness.py` |
| Strict checks and reviewer without LLM | `invoiceparse/specs/verify.py`, `invoiceparse/specs/review.py` |
| Job queue, recovery, stop, retry | `invoiceparse/specs/jobs.py`, table `learn_job` |
| Traces and costs | `invoiceparse/specs/trace.py`, `invoiceparse/costs.py` |
| Jobs and Costs pages | `web/src/pages/JobsPage.tsx`, `JobPage.tsx`, `CostsPage.tsx` |
| Readers for PPTX, DOCX, Terraform, CloudFormation | `invoiceparse/readers.py`, `lane_terraform.py`, `lane_cloudformation.py` |

## 9. Risks

- **Cost.** The LLM loop runs for each new layout and each repair. Limits per job and per day; one job per
  layout, not per file.
- **Wrong proof.** A tree that adds up by chance (for example many zero amounts). The solver prefers trees that
  agree with levels, and zero rows do not count as proof.
- **Customer data.** The LLM sees redacted row samples and the rows that fail. The real files stay local.
- **Overfitting a spec to one file.** The regression gate and the merge step run on many files.
