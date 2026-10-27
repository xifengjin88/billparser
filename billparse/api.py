"""HTTP API: upload a file, get the parsed document; read and manage the layout specs and the learning jobs.

    uvicorn billparse.api:app --host 0.0.0.0 --port 8000        (the API page: GET /parse)
    curl -F file=@invoice.pdf localhost:8000/parse

The paths and response models are invoice-parser's (invoiceparse/api.py and webapi.py), so its web app and clients
work unchanged. The store comes from the environment (DATABASE_URL, PARSER_DATA_DIR), as for the CLI. Parsing runs
billparse's pipeline: built-in specs, learned specs, the rule mapper, then (with ANTHROPIC_API_KEY) the LLM routes.

Parses run in worker processes (python -m billparse worker), never in the API: POST /parse/jobs queues one and answers
at once (poll GET /parse/jobs/{id}, or give a callback_url for a signed webhook). POST /parse queues one and waits for
it, up to PARSE_WAIT_SECONDS (300): a parse that takes longer answers 202 with the job, as POST /parse/jobs does.
With AGENT_ENABLED=1, a file that no spec proves gets one from the agent in a learning job of its own (agent.py): the
parse answers with its first-pass result, and a proved spec replaces the stored result later (the webhook event
result.updated). /learn/jobs lists every learning run. Learning costs money: it is off unless AGENT_ENABLED=1.
"""

from __future__ import annotations

from .settings import configure_logging, load_env

load_env()               # before the modules below read their settings from os.environ
configure_logging()

import asyncio  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import time  # noqa: E402
from typing import Any, Literal  # noqa: E402

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile  # noqa: E402
from fastapi.concurrency import run_in_threadpool  # noqa: E402
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from sqlalchemy import text  # noqa: E402
from starlette.middleware.sessions import SessionMiddleware  # noqa: E402

from . import agent, auth, contract, jobs, webapi, worker  # noqa: E402
from .docmodel import load_bytes  # noqa: E402
from .export import to_schema  # noqa: E402
from .learn import metrics  # noqa: E402
from .pipeline import Pipeline  # noqa: E402
from .spec import SpecError, check_spec  # noqa: E402
from .store import Store  # noqa: E402
from .webapi import get_pipeline, get_store, read_upload, save_upload  # noqa: E402

# GET /parse shows the API page (Swagger UI); POST /parse parses a file
app = FastAPI(title="billparse", version="1", description=__doc__ or "", docs_url="/parse", redoc_url=None)
# the sign-in check runs inside the session middleware (the last one added runs first)
app.middleware("http")(auth.require_login)
app.add_middleware(SessionMiddleware, secret_key=auth.session_secret(), session_cookie="billparse_session",
                   max_age=7 * 24 * 3600, same_site="lax", https_only=auth.https_only())
app.add_middleware(GZipMiddleware, minimum_size=2000)     # a large document's view is mostly repeated text
app.include_router(auth.router)                           # Google sign-in, invite codes (AUTH=google)
app.include_router(webapi.router)                         # the web app's endpoints (web/)


class ParseResult(BaseModel):
    sha256: str
    name: str
    status: str
    lane: str
    completeness: str
    confidence: str | None        # high | medium | low (the reasons are in document.confidence)
    elapsed_ms: int
    model_calls: int
    cached: bool
    cost_usd: dict[str, float]       # claude (LLM routes), sandbox (the agent's spec for this file), total
    document: dict[str, Any]


class StatusChange(BaseModel):
    status: Literal["trusted", "draft", "disabled"]


def _contract(store: Store, sha: str) -> dict:
    doc, _ = webapi._load(store, sha)
    return json.loads(to_schema(doc).model_dump_json())


def _rerun_jobs(store: Store, sha: str) -> None:
    """This file's last learning job, ended without a spec, runs again on its next parse."""
    for j in store.agent_jobs():
        if j["sha256"] == sha and j["status"] not in ("queued", "running", "stopping", "proved"):
            store.retry_agent_job(j["id"])


# ------------------------------------------------------------------ health
@app.get("/health")
def health(store: Store = Depends(get_store)) -> dict:
    with store.session() as s:
        s.exec(text("select 1"))  # type: ignore[call-overload]
    return {"ok": True, "database": store.engine.dialect.name}


