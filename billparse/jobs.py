"""The durable job queue: one row per unit of background work in the `job` table (db.JobRow), in billparse's own
database. No broker: PostgreSQL's FOR UPDATE SKIP LOCKED hands each job to one worker (worker.py).

    job, created = enqueue(store, "parse", "small", name=..., sha256=..., options={...})
    job = claim(store, ["small"], worker_name)          # queued, or running with an expired lease
    state = heartbeat(store, job["id"], worker_name)    # None: the job is no longer this worker's (lost)
    finish(store, job["id"], worker_name, "done", result={...})

A claimed job belongs to its worker for LEASE_SECONDS; the worker renews the lease while it works. A job whose lease
expired (the worker died) is claimed again, or ends `error` once it used its attempts. Nothing at startup marks
another process's job failed: the leases decide. Each job has a deadline per queue (JOB_DEADLINE_<QUEUE> seconds).

Callbacks (webhooks): a finished job with a callback_url is `pending`; every worker process delivers pending ones in a
loop of its own (worker.deliver_callbacks), signed with CALLBACK_SECRET, to URLs on CALLBACK_ALLOWLIST only.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import os
import threading
import time
from typing import Any

import sqlalchemy as sa

from .db import JobRow

T = JobRow.__table__
QUEUES = ("small", "large", "learn", "maintenance")
KINDS = ("parse", "reprice", "learn", "prices", "nightly")
ACTIVE = ("queued", "running", "stopping")
FINAL = ("done", "error", "stopped")
FILE_KINDS = ("parse", "reprice")                    # at most one active per file (job_active_file)
LEASE_SECONDS = 60.0
DEADLINES = {"small": 1800, "learn": 3600, "large": 7200, "maintenance": 7200}
CALLBACK_BACKOFF = (30, 120, 600, 3600, 6 * 3600)   # seconds after the 1st, 2nd, ... failed delivery
NOTIFY_CHANNEL = "billparse_jobs"

# the job a runner is working on (the pipeline reads it to link a learn job to its parse job)
current_job: contextvars.ContextVar[int | None] = contextvars.ContextVar("billparse_job", default=None)

_sqlite_claim = threading.Lock()                    # SQLite (one process): one claim at a time
_wakeups: dict[str, threading.Event] = {}            # database URL -> set when a job is enqueued in this process
_wake_lock = threading.Lock()


class JobStopped(Exception):
    """The job was asked to stop (state stopping), or passed its deadline: the runner ends without a result."""

    def __init__(self, reason: str, state: str = "stopped"):
        super().__init__(reason)
        self.state = state


class JobLost(Exception):
    """Another worker owns the job now (a missed lease): drop the result, write nothing."""


def deadline_seconds(queue: str) -> float:
    return float(os.environ.get(f"JOB_DEADLINE_{queue.upper()}") or DEADLINES.get(queue, 3600))


def wakeup(url: str) -> threading.Event:
    """The event a worker of this process waits on between claims (set by enqueue)."""
    with _wake_lock:
        return _wakeups.setdefault(url, threading.Event())


def _pg(store) -> bool:
    return store.engine.dialect.name == "postgresql"


def _row(r) -> dict | None:
    return dict(r._mapping) if r is not None else None


# ---------------------------------------------------------------- enqueue, read
def enqueue(store, kind: str, queue: str, *, name: str, sha256: str | None = None, options: dict | None = None,
            dedupe_key: str | None = None, max_attempts: int | None = None, created_by: str | None = None,
            callback_url: str | None = None, parent_id: int | None = None) -> tuple[dict, bool]:
    """A new queued job: (job, True). A file's parse or reprice already queued or running, or a schedule's job
    already made (the same dedupe_key): (that job, False)."""
    if kind not in KINDS or queue not in QUEUES:
        raise ValueError(f"kind {kind!r} / queue {queue!r}")
    values = {"kind": kind, "queue": queue, "state": "queued", "sha256": sha256, "name": name,
              "options": options or {}, "dedupe_key": dedupe_key, "created": time.time(), "created_by": created_by,
              "max_attempts": max_attempts or (1 if kind == "learn" else 2), "callback_url": callback_url,
              "parent_id": parent_id, "attempts": 0, "callback_attempts": 0}
    try:
        with store.engine.begin() as c:
            job_id = c.execute(T.insert().values(**values)).inserted_primary_key[0]
    except sa.exc.IntegrityError:
        existing = None
        if dedupe_key:
            existing = _first(store, T.c.dedupe_key == dedupe_key)
        if existing is None and sha256 and kind in FILE_KINDS:
            existing = _first(store, (T.c.sha256 == sha256) & T.c.kind.in_(FILE_KINDS) & T.c.state.in_(ACTIVE))
        if existing is None:
            raise
        return existing, False
    _notify(store, queue)
    return get(store, job_id), True


def _notify(store, queue: str) -> None:
    wakeup(store.url).set()
    if _pg(store):
        with store.engine.begin() as c:
            c.execute(sa.text("SELECT pg_notify(:ch, :q)"), {"ch": NOTIFY_CHANNEL, "q": queue})
    from .worker import ensure_inline  # INLINE_WORKER: this process runs the jobs itself
    ensure_inline(store)


def _first(store, where) -> dict | None:
    with store.engine.connect() as c:
        return _row(c.execute(sa.select(T).where(where).order_by(T.c.id.desc()).limit(1)).first())


def get(store, job_id: int) -> dict | None:
    with store.engine.connect() as c:
        return _row(c.execute(sa.select(T).where(T.c.id == job_id)).first())


def list_jobs(store, sha256: str | None = None, state: str | None = None, kinds: tuple[str, ...] | None = None,
              limit: int = 50, states: tuple[str, ...] | None = None, parent_id: int | None = None) -> list[dict]:
    """Newest first."""
    q = sa.select(T)
    if sha256:
        q = q.where(T.c.sha256 == sha256)
    if state:
        q = q.where(T.c.state == state)
    if states:
        q = q.where(T.c.state.in_(states))
    if parent_id is not None:
        q = q.where(T.c.parent_id == parent_id)
    if kinds:
        q = q.where(T.c.kind.in_(kinds))
    with store.engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(q.order_by(T.c.id.desc()).limit(limit))]


def latest(store, sha256: str, kinds: tuple[str, ...] = FILE_KINDS) -> dict | None:
    return _first(store, (T.c.sha256 == sha256) & T.c.kind.in_(kinds))


def active(store, sha256: str, kinds: tuple[str, ...] = (*FILE_KINDS, "learn")) -> list[dict]:
    """This file's jobs that are queued, running or stopping."""
    return [j for j in list_jobs(store, sha256=sha256, kinds=kinds, limit=20) if j["state"] in ACTIVE]


