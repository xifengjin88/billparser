"""HTTP API: upload a file, get the parsed document; read and manage the layout specs and the learning jobs.

    uvicorn billparse.api:app --host 0.0.0.0 --port 8000        (the API page: GET /parse)
    curl -F file=@invoice.pdf localhost:8000/parse

The paths and response models are invoice-parser's (invoiceparse/api.py and webapi.py), so its web app and clients
work unchanged. The store comes from the environment (DATABASE_URL, PARSER_DATA_DIR), as for the CLI. Parsing runs
billparse's pipeline: built-in specs, learned specs, the rule mapper, then (with ANTHROPIC_API_KEY) the LLM routes.
With AGENT_ENABLED=1, a file that no spec proves gets one from the agent during its upload (agent.py), and is read
with it; /learn/jobs lists every learning run. Learning costs money: it is off unless AGENT_ENABLED=1.
"""

from __future__ import annotations

from .settings import configure_logging, load_env

load_env()               # before the modules below read their settings from os.environ
configure_logging()

import hashlib  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402
from typing import Any, Literal  # noqa: E402

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile  # noqa: E402
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from sqlalchemy import text  # noqa: E402
from starlette.middleware.sessions import SessionMiddleware  # noqa: E402

from . import agent, auth, contract, webapi  # noqa: E402
from .docmodel import load_bytes  # noqa: E402
from .export import to_schema  # noqa: E402
from .learn import metrics, run_nightly  # noqa: E402
from .pipeline import Pipeline  # noqa: E402
from .spec import SpecError, check_spec  # noqa: E402
from .store import Store  # noqa: E402
from .webapi import get_pipeline, get_store, read_upload  # noqa: E402

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
@app.post("/parse", response_model=ParseResult)
def parse(request: Request, file: UploadFile = File(...),
          force: bool = Query(False, description="parse again a file seen before; a learning or improving job of the "
                                                 "file that ended without a spec runs again"),
          pipe: Pipeline = Depends(get_pipeline)) -> ParseResult:
    """Parse one file (CSV, TSV, xlsx, JSON, YAML, Terraform, PDF, pptx, docx, text, zip)."""
    data = read_upload(file)
    if force:
        _rerun_jobs(pipe.store, hashlib.sha256(data).hexdigest())
    t0 = time.time()
    r = pipe.parse_bytes(data, file.filename or "upload", force=force, user=auth.current_user(request))
    c = r.get("contract") or {}
    row = pipe.store.document(r["sha256"])
    job = pipe.store.agent_job_for(r["sha256"]) if not r.get("cached") else None
    learn = float(job["cost_usd"] or 0) if job and (job.get("started") or 0) >= t0 - 1 else 0.0
    claude = float(r.get("claude_cost_usd") or 0.0)
    return ParseResult(sha256=r["sha256"], name=r["file"], status=contract.status_of(row.status if row else r["status"]),
                       lane=c.get("lane") or "", completeness=c.get("completeness") or "unchecked",
                       confidence=c.get("confidence"), elapsed_ms=int((r.get("seconds") or 0) * 1000),
                       model_calls=int(r.get("llm_calls") or 0), cached=bool(r.get("cached")),
                       cost_usd={"claude": claude, "sandbox": learn, "total": round(claude + learn, 6)},
                       document=_contract(pipe.store, r["sha256"]))


@app.get("/documents", response_model=list[webapi.DocumentItem])
def documents(store: Store = Depends(get_store)) -> list[dict]:
    runs: dict[str, list[dict]] = {}
    for r in store.runs():
        runs.setdefault(r["sha256"], []).append(r)
    return [webapi.row_dict(store, row, runs.get(row.sha256, [])) for row in store.list_documents()]


@app.get("/documents/{sha256}")
def document(sha256: str, store: Store = Depends(get_store)) -> dict:
    """The parsed document in the output contract (the approved version when there is one)."""
    t = webapi._tasks.get(sha256)
    if t and not t["future"].done() and t.get("step") == "learning":
        raise HTTPException(409, "A layout spec is being learned for this file; its result is written when the "
                                 "learning job ends (GET /learn/jobs).")
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
    """Let this layout be sent to Claude again on its next upload (or backfill)."""
    if not store.retry_agent_job(job_id):
        raise HTTPException(409, "No such run, or it is running.")
    return {"id": job_id, "status": "queued"}


@app.post("/learn/jobs/{job_id}/stop")
def learn_stop(job_id: int, store: Store = Depends(get_store)) -> dict:
    """Stop a running job: it ends at its next step as failed, with what it cost so far, and the file keeps the
    result it had before the run."""
    status = webapi.stop_job(store, job_id)
    if status is None:
        raise HTTPException(404, "No such job.")
    return {"id": job_id, "status": status}


@app.post("/learn/backfill")
def learn_backfill(store: Store = Depends(get_store), pipe: Pipeline = Depends(get_pipeline)) -> list[dict]:
    """Parse again every stored document that no spec proved, so each layout gets a spec. Calls the Claude API
    once per new layout: allowed only with AGENT_ENABLED=1."""
    if not agent.enabled():
        raise HTTPException(403, "Learning calls the Claude API and costs money; set AGENT_ENABLED=1 to allow it.")
    out = []
    for row in store.list_documents():
        summary = row.summary or {}
        if not agent.needs_agent(summary) or (loaded := store.load_doc(row.sha256)) is None:
            continue
        r = pipe.parse_bytes(loaded[0], loaded[1], force=True)
        out.append({"sha256": row.sha256, "name": row.name, "status": contract.status_of(r.get("status")),
                    "completeness": (r.get("contract") or {}).get("completeness")})
    return out


# ------------------------------------------------------------------ billparse's own: the nightly loop
@app.post("/nightly")
def nightly(pipe: Pipeline = Depends(get_pipeline)) -> dict[str, Any]:
    """Run the nightly loop and answer with its report."""
    return run_nightly(pipe)


@app.get("/metrics")
def get_metrics(store: Store = Depends(get_store)) -> dict[str, Any]:
    return metrics(store.read("records"))
