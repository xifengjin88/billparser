"""Endpoints for the web app (web/, the same app as invoice-parser's): documents as views, the source viewer, review,
jobs, costs and the service map. The paths and response models are invoice-parser's (invoiceparse/webapi.py), so its
web app runs unchanged; behind them is billparse's pipeline, store and agent.

A document's view is the contract document (contract.py) that each parse saves beside its lines; the approved
version ("gold") replaces it after review. The server keeps nothing in memory between requests but the parse tasks.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import agent, auth, bigfile, contract, highlight
from . import llm as llm_mod
from .analyze import RESOLVER_LOCK, get_resolver
from .pipeline import Pipeline
from .pipeline import Settings as PipelineSettings
from .review import CHECKLIST, apply_edits, approve, charge_rows, drop_gaps
from .schema import DOC_TYPES, KINDS, Cite, Document, cv
from .settings import catalog_dir, data_dir
from .store import Store

router = APIRouter()
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "2048"))


@lru_cache(maxsize=1)
def get_store() -> Store:
    """The store of the API (billparse.api uses this same function, so a test's override covers both)."""
    store = Store(data_dir())
    store.recover_agent_jobs()                       # agent jobs that a stopped app left running
    return store


def llm_wanted() -> bool:
    return os.environ.get("BILLPARSE_USE_LLM", "1") != "0" and bool(os.environ.get("ANTHROPIC_API_KEY"))


@lru_cache(maxsize=32)
def _pipeline(store: Store, use_llm: bool, backend: str | None = None, fallback: str | None = None) -> Pipeline:
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


def get_pipeline(store: Store = Depends(get_store)) -> Pipeline:
    return _pipeline(store, llm_wanted())


def read_upload(upload: UploadFile) -> bytes:
    data = upload.file.read(MAX_UPLOAD_MB * 1024 * 1024 + 1)
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"The file is larger than {MAX_UPLOAD_MB} MB.")
    if not data:
        raise HTTPException(400, "The file is empty.")
    return data


def save_upload(upload: UploadFile, store: Store) -> tuple[Path, str, int]:
    """Copy an upload to a temporary file in the data directory, 8 MB at a time, never whole in memory: (path,
    SHA-256, size). 413 above MAX_UPLOAD_MB. The caller removes the file, or the pipeline moves it into the store."""
    limit = MAX_UPLOAD_MB * 1024 * 1024
    path = store.temp_path(Path(upload.filename or "").suffix)
    h, n = hashlib.sha256(), 0
    try:
        with path.open("wb") as out:
            while (b := upload.file.read(8 * 1024 * 1024)):
                n += len(b)
                if n > limit:
                    raise HTTPException(413, f"The file is larger than {MAX_UPLOAD_MB} MB.")
                h.update(b)
                out.write(b)
        if not n:
            raise HTTPException(400, "The file is empty.")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path, h.hexdigest(), n


# ------------------------------------------------------------------ models (invoice-parser's)
class CiteOut(BaseModel):
    label: str
    page: int | None = None
    line: int | None = None
    table: str | None = None
    row: int | None = None
    column: str | None = None
    text: str | None = None


class DocumentItem(BaseModel):
    sha256: str
    name: str
    fmt: str | None = None
    uploaded_at: float | None = None
    status: str | None = None
    completeness: str | None = None
    lane: str | None = None
    elapsed_ms: int | None = None
    model_calls: int | None = None
    reviewed_at: float | None = None
    money_at_risk: float | None = None
    uploaded_by: str | None = None
    total_ms: int | None = None


class ChargeRow(BaseModel):
    id: str
    description: str
    cost: float | None = None
    cost_exact: str | None = None
    basis: str | None = None
    list_cost: float | None = None
    contracted_cost: float | None = None
    effective_cost: float | None = None
    coverage: str | None = None             # on_demand | reserved | savings_plan | committed_use | spot | unknown
    counted: bool
    kind: str
    kind_by: str = ""
    service: str | None = None
    service_code: str | None = None
    region: str | None = None
    account: str | None = None
    instance_type: str | None = None
    operating_system: str | None = None
    database_engine: str | None = None
    deployment_option: str | None = None
    pricing_model: str | None = None
    quantity: float | None = None
    unit: str | None = None
    unit_price: float | None = None
    section: str | None = None
    source: str
    cite: CiteOut
    has_gap: bool = False


class TotalRow(BaseModel):
    id: str
    label: str
    printed: str
    computed: str | None = None
    status: str
    level: str
    section: str | None = None
    lines: int
    cite: CiteOut


class GapRow(BaseModel):
    index: int
    kind: str
    message: str
    field: str | None = None
    ref: str | None = None
    amount: str | None = None
    candidates: list[str]
    cite: CiteOut | None = None


class NoteRow(BaseModel):
    message: str
    where: str | None = None


class FactRow(BaseModel):
    label: str
    amount: str
    currency: str | None = None
    period: str | None = None
    cite: CiteOut


class CommitmentRow(BaseModel):
    type: str
    id: str | None = None
    term: str | None = None
    payment: str | None = None
    amount: str | None = None
    charges: list[str]


class RunOut(BaseModel):
    id: int
    ts: float
    elapsed_ms: int
    model_calls: int
    lane: str | None = None
    status: str | None = None
    completeness: str | None = None
    version: str | None = None
    jev_cost_usd: float | None = None
    claude_cost_usd: float | None = None
    learn_cost_usd: float | None = None
    learn_job_id: int | None = None
    parsed_by: str | None = None
    total_ms: int | None = None
    quality: dict[str, Any] | None = None


class ConfidenceOut(BaseModel):
    level: Literal["high", "medium", "low"]
    reasons: list[str]


