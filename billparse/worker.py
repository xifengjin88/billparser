"""Worker processes: they run the jobs of the `job` table (jobs.py), outside the API.

    python -m billparse worker --queues small:2,learn:1
    python -m billparse worker --queues large:1,maintenance:1

Each queue has its own threads, so a long learning run or a large file never takes a `small` thread. A thread claims
a job, runs it with a heartbeat (lease renewed every 15 s; the job's state read every second, so a stop request or
the deadline ends it at the parse's next step, between two chunks of a large file), and finishes it. A job another
worker took over (a missed lease) is dropped without writing. Besides the job threads, each process delivers pending
webhooks (deliver_callbacks) and enqueues what the schedules make due (the weekly price refresh: PRICE_PROVIDERS, the nightly
loop; `dedupe_key` keeps two processes from enqueuing the same run). On PostgreSQL a worker LISTENs for new jobs;
polling (WORKER_POLL_SECONDS) stays the fallback. SIGTERM: no new claims, running jobs finish; what is left comes back
through its lease (a parse is idempotent per file).

INLINE_WORKER=1 (the default with SQLite) runs one such worker inside the process that enqueues (the API in local
runs, the tests): `make api-local` needs nothing else.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import logging
import os
import shutil
import signal
import socket
import threading
import time
import traceback
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

import requests

from . import agent, contract, jobs
from . import llm as llm_mod
from .pipeline import Pipeline
from .pipeline import Settings as PipelineSettings
from .pricing import PriceDefaults
from .settings import catalog_dir

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 15.0          # the lease is renewed this often
STATE_SECONDS = 1.0               # the job's state (stopping) is read this often
LARGE_QUEUES = ("large", "maintenance")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


# ---------------------------------------------------------------- the pipeline of a job
@lru_cache(maxsize=32)
def pipeline_for(store, use_llm: bool, backend: str | None = None, fallback: str | None = None) -> Pipeline:
    """One pipeline per choice of models (kept and reused): backend sets the spec route's model and effort, fallback
    the agent's ("model:effort", see llm.llm_choices); None or "claude" keep the .env settings."""
    pipe = Pipeline(PipelineSettings(store_dir=store.root, catalog_dir=catalog_dir(), use_llm=use_llm), store=store)
    notes = []
    if use_llm and (c := llm_mod.parse_choice(backend)):
        pipe.llm = llm_mod.AnthropicLLM(model=c[0], effort=c[1])
        notes.append(f"spec {backend}")
    if (c := llm_mod.parse_choice(fallback)):
        base = agent.Limits.from_env()
        pipe.agent_limits = replace(base, model=c[0], effort=c[1] or base.effort)
        notes.append(f"agent {fallback}")
    pipe.choice_note = ", ".join(notes) or None
    return pipe


def task_summary(store, sha: str, r: dict, started: float | None, finished: float | None) -> dict:
    """What the web app shows of a finished parse (ParseTask's fields): status, lane, completeness, counts, times."""
    c = r.get("contract") or {}
    row = store.document(sha)
    learned = store.agent_job_for(sha) if not r.get("cached") else None
    ran = learned if learned and started and (learned.get("started") or 0) >= started - 1 else None
    return {"status": contract.status_of(row.status if row else r.get("status")), "lane": c.get("lane"),
            "completeness": c.get("completeness"), "confidence": c.get("confidence"), "charges": c.get("charges"),
            "gaps": c.get("gaps"), "counted_total": c.get("counted_total") if c.get("charges") else None,
            "elapsed_ms": int((r.get("seconds") or 0) * 1000), "model_calls": int(r.get("llm_calls") or 0),
            "cached": bool(r.get("cached")), "claude_cost_usd": r.get("claude_cost_usd"),
            "total_ms": int(((finished or time.time()) - (started or time.time())) * 1000) if not r.get("cached")
            else None,
            "cost_usd": round(ran["cost_usd"], 6) if ran else None}