# ------------------------------------------------------------------ documents
class JobOut(BaseModel):
    """A parse job (POST /parse/jobs, GET /parse/jobs/{id}): the summary is ParseTask's fields once it is done."""
    job_id: int
    kind: str
    queue: str
    sha256: str | None
    name: str
    state: str                      # queued, running, stopping, done, error, stopped
    progress: str | None = None
    created: float
    started: float | None = None
    finished: float | None = None
    attempts: int
    error: str | None = None
    summary: dict[str, Any] | None = None
    learning: webapi.LearningOut | None = None
    callback_state: str | None = None
    status_url: str
    result_url: str | None = None


def job_out(store: Store, job: dict) -> JobOut:
    return JobOut(job_id=job["id"], kind=job["kind"], queue=job["queue"], sha256=job["sha256"], name=job["name"],
                  state=job["state"], progress=job.get("progress"), created=job["created"], started=job.get("started"),
                  finished=job.get("finished"), attempts=job["attempts"], error=job.get("error"),
                  summary=job.get("result"), callback_state=job.get("callback_state"),
                  learning=webapi.learning_of(store, job) if job["kind"] == "parse" else None,
                  status_url=f"/parse/jobs/{job['id']}",
                  result_url=f"/documents/{job['sha256']}" if job["state"] == "done" and job["sha256"] else None)


def _queue_parse(request: Request, file: UploadFile, store: Store, force: bool, defaults, callback_url: str | None,
                 backend: str | None = None, fallback: str | None = None) -> tuple[dict, bool]:
    try:
        jobs.check_callback_url(callback_url)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    webapi.check_choices(backend, fallback)
    path, sha, _ = save_upload(file, store)            # on disk, never whole in memory (a large export)
    if force:
        _rerun_jobs(store, sha)
    use_llm = webapi._use_llm(backend)
    return webapi.start_parse(store, file.filename or "upload", path, sha, force, use_llm, auth.current_user(request),
                              agent_step=fallback != "none", backend=backend, fallback=fallback, temp=True,
                              defaults=defaults, callback_url=callback_url)


FORCE = Query(False, description="parse again a file seen before; a learning or improving job of the file that ended "
                                 "without a spec runs again")


@app.post("/parse/jobs", response_model=JobOut, status_code=202,
          responses={200: {"model": JobOut, "description": "the file is already being parsed: its job"}})
def parse_job(request: Request, file: UploadFile = File(...), force: bool = FORCE,
              callback_url: str | None = Query(None, description="POSTed a signed JSON body when the job ends (and "
                                               "again when learning replaces the result); must start with one of "
                                               "CALLBACK_ALLOWLIST"),
              backend: str | None = Query(None), fallback: str | None = Query(None),
              assume_os: str | None = webapi.ASSUME_OS, assume_tenancy: str | None = webapi.ASSUME_TENANCY,
              assume_software: str | None = webapi.ASSUME_SOFTWARE, assume_license: str | None = webapi.ASSUME_LICENSE,
              assume_region: str | None = webapi.ASSUME_REGION, store: Store = Depends(get_store)) -> Any:
    """Queue a parse and answer at once (202). The file's parse already queued or running: that job (200)."""
    job, created = _queue_parse(request, file, store, force, webapi.price_defaults(
        assume_os, assume_tenancy, assume_software, assume_license, assume_region), callback_url, backend, fallback)
    out = job_out(store, job)
    return out if created else JSONResponse(out.model_dump(), status_code=200)


@app.get("/parse/jobs", response_model=list[JobOut])
def parse_jobs(sha256: str | None = None, state: str | None = None, limit: int = Query(50, le=500),
               store: Store = Depends(get_store)) -> list[JobOut]:
    """Parse and reprice jobs, newest first."""
    return [job_out(store, j) for j in jobs.list_jobs(store, sha256=sha256, state=state, kinds=jobs.FILE_KINDS,
                                                       limit=limit)]