class DocumentView(BaseModel):
    sha256: str
    name: str
    fmt: str | None = None
    status: str
    lane: str | None = None
    specs_used: list[str] = []          # the spec versions that read this file ("id@v3")
    uploaded_at: float | None = None
    reviewed_at: float | None = None
    reviewer_note: str | None = None
    uploaded_by: str | None = None
    total_ms: int | None = None
    elapsed_ms: int | None = None
    model_calls: int | None = None
    showing_reviewed: bool = False
    doc_type: str | None = None
    provider: str | None = None
    currency: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    page_count: int = 1
    completeness: str
    confidence: ConfidenceOut | None = None
    cost_types: list[str] = []              # billed, list, contracted, effective: the cost types the file gives
    cost_basis: str | None = None           # actual | amortized | unknown (Azure exports)
    counted_total: str
    money_at_risk: str
    charges: list[ChargeRow]
    totals: list[TotalRow]
    gaps: list[GapRow]
    flags: list[NoteRow]
    notes: list[NoteRow]
    facts: list[FactRow]
    commitments: list[CommitmentRow]
    runs: list[RunOut]
    decider: str | None = None
    version: str | None = None
    timings_ms: dict[str, int]
    tables: list[dict[str, Any]]
    llm_calls: list[dict[str, Any]]
    decider_errors: list[str]
    learned_spec: dict[str, Any] | None = None
    improved_spec: dict[str, Any] | None = None
    cost_usd: dict[str, float]


class FieldOut(BaseModel):
    field: str
    value: str
    origin: str
    source: str | None = None
    confidence: float | None = None
    cite: CiteOut


class ChargeDetail(BaseModel):
    id: str
    description: str
    cite: CiteOut
    fields: list[FieldOut]
    attributes: dict[str, Any]


class TableInfo(BaseModel):
    name: str
    row_offset: int
    col_letters: bool = False
    truncated: bool = False
    rows: int


class SourceInfo(BaseModel):
    fmt: str
    page_count: int
    has_pdf: bool
    tables: list[TableInfo]
    lines: int


class SourceRow(BaseModel):
    row: int
    cells: list[str]
    role: str


class SourceTable(BaseModel):
    name: str
    row_offset: int
    col_letters: bool
    columns: list[str]
    total_rows: int
    offset: int
    rows: list[SourceRow]
    truncated: bool
    from_file: bool = False


class SourceLine(BaseModel):
    page: int
    line: int
    text: str
    role: str
    top: float | None = None         # PDF: the line's top in points (the page image has highlight.SCALE pixels a point)


class ParseTask(BaseModel):
    sha256: str
    name: str
    state: Literal["running", "done", "error"]
    started: float
    started_by: str | None = None
    finished: float | None = None
    error: str | None = None
    progress: str | None = None
    status: str | None = None
    lane: str | None = None
    completeness: str | None = None
    confidence: str | None = None
    charges: int | None = None
    counted_total: str | None = None
    gaps: int | None = None
    elapsed_ms: int | None = None
    total_ms: int | None = None
    model_calls: int | None = None
    cached: bool | None = None
    cost_usd: float | None = None


class HeaderEdit(BaseModel):
    doc_type: str | None = None
    provider: str | None = None
    currency: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    basis: Literal["billed", "estimated", "priced"] | None = None


class ReviewEdits(BaseModel):
    """A person's changes to one document, sent whole each time (the server keeps no review session):
    `rows` edits charges by id (any of description, cost, kind, counted, service, service_code, delete),
    `added` are new charges, `dropped_gaps` the indexes of gaps marked resolved (of the stored document)."""
    rows: dict[str, dict[str, Any]] = Field(default_factory=dict)
    added: list[dict[str, Any]] = Field(default_factory=list)
    dropped_gaps: list[int] = Field(default_factory=list)
    header: HeaderEdit | None = None


class ApproveRequest(ReviewEdits):
    note: str = ""
    checklist: list[str] = Field(default_factory=list)


class ReviewPreview(BaseModel):
    view: DocumentView
    changes: dict[str, Any]


class Settings(BaseModel):
    decision_backend: str
    decision_fallback: str
    backends: list[str]
    keys: dict[str, bool]
    learner_enabled: bool
    max_upload_mb: int
    kinds: list[str]
    doc_types: list[str]
    checklist: list[dict[str, str]]


class MappingRow(BaseModel):
    provider: str
    code: str
    name: str
    match: str
    aliases: list[str]
    note: str = ""
    was: str = ""


class MappingPage(BaseModel):
    rows: list[MappingRow]
    total: int


class Resolution(BaseModel):
    code: str | None = None
    name: str | None = None
    how: str
    candidates: list[tuple[str, str | None]]


class AliasIn(BaseModel):
    provider: Literal["aws", "azure", "gcp"]
    label: str
    code: str


class CostsOut(BaseModel):
    runs: list[dict[str, Any]]
    jobs: list[dict[str, Any]]
    prices: dict[str, dict[str, float]]
    totals: dict[str, float]


# ------------------------------------------------------------------ helpers
def _cite(c: Cite | None) -> CiteOut:
    if c is None:
        return CiteOut(label="—")
    return CiteOut(label=c.label(), page=c.page, line=c.line, table=c.table, row=c.row, column=c.column, text=c.text)


def _v(x) -> str | None:
    return None if x is None else str(x.value)


def _f(x) -> float | None:
    return None if x is None else float(x.value)


def _row(store: Store, sha: str) -> dict:
    """The document's list row (invoice-parser's fields), from billparse's document row and its contract summary."""
    row = store.document(sha)
    if row is None:
        raise HTTPException(404, "No document with this SHA-256.")
    return row_dict(store, row)