# ---------------------------------------------------------------- one running job
class Running:
    """A claimed job while it runs: a heartbeat thread renews the lease and caches the job's state, and check() (the
    parse's on_step) raises JobStopped or JobLost from that cache, without a query per call."""

    def __init__(self, store, job: dict, worker: str):
        self.store, self.job, self.worker = store, job, worker
        self.state, self.lost = "running", False
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._beat, name=f"heartbeat-{job['id']}", daemon=True)
        self._thread.start()

    def _beat(self) -> None:
        renewed = time.monotonic()
        while not self._done.wait(STATE_SECONDS):
            renew = time.monotonic() - renewed >= HEARTBEAT_SECONDS
            try:
                state = jobs.heartbeat(self.store, self.job["id"], self.worker, renew=renew)
            except Exception:                               # noqa: BLE001  a database hiccup: try again next second
                continue
            if renew:
                renewed = time.monotonic()
            if state is None:
                self.lost = True
                return
            self.state = state
            if self.job["kind"] == "learn" and (state == "stopping" or self.overdue):
                self._stop_agent()

    @property
    def overdue(self) -> bool:
        return bool(self.job.get("deadline")) and time.time() > self.job["deadline"]

    def _stop_agent(self) -> None:
        """A learn job is stopped through its agent job (agent.run_job ends at its next step)."""
        aj = (self.job.get("options") or {}).get("agent_job_id")
        if aj and (j := self.store.agent_job(aj)) and j["status"] == "running":
            self.store.update_agent_job(aj, status="stopping")

    def check(self, step: str | None = None) -> None:
        if self.lost:
            raise jobs.JobLost(f"job {self.job['id']}")
        if self.state == "stopping":
            raise jobs.JobStopped("Stopped by a person")
        if self.overdue:
            raise jobs.JobStopped(f"Over JOB_DEADLINE_{self.job['queue'].upper()} "
                                  f"({jobs.deadline_seconds(self.job['queue']):.0f} s)", state="error")
        if step and step.startswith("reading"):
            jobs.set_progress(self.store, self.job["id"], self.worker, step[0].upper() + step[1:])

    def close(self) -> None:
        self._done.set()


# ---------------------------------------------------------------- runners
def _defaults(options: dict) -> PriceDefaults | None:
    d = options.get("price_defaults") or {}
    out = PriceDefaults(**{k: v for k, v in d.items() if v})
    return out if out.given() else None


def run_parse(store, job: dict, run: Running) -> dict:
    o = job.get("options") or {}
    temp = Path(o["temp_path"]) if o.get("temp_path") else None
    path = temp if temp is not None and temp.exists() else store.file_path(job["sha256"])
    if path is None:
        raise FileNotFoundError("The uploaded file is not stored")
    from . import bigfile
    info = bigfile.classify(path, job["name"])
    pipe = pipeline_for(store, bool(o.get("use_llm")), o.get("backend") or None, o.get("fallback") or None)
    jobs.set_progress(store, job["id"], run.worker, "reading" if info.large else "parsing")
    run.check()
    r = pipe.parse_path(path, job["name"], force=bool(o.get("force")), agent=o.get("agent_step", True),
                        user=job.get("created_by"), on_step=run.check, move=path == temp, sha=job["sha256"],
                        info=info, price_defaults=_defaults(o))
    row = store.document(job["sha256"])
    if row is not None and job.get("created_by") and not row.uploaded_by:
        store.update_document(job["sha256"], uploaded_by=job["created_by"])
    return task_summary(store, job["sha256"], r, job.get("started"), time.time())


def run_reprice(store, job: dict, run: Running) -> dict:
    jobs.set_progress(store, job["id"], run.worker, "pricing")
    run.check()
    r = pipeline_for(store, False).reprice(job["sha256"], _defaults(job.get("options") or {}))
    return task_summary(store, job["sha256"], r, job.get("started"), time.time())


def run_learn(store, job: dict, run: Running) -> dict:
    """agent.run_job for the job's agent job. A run a dead worker left is queued again first. When learning proves a
    spec, run_job has stored the re-parse: the parse job's webhook is sent again (result.updated)."""
    aj = (job.get("options") or {})["agent_job_id"]
    j = store.agent_job(aj)
    if j is None:
        raise KeyError(f"agent job {aj}")
    if j["status"] in ("running", "stopping"):
        store.update_agent_job(aj, status="queued", note="Run again: the worker that ran it stopped")
    elif j["status"] != "queued":
        return {"agent_job_id": aj, "agent_status": j["status"], "skipped": True}
    jobs.set_progress(store, job["id"], run.worker, "learning a layout spec")
    pipe = pipeline_for(store, True, None, (job.get("options") or {}).get("fallback") or None)
    done = agent.run_job(pipe, aj, llm=pipe.agent_llm, limits=pipe.agent_limits)
    if done["status"] == "proved" and job.get("parent_id"):
        jobs.fire(store, job["parent_id"], "result.updated")
    return {"agent_job_id": aj, "agent_status": done["status"], "cost_usd": done.get("cost_usd"),
            "spec_id": done.get("spec_id")}


