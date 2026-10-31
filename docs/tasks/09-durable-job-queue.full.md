# Task 09: Durable job queue, worker processes, async parse API, schedules (self-contained)

This file holds everything you need for this task: the project context first (parts A–D), then the task itself
(part E). It replaces the older `09-durable-job-queue.md`.

**Order:** start after task 08 (`task/08-structured-llm`) is merged into `main`. **Needs:** task 02 (large files),
task 05 (price catalog load), task 08. **STOP gate:** answered. The user's decisions are in part E, step 0; do not ask
again.

---

## A. What billparse is

billparse reads cloud bills and cost files (CUR, FOCUS, invoices, estimates, Terraform) and returns canonical line
items with proof, priced against the public AWS list prices kept in its own PostgreSQL database. Callers are other
services (savings estimates, cost estimates, forecasts) and the web app. A parse of a small file takes seconds. A
5-million-row CUR takes minutes (task 02 reads it in chunks). Learning a spec for a new layout calls Claude and takes
minutes and money.

## B. What exists today (verified on `task/08-structured-llm`)

**Two APIs, both run parses inside the API process:**

- **The public API, `billparse/api.py`.** `POST /parse` is **synchronous**: it saves the upload (`save_upload`,
  streamed to a temporary file in the data directory, never whole in memory), then calls `pipe.parse_path(...)`
  inside the request.
  - A large CUR holds the HTTP request open for minutes.
  - On a small file that needs learning, the agent runs inside the request too.
  - It returns `ParseResult`.
  - `POST /nightly` runs the whole nightly loop inside the request.
- **The web app's API, `billparse/webapi.py`, section "parsing in the background".**
  - `_pool = ThreadPoolExecutor(PARSE_WORKERS)` handles small files, and `_large_pool` (`LARGE_PARSE_WORKERS`)
    handles large files.
  - `_tasks: dict[str, dict]` keyed by sha256 holds a `Future` per parse. It lives **in memory only**, so after a
    restart `GET /parse/tasks/{sha}` returns 404 and running parses are lost.
  - `start_parse(...)` classifies the file (`bigfile.classify`) and submits `pipe.parse_path(..., move=temp)` to
    one of the pools.
  - `_task_out` builds `ParseTask` (`state`: `running | done | error`, `progress`, summary fields).
  - `_progress` reads the running agent job's trace.
  - The endpoints `/parse/start`, `/parse/tasks`, `/parse/tasks/{sha}`, `/documents/{sha}/reparse`, `/reprice`,
    `/stop`, `/parse-current` and `DELETE /documents/{sha}` all use `_tasks`.

**Learning (the agent):**

- `pipeline.parse_bytes` (small files): when `agent_mod.needs_agent(result)`, it calls `enqueue_for` and then
  **`agent_mod.run_job(...)` inline**, so the parse thread is busy for the whole learning job. When the job ends
  `proved`, the stored result is the re-parse with the new spec (`store.get_result`).
- `pipeline.parse_path` (large files) only calls `enqueue_for`. Those jobs run later, through
  `learn.run_queued_jobs` (from the nightly loop) or `python -m billparse agent drain`.
- Table `agent_job` (`db.AgentJobRow`):
  - one row per layout key (`enqueue_agent_job` deduplicates);
  - statuses `queued | running | stopping | proved | unproved | limit | rejected | error | stopped`;
  - the row also holds cost, rounds, trace and report.
- `webapi.stop_job` sets `stopping`, and `run_job` honors it.
- **`store.recover_agent_jobs()` runs in `get_store()` at API start** and marks every `running`/`stopping` agent
  job `error`. With separate workers that would kill jobs another process is running. **This must change.**

**Prices:** `scripts/aws/fetch_aws_price_files.py` downloads the AWS bulk files (about 1 GB gz).
`scripts/refresh_prices.py` (`make prices`) loads them with `prices.load.load_snapshot`. That load writes only the
changes, refuses a load that loses more than 10% of a price list's rates, and peaked at about 1.9 GB of memory and
about 3 minutes for a full load in the task 05 review. Nothing runs it on a schedule.

**Database and deploy:**