def row_dict(store: Store, row, runs: list[dict] | None = None) -> dict:
    summary = row.summary or {}
    c = summary.get("contract") or {}
    last = (runs if runs is not None else store.runs(row.sha256))[-1:] or [{}]
    t = _tasks.get(row.sha256)
    running = t is not None and not t["future"].done()
    return {"sha256": row.sha256, "name": row.name, "fmt": row.file_type, "uploaded_at": row.uploaded_at,
            "status": "processing" if running and not summary else contract.status_of(row.status),
            "completeness": c.get("completeness") or summary.get("completeness"),
            "lane": c.get("lane") or contract.lane_of(summary.get("spec")),
            "elapsed_ms": int((row.seconds or 0) * 1000) if row.seconds is not None else None,
            "model_calls": row.llm_calls, "reviewed_at": row.reviewed_at,
            "money_at_risk": c.get("money_at_risk"), "uploaded_by": row.uploaded_by,
            "total_ms": last[0].get("total_ms"), "reviewer_note": row.reviewer_note}


def ensure_contract(store: Store, sha: str) -> None:
    """A result parsed before the contract was kept: build it now from the stored lines and file."""
    if store.part(sha, "doc"):
        return
    summary = store.get_result(sha)
    if not summary:
        raise HTTPException(409, "This file has no result yet (it is being parsed, or its run was interrupted).")
    file_doc = bigfile.stored_doc(store, sha)          # a large file: its first rows only
    rows = store.get_rolled(sha) if summary.get("large") else store.get_rows(sha)
    doc, meta, bundle = contract.build(summary, rows, file_doc)
    store.save_part(sha, "doc", doc.model_dump_json(exclude_none=True))
    store.save_part(sha, "meta", json.dumps(meta, default=str))
    store.save_part(sha, "bundle", json.dumps(bundle, default=str))


def _load(store: Store, sha: str, gold: bool = True) -> tuple[Document, bool]:
    """The approved version when there is one (and gold), else the parsed result; and whether it is the approved."""
    if gold and (text := store.part(sha, "gold")):
        return Document.model_validate_json(text), True
    ensure_contract(store, sha)
    return Document.model_validate_json(store.part(sha, "doc") or "{}"), False


def _meta(store: Store, sha: str) -> dict:
    return json.loads(store.part(sha, "meta") or "{}")


def _note(n) -> NoteRow:
    if isinstance(n, dict):
        where = n.get("cite") or n.get("ref")
        return NoteRow(message=str(n.get("message", "")), where=str(where) if where else None)
    return NoteRow(message=str(n))


def charge_row(c, gap_refs: set) -> ChargeRow:
    kind_by = (f"{c.kind.source or ''} {c.kind.confidence:.2f}".strip() if c.kind.confidence is not None
               else (c.kind.source or ""))
    return ChargeRow(
        id=c.id, description=str(c.description.value), cost=_f(c.cost), cost_exact=_v(c.cost), basis=c.basis,
        list_cost=_f(c.list_cost), contracted_cost=_f(c.contracted_cost), effective_cost=_f(c.effective_cost),
        coverage=_v(c.coverage),
        counted=c.counted, kind=str(c.kind.value), kind_by=kind_by, service=_v(c.context.service),
        service_code=_v(c.service_code), region=_v(c.context.region), account=_v(c.context.account_id),
        instance_type=_v(c.instance_type), operating_system=_v(c.operating_system),
        database_engine=_v(c.database_engine), deployment_option=_v(c.deployment_option),
        pricing_model=_v(c.pricing_model), quantity=_f(c.quantity), unit=_v(c.unit), unit_price=_f(c.unit_price),
        section=c.section_id, source=c.cite.label(), cite=_cite(c.cost.cite if c.cost else c.cite),
        has_gap=c.id in gap_refs)


def view_of(doc: Document, row: dict, meta: dict, *, showing_reviewed: bool = False,
            runs: list[dict] | None = None) -> DocumentView:
    gap_refs = {g.ref for g in doc.gaps if g.ref}
    return DocumentView(
        sha256=doc.sha256, name=row.get("name") or doc.source_name, fmt=row.get("fmt"), status=row.get("status") or "",
        lane=row.get("lane"), specs_used=list(meta.get("specs_used") or []), uploaded_at=row.get("uploaded_at"),
        reviewed_at=row.get("reviewed_at"),
        reviewer_note=row.get("reviewer_note"), uploaded_by=row.get("uploaded_by"), total_ms=row.get("total_ms"),
        elapsed_ms=row.get("elapsed_ms"), model_calls=row.get("model_calls"),
        showing_reviewed=showing_reviewed, doc_type=_v(doc.doc_type), provider=_v(doc.provider),
        currency=_v(doc.currency), period_start=_v(doc.billing_period_start), period_end=_v(doc.billing_period_end),
        page_count=doc.page_count, completeness=doc.completeness,
        confidence=ConfidenceOut(**doc.confidence.model_dump()) if doc.confidence else None,
        cost_types=list(doc.cost_types), cost_basis=doc.cost_basis,
        counted_total=str(doc.counted_total()), money_at_risk=str(contract.money_at_risk(doc)),
        charges=[charge_row(c, gap_refs) for c in doc.charges],
        totals=[TotalRow(id=t.id, label=str(t.label.value), printed=str(t.amount.value),
                         computed=None if t.computed is None else str(t.computed), status=t.status, level=t.level,
                         section=t.section_id, lines=len(t.covers), cite=_cite(t.amount.cite)) for t in doc.stated_totals],
        gaps=[GapRow(index=i, kind=g.kind, message=g.message, field=g.field, ref=g.ref,
                     amount=None if g.amount is None else str(g.amount), candidates=g.candidates,
                     cite=_cite(g.cite) if g.cite else None) for i, g in enumerate(doc.gaps)],
        flags=[_note(f) for f in meta.get("flags") or []],
        notes=[_note(n) for n in meta.get("notes") or []],
        facts=[FactRow(label=f.label, amount=str(f.amount.value), currency=f.currency, period=f.period,
                       cite=_cite(f.cite)) for f in doc.stated_facts],
        commitments=[CommitmentRow(type=str(k.commitment_type.value), id=_v(k.commitment_id), term=_v(k.term),
                                   payment=_v(k.payment_option), amount=_v(k.amount), charges=k.charge_ids)
                     for k in doc.commitments],
        runs=[RunOut.model_validate({**{k: r.get(k) for k in RunOut.model_fields},
                                     "elapsed_ms": r.get("elapsed_ms") or 0, "model_calls": r.get("model_calls") or 0})
              for r in runs or []],
        decider=meta.get("decider"), version=meta.get("version"),
        timings_ms={k: int(v) for k, v in (meta.get("timings_ms") or {}).items()},
        tables=meta.get("tables") or [], llm_calls=meta.get("llm_calls") or [],
        decider_errors=meta.get("decider_errors") or [],
        learned_spec=meta.get("learned_spec"), improved_spec=meta.get("improved_spec"),
        cost_usd={k: float(v) for k, v in (meta.get("cost_usd") or {}).items()})