def run_nightly(store, job: dict, run: Running) -> dict:
    from .learn import run_nightly as nightly
    jobs.set_progress(store, job["id"], run.worker, "nightly loop")
    report = nightly(pipeline_for(store, True))
    report["temp_removed"] = clean_temp(store)
    return json.loads(json.dumps(report, default=str))


def clean_temp(store, older_than: float = 86400.0) -> int:
    """Temporary uploads older than a day that no job still waits on."""
    keep = {str((j.get("options") or {}).get("temp_path")) for j in jobs.list_jobs(store, kinds=("parse",), limit=500)
            if j["state"] in jobs.ACTIVE}
    n, now = 0, time.time()
    for p in (store.root / "tmp").glob("*"):
        if p.is_file() and str(p) not in keep and now - p.stat().st_mtime > older_than:
            p.unlink(missing_ok=True)
            n += 1
    return n


FETCHERS = {"aws": "fetch_aws_price_files", "azure": "fetch_azure_retail_prices", "gcp": "fetch_gcp_prices"}


def _fetcher(provider: str = "aws"):
    """scripts/<provider>/<fetcher>.py (FETCHERS), imported (never run as a process)."""
    name = FETCHERS[provider]
    path = Path(__file__).resolve().parents[1] / "scripts" / provider / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                          # type: ignore[union-attr]
    return mod


PRICE_PROVIDERS = ("aws", "azure", "gcp")


def price_providers() -> list[str]:
    """PRICE_PROVIDERS (comma separated, default aws): the providers the weekly price refresh downloads and loads,
    in this order."""
    got = [p.strip().lower() for p in (os.environ.get("PRICE_PROVIDERS") or "aws").split(",") if p.strip()]
    bad = [p for p in got if p not in PRICE_PROVIDERS]
    if bad:
        raise ValueError(f"PRICE_PROVIDERS: unknown provider(s) {', '.join(bad)} (known: {', '.join(PRICE_PROVIDERS)})")
    return list(dict.fromkeys(got)) or ["aws"]


def price_files_dir() -> Path:
    return Path(os.environ.get("PRICE_FILES_DIR") or "~/aws-price-files").expanduser()


def azure_price_files_dir() -> Path:
    return Path(os.environ.get("AZURE_PRICE_FILES_DIR") or "~/azure-price-files").expanduser()


def gcp_price_files_dir() -> Path:
    return Path(os.environ.get("GCP_PRICE_FILES_DIR") or "~/gcp-price-files").expanduser()


def download_prices(dest: Path) -> int:
    """The fetcher's download of every AWS price list and the Savings Plans into dest (its exit code)."""
    return _fetcher().main(["--out", str(dest), "--all-services", "--savings-plans", "--progress-every", "0"])


def download_azure_prices(dest: Path) -> int:
    """The Azure fetcher's download into dest (its exit code): AZURE_PRICE_SERVICES (comma separated; empty: the
    fetcher's default services; "all": every service) in AZURE_PRICE_REGIONS (comma separated; empty: every region)."""
    args = ["--out", str(dest)]
    services = [s.strip() for s in (os.environ.get("AZURE_PRICE_SERVICES") or "").split(",") if s.strip()]
    if [s.lower() for s in services] == ["all"]:
        args.append("--all-services")
    else:
        for s in services:
            args += ["--service", s]
    for r in (os.environ.get("AZURE_PRICE_REGIONS") or "").split(","):
        if r.strip():
            args += ["--region", r.strip()]
    return _fetcher("azure").main(args)


def download_gcp_prices(dest: Path) -> int:
    """The GCP fetcher's download of every Google Cloud service's SKUs into dest (its exit code; it reads
    GCP_BILLING_API_KEY itself)."""
    return _fetcher("gcp").main(["--out", str(dest)])


def run_prices(store, job: dict, run: Running) -> dict:
    """For each provider of PRICE_PROVIDERS (default aws; aws,azure,gcp: AWS, then Azure, then GCP): 1. download into
    <its folder>.incoming-<date> (AWS: PRICE_FILES_DIR, Azure: AZURE_PRICE_FILES_DIR, GCP: GCP_PRICE_FILES_DIR); only
    a complete download replaces the previous files (a failed one leaves them, and the current snapshot, as they
    were). 2. Load it (prices.load.load_snapshot): a load that loses more than 10% of a price list's rates is refused,
    the job ends `error` and the current snapshot stays (a provider loaded before stays loaded). GCP needs
    GCP_BILLING_API_KEY: without it GCP is skipped (said in its result). Stored documents are not repriced (reprice
    does that). The result is the first provider's, with every provider's under `providers` when there are several."""
    providers = price_providers()
    results = {p: _refresh_prices(store, job, run, p) for p in providers}
    first = results[providers[0]]
    return {**first, "providers": results} if len(results) > 1 else first