@app.get("/parse/jobs/{job_id}", response_model=JobOut)
def parse_job_get(job_id: int, store: Store = Depends(get_store)) -> JobOut:
    job = jobs.get(store, job_id)
    if job is None:
        raise HTTPException(404, "No job with this id.")
    return job_out(store, job)


@app.post("/parse/jobs/{job_id}/stop", response_model=JobOut)
def parse_job_stop(job_id: int, store: Store = Depends(get_store)) -> JobOut:
    """A queued job ends `stopped` at once; a running one at the parse's next step (between two chunks of a large
    file)."""
    job = jobs.request_stop(store, job_id)
    if job is None:
        raise HTTPException(404, "No job with this id.")
    return job_out(store, job)


def parse_wait() -> float:
    return float(os.environ.get("PARSE_WAIT_SECONDS") or 300)


@app.post("/parse", response_model=ParseResult,
          responses={202: {"model": JobOut, "description": "not done within PARSE_WAIT_SECONDS: the job, to poll"}})
async def parse(request: Request, file: UploadFile = File(...), force: bool = FORCE,
          assume_os: str | None = webapi.ASSUME_OS, assume_tenancy: str | None = webapi.ASSUME_TENANCY,
          assume_software: str | None = webapi.ASSUME_SOFTWARE, assume_license: str | None = webapi.ASSUME_LICENSE,
          assume_region: str | None = webapi.ASSUME_REGION, store: Store = Depends(get_store)) -> Any:
    """Parse one file (CSV, TSV, xlsx, JSON, YAML, Terraform, PDF, pptx, docx, text, zip): a worker parses it while
    this request waits, up to PARSE_WAIT_SECONDS (300). Longer (a large export): 202 with the job, to poll at
    GET /parse/jobs/{id}. The result is the first pass: a learning run, when the file needs one, runs after it.
    The wait holds no request thread (async): many callers waiting never block the other endpoints."""
    job, _ = await run_in_threadpool(_queue_parse, request, file, store, force, webapi.price_defaults(
        assume_os, assume_tenancy, assume_software, assume_license, assume_region), None)
    end = time.monotonic() + parse_wait()
    while job["state"] in jobs.ACTIVE and time.monotonic() < end:
        await asyncio.sleep(0.5)
        job = await run_in_threadpool(jobs.get, store, job["id"]) or job
    if job["state"] in jobs.ACTIVE:
        return JSONResponse((await run_in_threadpool(job_out, store, job)).model_dump(), status_code=202)
    if job["state"] != "done":
        raise HTTPException(500 if job["state"] == "error" else 409, job.get("error") or job["state"])
    return await run_in_threadpool(_parse_result, store, job)


def _parse_result(store: Store, job: dict) -> ParseResult:
    """A done parse job as POST /parse's answer (it reads the stored document: in a thread, never the event loop)."""
    t = job.get("result") or {}
    row = store.document(job["sha256"])
    if row is None:
        raise HTTPException(500, "The parse kept no document")
    claude = float(t.get("claude_cost_usd") or 0.0)
    return ParseResult(sha256=job["sha256"], name=row.name, status=t.get("status") or contract.status_of(row.status),
                       lane=t.get("lane") or "", completeness=t.get("completeness") or "unchecked",
                       confidence=t.get("confidence"), elapsed_ms=int(t.get("elapsed_ms") or 0),
                       model_calls=int(t.get("model_calls") or 0), cached=bool(t.get("cached")),
                       cost_usd={"claude": claude, "sandbox": 0.0, "total": round(claude, 6)},
                       document=_contract(store, job["sha256"]))


@app.get("/documents", response_model=list[webapi.DocumentItem])
def documents(store: Store = Depends(get_store)) -> list[dict]:
    runs: dict[str, list[dict]] = {}
    for r in store.runs():
        runs.setdefault(r["sha256"], []).append(r)
    busy = webapi.busy(store)
    return [webapi.row_dict(store, row, runs.get(row.sha256, []), busy) for row in store.list_documents()]


@app.get("/documents/{sha256}")
def document(sha256: str, store: Store = Depends(get_store)) -> dict:
    """The parsed document in the output contract (the approved version when there is one)."""
    if store.document(sha256) is None:
        raise HTTPException(404, "No document with this SHA-256.")
    return _contract(store, sha256)