# ------------------------------------------------------------------ jobs (billparse's agent jobs, as invoice-parser's)
JOB_STATUS = {"queued": "queued", "running": "running", "stopping": "running", "proved": "saved",
              "rejected": "rejected", "unproved": "failed", "limit": "failed", "stopped": "failed", "error": "failed"}
SPEC_STATUS = {"builtin": "trusted", "verified": "trusted", "candidate": "draft", "disabled": "disabled"}


def job_out(store: Store, j: dict, report: bool = False) -> dict:
    spec = store.get_spec(j["spec_id"]) if j.get("spec_id") else None
    failed = JOB_STATUS.get(j["status"]) == "failed"
    out = {"id": j["id"], "layout_key": j["layout_key"].replace("repair:", "improve:", 1), "sha256": j.get("sha256"),
           "name": j.get("name") or "", "status": JOB_STATUS.get(j["status"], j["status"]), "spec_id": j.get("spec_id"),
           "spec_status": SPEC_STATUS.get((spec or {}).get("trust") or "") if spec else None,
           "attempts": j.get("attempts") or 0, "created": j["created"], "updated": j["updated"],
           "error": j.get("error") or (j.get("note") if failed else None), "cost_usd": j.get("cost_usd"),
           "started": j.get("started"), "seconds": j.get("seconds")}
    if report:
        out["report"] = j.get("report")
    return out


def list_jobs(store: Store) -> list[dict]:
    return [job_out(store, j) for j in sorted(store.agent_jobs(), key=lambda j: -j["id"])]


def stop_job(store: Store, job_id: int) -> str | None:
    j = store.agent_job(job_id)
    if j is None:
        return None
    if j["status"] == "queued":
        store.update_agent_job(job_id, status="stopped", note="Stopped by a person before it ran")
    elif j["status"] == "running":
        store.update_agent_job(job_id, status="stopping")
    return JOB_STATUS.get((store.agent_job(job_id) or j)["status"], j["status"])


# ------------------------------------------------------------------ settings
@router.get("/settings", response_model=Settings, tags=["web"])
def settings() -> Settings:
    key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    # Backend: the model and effort of the spec route; Fallback: the agent's ("claude": the .env settings)
    return Settings(decision_backend="claude" if llm_wanted() else "none",
                    decision_fallback="claude" if agent.enabled() else "none",
                    backends=["claude", "none", *llm_mod.llm_choices()], keys={"jev": False, "claude": key},
                    learner_enabled=agent.enabled(), max_upload_mb=MAX_UPLOAD_MB, kinds=list(KINDS),
                    doc_types=list(DOC_TYPES), checklist=[{"key": k, "text": t} for k, t in CHECKLIST])


# ------------------------------------------------------------------ parsing in the background
# A parse can take minutes (a layout spec is learned or improved): the browser starts it, then polls its task.
_pool = ThreadPoolExecutor(max_workers=int(os.environ.get("PARSE_WORKERS", "2")), thread_name_prefix="parse")
# large files (bigfile.py) in a pool of their own, so they never hold up small files: LARGE_PARSE_WORKERS at a time
_large_pool = ThreadPoolExecutor(max_workers=int(os.environ.get("LARGE_PARSE_WORKERS", "1")),
                                 thread_name_prefix="parse-large")
_tasks: dict[str, dict] = {}
_lock = threading.Lock()


def _task_out(store: Store, sha: str, t: dict) -> ParseTask:
    fut: Future = t["future"]
    out = ParseTask(sha256=sha, name=t["name"], state="running", started=t["started"], started_by=t.get("by"))
    if not fut.done():
        out.progress = _progress(store, sha, t["started"]) or t.get("progress")
        return out
    out.finished = t.get("finished")
    if (e := fut.exception()) is not None:
        out.state, out.error = "error", f"{type(e).__name__}: {e}"
        return out
    r: dict = fut.result()
    c = r.get("contract") or {}
    row = store.document(sha)
    job = store.agent_job_for(sha) if not r.get("cached") else None
    out.state = "done"
    out.status = contract.status_of(row.status if row else r.get("status"))
    out.lane, out.completeness, out.confidence = c.get("lane"), c.get("completeness"), c.get("confidence")
    out.charges, out.gaps = c.get("charges"), c.get("gaps")
    out.counted_total = c.get("counted_total") if c.get("charges") else None
    out.elapsed_ms = int((r.get("seconds") or 0) * 1000)
    out.model_calls, out.cached = int(r.get("llm_calls") or 0), bool(r.get("cached"))
    out.total_ms = int(((t.get("finished") or time.time()) - t["started"]) * 1000) if not r.get("cached") else None
    ran = job if job and (job.get("started") or 0) >= t["started"] - 1 else None
    out.cost_usd = round(ran["cost_usd"], 6) if ran else None
    return out


