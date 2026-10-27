# Task 08: Structured LLM output and parallel spec candidates (self-contained)

This file holds everything you need for this task: the project context first (parts A–D), then the task itself
(part E). It replaces the older `08-structured-llm-output.md`.

**Order:** start after task 07 (`task/07-price-checks`) is merged into `main`, including its last review fix (a
printed SKU or rate code that exists in the catalog but disagrees with the line's printed region is a
`price_conflict`, not `catalog_row`). **Needs:** task 07 (attempts are priced and checked). **STOP gate:** none.

---

## A. What billparse is, and where the LLM fits

billparse reads cloud bills and cost files and returns canonical line items with proof. A file goes through routes,
cheapest first, and stops at the first accepted attempt:

```
_builtin -> _learned -> _mapper -> _llm_spec (LLM writes a spec, up to max_repairs rounds) -> _llm_extract
```

The LLM **never** produces the final rows on the spec route. It writes a JSON **spec**, and the trusted interpreter
(`spec.run`) applies it. Every attempt is then cleaned, priced (task 07: one `KeyCache` per parse, through the
`_PRICING` context variable in `pipeline.py`), and checked by `validate`. A spec the code does not prove is never
accepted. So this task is about **cost and speed** (fewer failed rounds, better first candidates), not about trusting
the LLM more.

## B. What exists today (verified on `task/07-price-checks`)

- `billparse/llm.py`:
  - `AnthropicLLM.complete(system, user, max_tokens) -> str` posts to the Messages API with `requests`
    (`output_config.effort` when set) and tracks cost (`spent()`).
  - `generate_spec(llm, preview, examples, feedback, previous) -> dict` and `extract_rows(llm, preview) -> dict` ask
    for JSON in the prompt, then `_json_from(text)` cuts from the first `{` to the last `}`. A malformed or cut-off
    answer costs a whole repair round.
  - `SPEC_GUIDE` lists the spec keys, `CANONICAL` fields and `TRANSFORMS`.
- `spec.check_spec(spec)` validates a spec afterwards (`ALLOWED_TOP`, canonical fields, registered transforms,
  expressions, tree and context options, `meta.post` steps). Bad specs never pass.
- `pipeline._llm_spec`: **one** candidate per round, up to `Settings.max_repairs` (3) rounds, one after another. The
  feedback for the next round is the attempt's `verdict["feedback"]`, which since task 07 includes the price checks'
  example rows.
- `billparse/agent.py` `ClaudeClient` already uses the anthropic SDK with **tools whose `input_schema` constrains the
  output**: a working example of schema-constrained output in this codebase.
- Tests use `FakeLLM` (`tests/test_pipeline.py`), which only has `complete(system, user, max_tokens) -> str`.
- **Pricing context:** `_try` reads the per-parse price context (`_PRICING`, a `contextvars.ContextVar`). A context
  variable is **not** carried into a `ThreadPoolExecutor` thread automatically. Read step 3 before running anything
  in threads.

## C. Rules for this task

1. **Start clean:** `git status` is clean, on an up-to-date `main`, and `make test` passes. Branch:
   `task/08-structured-llm`.
2. **Read before you write:** `billparse/llm.py`, `billparse/spec.py` (`check_spec`, `ALLOWED_TOP`),
   `billparse/pipeline.py` (`_llm_spec`, `_try`, `_accept`, `_PRICING`, `parse_bytes`), `billparse/agent.py`
   (`ClaudeClient`, `_tool`, `Limits`), `tests/test_pipeline.py` (`FakeLLM`). If the code differs from this file, stop
   and tell the user.
3. **No behavior change without an LLM:** with `BILLPARSE_USE_LLM=0`, every sample's result is byte-for-byte the same
   as on `main`. `EXPECTED` unchanged.
4. **No network in tests:** fakes only.
5. **Cost stays bounded:** every call counts in `llm_calls` and cost; `max_repairs` keeps its meaning (rounds, not
   calls); the agent's daily budget and limits (`agent.Limits`) are respected where they apply.
6. **Docs:** README "Routing" step 4, and `example.env` for any new setting.
7. **Finish with a report:** what changed, each "Done when" item and how you verified it, and anything you decided
   that this file does not specify.

## D. Words

- **Candidate:** one spec the LLM proposed for a file.
- **Round:** one try that may contain several candidates; a repair round fixes the best candidate so far.

---

## E. The task

### In one sentence

Make the LLM return specs as schema-valid JSON every time, and try two candidates at once, picked by the same checks
that judge every route, so a new layout gets a proved spec in fewer, cheaper rounds.

### 1. A JSON Schema for specs (`billparse/spec_schema.py`)

- `spec_json_schema() -> dict`, built from the constants `check_spec` uses: `spec.ALLOWED_TOP` for the top-level
  keys, `transforms.CANONICAL` as the enum of `columns` and `derive` keys, and `transforms.TRANSFORMS` for transform
  names (both `"name"` and `{"name": arg}` forms).
