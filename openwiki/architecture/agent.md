---
type: architecture
title: The spec-authoring agent
description: How billparse sends a file that no spec proved to a Claude agent that writes or repairs a JSON spec with tools on the real file, and how only the code's own checks decide whether that spec is saved.
tags: [agent, llm, specs, learning, jobs]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-25T20:19:27.415Z
sources:
  - id: openwiki-source-07252ff4952d237f35ee1eca
    resource: repo://billparse/agent.py
  - id: openwiki-source-adb9568b7bddb4c8bc2d2760
    resource: repo://billparse/api.py
  - id: openwiki-source-32155ba535ca23dae2a10494
    resource: repo://billparse/pipeline.py
  - id: openwiki-source-c9b0d4adb896b06d8a75bc74
    resource: repo://billparse/store.py
  - id: openwiki-source-6493e5e062ec6f788b766acc
    resource: repo://billparse/webapi.py
generated: { by: "claude-code", at: "2026-10-25T20:19:27.415Z" }
---

# The spec-authoring agent

`billparse/agent.py` runs an LLM agent on a file whose parse was not proved. The agent never turns the bill into
lines itself. It writes a **spec** (the JSON that the interpreter in `spec.py` runs) for the file's layout. The
code then runs that spec with every validation check, and only the code decides if the spec is kept.

```
evidence report (code) -> author (Claude + tools on the real file) -> verify (code) -> regression gate (code)
                                  ^                                         |
                                  +------ failed checks as feedback --------+   (at most AGENT_MAX_ROUNDS)
```

## When a file goes to the agent

`Pipeline.parse_bytes` (`billparse/pipeline.py`) calls the agent step after the normal parse, but only when all of
these are true:

- the pipeline allows it (`agent=True`) and `Settings.use_agent` is on. `use_agent` defaults to `agent.enabled()`,
  which needs `AGENT_ENABLED=1` **and** `ANTHROPIC_API_KEY`;
- the result is not a cached result;
- `needs_agent(result)` is true: the result is cost lines (or has no kind), its status is not `unsupported` or
  `approved`, and its completeness is `contradicted` or `unchecked` (or not known).

A `consistent` result is deliberately **not** sent: the file prints no total, every internal check passes, and no
spec can prove a total that the file does not print, so a job would only cost money.

The agent step runs **inline** in the parse: `parse_bytes` calls `run_job` and waits for it. If the job ends
`proved`, the stored result (re-parsed with the new spec) replaces the first-pass result; otherwise the first pass
stands. The web API reports the step to the upload page as `learning` through the `on_step` callback.

## Jobs: one per layout

`enqueue_for` creates a row in the `agent_job` table (`AgentJobRow` in `billparse/db.py`) through
`Store.enqueue_agent_job`. The job key makes a layout go to the LLM once:

| Kind | Key | When |
|---|---|---|
| `new` | `layout:<fingerprint hash>` | the result was not read by a learned spec |
| `repair` | `repair:<spec id>@v<version>:<sha prefix>` | a learned spec read the file but did not prove it |

If a row with that key exists and is not `queued`, no new job is made, so a layout the agent already tried is not
tried again. `POST /learn/jobs/{id}/retry` (`Store.retry_agent_job`) sets the job back to `queued`, so the next
upload of that layout (or `POST /learn/backfill`) runs it again.

Job statuses: `queued`, `running`, `stopping`, `proved`, `unproved`, `limit`, `stopped`, `rejected`, `error`. When
the web API starts, `Store.recover_agent_jobs` turns jobs left `running` or `stopping` by a stopped process into
`error`, so the file keeps its earlier result.

## One job (`run_job`)