def _progress(store: Store, sha: str, since: float) -> str | None:
    """What the learning or improving job of this file is doing now (from its trace), or None."""
    job = store.agent_job_for(sha)
    if not job or job["status"] not in ("running", "stopping") or job["updated"] < since - 1:
        return None
    events = agent.read_trace(store, job["id"])
    kind = "Improving the layout spec" if job["layout_key"].startswith("repair:") else "Learning a layout spec"
    rounds = max((e.get("round", 0) for e in events), default=0)
    tools = [e for e in events if e["event"] == "tool"]
    checks = [e for e in tools if e.get("name") == "check_spec"]
    cost = sum(e.get("cost_usd") or 0 for e in events if e["event"] == "response")
    parts = [f"{kind} (job {job['id']})", f"{int(time.time() - (job.get('started') or job['created']))} s"]
    if rounds:
        parts.append(f"round {rounds}")
    parts.append(f"{len(tools)} tool call(s), {len(checks)} check(s), about ${cost:.2f}")
    if checks:
        parts.append("last check: " + str(checks[-1].get("result") or "").split("\n")[0][:90])
    return " · ".join(parts)


def _use_llm(backend: str | None) -> bool:
    if backend is None:
        return llm_wanted()
    return backend != "none" and bool(os.environ.get("ANTHROPIC_API_KEY"))


def start_parse(store: Store, name: str, path: Path, sha: str, force: bool, use_llm: bool, user: str | None = None,
                agent_step: bool = True, backend: str | None = None, fallback: str | None = None,
                temp: bool = False) -> str:
    """Parse the file at path (its SHA-256 is sha) in the background. temp: path is an upload, moved into the store
    or removed when the parse ends."""
    large = bigfile.classify(path, name).large
    with _lock:
        t = _tasks.get(sha)
        if t and not t["future"].done():
            if temp:
                path.unlink(missing_ok=True)
            return sha                                   # already being parsed: the same task
        pipe = _pipeline(store, use_llm, backend or None, fallback or None)
        task: dict = {"name": name, "started": time.time(), "by": user, "progress": "queued" if large else None}

        def step(s: str) -> None:
            task.update(step=s)
            if s.startswith("reading"):
                task["progress"] = s[0].upper() + s[1:]

        def run() -> dict:
            task["progress"] = "reading" if large else None
            try:
                result = pipe.parse_path(path, name, force=force, agent=agent_step, user=user, on_step=step,
                                         move=temp, sha=sha)
                row = store.document(sha)
                if row is not None and user and not row.uploaded_by:
                    store.update_document(sha, uploaded_by=user)
                return result
            except Exception:
                traceback.print_exc()
                raise
            finally:
                task["finished"] = time.time()
                if temp:
                    path.unlink(missing_ok=True)
        task["future"] = (_large_pool if large else _pool).submit(run)
        _tasks[sha] = task
    return sha


@router.post("/parse/start", response_model=ParseTask, tags=["web"])
def parse_start(request: Request, file: UploadFile = File(...), force: bool = Query(False),
                backend: str | None = Query(None), fallback: str | None = Query(None),
                store: Store = Depends(get_store)) -> ParseTask:
    """Start parsing a file and return at once; GET /parse/tasks/{sha256} says how it goes."""
    for choice in (backend, fallback):
        if choice and choice not in ("claude", "none") and llm_mod.parse_choice(choice) is None:
            raise HTTPException(400, f"Unknown model choice {choice!r}; known: claude, none, "
                                     f"{', '.join(llm_mod.llm_choices())}")
    path, sha, _ = save_upload(file, store)
    sha = start_parse(store, file.filename or "upload", path, sha, force, _use_llm(backend), auth.current_user(request),
                      agent_step=fallback != "none", backend=backend, fallback=fallback, temp=True)
    return _task_out(store, sha, _tasks[sha])


@router.get("/parse/tasks", response_model=list[ParseTask], tags=["web"])
def parse_tasks(store: Store = Depends(get_store)) -> list[ParseTask]:
    return [_task_out(store, sha, t) for sha, t in sorted(_tasks.items(), key=lambda x: -x[1]["started"])]


@router.get("/parse/tasks/{sha256}", response_model=ParseTask, tags=["web"])
def parse_task(sha256: str, store: Store = Depends(get_store)) -> ParseTask:
    t = _tasks.get(sha256)
    if t is None:
        raise HTTPException(404, "No parse of this file was started since the API started.")
    return _task_out(store, sha256, t)


def _stored(store: Store, sha: str) -> Path:
    path = store.file_path(sha)
    if path is None:
        raise HTTPException(404, "The uploaded file is not stored.")
    return path


@router.post("/documents/{sha256}/reparse", response_model=ParseTask, tags=["web"])
def reparse(sha256: str, request: Request, store: Store = Depends(get_store)) -> ParseTask:
    """Parse the stored file again and run the agent for it again (a new spec, or an improve round on the spec that
    reads it), in the background. Paid when learning is on."""
    row = _row(store, sha256)
    path = _stored(store, sha256)
    for j in store.agent_jobs():                     # this file's last job runs again on this parse
        if j["sha256"] == sha256 and j["status"] not in ("queued", "running", "stopping"):
            store.retry_agent_job(j["id"])
    sha = start_parse(store, row["name"], path, sha256, True, llm_wanted(), auth.current_user(request))
    return _task_out(store, sha, _tasks[sha])