# ------------------------------------------------------------------ layout specs
SPEC_STATUS = webapi.SPEC_STATUS
TRUST_OF_STATUS = {"trusted": "verified", "draft": "candidate", "disabled": "disabled"}


def _builtin_ids(store: Store) -> set[str]:
    return {s["id"] for s in store.builtin_specs()}


def _summary(store: Store, s: dict, builtin: bool, status: dict[str, str]) -> dict:
    meta = s.get("meta") or {}
    return {"id": s["id"], "name": meta.get("format") or s["id"], "version": s.get("version") or 1,
            "status": status.get(s["id"]) or ("trusted" if builtin else SPEC_STATUS.get(s.get("trust") or "", "draft")),
            "engine": "builtin" if builtin else (s.get("stats") or {}).get("created_by") or "mapper",
            "priority": 0 if builtin else 1, "verified": len(store.spec_docs(s["id"])),
            "updated_at": s.get("created_at") or 0.0}


@app.get("/specs")
def list_specs(store: Store = Depends(get_store)) -> list[dict]:
    status = store.builtin_status()
    return [_summary(store, s, True, status) for s in store.builtin_specs()] + \
           [_summary(store, s, False, {}) for s in store.learned_specs(include_disabled=True)]


@app.get("/specs/{spec_id}")
def get_spec(spec_id: str, store: Store = Depends(get_store)) -> dict:
    spec = store.get_spec(spec_id)
    if spec is None:
        raise HTTPException(404, "No spec with this id.")
    head = _summary(store, spec, spec_id in _builtin_ids(store), store.builtin_status())
    return {**spec, "id": spec_id, "status": head["status"], "name": head["name"], "version": head["version"],
            "engine": head["engine"], "notes": (spec.get("meta") or {}).get("notes")}


@app.get("/specs/{spec_id}/history")
def spec_history(spec_id: str, store: Store = Depends(get_store)) -> list[dict]:
    if spec_id in _builtin_ids(store):
        return [{"version": 1, "saved_at": 0.0, "spec": store.get_spec(spec_id) or {}}]
    return [{"version": h["version"], "saved_at": h["created_at"], "spec": h["spec"]}
            for h in sorted(store.spec_history(spec_id), key=lambda h: h["version"])]


@app.put("/specs/{spec_id}")
def put_spec(spec_id: str, spec: dict[str, Any], store: Store = Depends(get_store)) -> dict:
    """Create a spec, or save a new version of it. A spec without a status is saved as a draft."""
    if spec.get("id", spec_id) != spec_id:
        raise HTTPException(400, "The spec's id is not the id in the path.")
    if spec_id in _builtin_ids(store):
        raise HTTPException(400, "A built-in spec cannot be changed; save it under a new id.")
    status = spec.get("status") or "draft"
    body = {k: v for k, v in spec.items() if k not in ("status", "name", "engine", "notes", "version", "trust")}
    try:
        check_spec(dict(body, id=spec_id))
    except SpecError as e:
        raise HTTPException(422, str(e)) from None
    saved = store.save_spec(dict(body, id=spec_id, trust=TRUST_OF_STATUS.get(status, "candidate")))
    return {"id": spec_id, "version": saved["version"], "status": SPEC_STATUS.get(saved["trust"], "draft")}


@app.patch("/specs/{spec_id}/status")
def set_spec_status(spec_id: str, change: StatusChange, store: Store = Depends(get_store)) -> dict:
    spec = store.get_spec(spec_id)
    if spec is None:
        raise HTTPException(404, "No spec with this id.")
    if spec_id in _builtin_ids(store):
        store.set_builtin_status(spec_id, change.status)
        return {"id": spec_id, "version": 1, "status": change.status}
    saved = store.save_spec(dict(spec, trust=TRUST_OF_STATUS[change.status]))
    return {"id": spec_id, "version": saved["version"], "status": change.status}


def _spec_json(store: Store, spec: str | None, spec_id: str | None) -> dict:
    if spec:
        try:
            return json.loads(spec)
        except json.JSONDecodeError as e:
            raise HTTPException(422, f"The spec is not JSON: {e}") from None
    if spec_id and (saved := store.get_spec(spec_id)):
        return saved
    raise HTTPException(400, "Give a spec (JSON) or the id of a saved spec.")