# ---------------------------------------------------------------- claim, lease, finish
def claim(store, queues: list[str] | tuple[str, ...], worker: str) -> dict | None:
    """The oldest job of these queues that is queued, or running with an expired lease (its worker died), now this
    worker's: running, leased, attempts + 1, with its deadline. A job out of attempts ends `error` instead; one that
    was stopping when its worker died ends `stopped`. One transaction (PostgreSQL: SKIP LOCKED, so two workers never
    take the same job)."""
    now = time.time()
    pg = _pg(store)
    lock = _sqlite_claim if not pg else None
    lost_learning: list[int] = []
    if lock:
        lock.acquire()
    try:
        with store.engine.begin() as c:
            for _ in range(50):
                q = (sa.select(T).where(T.c.queue.in_(list(queues)))
                     .where((T.c.state == "queued") | (T.c.state.in_(("running", "stopping")) & (T.c.lease_until < now)))
                     .order_by(T.c.id).limit(1))
                if pg:
                    q = q.with_for_update(skip_locked=True)
                r = c.execute(q).first()
                if r is None:
                    return None
                job = dict(r._mapping)
                if job["state"] == "stopping":
                    c.execute(T.update().where(T.c.id == job["id"]).values(
                        state="stopped", finished=now, lease_until=None, error="Stopped (its worker ended first)",
                        **_callback(job, "stopped", now)))
                    continue
                if job["state"] == "running" and job["attempts"] >= job["max_attempts"]:
                    c.execute(T.update().where(T.c.id == job["id"]).values(
                        state="error", finished=now, lease_until=None,
                        error="The worker stopped during the job (no attempts left)", **_callback(job, "error", now)))
                    if job["kind"] == "learn" and (aj := (job["options"] or {}).get("agent_job_id")):
                        lost_learning.append(aj)
                    continue
                values = {"state": "running", "worker": worker, "started": now, "lease_until": now + LEASE_SECONDS,
                          "attempts": job["attempts"] + 1, "deadline": now + deadline_seconds(job["queue"])}
                c.execute(T.update().where(T.c.id == job["id"]).values(**values))
                return {**job, **values}
    finally:
        if lock:
            lock.release()
        for aj in lost_learning:                         # its learning run: ended by the dead worker, not retried
            if (j := store.agent_job(aj)) and j["status"] in ("queued", "running", "stopping"):
                store.update_agent_job(aj, status="error", error="The worker stopped during the run (no attempts left)")
    return None


