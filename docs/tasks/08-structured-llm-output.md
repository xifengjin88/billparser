# Task 08: Structured LLM output and parallel spec candidates

**Order:** after 07. **Needs:** nothing (independent; scheduled here so it does not interrupt tasks 01–07).
**STOP gate:** none.
**Read first:** `billparse/llm.py`, `billparse/agent.py` (`ClaudeClient`, `TOOLS`, `_tool`), `pipeline._llm_spec`.

## In one sentence

Make the LLM return specs as schema-valid JSON every time, and try several spec candidates at once instead of one
after another.

## What exists today (verified)

- `llm.AnthropicLLM.complete(system, user, max_tokens)` posts to the Messages API with `requests` and returns the
  text.
- `llm.generate_spec` and `llm.extract_rows` ask for JSON in the prompt, then `_json_from` pulls it out of the
  text: it strips code fences and takes everything from the first `{` to the last `}`. A malformed or cut-off answer
  costs a whole repair round.
- `spec.check_spec` validates specs afterwards, so bad specs never pass. This task is about cost and speed, not
  correctness.
- `pipeline._llm_spec`: one candidate per round, up to `Settings.max_repairs` (3) rounds, one after another.
- `agent.ClaudeClient` already uses the anthropic SDK with tools (`TOOLS` built by `_tool(...)` with an
  `input_schema`). It is a working example of schema-constrained tool input in this codebase.
- Tests replace the LLM with `FakeLLM`, which has only `complete(system, user, max_tokens) -> str`.

## Steps

1. **JSON Schema for the spec format** (`billparse/spec_schema.py`): a function `spec_json_schema() -> dict` built
   from the same constants `check_spec` uses:
   - `spec.ALLOWED_TOP` for the top-level keys;
   - `transforms.CANONICAL` as the enum of column and derive keys;
   - `transforms.TRANSFORMS` for transform names.

   Keep nested objects (`table`, `detect`, `reshape`, `filters`, `fields`, `lines`, `totals`) as loose as
   `check_spec` is. The schema must not reject anything `check_spec` accepts. Add a test that every file in
   `specs/builtin/` validates against it (use the `jsonschema` package; add it to dev dependencies if missing).

2. **Structured calls.**
   - Extend the `LLM` protocol with an optional method `complete_json(system, user, schema, max_tokens) -> dict`.
   - In `AnthropicLLM`, implement it the same way `agent.py` constrains output: a single tool whose `input_schema`
     is the schema, with `tool_choice` forcing that tool. Read the tool's input as the result.
   - If the current Anthropic API documentation offers a dedicated structured-output (JSON schema) option for the
     configured model, you may use it instead; say which one you used in the report.
   - `generate_spec` and `extract_rows` call `complete_json` when the LLM object has it, and fall back to
     `complete` + `_json_from` otherwise, so `FakeLLM` keeps working.
   - Write a schema for `extract_rows`' output too, from `EXTRACT_GUIDE`.
   - Add `complete_json` to `FakeLLM` in a new test, to cover the structured path.

3. **Parallel candidates** (`pipeline._llm_spec`):
   - New setting `spec_candidates` in `Settings`, from the `BILLPARSE_SPEC_CANDIDATES` env var, default 2. Add it to
     `example.env`.
   - In the first round, generate `spec_candidates` specs concurrently (`concurrent.futures.ThreadPoolExecutor`).
     Vary them a little: for example, include different examples from `_examples` in each prompt.
   - Run `_try` on each candidate and keep the best: an accepted attempt first, then the highest confidence.
   - Repair rounds continue from the best candidate, one at a time, as today.
   - Every call counts in `llm_calls` and in the cost (`AnthropicLLM.spent`).
   - The attempts list records each candidate.

4. **Agreement check.** When two or more first-round candidates both produce rows, add a check
   `candidates_agree` (weight +0.05) when their row counts and totals are equal and their mapped columns hold the
   same values on a sample of 50 rows. When they disagree on a column, add that column's name to the feedback.

## Tests

- schema: every built-in spec validates; a spec with an unknown transform fails validation;
- the structured path: a fake with `complete_json` is used, and `_json_from` is not called;
- the fallback path: `FakeLLM` (text only) still works, including one invalid response followed by a valid one;
- with two candidates where one is better, the better one is chosen and `llm_calls == 2`;
- `candidates_agree` passes for two identical candidates and is absent with one candidate;
- `EXPECTED` unchanged.

## Done when

- [ ] Spec and row-extraction calls use schema-constrained output when available.
- [ ] Candidates run concurrently; repair limits keep their meaning.
- [ ] README "Routing" step 4 updated; `example.env` documents `BILLPARSE_SPEC_CANDIDATES`.