@app.post("/specs/verify")
def verify_spec(file: UploadFile = File(...), spec: str | None = Form(None, description="the spec JSON to check"),
                spec_id: str | None = Form(None, description="or the id of a saved spec"),
                store: Store = Depends(get_store), pipe: Pipeline = Depends(get_pipeline)) -> dict:
    """Run the strict verifier: may this spec read this file? (Models off; nothing is saved.)"""
    spec_json = _spec_json(store, spec, spec_id)
    doc = load_bytes(read_upload(file), file.filename or "upload")
    try:
        report, _, verdict = agent.check(spec_json, doc, pipe.s.catalog_dir)
    except (SpecError, ValueError, KeyError) as e:
        raise HTTPException(422, f"The spec cannot run: {e}") from None
    return {"ok": verdict.get("completeness") == "reconciled", **report}


@app.post("/specs/learn")
def learn_spec(file: UploadFile = File(...), examples: str = Form("", description="ids of saved specs to show as "
                                                                    "examples (default: saved specs of similar files)"),
               rounds: int = Form(3), dry_run: bool = Form(False, description="true: do not save the spec"),
               replace: bool = Form(False),
               store: Store = Depends(get_store), pipe: Pipeline = Depends(get_pipeline)) -> dict:
    """Ask the agent for a spec for this file; save it as a draft when the code's checks prove it. Calls the Claude
    API: allowed only with AGENT_ENABLED=1. A file that a saved spec already reads is refused (409, no model call)
    unless `replace` is true. The new spec never overwrites a saved one: an id that exists gets a suffix."""
    if not agent.enabled():
        raise HTTPException(403, "The learner calls the Claude API and costs money; set AGENT_ENABLED=1 to allow it.")
    from .fingerprint import fingerprint
    data, name = read_upload(file), file.filename or "upload"
    summary = pipe.parse_bytes(data, name, force=True, agent=False, run=False)
    spec_info = summary.get("spec") or {}
    if not replace and spec_info.get("route") in ("builtin", "learned") and summary.get("completeness") == "reconciled":
        raise HTTPException(409, f"The saved spec '{spec_info.get('id')}' already reads this file; send replace=true "
                                 "to learn anyway.")
    doc = load_bytes(data, name)
    fp = fingerprint(doc)
    example_specs = []
    for i in (x.strip() for x in examples.split(",") if x.strip()):
        s = store.get_spec(i)
        if s is None:
            raise HTTPException(400, f"No spec {i} to use as an example.")
        example_specs.append(s)
    if not examples.strip():
        example_specs = [e for _, e in pipe._examples(fp)][:3]
    limits = agent.Limits.from_env()
    limits.rounds = rounds
    out = agent.author(doc, summary, agent.ClaudeClient(), limits, pipe.s.catalog_dir, example_specs)
    result: dict = {"ok": out.status == "proved", "status": out.status, "spec": out.spec, "report": out.report,
                    "rounds": out.rounds, "tool_calls": out.tool_calls, "cost_usd": round(out.cost_usd, 4),
                    "note": out.note}
    if result["ok"] and not dry_run and out.spec:
        spec_id = f"agent-{doc.file_type}-{fp['hash'][:10]}"
        if store.get_spec(spec_id) is not None:            # never a new version of another spec
            spec_id = f"{spec_id}-{summary['sha256'][:8]}"
        saved = store.save_spec(dict(out.spec, id=spec_id, trust="candidate", fingerprint=fp,
                                     stats={"created_by": "agent", "model": limits.model}))
        result["saved"] = {"id": spec_id, "version": saved["version"], "status": "draft"}
    return result


# ------------------------------------------------------------------ learning (a spec for every layout)
@app.get("/learn/jobs")
def learn_jobs(store: Store = Depends(get_store)) -> list[dict]:
    """Every learning run: one per layout that no spec read (status, spec made, its status, cost)."""
    return webapi.list_jobs(store)