@router.post("/documents/{sha256}/stop", tags=["web"])
def stop_document(sha256: str, store: Store = Depends(get_store)) -> dict[str, list[int]]:
    """Stop the learning or improving run of this file (the parse itself takes seconds and is not stopped)."""
    _row(store, sha256)
    stopped = [j["id"] for j in store.agent_jobs() if j["sha256"] == sha256 and j["status"] in ("queued", "running")]
    for job_id in stopped:
        stop_job(store, job_id)
    return {"stopped": stopped}


@router.post("/documents/{sha256}/parse-current", response_model=ParseTask, tags=["web"])
def parse_current(sha256: str, request: Request, store: Store = Depends(get_store)) -> ParseTask:
    """Parse the stored file again with the specs and code of today, in the background: no agent and no model
    request, so it is free."""
    row = _row(store, sha256)
    path = _stored(store, sha256)
    sha = start_parse(store, row["name"], path, sha256, True, False, auth.current_user(request), agent_step=False)
    return _task_out(store, sha, _tasks[sha])


# ------------------------------------------------------------------ documents
@router.get("/documents/{sha256}/view", response_model=DocumentView, tags=["web"])
def document_view(sha256: str, store: Store = Depends(get_store)) -> DocumentView:
    """Everything the result page shows, with every charge (the browser pages and filters them)."""
    row = _row(store, sha256)
    doc, gold = _load(store, sha256)
    return view_of(doc, row, _meta(store, sha256), showing_reviewed=gold, runs=store.runs(sha256))


@router.get("/documents/{sha256}/charges/{charge_id}", response_model=ChargeDetail, tags=["web"])
def charge_detail(sha256: str, charge_id: str, store: Store = Depends(get_store)) -> ChargeDetail:
    doc, _ = _load(store, sha256)
    c = next((c for c in doc.charges if c.id == charge_id), None)
    if c is None:
        raise HTTPException(404, "No charge with this id.")
    named = {"cost": c.cost, "currency": c.currency, "kind": c.kind, "service": c.context.service,
             "service_code": c.service_code, "region": c.context.region, "account": c.context.account_id,
             "instance_type": c.instance_type, "operating_system": c.operating_system,
             "database_engine": c.database_engine, "deployment_option": c.deployment_option,
             "usage_type": c.usage_type, "operation": c.operation, "quantity": c.quantity, "unit": c.unit,
             "unit_price": c.unit_price, "pricing_model": c.pricing_model, "commitment_id": c.commitment_id,
             "commitment_type": c.commitment_type, "period_start": c.period_start, "period_end": c.period_end,
             "marketplace_seller": c.marketplace_seller, "sku_id": c.sku_id, "meter_id": c.meter_id,
             "list_cost": c.list_cost, "contracted_cost": c.contracted_cost, "effective_cost": c.effective_cost,
             "coverage": c.coverage, "commitment_status": c.commitment_status, "line_type_raw": c.line_type_raw}
    fields = [FieldOut(field=k, value=str(v.value), origin=v.origin, source=v.source, confidence=v.confidence,
                       cite=_cite(v.cite)) for k, v in named.items() if v is not None]
    return ChargeDetail(id=c.id, description=str(c.description.value), cite=_cite(c.cite), fields=fields,
                        attributes=json.loads(json.dumps(c.attributes, default=str)))


@router.delete("/documents/{sha256}", tags=["web"])
def delete_document(sha256: str, store: Store = Depends(get_store)) -> dict:
    """Delete the result, its runs and the uploaded file. Layout specs and jobs stay."""
    row = _row(store, sha256)
    t = _tasks.get(sha256)
    if row["status"] == "processing" or (t and not t["future"].done()):
        raise HTTPException(409, "This file is being parsed; delete it when the parse ends.")
    store.delete_document(sha256)
    _tasks.pop(sha256, None)
    return {"deleted": sha256}


# ------------------------------------------------------------------ source viewer
def _bundle(store: Store, sha: str) -> dict:
    ensure_contract(store, sha)
    return json.loads(store.part(sha, "bundle") or "{}")


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, Decimal):
        return format(v, "f")
    return str(v)


def _col_name(i: int, letters: bool) -> str:
    if not letters:
        return str(i + 1)
    s, n = "", i + 1
    while n:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s


@router.get("/documents/{sha256}/source", response_model=SourceInfo, tags=["web"])
def source_info(sha256: str, store: Store = Depends(get_store)) -> SourceInfo:
    _row(store, sha256)
    b = _bundle(store, sha256)
    path = store.file_path(sha256)
    return SourceInfo(fmt=b.get("fmt", ""), page_count=int(b.get("page_count") or 1),
                      has_pdf=b.get("fmt") == "pdf" and path is not None,
                      tables=[TableInfo(name=t["name"], row_offset=t.get("row_offset", 1),
                                        col_letters=bool(t.get("col_letters")), truncated=bool(t.get("truncated")),
                                        rows=len(t.get("rows") or [])) for t in b.get("tables", [])],
                      lines=len(b.get("lines") or []))