def _refresh_prices(store, job: dict, run: Running, provider: str) -> dict:
    from prices.load import Refused, load_snapshot
    if provider == "gcp" and not (os.environ.get("GCP_BILLING_API_KEY") or "").strip():
        return {"skipped": "GCP_BILLING_API_KEY is not set"}
    target, download, what = {
        "aws": (price_files_dir(), download_prices, "the AWS price files"),
        "azure": (azure_price_files_dir(), download_azure_prices, "the Azure retail prices"),
        "gcp": (gcp_price_files_dir(), download_gcp_prices, "the GCP prices"),
    }[provider]
    run.check()
    incoming = target.parent / f"{target.name}.incoming-{dt.date.today().isoformat()}"
    jobs.set_progress(store, job["id"], run.worker, f"downloading {what}")
    try:
        code = download(incoming)
        if code != 0 or not (incoming / "manifest.json").exists():
            raise RuntimeError(f"The download failed (exit code {code}); the previous files stay")
    except BaseException:
        shutil.rmtree(incoming, ignore_errors=True)
        raise
    old = target.parent / f"{target.name}.previous"
    shutil.rmtree(old, ignore_errors=True)
    if target.exists():
        target.rename(old)
    incoming.rename(target)
    shutil.rmtree(old, ignore_errors=True)
    run.check()
    jobs.set_progress(store, job["id"], run.worker, "loading the price catalog" if provider == "aws"
                      else f"loading the price catalog ({provider})")
    try:
        res = load_snapshot(store.engine, target)
    except Refused as e:
        raise RuntimeError(f"Load refused, the current snapshot stays: {e}") from e
    return {"snapshot_id": res.snapshot_id, "status": res.status, "price_lists": sorted(res.counts),
            "products": res.products, "rates": res.rates, "sp_rates": res.sp_rates, "rates_closed": res.closed,
            "rates_inserted": res.inserted, "seconds": res.seconds}


RUNNERS = {"parse": run_parse, "reprice": run_reprice, "learn": run_learn, "nightly": run_nightly,
           "prices": run_prices}


def run_job(store, job: dict, worker: str) -> str | None:
    """Run one claimed job and finish it; the final state, or None when the job was lost to another worker."""
    run = Running(store, job, worker)
    token = jobs.current_job.set(job["id"])
    result, state, error = None, "done", None
    try:
        result = RUNNERS[job["kind"]](store, job, run)
    except jobs.JobLost:
        return None
    except jobs.JobStopped as e:
        state, error = e.state, str(e)
    except Exception as e:                                  # noqa: BLE001  a job's failure is its own, kept on the row
        state, error = "error", f"{type(e).__name__}: {e}"
        log.warning("job %s failed: %s", job["id"], traceback.format_exc(limit=5))
    finally:
        run.close()
        jobs.current_job.reset(token)
    if run.lost:
        return None
    if job["kind"] in jobs.FILE_KINDS and (temp := (job.get("options") or {}).get("temp_path")):
        Path(temp).unlink(missing_ok=True)                  # the upload: moved into the store, or not needed now
    return state if jobs.finish(store, job["id"], worker, state, result, error) else None


# ---------------------------------------------------------------- webhooks
def deliver_one(store) -> str | None:
    """Deliver one due webhook: POST, 10 s timeout, no redirects, signed. Its new callback state, or None."""
    job = jobs.claim_callback(store)
    if job is None:
        return None
    try:
        jobs.check_callback_url(job["callback_url"])
    except ValueError:
        return jobs.callback_result(store, job["id"], False)
    body = json.dumps({"event": job.get("callback_event") or jobs.EVENTS.get(job["state"], "parse.done"),
                       "job_id": job["id"], "sha256": job["sha256"], "state": job["state"],
                       "summary": job.get("result"), "result_url": f"/documents/{job['sha256']}",
                       "sent_at": time.time()}, default=str).encode()
    headers = {"Content-Type": "application/json", "X-Billparse-Signature": jobs.sign(body),
               "X-Billparse-Event": job.get("callback_event") or "", "X-Billparse-Job": str(job["id"])}
    try:
        r = requests.post(job["callback_url"], data=body, headers=headers, timeout=10, allow_redirects=False)
        ok = 200 <= r.status_code < 300
    except requests.RequestException:
        ok = False
    return jobs.callback_result(store, job["id"], ok)