@app.get("/learn/jobs/{job_id}")
def learn_job(job_id: int, store: Store = Depends(get_store)) -> dict:
    """One learning run, with the code's report on the spec the agent wrote."""
    job = store.agent_job(job_id)
    if job is None:
        raise HTTPException(404, "No learning run with this id.")
    return webapi.job_out(store, job, report=True)


@app.post("/learn/jobs/{job_id}/retry")
def learn_retry(job_id: int, store: Store = Depends(get_store)) -> dict:
    """Run this layout's learning again: a learn job (a worker runs it when the agent is on)."""
    if not store.retry_agent_job(job_id):
        raise HTTPException(409, "No such run, or it is running.")
    learn = worker.pipeline_for(store, False).queue_learning(job_id) if agent.enabled() else None
    return {"id": job_id, "status": "queued", "learn_job_id": learn}


@app.post("/learn/jobs/{job_id}/stop")
def learn_stop(job_id: int, store: Store = Depends(get_store)) -> dict:
    """Stop a running job: it ends at its next step as failed, with what it cost so far, and the file keeps the
    result it had before the run."""
    status = webapi.stop_job(store, job_id)
    if status is None:
        raise HTTPException(404, "No such job.")
    for j in jobs.list_jobs(store, kinds=("learn",), states=jobs.ACTIVE, limit=500):
        if (j.get("options") or {}).get("agent_job_id") == job_id:
            jobs.request_stop(store, j["id"])
    return {"id": job_id, "status": status}


@app.post("/learn/backfill", status_code=202)
def learn_backfill(request: Request, store: Store = Depends(get_store)) -> list[dict]:
    """Queue a parse of every stored document that no spec proved, so each layout gets a spec (in learn jobs). Calls
    the Claude API once per new layout: allowed only with AGENT_ENABLED=1."""
    if not agent.enabled():
        raise HTTPException(403, "Learning calls the Claude API and costs money; set AGENT_ENABLED=1 to allow it.")
    out = []
    for row in store.list_documents():
        summary = row.summary or {}
        if not agent.needs_agent(summary) or (path := store.file_path(row.sha256)) is None:
            continue
        job, _ = webapi.start_parse(store, store.doc_name(row.sha256), path, row.sha256, True, webapi.llm_wanted(),
                                    auth.current_user(request))
        out.append({"sha256": row.sha256, "name": row.name, "job_id": job["id"], "state": job["state"]})
    return out


# ------------------------------------------------------------------ billparse's own: the nightly loop, schedules
@app.post("/nightly", status_code=202)
def nightly(request: Request, store: Store = Depends(get_store)) -> dict[str, Any]:
    """Queue the nightly loop (a worker runs it); its report is the job's result (GET /jobs/{id})."""
    job, _ = jobs.enqueue(store, "nightly", "maintenance", name="nightly (by request)",
                          created_by=auth.current_user(request), max_attempts=1)
    return {**job, "status_url": f"/jobs/{job['id']}"}


@app.get("/jobs/schedules")
def schedules(store: Store = Depends(get_store)) -> list[dict]:
    """Each schedule (the weekly AWS price refresh, the nightly loop): its next run and its last job."""
    out = []
    for s in worker.schedules():
        last = next(iter(jobs.list_jobs(store, kinds=(s["kind"],), limit=1)), None)
        out.append({k: v for k, v in s.items() if k != "due"} | {"last_job": None if last is None else {
            "id": last["id"], "state": last["state"], "created": last["created"], "finished": last.get("finished"),
            "error": last.get("error")}})
    return out


@app.get("/jobs/stats")
def job_stats(store: Store = Depends(get_store)) -> dict[str, Any]:
    """Jobs per queue and state."""
    return jobs.stats(store)


@app.get("/jobs/{job_id}")
def job_get(job_id: int, store: Store = Depends(get_store)) -> dict[str, Any]:
    """Any job (a parse, reprice, learning run, price refresh or nightly loop), with its result."""
    job = jobs.get(store, job_id)
    if job is None:
        raise HTTPException(404, "No job with this id.")
    return job


@app.get("/metrics")
def get_metrics(store: Store = Depends(get_store)) -> dict[str, Any]:
    return metrics(store.read("records"))