- `db.engine_for`: PostgreSQL in Docker (schema from Alembic `migrations/`), SQLite for tests and local runs (tables
  from `create_all`).
- `docker-compose.yml` has the services `db`, `api` and `web`. The volume `billparse-data` (`/data`) holds uploads,
  Parquet results and the catalog files.
- `Dockerfile` CMD is `uvicorn billparse.api:app`.
- README "Known limits" says: one API container only, tasks in memory.

**Tests:** SQLite (`tests/conftest.py`). `tests/test_webapi.py` clears `webapi._tasks` in its fixture.
`tests/test_large_files.py` patches `webapi._large_pool` and `_tasks`.

## C. Rules for this task

1. **Start clean:** `git status` is clean, on an up-to-date `main`, and `make test` passes. Branch:
   `task/09-job-queue`.
2. **Read before you write:**
   - `billparse/webapi.py` (the background section, `stop_job`, `save_upload`, `get_store`);
   - `billparse/api.py` (`/parse`, `/nightly`, `/learn/jobs*`);
   - `billparse/pipeline.py` (`parse_path`, `parse_bytes`, `on_step`);
   - `billparse/agent.py` (`run_job`, `needs_agent`, `enqueue_for`, `Limits`);
   - `billparse/learn.py` (`run_queued_jobs`, `run_nightly`);
   - `billparse/store.py` (agent job methods, `temp_path`, `file_path`);
   - `billparse/db.py`, `billparse/cli.py`, `scripts/refresh_prices.py`, `scripts/aws/fetch_aws_price_files.py`;
   - `docker-compose.yml`, `docker-compose.dev.yml`, `Dockerfile`, `Makefile`;
   - `web/src/api/queries.ts`.

   If the code differs from part B, stop and tell the user.
3. **Results do not change:** the same file gives the same stored result and contract as on `main`. This task moves
   *where* work runs, not *what* it computes. `EXPECTED` is unchanged.
4. **Every response shape the web app reads stays compatible.** Fields may be added, not removed or renamed. `make
   typecheck` passes.
5. **No network in tests:** the price download and webhooks use fakes.
6. **Migrations:** new tables through an Alembic migration (PostgreSQL) and the SQLModel classes (SQLite).
7. **Docs:** README (Running, API, Known limits) and `example.env` for every new setting.
8. **Finish with a report:** what changed, each "Done when" item and how you verified it, and anything you decided
   that this file does not specify.

## D. Words

- **Job:** one unit of background work in the `job` table (a parse, a reprice, a learning run, a scheduled task).
- **Queue:** the lane a job waits in. A worker serves one or more queues.
- **Lease:** how long a claimed job belongs to a worker. The worker renews it while it works. An expired lease
  means the worker died.
- **First-pass result:** the result of a parse before any learning. Learning may later replace it.

---

## E. The task

### In one sentence

Every parse, reprice, learning run and scheduled task becomes a row in a PostgreSQL `job` table, run by worker
processes outside the API. Jobs survive restarts, small files never wait behind large ones or behind learning,
callers can submit a parse and poll it or get a webhook, and AWS prices refresh weekly without anyone running a
command.

### 0. Decisions (made by the user; do not ask again)

| Question | Answer |
|---|---|
| Queue | **PostgreSQL** (`FOR UPDATE SKIP LOCKED`), no Redis |
| Deployment | **One host, docker compose.** API and workers share the `billparse-data` volume, so no object storage. Keep every file access going through `Store`, so S3 can be added later in one place. |
| Completion | **Polling, plus an optional webhook** per job (allowlisted URL, signed) |
| Schedules | **Yes.** The workers run the weekly AWS price refresh and the nightly loop. No cron. |

### 1. The `job` table (`db.JobRow` + migration)

| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `kind` | str | `parse`, `reprice`, `learn`, `prices`, `nightly` |
| `queue` | str, index | `small`, `large`, `learn`, `maintenance` |
| `state` | str, index | `queued`, `running`, `stopping`, `done`, `error`, `stopped` |
| `sha256` | str, nullable, index | the file (parse, reprice, learn) |
| `name` | str | file name or task name |
| `options` | JSON | force, use_llm, backend, fallback, agent_step, the `assume_*` price defaults, `temp_path` (an upload not yet moved into the store), `agent_job_id` (learn) |
| `dedupe_key` | str, nullable, **unique** | `prices:2026-W44`, `nightly:2026-10-27` (schedules) |
| `progress` | str, nullable | what the web app shows (`queued`, `reading 1.2M rows`, `pricing`, ...) |
| `attempts`, `max_attempts` | int | default max 2 (learn: 1, because a retry costs money) |
| `lease_until` | float, nullable | |
| `worker` | str, nullable | `hostname:pid:thread` |
| `created`, `started`, `finished` | float | |
| `deadline` | float, nullable | `started + JOB_DEADLINE_SECONDS` for its queue |
| `created_by` | str, nullable | |
| `error` | text, nullable | |
| `result` | JSON, nullable | a summary: `ParseTask`'s fields, or the nightly/prices report. Never the rows: they stay where `Store` keeps them. |
| `callback_url` | str, nullable | |
| `callback_state` | str, nullable | `pending`, `sent`, `failed` |
| `callback_attempts`, `callback_next` | int, float | |
| `parent_id` | bigint, nullable | a learn job's parse job |

**At most one active parse or reprice per file.** Use a partial unique index on `sha256` where
`kind IN ('parse','reprice') AND state IN ('queued','running','stopping')`. Both PostgreSQL and SQLite support
partial indexes. Enqueueing catches the unique violation and returns the existing job, so two uploads of the same
file return the same job, as `start_parse` does today.

**Keep `agent_job`** as the record of a learning run (layout deduplication, cost, trace, report: the Jobs page reads
it). A `learn` job only says *run agent_job N now*, through `options.agent_job_id`.

### 2. Claiming, leases, deadlines (`billparse/jobs.py`)

- **`enqueue(kind, queue, **fields) -> JobRow`**, `claim(queues, worker) -> JobRow | None`,
  `heartbeat(job_id, worker)`, `finish(job_id, state, result=None, error=None)`, `request_stop(job_id)`, `get`,
  `list`.
- **Claim on PostgreSQL:** one transaction.

  ```sql
  SELECT id FROM job
  WHERE queue = ANY(:queues)
    AND (state = 'queued' OR (state IN ('running','stopping') AND lease_until < :now))
  ORDER BY id
  FOR UPDATE SKIP LOCKED
  LIMIT 1
  ```

  Then set `running`, `worker`, `started`, `lease_until = now + LEASE_SECONDS` (60), `attempts += 1` and
  `deadline`.
- **Claim on SQLite:** the same query without `SKIP LOCKED`, inside `BEGIN IMMEDIATE`. That is correct for one
  process, which is all SQLite is used for.
- **Expired lease:** if `attempts >= max_attempts`, the job is set to `error` ("the worker stopped during the job")
  instead of being claimed. Do this inside the claim transaction.
- **Heartbeat:** a thread per running job renews `lease_until` every 15 s with its own session. If the row is no
  longer this worker's (another worker took it after a missed lease), the heartbeat flags the job as lost and the
  worker drops its result without writing it.
- **Stop and deadline:** the worker passes the pipeline an `on_step` that raises `JobStopped` when the row is
  `stopping` or `now > deadline`. It checks at most once a second, through the heartbeat's cached state, not with a
  query per call.
  - `stopping` ends `stopped`.
  - A deadline ends `error` ("over JOB_DEADLINE_SECONDS").
  - Make sure the large path calls `on_step` between chunks. If it does not, add that call. A stop request must
    take effect within one chunk.
- **Recovery:** delete `store.recover_agent_jobs()` from `get_store()`. A dead worker's `learn` job comes back
  through its lease. Its `agent_job` row is set back to `queued` by the `learn` runner before it starts again, or
  `error` when out of attempts.

### 3. Workers (`python -m billparse worker`)

- `python -m billparse worker --queues small,learn --concurrency 2`: N threads in one process, each looping on claim,
  run, finish. It sleeps `WORKER_POLL_SECONDS` (default 1) when idle.
  - On PostgreSQL, also `LISTEN billparse_jobs`. `enqueue` sends `NOTIFY` after commit so jobs start at once.
  - Polling stays the fallback.