- Keep nested objects (`table`, `detect`, `reshape`, `filters`, `fields`, `lines`, `totals`, `tree`, `context`,
  `meta`) **as loose as `check_spec` is**. The schema must not reject anything `check_spec` accepts.
- `rows_json_schema() -> dict` for `extract_rows`' output, from `EXTRACT_GUIDE`.
- Tests: every file in `specs/builtin/` validates; every learned spec the tests create validates; a spec with an
  unknown transform or field fails. Use `jsonschema`, adding it to the dev dependencies.

### 2. Structured calls

- Add an optional method to the `LLM` protocol: `complete_json(system, user, schema, max_tokens) -> dict`.
- `AnthropicLLM.complete_json`: one tool whose `input_schema` is the schema, `tool_choice` forcing that tool, and the
  tool's input read as the result. This is the same mechanism `agent.ClaudeClient` uses. If the current Anthropic API
  documentation offers a dedicated JSON-schema output option for the configured model, you may use it instead; say
  which one in the report. Keep cost tracking (`spent`).
- `generate_spec` and `extract_rows`: use `complete_json` when the LLM object has it; otherwise use `complete` +
  `_json_from` as today, so `FakeLLM` and older models keep working.
- A schema-valid answer can still fail `check_spec` (an expression, a regex). That stays a normal repair round.

### 3. Two candidates in the first round

- **Setting:** `Settings.spec_candidates`, from `BILLPARSE_SPEC_CANDIDATES`, default 2 (1 = today's behavior). Add it
  to `example.env`.
- **Generate in parallel:** in `_llm_spec`'s first round, request `spec_candidates` specs at once
  (`concurrent.futures.ThreadPoolExecutor`). Make them different: give each a different subset or order of
  `_examples(fp)`, and tell the second one that the first approach may have mapped columns wrong. Do not raise
  temperature.
- **Evaluate in the parse's own thread:** run `_try` on each candidate **sequentially, in the calling thread**. That
  keeps the `_PRICING` context, the shared `KeyCache` and the store access exactly as today. Only the LLM calls run in
  parallel.
- **Pick the best:**
  1. an attempt `_accept` accepts;
  2. else no failed `CRITICAL` check;
  3. else the fewest failed price checks (task 07);
  4. else the highest confidence.

  Yield every candidate as its own `Attempt`, so the attempts log shows them all.
- **Repair rounds** continue from the best candidate, one at a time, as today, with its feedback. `max_repairs` counts
  rounds: round 1 has `spec_candidates` calls, and each later round has one.
- **Agreement:** when two first-round candidates both produce rows, compare them: the same row count, the same total
  (to the cent), and the same values in each mapped canonical column on a 50-row sample. If they agree, add a check
  `candidates_agree` (pass, +0.05) to the chosen attempt's verdict. If they disagree on a column, put "the candidates
  disagree on column X" into the repair feedback. With one candidate, no check.

### 4. Large files

On the large path, routes run on the sample document (`bigfile.sample_doc`), so nothing changes there except that
the accepted spec then reads the whole file, as today. Test that a large file whose layout needs the LLM gets two
candidates on its sample and the chosen spec reads the whole file.

### Tests

Add `tests/test_llm_spec.py`, with fakes only.

- **Schema:** built-in specs validate; invalid specs are rejected (step 1).
- **Structured path:** a fake that has `complete_json` is used, and `_json_from` is not called.
- **Fallback path:** `FakeLLM` (text only) still works, including one invalid answer followed by a valid one.
- **Two candidates, one with swapped columns** (quantity and unit price): the correct one is chosen because of the
  task 07 checks; `llm_calls == 2`; both appear in `attempts`.
- **Two identical candidates:** `candidates_agree` passes. **Two that disagree:** the disagreement is in the feedback
  of the next round.
- **Pricing in candidates:** a candidate's verdict includes the price checks. This proves the evaluation ran in the
  parse's context, not in a thread without it.
- **`BILLPARSE_SPEC_CANDIDATES=1`:** exactly today's behavior and call count.
- **No LLM:** every sample's result equals `main`'s (rule 3).

### Done when

- [ ] Spec generation and row extraction use schema-constrained output when available, with the text fallback kept.
- [ ] Round 1 makes `spec_candidates` calls in parallel; evaluation stays in the parse's thread; repair rounds and
      limits keep their meaning.
- [ ] `candidates_agree` and disagreement feedback.
- [ ] README and `example.env` updated.
- [ ] Report: if an API key is available, the LLM calls and cost per new layout on a few sample files, before and
      after; otherwise say it was not measured.

### Do not

- Do not let the LLM produce final rows on the spec route, or accept a spec the checks do not prove.
- Do not run `_try` in worker threads.
- Do not change routes, checks or pricing.