@router.get("/documents/{sha256}/source/table", response_model=SourceTable, tags=["web"])
def source_table(sha256: str, name: str, offset: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=2000),
                 store: Store = Depends(get_store)) -> SourceTable:
    """Rows `offset`..`offset+limit` of one table (0-based in the table), each with the role the parser gave it.
    Rows past the stored 5,000 of a large table are read from the uploaded file."""
    b = _bundle(store, sha256)
    t = next((t for t in b.get("tables", []) if t["name"] == name), None)
    if t is None:
        raise HTTPException(404, "No table with this name.")
    rows, from_file, total = t.get("rows") or [], False, len(t.get("rows") or [])
    if t.get("truncated") and offset + limit > len(rows) and (file_doc := bigfile.stored_doc(store, sha256)):
        region = next((r for r in file_doc.regions if r.name == name), None)   # a large file: its first rows
        if region is not None:
            full = contract.bundle_of_region(region, None)
            rows, from_file, total = full["rows"], True, len(full["rows"])
    doc, _ = _load(store, sha256)
    found = highlight.roles(doc, _meta(store, sha256))
    off = t.get("row_offset", 1)
    page = rows[offset:offset + limit]
    width = max((len(r) for r in page), default=0)
    return SourceTable(name=name, row_offset=off, col_letters=bool(t.get("col_letters")),
                       columns=[_col_name(i, bool(t.get("col_letters"))) for i in range(width)], total_rows=total,
                       offset=offset, truncated=bool(t.get("truncated")), from_file=from_file,
                       rows=[SourceRow(row=offset + i + off, cells=[_cell(v) for v in r] + [""] * (width - len(r)),
                                       role=highlight.style(found.get(("table", name, offset + i + off), set())))
                             for i, r in enumerate(page)])


@router.get("/documents/{sha256}/source/lines", response_model=list[SourceLine], tags=["web"])
def source_lines(sha256: str, page: int | None = None, store: Store = Depends(get_store)) -> list[SourceLine]:
    b = _bundle(store, sha256)
    doc, _ = _load(store, sha256)
    found = highlight.roles(doc, _meta(store, sha256))
    return [SourceLine(page=ln["page"], line=ln["line"], text=ln["text"],
                       role=highlight.style(found.get((ln["page"], ln["line"]), set())),
                       top=ln["bbox"][1] if ln.get("bbox") else None)
            for ln in b.get("lines", []) if page is None or ln["page"] == page]


@router.get("/documents/{sha256}/source/page.png", tags=["web"], response_class=Response,
            responses={200: {"content": {"image/png": {}}}})
def source_page(sha256: str, page: int = Query(1, ge=1), focus_line: int | None = None,
                colored: bool = True, store: Store = Depends(get_store)) -> Response:
    """A PDF page as an image: every line boxed by what the parser made of it, `focus_line` in yellow."""
    path = store.file_path(sha256)
    if path is None or path.suffix.lower() != ".pdf":
        raise HTTPException(404, "This document has no stored PDF.")
    b = _bundle(store, sha256)
    lines = [ln for ln in b.get("lines", []) if ln["page"] == page]
    if colored:
        doc, _ = _load(store, sha256)
        meta = _meta(store, sha256)
    else:
        doc, meta = Document(source_name="", sha256=sha256), {}
    img = highlight.render_page(path, page, lines, doc, meta, focus=(page, focus_line) if focus_line else None)
    if img is None:
        raise HTTPException(404, "No such page.")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=False)
    return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "private, max-age=300"})


@router.get("/highlight/legend", tags=["web"])
def highlight_legend() -> dict:
    return {"legend": highlight.LEGEND, "row_colors": highlight.ROW_COLORS, "order": highlight.ORDER}


# ------------------------------------------------------------------ review
@router.get("/review/queue", response_model=list[DocumentItem], tags=["web"])
def review_queue(store: Store = Depends(get_store)) -> list[DocumentItem]:
    """Documents waiting for review, most money at risk first."""
    runs: dict[str, list[dict]] = {}
    for r in store.runs():
        runs.setdefault(r["sha256"], []).append(r)
    out = []
    for row in store.list_documents():
        if contract.status_of(row.status) != "needs_review":
            continue
        d = row_dict(store, row, runs.get(row.sha256, []))
        if d.get("money_at_risk") is None:
            doc, _ = _load(store, row.sha256, gold=False)
            d["money_at_risk"] = float(contract.money_at_risk(doc))
        out.append(DocumentItem(**d))
    return sorted(out, key=lambda d: (-(d.money_at_risk or 0), d.uploaded_at or 0))


def _edited(store: Store, sha: str, edits: ReviewEdits) -> tuple[Document, dict, dict, dict]:
    """The stored document with a person's edits applied (nothing saved): (document, changes, row, meta)."""
    row = _row(store, sha)
    doc, _ = _load(store, sha)
    meta = _meta(store, sha)
    if edits.dropped_gaps:
        doc = drop_gaps(doc, edits.dropped_gaps)
    rows = [dict(r, **edits.rows.get(r["id"], {})) for r in charge_rows(doc)] + list(edits.added)
    doc, changes = apply_edits(doc, rows)
    h = edits.header
    if h is not None:
        def keep(old, value, cite=None):
            return cv(value, (old.cite if old else None) or cite or Cite(text="set in review"), "inferred", 1.0, "human")
        if h.doc_type and h.doc_type != _v(doc.doc_type):
            doc.doc_type = keep(doc.doc_type, h.doc_type)
        if h.provider and h.provider != _v(doc.provider):
            doc.provider = keep(doc.provider, h.provider)
        if h.currency and h.currency.upper() != _v(doc.currency):
            doc.currency = keep(doc.currency, h.currency.upper())
        if h.period_start and h.period_start != _v(doc.billing_period_start):
            doc.billing_period_start = keep(None, h.period_start)
        if h.period_end and h.period_end != _v(doc.billing_period_end):
            doc.billing_period_end = keep(None, h.period_end)
        if h.basis:
            for c in doc.charges:
                if c.cost is not None:
                    c.basis = h.basis
    return doc, changes, row, meta


@router.post("/documents/{sha256}/review/preview", response_model=ReviewPreview, tags=["web"])
def review_preview(sha256: str, edits: ReviewEdits, store: Store = Depends(get_store)) -> ReviewPreview:
    """Apply the edits and check the totals again; nothing is saved."""
    doc, changes, row, meta = _edited(store, sha256, edits)
    return ReviewPreview(view=view_of(doc, row, meta), changes=json.loads(json.dumps(changes, default=str)))