- **The parse runner** (`kind=parse`):
  1. `pipe.parse_path(path, name, force=..., agent=..., user=..., on_step=..., move=temp, sha=..., price_defaults=...)`,
     with the pipeline built as `webapi._pipeline` builds it (backend and fallback choices).
  2. `result` gets the `ParseTask` summary (move `_task_out`'s summary code into a function both use).
  3. Remove the temporary upload when the job ends in any state.
- **The reprice runner:** `pipe.reprice(sha, defaults)`, as the endpoint does today.
- **Learning leaves the parse.** In `parse_bytes`, replace the inline `run_job` with: `enqueue_for` as today, then
  `jobs.enqueue("learn", "learn", sha256=..., options={"agent_job_id": id}, parent_id=<this parse job, when there is
  one>)`, then return the first-pass result. Large files do the same, so learning is identical for both paths.
- **The `learn` runner:** `agent.run_job(pipe, agent_job_id, llm=..., limits=...)`.
  - When it ends `proved`, `run_job` has already stored the re-parse. Then fire the parse job's webhook again with
    the event `result.updated` (step 6).
  - Keep `agent.Limits` (daily budget) as it is. A job refused for budget ends `done`, with the `agent_job` status
    `limit`.
- **`learn.run_queued_jobs` and `agent drain`** no longer run jobs. They make sure each `queued` `agent_job` has a
  `learn` job. Keep the CLI command, and say so in its help.
- **Graceful stop:** on SIGTERM, stop claiming and let running jobs finish within the container's stop grace period.
  Whatever is left comes back through its lease. That is safe because a parse is idempotent per sha256.
- **Local, one command:** with `INLINE_WORKER=1` (default `1` when `DATABASE_URL` is SQLite, else `0`), the API
  process starts one worker thread for all queues, so `make api-local` and tests need nothing else.

### 4. The APIs

**Public API (`api.py`).** New, additive:

- `POST /parse/jobs`: multipart file and the same query parameters as `POST /parse`, plus an optional `callback_url`.
  It saves the upload and enqueues.
  - Response `202 {"job_id", "sha256", "state", "status_url": "/parse/jobs/{id}", "result_url": "/documents/{sha256}"}`.
  - A file already being parsed returns its job with `200`.
- `GET /parse/jobs/{id}`: state, progress, created, started, finished, error, the summary (`ParseTask` fields), a
  `learning` object when a learn job exists (`state`, `agent_status`, `cost_usd`), and `result_url` once `done`.
- `GET /parse/jobs?sha256=&state=&limit=50`.
- `POST /parse/jobs/{id}/stop`.

**`POST /parse` stays,** with the same request and the same `ParseResult` shape. It enqueues and waits, polling the
row every 0.5 s, up to `PARSE_WAIT_SECONDS` (default 300). If the job is not done by then, it answers **202** with
the job body above. Say this in the README and the OpenAPI description. Parses no longer run in the API process.

**`POST /nightly`** enqueues a `nightly` job and returns it (202). The report is the job's `result`.

**`/learn/jobs/{id}/retry`** also enqueues a `learn` job. **`/learn/jobs/{id}/stop`** also requests the stop on the
`learn` job.

**Web app API (`webapi.py`).** Same paths, same `ParseTask` shape:

- `start_parse` enqueues and returns the sha.
- `_task_out` reads the latest parse or reprice job of the sha. `queued` maps to `state="running"`,
  `progress="queued"`. `stopping`/`stopped` map to `running`/`error` with a message.
- `_progress` keeps showing the agent's trace while the file's `learn` job runs.
- `GET /parse/tasks`: the latest 100 parse and reprice jobs. `GET /parse/tasks/{sha}` works after a restart.
- Add `learning: {state, agent_status} | None` to `ParseTask`, and show it on the Upload page: "first result ready,
  learning a better spec". Today that page waits for learning to end. **Upload must not wait for learning any
  more.**
- `DELETE /documents/{sha}` refuses with 409 while a parse, reprice or learn job of the file is active.
- Delete `_pool`, `_large_pool`, `_tasks` and `_lock`. `PARSE_WORKERS` becomes the `worker` concurrency, and
  `LARGE_PARSE_WORKERS` becomes the `worker-large` concurrency.

### 5. Queues and where they run

| Queue | Jobs | Served by | Default concurrency |
|---|---|---|---|
| `small` | parse and reprice of files under `LARGE_FILE_MB` (task 02's `bigfile.classify` decides; no second threshold) | `worker` | `PARSE_WORKERS` = 2 |
| `learn` | agent runs | `worker` | 1 thread (cost) |
| `large` | parse and reprice of large files | `worker-large` | `LARGE_PARSE_WORKERS` = 1 |
| `maintenance` | `prices`, `nightly` | `worker-large` | shares its 1 thread |

Per-queue concurrency: `--queues small:2,learn:1`. Each queue has its own thread count, so a long learning run
never takes a `small` thread.

`JOB_DEADLINE_SECONDS`: `small` 1800, `learn` 3600, `large` 7200, `maintenance` 7200, each overridable as
`JOB_DEADLINE_<QUEUE>`.

### 6. Webhooks

- `callback_url` is accepted only if it starts with one of `CALLBACK_ALLOWLIST` (comma-separated URL prefixes;
  empty means webhooks are off and a request with a URL gets 400). Must be `https`, except `http://localhost` in
  development.
- Events: `parse.done`, `parse.error`, `parse.stopped`, and `result.updated` (learning proved a new spec and the
  stored result changed).
  - Body: `{"event", "job_id", "sha256", "state", "summary", "result_url", "sent_at"}`. Never the rows.
  - Header `X-Billparse-Signature: sha256=<hex HMAC of the body with CALLBACK_SECRET>`, plus
    `X-Billparse-Event` and `X-Billparse-Job`.
- **Delivery is its own loop** in every worker process, not part of the job:
  - It claims rows with `callback_state='pending' AND callback_next <= now` (SKIP LOCKED).
  - Timeout 10 s, no redirects followed. A `2xx` marks the row `sent`.
  - Otherwise it retries with backoff (30 s, 2 min, 10 min, 1 h, 6 h), then `failed`.
  - A slow receiver never delays a parse.

### 7. Schedules

- A scheduler loop in each worker process. It is cheap, and duplicates are prevented by `dedupe_key`. Every
  minute it enqueues what is due:
  - **`prices`** (queue `maintenance`), weekly: `PRICES_REFRESH_DAY` (default `sun`) at `PRICES_REFRESH_HOUR_UTC`
    (default `10`). `dedupe_key = prices:<ISO year-week>`.
  - **`nightly`** (queue `maintenance`), daily at `NIGHTLY_HOUR_UTC` (default `11`). `dedupe_key = nightly:<date>`.
  - An empty setting turns that schedule off.
- **The `prices` job** has two steps, and `progress` says which one is running:
  1. **Download:** run the fetcher's code (import it; do not shell out) into
     `PRICE_FILES_DIR/.incoming-<date>`. On success, rename it to replace the previous download. A failed
     download leaves the previous files and the current snapshot untouched.
  2. **Load:** `load_snapshot` as `scripts/refresh_prices.py` does. A `Refused` load (lost more than 10% of rates)
     ends the job `error` with the reason. The current snapshot stays, and that is the alert.
  - The result is the snapshot id, the price lists loaded, and the rates added, changed and closed.
  - Stored documents are **not** repriced automatically; `reprice` exists for that.
- **The `nightly` job:** `learn.run_nightly(pipe)`, which now only enqueues learn jobs instead of running them
  (step 3). It also removes temporary uploads older than one day that no job references.
- `GET /jobs/schedules` returns each schedule's next run and its last job (state, finished, error), for the web app
  and for checking.

### 8. docker-compose

- `api`: unchanged command. `INLINE_WORKER=0`.
- `worker`: same image, `command: python -m billparse worker --queues small:${PARSE_WORKERS:-2},learn:1`, same env
  and volume, `stop_grace_period: 2m`, `restart: unless-stopped`, `depends_on: db (healthy)`.
- `worker-large`: `--queues large:${LARGE_PARSE_WORKERS:-1},maintenance:1`, `stop_grace_period: 10m`, `mem_limit:
  ${LARGE_WORKER_MEM:-6g}` (a full price load peaks near 2 GB; a large parse stays under 1 GB, per tasks 02 and 05).
- Each process's SQLAlchemy pool: `pool_size=threads+2`, `max_overflow=2`, so all containers together stay far
  under PostgreSQL's default 100 connections. Say the total in the README.
- `docker-compose.dev.yml`: the workers mount the code and restart on changes, like `api`.
- `make worker` / `make logs-worker`. Running several API containers is now supported; say how in the README (state
  lives in PostgreSQL and the shared volume).

### Tests (`tests/test_jobs.py`, SQLite, fakes only)

- **Restart:** enqueue a parse, build a new app and store on the same database, and `GET /parse/tasks/{sha}` and
  `GET /parse/jobs/{id}` still return it.
- **Duplicates:** two uploads of the same file while it is queued return the same job; the partial unique index
  holds.
- **Leases:** a `running` job with an expired lease is claimed again with `attempts` 2. At `max_attempts` it ends
  `error`. A worker whose job was taken over drops its result.
- **Stop:** a job set to `stopping` ends `stopped` within one `on_step`. A large file stops between chunks.
- **Deadline:** a job past its deadline ends `error`.
- **Learning:** with `FakeLLM` and the agent on, the parse job ends `done` with the first-pass result **before** the
  learn job runs. When the learn job proves a spec, the stored result is replaced and a `result.updated` webhook is
  queued. `agent_job` is never set to `error` by an API start.
- **Queues:** a long job on `large` (a fake that waits on an event) does not delay a job on `small`. A long `learn`
  job does not delay `small`. Run the workers in threads.
- **`POST /parse`:** the same `ParseResult` as `main` for the sample files. A job slower than `PARSE_WAIT_SECONDS`
  (set to 1) answers 202 with the job.
- **Webhooks:**
  - not on the allowlist → 400;
  - the signature verifies;
  - a 500 then a 200 → `sent` after two attempts;
  - five failures → `failed`;
  - a hanging receiver does not delay the next parse.
- **Schedules:**
  - two worker loops at the same due minute create one `prices` job (`dedupe_key`);
  - a fake fetcher and a fixture download load a snapshot;
  - a fake `Refused` ends the job `error` and the old snapshot stays current;
  - a failed download leaves `PRICE_FILES_DIR` as it was.
- **Existing tests:** `test_webapi.py` and `test_large_files.py` pass, their fixtures updated to clear the `job`
  table instead of `_tasks` and `_large_pool`.
- **PostgreSQL (in the report, not CI):** with `docker compose up`, run two `worker` containers
  (`--scale worker=2`), upload 50 small files and 2 large ones at once, and show that no job ran twice (each `job.id`
  has one `started`) and that small files finished while the large ones were running. Then `docker compose restart
  worker-large` during a large parse and show that it is retried and ends `done`.

### Done when

- [ ] No parse, reprice or learning state lives only in memory; `_pool`, `_large_pool`, `_tasks` are gone.
- [ ] The API process runs no parse, learning or price load (with `INLINE_WORKER=0`).
- [ ] `POST /parse/jobs`, `GET /parse/jobs/{id}`, webhooks, and `POST /parse` keeping its shape.
- [ ] Learning runs only on the `learn` queue; uploads return the first-pass result without waiting for it.
- [ ] Weekly price refresh and nightly loop run from the workers; `GET /jobs/schedules` shows them.
- [ ] `docker-compose.yml` runs `api`, `worker`, `worker-large`; README "Known limits" no longer lists in-memory tasks
      or one API container; `example.env` has every new setting.
- [ ] Web app Upload, Documents and Jobs pages work (`make typecheck`), with the Upload page showing learning
      separately.
- [ ] Report with the PostgreSQL run above.

### Do not

- Do not add Redis, Celery or another broker.
- Do not change what a parse computes, the checks, pricing, or the contract.
- Do not put rows or file contents in the `job` table or in webhook bodies.
- Do not follow redirects or call URLs outside `CALLBACK_ALLOWLIST`.
- Do not mark another process's running job as failed at startup. Leases decide.
