# Task 08: Durable job queue

**Order:** after 07. **Needs:** nothing in code. **STOP gate: yes. Ask the user before writing code (step 0).**
**Read first:** `billparse/webapi.py` (the "parsing in the background" section), `pipeline.Pipeline.parse_bytes`,
`billparse/agent.py` (`run_job`, `needs_agent`, `enqueue_for`), `billparse/store.py` (agent job methods),
`billparse/db.py`, `docker-compose.yml`.

## In one sentence

Parse jobs survive restarts and run in worker processes separate from the API, small and large files do not wait on
each other, and a slow learning job never blocks a parse.

## Step 0: STOP and ask the user (required)

1. Use **PostgreSQL as the queue** (no new infrastructure; this spec's default), or add **Redis** with a queue
   library?
2. Where will this run in production, how many API and worker containers, and is there a limit on database
   connections?
3. Should the parse API notify callers when a job ends (a callback URL), or will callers poll?

Write the answers into `docs/tasks/08-durable-job-queue-answers.md`. The rest of this spec assumes PostgreSQL and
polling plus an optional callback. If the user chooses Redis, keep the same table of job states and API, and swap
only the claiming mechanism.

## What exists today (verified)

- `webapi.py`:
  - `_pool = ThreadPoolExecutor(PARSE_WORKERS)` and `_tasks: dict[str, dict]` hold the running parses in memory.
  - `start_parse()` submits `pipe.parse_bytes(...)` to the pool.
  - `_task_out()` builds `ParseTask`, the response model, from the in-memory task.
  - `GET /parse/tasks/{sha256}` returns 404 for anything started before the last restart.
  - The endpoints reparse, stop and parse-current also use `_tasks`.
- `pipeline.parse_bytes`: when `agent.needs_agent(result)` is true, it calls `agent_mod.run_job(...)` **inline**,
  so the parse worker is busy for the whole learning job (minutes, with LLM calls).
- `agent_job` (`db.AgentJobRow`) already exists with states, and `store.recover_agent_jobs()` exists. Reuse that
  pattern.
- The README states the limit: one API container only.
- Tests run on **SQLite** (`tests/conftest.py`), which has no `FOR UPDATE SKIP LOCKED`.

## Design

### Table `parse_job` (`db.py` + Alembic migration)

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `sha256` | str, index | the file (already stored by `store.save_doc`) |
| `name` | str | |
| `queue` | str, index | `small`, `large`, `learn` |
| `state` | str, index | `queued`, `running`, `stopping`, `done`, `error`, `stopped` |
| `step` | str | reading, routing, pricing, validating, learning |
| `options` | JSON | force, backend, fallback, `assume_os` / `assume_tenancy` (task 05), callback_url |
| `attempts`, `max_attempts` | int | default max 2 |
| `lease_until` | float | the worker renews it every 15 s while working |
| `worker` | str | host and pid |
| `created`, `started`, `finished` | float | |
| `created_by` | str | |
| `error` | text | |
| `result` | JSON | the `ParseTask` fields at the end |

At most one job per `sha256` may be in `queued` or `running`. Enforce it in the code that enqueues, inside a
transaction. A second upload of the same file returns the existing job.

### Claiming jobs

- PostgreSQL: `SELECT ... WHERE queue = ANY(:queues) AND (state='queued' OR (state='running' AND lease_until <
  now)) ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1`, then set it to running, set the lease and increment `attempts`.
- SQLite (tests and local runs): the same query without `SKIP LOCKED`, inside `BEGIN IMMEDIATE`. That is correct for
  one worker process.
- A job whose `attempts` reached `max_attempts` when its lease expired is set to `error` ("worker stopped during
  the job").

### Queues

- The upload size decides the queue: at or below `LARGE_FILE_MB` (default 20) → `small`, above → `large`.
- Learning jobs go to `learn`.

### Learning out of the parse

- `parse_bytes` stops calling `run_job` inline. When `needs_agent(result)` is true, it calls `enqueue_for` as today
  and puts a `learn` job in `parse_job` with that agent job's id in `options`, then returns the first-pass result.
- The `learn` worker runs `agent.run_job`. When the job ends `proved`, it re-parses the file with the new spec
  (as the inline code does today), so the document's stored result is replaced.

### Workers

- New CLI command: `python -m billparse worker --queues small,learn --concurrency 2` (`cli.py`). It loops: claim,
  run, heartbeat, finish.
- Each step of `parse_bytes` reports progress through the existing `on_step` callback. Write the step to the job.
- `docker-compose.yml` gets two new services from the same image:
  - `worker` (`--queues small,learn`);
  - `worker-large` (`--queues large --concurrency 1`).
- The API container no longer runs parses.
- Local development: `INLINE_WORKER=1`, set by `make api-local`, starts one worker thread inside the API process, so
  local development stays one command.

### Timeouts and stopping

- `JOB_DEADLINE_SECONDS` (default 1800): a job over it is stopped between steps and ends `error`, with its best
  result saved if there is one.
- `POST /documents/{sha256}/stop` sets `stopping`; the worker checks between steps and ends the job `stopped`.

### API (paths and response shapes unchanged)

- `POST /parse/start`, `GET /parse/tasks`, `GET /parse/tasks/{sha256}`, `POST /documents/{sha256}/reparse`,
  `/stop` and `/parse-current` read and write `parse_job` instead of `_tasks`. `ParseTask.state` keeps its values
  (`running`, `done`, `error`); map `queued` to `running` with `progress = "queued"`.
- `GET /parse/tasks` lists the most recent 100 jobs.
- If step 0 said yes to callbacks: an optional `callback_url` on `POST /parse/start`.
  - It must start with an entry of the new `CALLBACK_ALLOWLIST` env var (comma-separated URL prefixes); otherwise
    reject the request with 400.
  - When the job ends, the worker POSTs the `ParseTask` JSON to it, retrying 3 times with backoff.
- Delete `_pool` and `_tasks`.

## Tests (`tests/test_jobs.py`, SQLite)

- **Restart:** start a job, create a new app and store on the same database, and `GET /parse/tasks/{sha}` still
  returns it.
- **Duplicates:** two uploads of the same file while it is queued return the same job.
- **Leases:** a job with an expired lease is claimed again; after `max_attempts` it ends `error`.
- **Learning:** with `FakeLLM` and the agent on, the parse returns its first-pass result before the learning job
  runs, and the stored result is replaced when the job ends `proved`.
- **Queues:** a job on `large` does not delay a job on `small`. Run two workers in threads with separate queues.
- **Stop:** a job set to `stopping` ends `stopped`.
- **Existing:** `tests/test_webapi.py` passes. It clears `webapi._tasks` in its fixture; update the fixture to clear
  `parse_job` instead.

## Done when

- [ ] No parse state lives only in memory.
- [ ] Learning runs only on the `learn` queue.
- [ ] `docker-compose.yml` runs the API, `worker` and `worker-large`; README explains running several API
      containers.
- [ ] Web app pages Upload, Documents and Jobs work unchanged (`make typecheck`).
- [ ] README "Known limits" no longer lists the in-memory task limit.