@router.post("/documents/{sha256}/review/approve", tags=["web"])
def review_approve(sha256: str, req: ApproveRequest, request: Request, store: Store = Depends(get_store)) -> dict:
    """Approve the document with the edits: it goes to the gold set, its candidate spec is verified when no charge
    changed, corrected service codes become synonyms. Every checklist item must be ticked."""
    missing = [k for k, _ in CHECKLIST if k not in req.checklist]
    if missing:
        raise HTTPException(400, f"Tick every checklist item first (missing: {', '.join(missing)}).")
    doc, changes, _, _ = _edited(store, sha256, req)
    approve(store, sha256, doc, changes, req.note, auth.current_user(request))
    return {"sha256": sha256, "status": "approved"}


# ------------------------------------------------------------------ learning jobs
@router.get("/learn/jobs/{job_id}/trace", tags=["web"])
def job_trace_events(job_id: int, store: Store = Depends(get_store)) -> list[dict[str, Any]]:
    """Every step of the job: what Claude read, its reasoning summary, each spec it checked and the result."""
    if store.agent_job(job_id) is None:
        raise HTTPException(404, "No learning run with this id.")
    return agent.read_trace(store, job_id)


# ------------------------------------------------------------------ costs
@router.get("/costs", response_model=CostsOut, tags=["web"])
def costs(store: Store = Depends(get_store)) -> CostsOut:
    runs = store.runs()
    jobs = list_jobs(store)
    claude = sum(r.get("claude_cost_usd") or 0 for r in runs)
    learn = sum(j.get("cost_usd") or 0 for j in jobs)
    return CostsOut(runs=list(reversed(runs)), jobs=jobs,
                    prices={m: {"input": i, "output": o} for m, (i, o) in agent.PRICES.items()},
                    totals={"total": claude + learn, "jev": 0.0, "claude": claude, "learning": learn})


# ------------------------------------------------------------------ service map (billparse's catalog)
def _map_rows(res) -> list[MappingRow]:
    names: dict[str, list[str]] = {}
    shared: dict[str, set[str]] = {}
    for table in (res.cat.names, res.cat.products):
        for k, key in table.items():
            _, _, n = k.partition(":")
            names.setdefault(key, []).append(n)
            shared.setdefault(n, set()).add(key)
    rows = []
    for key, s in res.cat.services.items():
        own = sorted(set(names.get(key, [])))
        ambiguous = any(len(shared.get(n, ())) > 1 for n in own)
        rows.append(MappingRow(provider=s["provider"], code=s.get("code") or "", name=s["name"],
                               match="ambiguous" if ambiguous else "auto" if s.get("code") else "never",
                               aliases=[n for n in own if n != s["name"].lower()][:40],
                               note="" if s.get("code") else "REVIEW: no service code in the catalog"))
    return sorted(rows, key=lambda r: (r.provider, r.name.lower()))


@router.get("/mapping", response_model=MappingPage, tags=["web"])
def mapping(q: str = "", provider: str | None = None,
            view: Literal["all", "flagged", "ambiguous"] = "all", offset: int = Query(0, ge=0),
            limit: int = Query(200, ge=1, le=2000)) -> MappingPage:
    with RESOLVER_LOCK:
        rows = _map_rows(get_resolver(catalog_dir()))
    out = []
    for row in rows:
        if provider and row.provider != provider:
            continue
        if view == "flagged" and not (row.match == "never" or "REVIEW" in row.note or row.was):
            continue
        if view == "ambiguous" and row.match != "ambiguous":
            continue
        if q and q.casefold() not in " ".join([row.code, row.name, *row.aliases]).casefold():
            continue
        out.append(row)
    return MappingPage(rows=out[offset:offset + limit], total=len(out))


@router.get("/mapping/resolve", response_model=Resolution, tags=["web"])
def mapping_resolve(label: str, provider: str | None = None) -> Resolution:
    with RESOLVER_LOCK:
        res = get_resolver(catalog_dir())
        r = res.resolve(label, provider or None)
        cands = [((res.cat.services.get(k) or {}).get("code") or k, (res.cat.services.get(k) or {}).get("name"))
                 for k, _ in (r.candidates or [])]
    return Resolution(code=r.service_code if r.service_key else None, name=r.service, how=r.method, candidates=cands)


@router.get("/aliases", tags=["web"])
def aliases() -> list[dict[str, Any]]:
    with RESOLVER_LOCK:
        res = get_resolver(catalog_dir())
        created = res.syn_path.stat().st_mtime if res.syn_path.exists() else time.time()
        out = []
        for k, key in sorted(res.synonyms.items()):
            provider, _, label_norm = k.partition("|")
            s = res.cat.services.get(key) or {}
            out.append({"provider": s.get("provider") if provider == "*" else provider, "label": label_norm,
                        "label_norm": label_norm, "code": s.get("code") or key, "source": "review", "created": created,
                        "scope": provider})
    return out


@router.post("/aliases", tags=["web"])
def add_alias(alias: AliasIn) -> dict:
    with RESOLVER_LOCK:
        res = get_resolver(catalog_dir())
        key = res.key_for_code(alias.code, alias.provider)
        if key is None:
            raise HTTPException(400, f"{alias.code} is not a {alias.provider} code in the mapping.")
        res.add_synonym(alias.label, key, provider=alias.provider)
    return {"ok": True}


@router.delete("/aliases/{provider}/{label_norm}", tags=["web"])
def delete_alias(provider: str, label_norm: str) -> dict:
    with RESOLVER_LOCK:
        res = get_resolver(catalog_dir())
        if not res.remove_synonym(provider, label_norm):
            res.remove_synonym(None, label_norm)
    return {"ok": True}