def heartbeat(store, job_id: int, worker: str, renew: bool = True) -> str | None:
    """The job's state while it is still this worker's (and its lease renewed); None when another worker took it."""
    now = time.time()
    with store.engine.begin() as c:
        r = c.execute(sa.select(T.c.state, T.c.worker).where(T.c.id == job_id)).first()
        if r is None or r.worker != worker or r.state not in ("running", "stopping"):
            return None
        if renew:
            c.execute(T.update().where((T.c.id == job_id) & (T.c.worker == worker))
                      .values(lease_until=now + LEASE_SECONDS))
        return r.state


def set_progress(store, job_id: int, worker: str, progress: str) -> None:
    with store.engine.begin() as c:
        c.execute(T.update().where((T.c.id == job_id) & (T.c.worker == worker)).values(progress=progress[:300]))


def finish(store, job_id: int, worker: str, state: str, result: dict | None = None,
           error: str | None = None) -> bool:
    """End this worker's job (done | error | stopped). False when it is not this worker's any more: nothing written."""
    if state not in FINAL:
        raise ValueError(state)
    now = time.time()
    with store.engine.begin() as c:
        r = c.execute(sa.select(T).where((T.c.id == job_id) & (T.c.worker == worker)
                                         & T.c.state.in_(("running", "stopping")))).first()
        if r is None:
            return False
        job = dict(r._mapping)
        c.execute(T.update().where(T.c.id == job_id).values(
            state=state, finished=now, lease_until=None, result=result, error=(error or None) and error[:4000],
            **_callback(job, state, now)))
    return True


def request_stop(store, job_id: int) -> dict | None:
    """Stop a job: a queued one ends `stopped` at once; a running one is `stopping` and its worker ends it at its next
    step (within a second; a large file between two chunks)."""
    now = time.time()
    with store.engine.begin() as c:
        r = c.execute(sa.select(T).where(T.c.id == job_id)).first()
        if r is None:
            return None
        job = dict(r._mapping)
        if job["state"] == "queued":
            c.execute(T.update().where((T.c.id == job_id) & (T.c.state == "queued")).values(
                state="stopped", finished=now, error="Stopped before it ran", **_callback(job, "stopped", now)))
        elif job["state"] == "running":
            c.execute(T.update().where((T.c.id == job_id) & (T.c.state == "running")).values(state="stopping"))
    return get(store, job_id)


# ---------------------------------------------------------------- callbacks (webhooks)
EVENTS = {"done": "parse.done", "error": "parse.error", "stopped": "parse.stopped"}


def _callback(job: dict, state: str, now: float) -> dict:
    """The callback columns of a job that ends in state: pending, when it has a URL. An event fired before the job
    ended (result.updated) is sent after this one."""
    if not job.get("callback_url") or job.get("kind") not in FILE_KINDS:
        return {}
    early = job.get("callback_event") if job.get("callback_state") == "pending" else None
    return {"callback_state": "pending", "callback_event": EVENTS[state], "callback_then": early,
            "callback_attempts": 0, "callback_next": now}


