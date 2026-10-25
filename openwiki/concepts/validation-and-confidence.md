---
type: concept
title: Validation, completeness and confidence
description: How billparse/validate.py cleans extracted rows and turns evidence checks (labels, line math, stated totals, tree checks, row coverage, dual extraction, currency, plausibility, service resolution) into a confidence score, a status, a completeness value and repair feedback.
tags: [validation, confidence, completeness, reconciliation, checks]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-25T20:19:27.415Z
sources:
  - id: openwiki-source-578cf64044232a7c0bddf2d1
    resource: repo://billparse/validate.py
generated: { by: "claude-code", at: "2026-10-25T20:19:27.415Z" }
---

# Validation, completeness and confidence

Validation produces **evidence**, not one pass/fail. Each check adds to or removes from a confidence score, and the
checks together decide the status (used for routing and review) and the completeness (how well the result is
proved). `billparse/validate.py` runs for every route attempt and once more on the chosen reading
(see [Architecture overview](../architecture/overview.md)).

## Cleaning rows (`clean_rows`)

Runs on every extraction before validation:

- lines whose label names a tax get `charge_type = Tax`; negative lines that name a credit, discount, refund,
  promotion or adjustment get `Credit` (only where no charge type is set);
- a `service_raw` that is a provider name ("Amazon Web Services") moves to `provider`, and the description becomes
  the service label;
- for non-tree readings, total rows ("Total", "Subtotal", "Amount due", "Invoice total") are moved out of the lines
  into stated totals. As a safety net for other languages, when no row has a total label, the single row equal to the
  sum of all the others is treated as a total. The tree of sums separates totals itself, so this step is skipped for
  it.

## The checks (`validate`)

Confidence starts at 0.55; each check adds its weight; the result is clipped to 0..1.

| Check | Pass / warn / fail (weight) |
|---|---|
| `rows` | any cost rows (+0.1); none → fail (−1.0) and the result ends there |
| `labels` | ≥ 90% of cost has a service or description (0); else fail (−0.35) |
| `line_math` | quantity × unit price = amount on ≥ 95% of lines (+0.15); a consistent ratio (unit block, discount, currency) → warn (0); else fail (−0.15); skipped with fewer than 3 lines |
| `stated_total` | see below: pass (+0.35), fail (−0.35), or skip when nothing is printed |
| `row_coverage` | grid tables: rows with amounts accounted for (≥ 98% +0.1, ≥ 90% −0.05, else −0.25); frames: source rows kept (≥ 90% +0.05, ≥ 50% −0.05, else −0.2) |
| `subtotals` | tree: groups add up (+0.2); groups that do not → fail (−0.25) when the difference is above 0.5% of the total, else warn (−0.05) |
| `amounts_explained` | tree: every amount is a line, a total or a repeat (+0.1); unexplained amounts → warn (−0.05) or fail (−0.25) by the same 0.5% rule |
| `views_agree` | tree: a total broken down two ways that agree (+0.05) |
| `dual_extraction` | an independent rule-based extraction has the same sum and row count (+0.15), else warn (−0.1) |
| `currency` | printed amounts in a second currency equal the amounts × the printed rate, to the cent (+0.05 / −0.2) |
| `dates`, `currency`, `negatives` | plausibility warnings (−0.05 each): dates out of range, several currencies, unexplained negative lines |
| `service_resolution` | final pass only: ≥ 90% of usage cost resolved (+0.05), ≥ 60% warn (−0.05), else fail (−0.15) |

### Stated totals

1. A printed row used as a line more than once (tree evidence `duplicate_lines`) fails at once: money counted twice.
2. If the tree of sums proved a printed total, its proof is checked exactly: printed total = lines + rounding at
   each printed total, to the cent, with no rounding above its bound
   ([Messy layouts and the tree of sums](../architecture/layout-and-tree.md)).
3. Otherwise any stated total (except subtotals and the LLM's own `llm_claim`) is compared, largest first, with the
   sum of lines, the sum before tax, and the sum plus the printed tax. The tolerance is `max(0.05, 0.05% of the sum)`
   but never more than half a cent per line plus half a cent.
4. With no total but subtotals, subtotals that add up to the non-tax lines pass.
5. Nothing printed: skip, and the result can at most be `unreconciled`.

## Status, completeness and feedback (`_summarize`)

**Critical checks**: `rows`, `stated_total`, `row_coverage`, `labels`, `subtotals`, `amounts_explained`.

| Status | Rule |
|---|---|
| `reconciled` | a stated total matched, confidence ≥ 0.7, no critical check failed |
| `unreconciled` | no critical failure and confidence ≥ 0.6 |
| `needs_review` | anything else |

| Completeness | Rule |
|---|---|
| `contradicted` | a critical check or `line_math` failed |
| `reconciled` | a printed total proves the lines |
| `consistent` | nothing printed to test, and an internal check passed (`subtotals`, `line_math`, `row_coverage`, `dual_extraction`, `amounts_explained`, `views_agree`) |
| `unchecked` | nothing could be tested |

`feedback` lists every failed or warned check with its detail. The LLM spec route sends it back as repair feedback
([LLM routes](../architecture/llm-and-induction.md)), and the agent receives the same checks through `check_spec`
([The spec-authoring agent](../architecture/agent.md)), which decides only on `completeness == "reconciled"`.

The pipeline adds two rules after validation: rows that the LLM wrote directly are capped at confidence 0.65, and a
direct LLM result that would be `consistent` becomes `unchecked`. The contract re-tests the totals on its own
document and never reports a better completeness than this verdict
([Output contract](output-contract.md)).