# ---------------------------------------------------------------- schedules
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _setting(name: str, default: str) -> str | None:
    v = os.environ.get(name, default)
    return v.strip().lower() or None


def schedules(now: dt.datetime | None = None) -> list[dict]:
    """The schedules and when each is next due (UTC): the weekly price refresh and the nightly loop. An empty setting
    turns one off."""
    now = now or dt.datetime.now(dt.UTC)
    out = []
    day, hour = _setting("PRICES_REFRESH_DAY", "sun"), _setting("PRICES_REFRESH_HOUR_UTC", "10")
    if day in DAYS and hour is not None:
        wd, h = DAYS.index(day), int(hour)
        due = now.weekday() == wd and now.hour >= h
        days = (wd - now.weekday()) % 7 or (7 if now.hour >= h else 0)
        nxt = (now + dt.timedelta(days=days)).replace(hour=h, minute=0, second=0, microsecond=0)
        year, week, _ = now.isocalendar()
        out.append({"kind": "prices", "queue": "maintenance", "due": due, "next_run": nxt.isoformat(),
                    "dedupe_key": f"prices:{year}-W{week:02d}", "every": f"weekly, {day} {h:02d}:00 UTC"})
    hour = _setting("NIGHTLY_HOUR_UTC", "11")
    if hour is not None:
        h = int(hour)
        nxt = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if now.hour >= h:
            nxt += dt.timedelta(days=1)
        out.append({"kind": "nightly", "queue": "maintenance", "due": now.hour >= h, "next_run": nxt.isoformat(),
                    "dedupe_key": f"nightly:{now.date().isoformat()}", "every": f"daily, {h:02d}:00 UTC"})
    return out


def enqueue_due(store, now: dt.datetime | None = None) -> list[int]:
    """Enqueue what is due; dedupe_key makes a second process (or a second minute) a no-op."""
    made = []
    for s in schedules(now):
        if s["due"]:
            job, created = jobs.enqueue(store, s["kind"], s["queue"], name=f"{s['kind']} ({s['every']})",
                                        dedupe_key=s["dedupe_key"], max_attempts=1)
            if created:
                made.append(job["id"])
    return made


# ---------------------------------------------------------------- the worker
def parse_queues(text: str, concurrency: int = 1) -> dict[str, int]:
    """"small:2,learn:1" -> {"small": 2, "learn": 1}; a queue without a count gets `concurrency` threads."""
    out = {}
    for part in (p.strip() for p in text.split(",") if p.strip()):
        name, _, n = part.partition(":")
        if name not in jobs.QUEUES:
            raise ValueError(f"Unknown queue {name!r}; known: {', '.join(jobs.QUEUES)}")
        out[name] = max(1, int(n or concurrency))
    return out