1. Loads the stored upload and its current result summary; for a repair, also the current spec as the start spec.
2. Picks up to 3 saved specs of similar layouts as examples (`pipeline._examples(fingerprint)`).
3. Runs `author(...)` (below), writing each event to the trace.
4. If the outcome is not `proved`, the job ends with that status and the file keeps its first result.
5. If it is proved, the spec gets the id `agent-<file type>-<fingerprint prefix>` (or keeps the repaired spec's id),
   `trust="candidate"`, the layout fingerprint and `stats.created_by="agent"`.
6. A **repair** must pass `learn.regression_gate`: the new version must still read every file the spec read before.
   A failure ends the job `rejected` and nothing is saved.
7. `Store.save_spec` saves it as a new version, and the file is parsed again with `force=True, agent=False,
   run=False`, so the learned route now finds the spec.

Any exception inside a job is caught and kept on the job as `error`, so a job never takes the API down.

## The author loop (`author`)

The first user message is the **evidence report** (`evidence_report`): the file and its regions, the current status
and completeness, the checks that failed or warned, amounts nothing explains, groups that do not add up, and the
specs already tried. It is passed through `redact` before it is sent. Example specs and the start spec are added.

The system prompt is `AGENT_GUIDE`: the LLM spec guide (`llm.SPEC_GUIDE`) plus the `tree` source and `context`
options and a working method. It tells the model never to put values of one file (an account, a date, an amount)
into a spec, because a spec describes a layout.

Each round, the model calls tools until it calls `submit_spec`. The tools run on the real file as the interpreter
reads it (`Workspace`):

| Tool | What it returns |
|---|---|
| `list_regions` | pages, sheets and tables, with row counts (and columns for frames) |
| `read_rows` | up to 120 rows of a region: text, x position, bold, amounts |
| `row_shapes` | every kind of row (indent, bold, number of amounts, words) with counts and examples |
| `find_rows` / `test_regex` | rows matching a regex / what a regex and its named groups match |
| `find_service` | catalog services for a label |
| `check_spec` | the interpreter and every check on the whole file (status, completeness, failed checks, totals, sample lines) |
| `submit_spec` | the round's answer: a spec and one sentence on what is still wrong |

Tool output is cut at 12,000 characters and redacted. A tool error (bad regex, bad spec) goes back to the model as
an `is_error` tool result, so the model can fix it.

After `submit_spec`, the code runs `check(...)` on the spec itself. Only `completeness == "reconciled"` proves the
file and ends the loop. Otherwise the check report goes back to the model as feedback for the next round. The best
spec so far is kept by score (`reconciled` 3, `consistent` 2, `unchecked` 1) and returned when the rounds end.

## Limits, cost and stopping

`Limits.from_env()` reads `AGENT_MAX_ROUNDS` (3), `AGENT_TOOL_CALLS` per round (40), `AGENT_MAX_COST_USD` (3.0),
`AGENT_MAX_SECONDS` (900), `AGENT_MAX_TOKENS` (16000), `BILLPARSE_AGENT_MODEL` (default `claude-opus-5-5`) and
`AGENT_EFFORT` (`high`). The cost of each call is estimated with `llm.cost_of`. Reaching the cost or time limit ends
the job as `limit` with the best spec so far. A person can stop a job (`POST /learn/jobs/{id}/stop`): the loop checks
`should_stop` before each model call and ends as `stopped`. A `refusal` stop reason ends the job as `error`.

## The model client

`ClaudeClient` calls the Messages API through the `anthropic` SDK (`client.beta.messages.create`) with
`tool_choice=auto`, `output_config.effort`, and the server-side fallback beta (`fallbacks="default"`). Tests replace it
with any object that has the same `create(system, messages, tools, limits)` shape (the `AgentLLM` protocol).

## Traces

Every round, model response (stop reason, usage, cost, shown blocks), tool call, verification, gate and decision is
appended as one JSON line to `<data dir>/agent/<job id>.jsonl` (`trace_path`). The web app reads it through
`GET /learn/jobs/{job_id}/trace`.

## Other entry points

- `POST /specs/learn` (`billparse/api.py`) runs `author` on an uploaded file outside the job queue and saves a
  proved spec as a new candidate (never as a new version of another spec). It refuses with 409 when a saved spec
  already reconciles the file, unless `replace=true`.
- `POST /specs/verify` runs `agent.check` on a given spec and file, with no model call.
- `POST /learn/backfill` parses again every stored document that `needs_agent` selects.

All endpoints that call the model answer 403 unless `AGENT_ENABLED=1`.

## Tests

`tests/test_agent.py` drives the loop with a scripted `FakeLLM`: a spec that proves a bill is saved; a spec the
checks do not prove is not saved; a bad option reaches the model as an error; the cost limit stops a job; a repair
must pass the regression gate; a parse runs the agent step by itself; a layout that was not proved is not tried
again; only contradicted or unchecked results go to the agent.
