---
type: architecture
title: LLM spec generation and extract-then-derive
description: The two LLM routes in billparse's parse ladder (an LLM-written spec with repair rounds, and LLM row extraction followed by deriving a deterministic spec), what the LLM sees, how its output is checked, and how cost and models are configured.
tags: [llm, specs, induction, redaction, cost]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-25T20:19:27.415Z
sources:
  - id: openwiki-source-d2f766169c1dc8b4bc06fd11
    resource: repo://billparse/induce.py
  - id: openwiki-source-57d69618549032e3a4f2e2c7
    resource: repo://billparse/llm.py
  - id: openwiki-source-32155ba535ca23dae2a10494
    resource: repo://billparse/pipeline.py
  - id: openwiki-source-6493e5e062ec6f788b766acc
    resource: repo://billparse/webapi.py
generated: { by: "claude-code", at: "2026-10-25T20:19:27.415Z" }
---

# LLM spec generation and extract-then-derive

The LLM is the last resort in the routing ladder (see [Routing and the spec interpreter](routing-and-specs.md)). It
has only two jobs, both in `billparse/llm.py`:

1. **`generate_spec`**: write a JSON spec for the layout from a redacted preview, with similar saved specs as
   examples, and fix it from validator feedback.
2. **`extract_rows`**: read the line items directly when no spec works. The pipeline then tries to derive a
   deterministic spec from those rows (`billparse/induce.py`).

The LLM never sees a whole file and never writes code. A spec is data that one interpreter runs.

A third LLM user, the spec-authoring agent, runs after the parse with tools on the whole file; see
[The spec-authoring agent](agent.md).

## What the LLM sees

`pipeline.build_preview(doc)` takes up to 4 regions and 40 rows each (400 rows for extraction): for frames the
column names plus the first rows, for grids the cell texts and which rows are bold. `llm.render_preview` prints them
one row per line and passes the result through `redact`, which replaces e-mail addresses, GUIDs, 12-digit account
ids, GCP billing account ids, ARNs and Azure resource paths with tokens.

## Route 4: LLM spec (`Pipeline._llm_spec`)

- The system prompt is `SPEC_GUIDE`: the allowed spec keys, the canonical fields (`transforms.CANONICAL`) and the
  registered transforms (`transforms.TRANSFORMS`), and the rules (map `billed_cost` and a label; never put totals
  into cost rows).
- Examples come from `Pipeline._examples(fingerprint)`: the saved learned specs most similar to the file's layout
  fingerprint (verified specs get a small bonus), padded with built-in specs shown as header lists. The number is
  `Settings.examples_k` (3).
- Up to `Settings.max_repairs` (3) calls are made, one after the other. Each spec goes through `check_spec`; a spec
  that fails or a response with no JSON becomes feedback for the next call. A spec that runs is tried like any other
  route, with the printed total that the tree of sums found added as an extra stated total, so an LLM spec cannot
  pass without a total to test. The loop stops at the first accepted attempt; otherwise the verdict's feedback goes
  into the next call together with the previous spec.

The JSON is cut out of the response text by `_json_from` (code fences removed, first `{` to last `}`).

## Route 5: LLM extract, then derive (`Pipeline._llm_extract`)

`extract_rows` asks for every billable line (`service_raw`, `description`, `billed_cost`, quantity, unit, unit price,
dates) and the grand total, with `EXTRACT_GUIDE` (tax lines as rows, no subtotal rows, one row per period in a
month-pivot table).

Then `induce.derive_spec(doc, rows)` searches for a deterministic spec that reproduces the LLM's amounts:

- for each frame and each grid table, the column whose numbers best match the LLM's amounts (at least half of them)
  becomes `billed_cost`, and the text column that best matches the LLM's labels becomes `description` (for grids,
  also a variant where it is `service_raw`);
- for PDFs, the mapper's line patterns (`mapper.LINE_PATTERNS`) are tried as `lines` specs;
- each candidate runs through the interpreter, and `agreement` scores it as the F1 between the two multisets of
  amounts rounded to cents.

| Result | What happens |
|---|---|
| a candidate with agreement ≥ 0.9 | tried as route `llm_extract_derived`; if accepted it is saved like a learned spec, so the next file of the layout needs no LLM |
| no such candidate | the LLM rows are used directly as route `llm_extract_direct`, never saved as a spec |

Direct LLM rows are tested only against totals that a route read from the file. The LLM's own `total` is kept as an
`llm_claim` that proves nothing. Their confidence is capped at 0.65, below the 0.7 accept level, so they are never
auto-accepted. After the final validation, a direct result that would be `consistent` is set to `unchecked`.

## Saving what the LLM produced

When a `mapper`, `llm_spec` or `llm_extract_derived` attempt is accepted, `Pipeline._finish` saves its spec as a
`candidate` with the id `<file type>-<fingerprint prefix>`, the layout fingerprint and a 20-row preview
(`stats.example_preview`), which later LLM calls use as an example. See
[Learning loop, spec lifecycle and review](../workflows/learning-and-review.md).

## Model, effort and cost

`AnthropicLLM` (the `LLM` protocol: `complete(system, user, max_tokens) -> str`) reads `ANTHROPIC_API_KEY`,
`BILLPARSE_MODEL` (default `claude-sonnet-5-5`), `BILLPARSE_EFFORT`, `BILLPARSE_MAX_TOKENS` (at least 16,000) and
`BILLPARSE_LLM_TIMEOUT` (300 s). It sends one request to the Messages API with `requests` (not the SDK), with no
retry. `default_llm()` returns `None` without a key. `Settings.use_llm=False` turns the LLM routes off (the CLI's
`--no-llm`); the web API uses the LLM only when `BILLPARSE_USE_LLM` is not `0` and a key is set (`webapi.llm_wanted`).

The cost of each call is estimated with `cost_of(usage, model)` from the `PRICES` table (cache writes at the input
price, cache reads at a tenth) and counted per thread, because parses run in parallel threads. `parse_bytes` stores
the difference as `claude_cost_usd` on the result. `llm_choices()` / `parse_choice()` give the model and effort pairs
the upload page offers (`LLM_CHOICES`, for example `sonnet-5-5:high`).

## Tests and open work

Tests never call the network: `tests/test_pipeline.py` passes a `FakeLLM` with scripted responses to the LLM routes. Structured JSON output and parallel spec candidates are planned in
`docs/new_direction/09-structured-llm-output.md`.