class Worker:
    def __init__(self, store, queues: dict[str, int], schedules_on: bool = True, name: str | None = None):
        self.store, self.queues, self.schedules_on = store, queues, schedules_on
        self.name = name or f"{socket.gethostname()}:{os.getpid()}"
        self.stopping = threading.Event()
        self.threads: list[threading.Thread] = []
        self.wake = jobs.wakeup(store.url)
        self.poll = _env_float("WORKER_POLL_SECONDS", 1.0)

    def start(self) -> Worker:
        for queue, n in self.queues.items():
            for i in range(n):
                self._spawn(self._jobs_loop, f"job-{queue}-{i + 1}", queue)
        self._spawn(self._callbacks_loop, "callbacks")
        if self.schedules_on:
            self._spawn(self._schedules_loop, "schedules")
        if self.store.engine.dialect.name == "postgresql":
            self._spawn(self._listen, "listen")
        return self

    def _spawn(self, fn, name: str, *args) -> None:
        t = threading.Thread(target=fn, args=args, name=f"{name}", daemon=True)
        t.start()
        self.threads.append(t)

    def _jobs_loop(self, queue: str) -> None:
        me = f"{self.name}:{threading.current_thread().name}"
        while not self.stopping.is_set():
            try:
                job = jobs.claim(self.store, [queue], me)
            except Exception:                               # noqa: BLE001  the database went away: try again
                log.warning("claim failed: %s", traceback.format_exc(limit=3))
                job = None
            if job is None:
                self.wake.wait(self.poll)
                self.wake.clear()
                continue
            run_job(self.store, job, me)

    def _callbacks_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                if deliver_one(self.store) is not None:
                    continue
            except Exception:                               # noqa: BLE001
                log.warning("webhook delivery failed: %s", traceback.format_exc(limit=3))
            self.stopping.wait(self.poll)

    def _schedules_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                enqueue_due(self.store)
            except Exception:                               # noqa: BLE001
                log.warning("schedules failed: %s", traceback.format_exc(limit=3))
            self.stopping.wait(60)

    def _listen(self) -> None:
        """PostgreSQL LISTEN billparse_jobs: a new job wakes the threads at once (polling stays the fallback)."""
        import psycopg
        url = self.store.url.replace("postgresql+psycopg://", "postgresql://")
        while not self.stopping.is_set():
            try:
                with psycopg.connect(url, autocommit=True) as conn:
                    conn.execute(f"LISTEN {jobs.NOTIFY_CHANNEL}")
                    while not self.stopping.is_set():
                        for _ in conn.notifies(timeout=5.0, stop_after=1):
                            self.wake.set()
            except Exception:                               # noqa: BLE001  reconnect after a database restart
                self.stopping.wait(5)

    def stop(self, wait: float | None = None) -> None:
        """No new claims; running jobs finish (wait: seconds to wait for them)."""
        self.stopping.set()
        self.wake.set()
        if wait:
            end = time.monotonic() + wait
            for t in self.threads:
                t.join(max(0.0, end - time.monotonic()))


# ---------------------------------------------------------------- inline (one process)
_inline: dict[str, Worker] = {}
_inline_lock = threading.Lock()


def inline_enabled(url: str) -> bool:
    v = os.environ.get("INLINE_WORKER")
    return v == "1" if v not in (None, "") else url.startswith("sqlite")


def ensure_inline(store) -> Worker | None:
    """With INLINE_WORKER, a worker for every queue in this process (the API in local runs, the tests). Schedules
    only with INLINE_SCHEDULES=1."""
    if not inline_enabled(store.url):
        return None
    with _inline_lock:
        w = _inline.get(store.url)
        if w is None or w.stopping.is_set():
            queues = {"small": int(os.environ.get("PARSE_WORKERS") or 2), "learn": 1,
                      "large": int(os.environ.get("LARGE_PARSE_WORKERS") or 1), "maintenance": 1}
            w = Worker(store, queues, schedules_on=os.environ.get("INLINE_SCHEDULES") == "1").start()
            _inline[store.url] = w
        return w


def stop_inline(wait: float = 5.0) -> None:
    """Stop every inline worker of this process (the tests, between tests)."""
    with _inline_lock:
        workers = list(_inline.values())
        _inline.clear()
    for w in workers:
        w.stop(wait)


# ---------------------------------------------------------------- the command
def main(argv: list[str] | None = None) -> None:
    from .settings import configure_logging, data_dir, load_env
    from .store import Store
    load_env()
    configure_logging()
    ap = argparse.ArgumentParser(prog="billparse worker", description="Run the jobs of the job table.")
    ap.add_argument("--queues", default="small:2,learn:1",
                    help="queue:threads, comma separated (small, large, learn, maintenance)")
    ap.add_argument("--concurrency", type=int, default=1, help="threads of a queue given without a count")
    ap.add_argument("--no-schedules", action="store_true", help="do not enqueue the scheduled jobs from this process")
    a = ap.parse_args(argv)
    queues = parse_queues(a.queues, a.concurrency)
    # PostgreSQL connections: one per job thread, the heartbeats', the callbacks, the schedules (README: the total)
    os.environ.setdefault("DB_POOL_SIZE", str(sum(queues.values()) + 2))
    os.environ.setdefault("DB_MAX_OVERFLOW", "2")
    store = Store(data_dir())
    w = Worker(store, queues, schedules_on=not a.no_schedules).start()
    log.info("worker %s: %s", w.name, a.queues)

    def stop(signum, frame) -> None:                        # SIGTERM: finish what runs, claim nothing new
        log.info("worker %s stopping (signal %s)", w.name, signum)
        w.stopping.set()
        w.wake.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not w.stopping.is_set():
        w.stopping.wait(1)
    for t in w.threads:
        t.join()
