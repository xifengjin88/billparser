"""The job queue (jobs.py) and its workers (worker.py), on SQLite with fakes: restarts, duplicates, leases, stop,
deadlines, learning in its own job, queues that do not wait on each other, POST /parse, webhooks and schedules."""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

import billparse.analyze as analyze_mod
import billparse.api as api
from billparse import contract, jobs, webapi, worker
from billparse.pipeline import Pipeline, Settings
from billparse.store import Store

ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / "samples" / "files"
PRICES = ROOT / "tests" / "fixtures" / "prices" / "download"
sys.path.insert(0, str(ROOT / "tests"))
from test_agent import BILL, GOOD, FakeLLM, tool_use  # noqa: E402

T = jobs.T


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A store whose jobs nobody runs until the test runs them (INLINE_WORKER=0)."""
    raw = tmp_path / "data" / "raw"
    raw.mkdir(parents=True)
    for f in (ROOT / "tests" / "fixtures" / "raw").glob("*.json"):
        shutil.copy(f, raw / f.name)
    monkeypatch.setenv("CATALOG_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PARSER_DATA_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("BILLPARSE_USE_LLM", "0")
    monkeypatch.setenv("INLINE_WORKER", "0")
    analyze_mod._RESOLVER = None
    webapi._pipeline.cache_clear()
    yield Store(tmp_path / "store")
    webapi._pipeline.cache_clear()
    analyze_mod._RESOLVER = None


@pytest.fixture
def client(store):
    api.app.dependency_overrides[webapi.get_store] = lambda: store
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def _set(store: Store, job_id: int, **values) -> None:
    with store.engine.begin() as c:
        c.execute(T.update().where(T.c.id == job_id).values(**values))


def _drain(store: Store, queues=("small", "large", "learn", "maintenance")) -> list[str | None]:
    """Run every queued job of these queues in this thread, oldest first."""
    out = []
    while (job := jobs.claim(store, queues, "test")) is not None:
        out.append(worker.run_job(store, job, "test"))
    return out


def _until(check, seconds: float = 20.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


def _upload(client: TestClient, name: str, path: str = "/parse/start", **params):
    with open(FILES / name, "rb") as fh:
        return client.post(path, params=params, files={"file": (name, fh)})


# ---------------------------------------------------------------- restart, duplicates
def test_a_queued_parse_survives_a_restart(client, store, tmp_path):
    task = _upload(client, "summary_invoice.pdf").json()
    assert task["state"] == "running" and task["progress"] == "queued" and task["job_id"]
    again = Store(tmp_path / "store")                       # a new app process: a new store on the same database
    api.app.dependency_overrides[webapi.get_store] = lambda: again
    fresh = TestClient(api.app)
    assert fresh.get(f"/parse/tasks/{task['sha256']}").json()["job_id"] == task["job_id"]
    assert fresh.get(f"/parse/jobs/{task['job_id']}").json()["state"] == "queued"
    _drain(again)
    done = fresh.get(f"/parse/tasks/{task['sha256']}").json()
    assert done["state"] == "done" and done["status"] == "auto_accepted"
    assert fresh.get(f"/parse/jobs/{task['job_id']}").json()["result_url"] == f"/documents/{task['sha256']}"


def test_two_uploads_of_a_queued_file_get_the_same_job(client, store):
    first = _upload(client, "azure_actual.csv", "/parse/jobs")
    second = _upload(client, "azure_actual.csv", "/parse/jobs")
    assert (first.status_code, second.status_code) == (202, 200)
    assert first.json()["job_id"] == second.json()["job_id"]
    assert len(jobs.list_jobs(store)) == 1 and not list((store.root / "tmp").glob("*"))[1:]   # one upload kept
    with pytest.raises(sa.exc.IntegrityError), store.engine.begin() as c:          # the partial unique index
        c.execute(T.insert().values(kind="reprice", queue="small", state="queued", sha256=first.json()["sha256"],
                                    name="x", created=time.time(), attempts=0, max_attempts=2, callback_attempts=0))


def test_an_api_start_never_ends_a_learning_run(store):
    aj = store.enqueue_agent_job("layout:x", "a" * 64, "bill.txt", kind="new")
    store.update_agent_job(aj, status="running")
    webapi.get_store.cache_clear()
    try:
        webapi.get_store()
    finally:
        webapi.get_store.cache_clear()
    assert store.agent_job(aj)["status"] == "running"


# ---------------------------------------------------------------- leases, stop, deadline
def test_an_expired_lease_is_claimed_again_then_ends(store):
    job, _ = jobs.enqueue(store, "parse", "small", name="x", sha256="a" * 64)
    first = jobs.claim(store, ["small"], "w1")
    assert first["attempts"] == 1
    assert jobs.claim(store, ["small"], "w2") is None                       # leased
    _set(store, job["id"], lease_until=time.time() - 1)                     # w1 died
    second = jobs.claim(store, ["small"], "w2")
    assert (second["id"], second["attempts"], second["worker"]) == (job["id"], 2, "w2")
    assert jobs.heartbeat(store, job["id"], "w1") is None                   # w1 lost it
    assert not jobs.finish(store, job["id"], "w1", "done", {"x": 1})        # and its result is dropped
    _set(store, job["id"], lease_until=time.time() - 1)                     # w2 died too: no attempt left
    assert jobs.claim(store, ["small"], "w3") is None
    end = jobs.get(store, job["id"])
    assert end["state"] == "error" and "no attempts left" in end["error"] and end["result"] is None


def test_a_worker_whose_job_was_taken_writes_nothing(store, monkeypatch):
    monkeypatch.setattr(worker, "STATE_SECONDS", 0.05)

    def taken(store_, job, run):
        _set(store, job["id"], worker="another")
        assert _until(lambda: run.lost, 5)
        run.check()
    monkeypatch.setitem(worker.RUNNERS, "parse", taken)
    job, _ = jobs.enqueue(store, "parse", "small", name="x", sha256="a" * 64)
    assert _drain(store) == [None]
    assert jobs.get(store, job["id"])["state"] == "running" and jobs.get(store, job["id"])["worker"] == "another"


def _checking(store_, job, run):
    for _ in range(200):
        run.check()
        time.sleep(0.02)
    return {}


def test_a_stop_request_ends_the_job_at_its_next_step(store, monkeypatch):
    monkeypatch.setattr(worker, "STATE_SECONDS", 0.05)
    monkeypatch.setitem(worker.RUNNERS, "parse", _checking)
    queued, _ = jobs.enqueue(store, "parse", "small", name="q", sha256="b" * 64)
    assert jobs.request_stop(store, queued["id"])["state"] == "stopped"     # never ran
    job, _ = jobs.enqueue(store, "parse", "small", name="x", sha256="a" * 64)
    claimed = jobs.claim(store, ["small"], "w")
    jobs.request_stop(store, job["id"])
    t0 = time.monotonic()
    assert worker.run_job(store, claimed, "w") == "stopped" and time.monotonic() - t0 < 2
    assert jobs.get(store, job["id"])["error"] == "Stopped by a person"


def test_a_job_past_its_deadline_ends_error(store, monkeypatch):
    monkeypatch.setitem(worker.RUNNERS, "parse", _checking)
    jobs.enqueue(store, "parse", "small", name="x", sha256="a" * 64)
    job = jobs.claim(store, ["small"], "w")
    job["deadline"] = time.time() - 1
    assert worker.run_job(store, job, "w") == "error"
    assert "Over JOB_DEADLINE_SMALL" in jobs.get(store, job["id"])["error"]


def test_a_large_file_stops_between_two_chunks(store, tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    import bench_large
    f = tmp_path / "big.csv.gz"
    bench_large.generate(f, 30_000)
    monkeypatch.setenv("LARGE_FILE_MB", "0.000001")
    monkeypatch.setenv("CHUNK_ROWS", "5000")
    pipe = Pipeline(Settings(store_dir=store.root, catalog_dir=tmp_path / "data", use_llm=False, use_agent=False),
                    store=store)
    jobs.enqueue(store, "parse", "large", name="big.csv.gz", sha256="a" * 64)
    run = worker.Running(store, jobs.claim(store, ["large"], "w"), "w")
    chunks = []

    def step(s: str | None = None) -> None:
        if s and s.startswith("reading:"):
            chunks.append(s)
            jobs.request_stop(store, run.job["id"])     # a person stops it after the first chunk
            run.state = "stopping"                      # what the heartbeat reads within a second
        run.check(s)
    try:
        with pytest.raises(jobs.JobStopped):
            pipe.parse_path(f, "big.csv.gz", on_step=step)
    finally:
        run.close()
    assert len(chunks) == 1                             # 6 chunks in the file; it stopped after the first


# ---------------------------------------------------------------- learning in its own job
def test_learning_runs_after_the_parse_and_updates_the_result(store, tmp_path, monkeypatch):
    monkeypatch.setenv("CALLBACK_ALLOWLIST", "https://hooks.test/")
    llm = FakeLLM([tool_use(1, "check_spec", {"spec": GOOD}), tool_use(2, "submit_spec", {"spec": GOOD, "note": ""})])
    pipe = Pipeline(Settings(store_dir=store.root, catalog_dir=tmp_path / "data", use_llm=False, use_agent=True),
                    store=store, agent_llm=llm)
    monkeypatch.setattr(worker, "pipeline_for", lambda *a, **k: pipe)
    f = store.temp_path(".txt")
    f.write_text(BILL)
    sha = hashlib.sha256(BILL.encode()).hexdigest()
    parse, _ = webapi.start_parse(store, "bill.txt", f, sha, False, False, temp=True,
                                  callback_url="https://hooks.test/done")
    assert _drain(store, ["small"]) == ["done"]
    first = store.get_result(sha)
    assert first["completeness"] != "reconciled" and not llm.sent           # the first pass, before any learning
    learn = jobs.list_jobs(store, kinds=("learn",))[0]
    assert (learn["state"], learn["parent_id"], learn["queue"]) == ("queued", parse["id"], "learn")
    assert jobs.get(store, parse["id"])["callback_event"] == "parse.done"
    jobs.callback_result(store, parse["id"], True)
    assert _drain(store, ["learn"]) == ["done"]
    assert store.get_result(sha)["completeness"] == "reconciled"           # the learned spec's re-parse
    done = jobs.get(store, parse["id"])
    assert (done["callback_state"], done["callback_event"]) == ("pending", "result.updated")
    assert store.agent_job(learn["options"]["agent_job_id"])["status"] == "proved"
    task = webapi._task_out(store, done)
    assert task.learning and task.learning.state == "done" and task.learning.agent_status == "proved"


def test_a_lost_learning_run_ends_its_agent_job(store):
    aj = store.enqueue_agent_job("layout:x", "a" * 64, "bill.txt", kind="new")
    job, _ = jobs.enqueue(store, "learn", "learn", name="x", sha256="a" * 64, options={"agent_job_id": aj})
    jobs.claim(store, ["learn"], "w")
    store.update_agent_job(aj, status="running")
    _set(store, job["id"], lease_until=time.time() - 1)                    # max_attempts 1: a retry costs money
    assert jobs.claim(store, ["learn"], "w2") is None
    assert jobs.get(store, job["id"])["state"] == "error" and store.agent_job(aj)["status"] == "error"


# ---------------------------------------------------------------- queues
def test_small_files_never_wait_for_large_files_or_learning(store, monkeypatch):
    gate = threading.Event()

    def runner(store_, job, run):
        if job["queue"] != "small":
            gate.wait(20)
        return {}
    for kind in ("parse", "learn"):
        monkeypatch.setitem(worker.RUNNERS, kind, runner)
    w = worker.Worker(store, {"small": 1, "large": 1, "learn": 1}, schedules_on=False).start()
    try:
        big, _ = jobs.enqueue(store, "parse", "large", name="big", sha256="a" * 64)
        learn, _ = jobs.enqueue(store, "learn", "learn", name="learn", sha256="b" * 64)
        assert _until(lambda: jobs.get(store, big["id"])["state"] == "running"
                      and jobs.get(store, learn["id"])["state"] == "running")
        small, _ = jobs.enqueue(store, "parse", "small", name="small", sha256="c" * 64)
        assert _until(lambda: jobs.get(store, small["id"])["state"] == "done", 5)
        assert jobs.get(store, big["id"])["state"] == "running" == jobs.get(store, learn["id"])["state"]
    finally:
        gate.set()
        w.stop(10)


# ---------------------------------------------------------------- POST /parse
@pytest.mark.parametrize("name", ["summary_invoice.pdf", "azure_actual.csv"])
def test_post_parse_answers_as_before(client, store, tmp_path, monkeypatch, name):
    monkeypatch.setenv("INLINE_WORKER", "1")
    body = _upload(client, name, "/parse").json()
    direct = Pipeline(Settings(store_dir=tmp_path / "direct", catalog_dir=tmp_path / "data", use_llm=False,
                               use_agent=False)).parse_file(FILES / name)
    c = direct["contract"]
    assert (body["status"], body["completeness"], body["lane"], body["confidence"], body["cached"]) == \
        (contract.status_of(direct["status"]), c["completeness"], c["lane"], c["confidence"], False)
    assert len(body["document"]["charges"]) == c["charges"] and set(body["cost_usd"]) == {"claude", "sandbox", "total"}


def test_post_parse_answers_202_when_the_parse_is_slow(client, monkeypatch):
    monkeypatch.setenv("INLINE_WORKER", "1")
    monkeypatch.setenv("PARSE_WAIT_SECONDS", "1")
    gate = threading.Event()
    monkeypatch.setitem(worker.RUNNERS, "parse", lambda s, j, r: gate.wait(20) and {})
    try:
        r = _upload(client, "azure_actual.csv", "/parse")
        assert r.status_code == 202 and r.json()["state"] == "running"
        assert r.json()["status_url"] == f"/parse/jobs/{r.json()['job_id']}"
    finally:
        gate.set()


def test_waiting_post_parse_calls_never_block_the_api(store, monkeypatch):
    """60 callers wait on POST /parse with no worker running: the waits hold no request thread (the server has 40),
    so /health answers. One event loop for every request, as in uvicorn (`with TestClient`)."""
    monkeypatch.setenv("PARSE_WAIT_SECONDS", "6")
    api.app.dependency_overrides[webapi.get_store] = lambda: store
    answers: list[int] = []

    def call(i: int) -> None:
        r = client.post("/parse", files={"file": (f"bill-{i}.txt", f"{BILL}\nref {i}\n".encode())})
        answers.append(r.status_code)
    try:
        with TestClient(api.app) as client:
            callers = [threading.Thread(target=call, args=(i,)) for i in range(60)]
            for t in callers:
                t.start()
            time.sleep(2)                               # every caller is in its wait
            t0 = time.monotonic()
            assert client.get("/health").status_code == 200
            assert time.monotonic() - t0 < 1
            for t in callers:
                t.join(30)
    finally:
        api.app.dependency_overrides.clear()
    assert answers == [202] * 60                        # still queued: each one gets its job to poll


# ---------------------------------------------------------------- webhooks
def test_a_callback_url_off_the_allowlist_is_refused(client, monkeypatch):
    assert _upload(client, "azure_actual.csv", "/parse/jobs", callback_url="https://x.test/").status_code == 400
    monkeypatch.setenv("CALLBACK_ALLOWLIST", "https://hooks.test/")
    for bad in ("https://evil.test/", "http://hooks.test/"):
        assert _upload(client, "azure_actual.csv", "/parse/jobs", callback_url=bad).status_code == 400


class _Post:
    def __init__(self, codes: list[int]):
        self.codes, self.sent = list(codes), []

    def __call__(self, url, data, headers, timeout, allow_redirects):
        assert allow_redirects is False and timeout == 10
        self.sent.append((url, data, headers))
        return type("R", (), {"status_code": self.codes.pop(0) if self.codes else 200})()


def _finished_with_callback(store: Store) -> dict:
    job, _ = jobs.enqueue(store, "parse", "small", name="x", sha256="a" * 64, callback_url="https://hooks.test/a")
    jobs.claim(store, ["small"], "w")
    jobs.finish(store, job["id"], "w", "done", {"status": "auto_accepted"})
    return job


def test_a_webhook_is_signed_and_retried(store, monkeypatch):
    monkeypatch.setenv("CALLBACK_ALLOWLIST", "https://hooks.test/")
    monkeypatch.setenv("CALLBACK_SECRET", "s3cret")
    post = _Post([500, 200])
    monkeypatch.setattr(worker.requests, "post", post)
    job = _finished_with_callback(store)
    assert worker.deliver_one(store) == "pending"
    assert worker.deliver_one(store) is None                                # not due before its backoff
    _set(store, job["id"], callback_next=time.time())
    assert worker.deliver_one(store) == "sent" and jobs.get(store, job["id"])["callback_attempts"] == 2
    url, data, headers = post.sent[-1]
    body = json.loads(data)
    assert body["event"] == "parse.done" and body["summary"] == {"status": "auto_accepted"} and "rows" not in body
    assert headers["X-Billparse-Signature"] == "sha256=" + hmac.new(b"s3cret", data, hashlib.sha256).hexdigest()
    assert (headers["X-Billparse-Event"], headers["X-Billparse-Job"]) == ("parse.done", str(job["id"]))


def test_result_updated_never_replaces_an_undelivered_parse_done(store, monkeypatch):
    monkeypatch.setenv("CALLBACK_ALLOWLIST", "https://hooks.test/")
    post = _Post([500, 500, 200, 200])                  # the receiver is down for two tries
    monkeypatch.setattr(worker.requests, "post", post)
    job = _finished_with_callback(store)
    assert worker.deliver_one(store) == "pending"
    assert jobs.fire(store, job["id"], "result.updated")             # learning proved a spec meanwhile
    assert jobs.fire(store, job["id"], "result.updated")             # once is enough
    for _ in range(3):
        _set(store, job["id"], callback_next=time.time())
        worker.deliver_one(store)
    assert worker.deliver_one(store) is None
    assert [json.loads(d)["event"] for _, d, _ in post.sent] == ["parse.done"] * 3 + ["result.updated"]
    assert jobs.get(store, job["id"])["callback_state"] == "sent"


def test_an_event_fired_before_the_parse_ends_is_sent_after_parse_done(store, monkeypatch):
    monkeypatch.setenv("CALLBACK_ALLOWLIST", "https://hooks.test/")
    post = _Post([200, 200])
    monkeypatch.setattr(worker.requests, "post", post)
    job, _ = jobs.enqueue(store, "parse", "small", name="x", sha256="a" * 64, callback_url="https://hooks.test/a")
    jobs.claim(store, ["small"], "w")
    jobs.fire(store, job["id"], "result.updated")                    # a fast learning run, before the parse ends
    assert worker.deliver_one(store) is None                         # not before parse.done
    jobs.finish(store, job["id"], "w", "done", {})
    assert worker.deliver_one(store) == "sent" and worker.deliver_one(store) == "sent"
    assert [json.loads(d)["event"] for _, d, _ in post.sent] == ["parse.done", "result.updated"]


def test_a_webhook_that_keeps_failing_ends_failed(store, monkeypatch):
    monkeypatch.setenv("CALLBACK_ALLOWLIST", "https://hooks.test/")
    monkeypatch.setattr(worker.requests, "post", _Post([500] * 10))
    job = _finished_with_callback(store)
    states = []
    for _ in range(len(jobs.CALLBACK_BACKOFF) + 1):                         # the first try, then one per backoff
        states.append(worker.deliver_one(store))
        _set(store, job["id"], callback_next=time.time())
    assert states[-1] == "failed" and set(states[:-1]) == {"pending"}
    assert worker.deliver_one(store) is None


def test_a_hanging_receiver_does_not_delay_the_next_parse(store, monkeypatch):
    monkeypatch.setenv("CALLBACK_ALLOWLIST", "https://hooks.test/")
    gate, called = threading.Event(), threading.Event()

    def hang(*a, **k):
        called.set()
        gate.wait(20)
        return type("R", (), {"status_code": 200})()
    monkeypatch.setattr(worker.requests, "post", hang)
    monkeypatch.setitem(worker.RUNNERS, "parse", lambda s, j, r: {})
    w = worker.Worker(store, {"small": 1}, schedules_on=False).start()
    try:
        jobs.enqueue(store, "parse", "small", name="x", sha256="a" * 64, callback_url="https://hooks.test/a")
        assert called.wait(10)
        nxt, _ = jobs.enqueue(store, "parse", "small", name="y", sha256="b" * 64)
        assert _until(lambda: jobs.get(store, nxt["id"])["state"] == "done", 5)
    finally:
        gate.set()
        w.stop(10)


# ---------------------------------------------------------------- schedules
def test_two_workers_at_the_same_minute_make_one_price_job(store, monkeypatch):
    import datetime as dt
    monkeypatch.setenv("PRICES_REFRESH_DAY", "sun")
    monkeypatch.setenv("PRICES_REFRESH_HOUR_UTC", "10")
    monkeypatch.setenv("NIGHTLY_HOUR_UTC", "")                              # off
    sunday = dt.datetime(2026, 11, 1, 10, 30, tzinfo=dt.UTC)
    made = [worker.enqueue_due(store, sunday), worker.enqueue_due(store, sunday)]
    assert len(made[0]) == 1 and made[1] == []
    job = jobs.get(store, made[0][0])
    assert (job["kind"], job["queue"], job["dedupe_key"]) == ("prices", "maintenance", "prices:2026-W44")
    assert worker.enqueue_due(store, sunday.replace(hour=9)) == []          # not due before the hour
    assert [s["kind"] for s in worker.schedules(sunday)] == ["prices"]


def test_the_price_job_downloads_then_loads(store, tmp_path, monkeypatch):
    from prices.store import PriceCatalog
    target = tmp_path / "aws-price-files"
    monkeypatch.setenv("PRICE_FILES_DIR", str(target))
    monkeypatch.setattr(worker, "download_prices", lambda dest: shutil.copytree(PRICES, dest) and 0)
    job, _ = jobs.enqueue(store, "prices", "maintenance", name="prices", dedupe_key="prices:test")
    assert _drain(store) == ["done"]
    result = jobs.get(store, job["id"])["result"]
    assert result["status"] == "current" and result["rates"] > 0
    assert PriceCatalog(store.engine).version == result["snapshot_id"]
    assert (target / "manifest.json").exists() and not list(tmp_path.glob("aws-price-files.*"))


def test_a_refused_load_keeps_the_current_snapshot(store, tmp_path, monkeypatch):
    import prices.load
    from prices.store import PriceCatalog
    target = tmp_path / "aws-price-files"
    monkeypatch.setenv("PRICE_FILES_DIR", str(target))
    prices.load.load_snapshot(store.engine, PRICES)
    before = PriceCatalog(store.engine).version
    monkeypatch.setattr(worker, "download_prices", lambda dest: shutil.copytree(PRICES, dest) and 0)

    def refuse(*a, **k):
        raise prices.load.Refused("AmazonEC2 lost 40% of its rates")
    monkeypatch.setattr(prices.load, "load_snapshot", refuse)
    job, _ = jobs.enqueue(store, "prices", "maintenance", name="prices", dedupe_key="prices:test")
    assert _drain(store) == ["error"]
    assert "Load refused" in jobs.get(store, job["id"])["error"] and PriceCatalog(store.engine).version == before


def test_a_failed_download_leaves_the_previous_files(store, tmp_path, monkeypatch):
    target = tmp_path / "aws-price-files"
    shutil.copytree(PRICES, target)
    before = sorted(p.relative_to(target) for p in target.rglob("*"))

    def broken(dest: Path) -> int:
        (dest / "offers").mkdir(parents=True)
        (dest / "offers" / "half.csv.gz").write_bytes(b"x")
        return 1
    monkeypatch.setenv("PRICE_FILES_DIR", str(target))
    monkeypatch.setattr(worker, "download_prices", broken)
    job, _ = jobs.enqueue(store, "prices", "maintenance", name="prices", dedupe_key="prices:test")
    assert _drain(store) == ["error"] and "download failed" in jobs.get(store, job["id"])["error"]
    assert sorted(p.relative_to(target) for p in target.rglob("*")) == before
    assert not list(tmp_path.glob("aws-price-files.*"))


def test_the_schedules_endpoint_shows_each_schedule(client, store, monkeypatch):
    monkeypatch.setenv("NIGHTLY_HOUR_UTC", "11")
    out = {s["kind"]: s for s in client.get("/jobs/schedules").json()}
    assert set(out) == {"prices", "nightly"} and out["nightly"]["last_job"] is None and out["prices"]["next_run"]
    r = client.post("/nightly")
    assert r.status_code == 202 and r.json()["kind"] == "nightly"
    assert client.get("/jobs/schedules").json()[1]["last_job"]["state"] == "queued"


def test_heartbeat_failures_are_logged(store, monkeypatch, caplog):
    """A heartbeat that cannot reach the database is retried, and said in the log: a lease lost that way (the job then
    runs again on another worker) has a cause to find."""
    monkeypatch.setattr(worker, "STATE_SECONDS", 0.02)
    monkeypatch.setattr(worker, "HEARTBEAT_SECONDS", 0.0)
    real, calls = jobs.heartbeat, {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise TimeoutError("QueuePool limit reached")
        return real(*a, **k)
    monkeypatch.setattr(jobs, "heartbeat", flaky)
    jobs.enqueue(store, "parse", "small", name="x", sha256="a" * 64)
    job = jobs.claim(store, ["small"], "w")
    with caplog.at_level("WARNING", logger="billparse.worker"):
        run = worker.Running(store, job, "w")
        deadline = time.monotonic() + 5
        while calls["n"] < 4 and time.monotonic() < deadline:
            time.sleep(0.02)
        run.close()
    text = caplog.text
    assert "heartbeat failed 1 time(s) in a row" in text and "TimeoutError: QueuePool limit reached" in text
    assert "heartbeat works again after 2 failure(s)" in text and not run.lost