def fire(store, job_id: int, event: str) -> bool:
    """Send this job's callback again with another event (result.updated: learning replaced the stored result). An
    event not yet delivered is never replaced: the new one is sent after it (parse.done, then result.updated)."""
    with store.engine.begin() as c:
        q = sa.select(T.c.callback_url, T.c.callback_state, T.c.callback_event, T.c.callback_then).where(
            T.c.id == job_id)
        r = c.execute(q.with_for_update() if _pg(store) else q).first()
        if r is None or not r.callback_url:
            return False
        if r.callback_state == "pending":
            if event not in (r.callback_event, r.callback_then):
                c.execute(T.update().where(T.c.id == job_id).values(callback_then=event))
            return True
        c.execute(T.update().where(T.c.id == job_id).values(
            callback_state="pending", callback_event=event, callback_then=None, callback_attempts=0,
            callback_next=time.time()))
    return True


def claim_callback(store) -> dict | None:
    """A pending callback that is due, held for 10 minutes by this worker (SKIP LOCKED on PostgreSQL)."""
    now = time.time()
    pg = _pg(store)
    lock = _sqlite_claim if not pg else None
    if lock:
        lock.acquire()
    try:
        with store.engine.begin() as c:
            q = (sa.select(T).where((T.c.callback_state == "pending") & (T.c.callback_next <= now)
                                    & T.c.state.in_(FINAL))                 # parse.done goes first
                 .order_by(T.c.callback_next).limit(1))
            if pg:
                q = q.with_for_update(skip_locked=True)
            r = c.execute(q).first()
            if r is None:
                return None
            c.execute(T.update().where(T.c.id == r.id).values(callback_next=now + 600))
            return dict(r._mapping)
    finally:
        if lock:
            lock.release()


def callback_result(store, job_id: int, ok: bool) -> str:
    """After a delivery: sent; else retried with backoff (30 s, 2 min, 10 min, 1 h, 6 h), then failed. Returns what
    became of this event. An event that waits behind it (callback_then) is pending from now on."""
    with store.engine.begin() as c:
        q = sa.select(T.c.callback_attempts, T.c.callback_then).where(T.c.id == job_id)
        r = c.execute(q.with_for_update() if _pg(store) else q).first()
        attempts = (r.callback_attempts if r else 0) + 1
        if ok:
            values = {"callback_state": "sent", "callback_attempts": attempts}
        elif attempts > len(CALLBACK_BACKOFF):
            values = {"callback_state": "failed", "callback_attempts": attempts}
        else:
            values = {"callback_state": "pending", "callback_attempts": attempts,
                      "callback_next": time.time() + CALLBACK_BACKOFF[attempts - 1]}
        outcome = values["callback_state"]
        if outcome != "pending" and r is not None and r.callback_then:
            values = {"callback_state": "pending", "callback_event": r.callback_then, "callback_then": None,
                      "callback_attempts": 0, "callback_next": time.time()}
        c.execute(T.update().where(T.c.id == job_id).values(**values))
    return outcome


def allowlist() -> list[str]:
    return [p.strip() for p in (os.environ.get("CALLBACK_ALLOWLIST") or "").split(",") if p.strip()]


def check_callback_url(url: str | None) -> str | None:
    """The URL when webhooks are on and it starts with an allowed prefix: https, or http://localhost (development).
    Raises ValueError otherwise."""
    if not url:
        return None
    prefixes = allowlist()
    if not prefixes:
        raise ValueError("Webhooks are off (CALLBACK_ALLOWLIST is empty)")
    if not (url.startswith("https://") or url.startswith(("http://localhost", "http://127.0.0.1"))):
        raise ValueError("A callback URL must be https (or http://localhost)")
    if not any(url.startswith(p) for p in prefixes):
        raise ValueError("The callback URL is not on CALLBACK_ALLOWLIST")
    return url


def sign(body: bytes) -> str:
    """X-Billparse-Signature: sha256=<hex HMAC of the body with CALLBACK_SECRET>."""
    secret = (os.environ.get("CALLBACK_SECRET") or "").encode()
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def stats(store) -> dict[str, Any]:
    """Jobs per queue and state (the Jobs page and a health check)."""
    with store.engine.connect() as c:
        rows = c.execute(sa.select(T.c.queue, T.c.state, sa.func.count()).group_by(T.c.queue, T.c.state)).all()
    out: dict[str, dict[str, int]] = {}
    for q, s, n in rows:
        out.setdefault(q, {})[s] = n
    return out
