"""Storage: rows in the database (db.py), large parts as files under the data directory.

Database (PostgreSQL with DATABASE_URL, else <root>/billparse.db):
  documents                 one row per uploaded file: its latest result summary, review state, approved version
  spec, spec_version        every version of every learned spec (never overwritten), and which one is current
  spec_doc, spec_fingerprint  which files and layouts each spec parsed (its regression set)
  journal                   records (one per parse), labels (row classifier data), review (the review queue)
  app_users                 people admitted to the web app (auth.py)

Files under <root>:
  docs/<sha>.<ext>          raw uploads kept for regression tests and for parsing again (see retain_docs)
  results/<sha>.parquet     canonical rows; results/<sha>.approved.parquet the rows after review
  parts/<sha>.<part>.json   the result in invoice-parser's contract (doc, gold after review), its meta, the source
                            bundle the viewer reads (contract.py)
  models/, nightly/         the row classifier and the nightly reports
"""

from __future__ import annotations

import json
import math
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import delete, func
from sqlmodel import Session, col, select

from .db import (
    AgentJobRow,
    DocumentRow,
    JournalRow,
    SpecDocRow,
    SpecFingerprintRow,
    SpecRow,
    SpecVersionRow,
    default_url,
    engine_for,
)

BUILTIN_DIR = Path(__file__).resolve().parents[1] / "specs" / "builtin"


def jsonable(value: Any) -> Any:
    """A value that every database stores as JSON: numpy and dates as text, NaN and infinity as null."""
    def clean(v: Any) -> Any:
        if isinstance(v, float) and not math.isfinite(v):
            return None
        if isinstance(v, dict):
            return {str(k): clean(x) for k, x in v.items()}
        if isinstance(v, list):
            return [clean(x) for x in v]
        return v
    return clean(json.loads(json.dumps(value, default=str)))


class Store:
    def __init__(self, root: str | Path = "store", retain_docs: bool = True, url: str | None = None):
        self.root = Path(root)
        self.retain_docs = retain_docs
        for d in ("docs", "results", "parts"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.url = url or default_url(self.root)
        self.engine = engine_for(self.url)
        self._builtin = {s["id"]: s for s in (json.loads(p.read_text()) for p in sorted(BUILTIN_DIR.glob("*.json")))}

    @contextmanager
    def session(self) -> Iterator[Session]:
        with Session(self.engine, expire_on_commit=False) as s:
            yield s

    # ---------------- specs
    def builtin_specs(self, include_disabled: bool = True) -> list[dict]:
        off = {k for k, v in self.builtin_status().items() if v == "disabled"} if not include_disabled else set()
        return [s for s in self._builtin.values() if s["id"] not in off]

    def builtin_status(self) -> dict[str, str]:
        """A person's status for built-in specs (trusted unless set): spec_status.json in the data directory."""
        p = self.root / "spec_status.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def set_builtin_status(self, spec_id: str, status: str) -> None:
        current = self.builtin_status()
        current[spec_id] = status
        (self.root / "spec_status.json").write_text(json.dumps(current, indent=1, sort_keys=True))

    def learned_specs(self, include_disabled: bool = False) -> list[dict]:
        """The current version of every learned spec; a disabled one only when asked (parsing never uses it)."""
        with self.session() as s:
            rows = s.exec(select(SpecVersionRow).join(
                SpecRow, (col(SpecRow.id) == col(SpecVersionRow.id))
                & (col(SpecRow.current_version) == col(SpecVersionRow.version))).order_by(col(SpecVersionRow.id))).all()
        return [r.spec for r in rows if include_disabled or r.spec.get("trust") != "disabled"]

    def get_spec(self, spec_id: str, version: int | None = None) -> dict | None:
        if spec_id in self._builtin:
            return self._builtin[spec_id]
        with self.session() as s:
            if version is None:
                current = s.get(SpecRow, spec_id)
                if current is None:
                    return None
                version = current.current_version
            row = s.get(SpecVersionRow, (spec_id, version))
        return row.spec if row else None

    def save_spec(self, spec: dict, make_current: bool = True) -> dict:
        with self.session() as s:
            last = s.exec(select(func.max(SpecVersionRow.version)).where(col(SpecVersionRow.id) == spec["id"])).one()
            spec = jsonable(dict(spec, version=(last or 0) + 1, created_at=time.time()))
            s.add(SpecVersionRow(id=spec["id"], version=spec["version"], spec=spec, created_at=spec["created_at"]))
            if make_current:
                s.merge(SpecRow(id=spec["id"], current_version=spec["version"], trust=spec.get("trust"),
                                updated_at=time.time()))
            s.commit()
        return spec

    def set_current(self, spec_id: str, version: int) -> None:
        with self.session() as s:
            row = s.get(SpecVersionRow, (spec_id, version))
            if row is None:
                raise KeyError(f"No version {version} of spec {spec_id}")
            s.merge(SpecRow(id=spec_id, current_version=version, trust=row.spec.get("trust"), updated_at=time.time()))
            s.commit()

    def versions(self, spec_id: str) -> list[int]:
        with self.session() as s:
            return sorted(s.exec(select(SpecVersionRow.version).where(col(SpecVersionRow.id) == spec_id)).all())

    def spec_history(self, spec_id: str) -> list[dict]:
        with self.session() as s:
            rows = s.exec(select(SpecVersionRow).where(col(SpecVersionRow.id) == spec_id)
                          .order_by(col(SpecVersionRow.version).desc())).all()
        return [{"version": r.version, "created_at": r.created_at, "spec": r.spec} for r in rows]

    def current_versions(self) -> dict[str, int]:
        with self.session() as s:
            return {r.id: r.current_version for r in s.exec(select(SpecRow)).all()}

    # ---------------- which docs each spec has parsed (regression set)
    def link(self, spec_id: str, sha: str, fingerprint: dict, accepted: dict) -> None:
        with self.session() as s:
            s.merge(SpecDocRow(spec_id=spec_id, sha256=sha, **accepted))      # {"rows": n, "total": x, "confidence": c}
            if s.get(SpecFingerprintRow, (spec_id, fingerprint["hash"])) is None:
                s.add(SpecFingerprintRow(spec_id=spec_id, hash=fingerprint["hash"], fingerprint=jsonable(fingerprint)))
            s.commit()

    def spec_docs(self, spec_id: str) -> dict[str, dict]:
        with self.session() as s:
            rows = s.exec(select(SpecDocRow).where(col(SpecDocRow.spec_id) == spec_id)).all()
        return {r.sha256: {"rows": r.rows, "total": r.total, "confidence": r.confidence, "checks": r.checks or {}}
                for r in rows}

    def fingerprints(self) -> list[tuple[str, dict]]:
        with self.session() as s:
            return [(r.spec_id, r.fingerprint) for r in s.exec(select(SpecFingerprintRow)).all()]

    # ---------------- raw uploads
    def save_doc(self, sha: str, data: bytes, name: str) -> None:
        if self.retain_docs:
            ext = Path(name).suffix or ".bin"
            p = self.root / "docs" / f"{sha}{ext}"
            if not p.exists():
                p.write_bytes(data)
                (self.root / "docs" / f"{sha}.name").write_text(name)

    def save_doc_path(self, sha: str, path: Path, name: str, move: bool = False) -> None:
        """save_doc for a file on disk (a large upload is never read into memory): moved (move) or copied."""
        if self.retain_docs:
            p = self.root / "docs" / f"{sha}{Path(name).suffix or '.bin'}"
            if not p.exists() and Path(path).resolve() != p.resolve():
                tmp = p.with_name(p.name + ".part")
                (shutil.move if move else shutil.copyfile)(str(path), str(tmp))
                tmp.replace(p)
                (self.root / "docs" / f"{sha}.name").write_text(name)

    def temp_path(self, suffix: str = "") -> Path:
        """A new file name in the data directory's tmp folder (uploads on their way to docs/)."""
        d = self.root / "tmp"
        d.mkdir(exist_ok=True)
        return d / f"{time.time_ns()}{suffix}"

    def file_path(self, sha: str) -> Path | None:
        return next((p for p in (self.root / "docs").glob(f"{sha}.*") if p.suffix not in (".name", ".part")), None)

    def doc_name(self, sha: str) -> str | None:
        p = self.file_path(sha)
        if p is None:
            return None
        name_p = self.root / "docs" / f"{sha}.name"
        return name_p.read_text() if name_p.exists() else p.name

    def load_doc(self, sha: str) -> tuple[bytes, str] | None:
        """The stored file's bytes and name. A large file is never read whole: see bigfile.stored_doc."""
        p = self.file_path(sha)
        if p is None:
            return None
        return p.read_bytes(), self.doc_name(sha) or p.name

    # ---------------- results
    def save_result(self, sha: str, summary: dict, rows: pd.DataFrame | None, rows_path: Path | None = None) -> None:
        """The summary, and the rows: a frame, or a Parquet file already written (a large file, bigfile.py), which is
        moved in place."""
        path = self.root / "results" / f"{sha}.parquet"
        (self.root / "results" / f"{sha}.rolled.parquet").unlink(missing_ok=True)
        if rows_path is not None:
            shutil.move(str(rows_path), str(path))
        elif rows is not None and not rows.empty:
            rows.astype({c: "string" for c in rows.columns if rows[c].dtype == object}).to_parquet(path, index=False)
        else:
            path.unlink(missing_ok=True)
        a = summary.get("analysis") or {}
        now = time.time()
        with self.session() as s:
            row = s.get(DocumentRow, sha) or DocumentRow(sha256=sha, name=summary["file"], status="", uploaded_at=now)
            spec = summary.get("spec") or {}
            row.name, row.file_type, row.kind = summary["file"], summary.get("file_type"), summary.get("kind")
            # a reviewed document keeps its approved version (and status) when the file is parsed again
            row.status = "approved" if row.approved is not None else summary["status"]
            row.confidence, row.rows = summary.get("confidence"), int(summary.get("rows") or 0)
            row.route, row.spec_id, row.spec_version = spec.get("route"), spec.get("id"), spec.get("version")
            row.llm_calls, row.seconds = int(summary.get("llm_calls") or 0), summary.get("seconds")
            # no analysis on a result that was not accepted: its lines' sum is still the money to review
            total = a.get("total_billed")
            if total is None and rows is not None and "billed_cost" in rows:
                total = round(float(pd.to_numeric(rows["billed_cost"], errors="coerce").sum()), 2)
            row.total_billed, row.currency = total, a.get("currency")
            row.summary, row.parsed_at = jsonable(summary), now
            s.add(row)
            s.commit()

    def save_rolled(self, sha: str, rows: pd.DataFrame) -> None:
        """A large file's rolled-up lines (bigfile.py): what its contract was built from."""
        if rows is not None and not rows.empty:
            rows.astype({c: "string" for c in rows.columns if rows[c].dtype == object}).to_parquet(
                self.root / "results" / f"{sha}.rolled.parquet", index=False)

    def get_rolled(self, sha: str) -> pd.DataFrame | None:
        p = self.root / "results" / f"{sha}.rolled.parquet"
        return pd.read_parquet(p) if p.exists() else None

    def get_result(self, sha: str) -> dict | None:
        with self.session() as s:
            row = s.get(DocumentRow, sha)
        return dict(row.summary) if row and row.summary else None

    def get_rows(self, sha: str, approved: bool = False) -> pd.DataFrame | None:
        p = self.root / "results" / f"{sha}{'.approved' if approved else ''}.parquet"
        return pd.read_parquet(p) if p.exists() else None

    # ---------------- documents (web app)
    def document(self, sha: str) -> DocumentRow | None:
        with self.session() as s:
            return s.get(DocumentRow, sha)

    def list_documents(self) -> list[DocumentRow]:
        with self.session() as s:
            return list(s.exec(select(DocumentRow).order_by(col(DocumentRow.uploaded_at).desc())).all())

    def update_document(self, sha: str, **fields: Any) -> None:
        with self.session() as s:
            row = s.get(DocumentRow, sha)
            if row is None:
                raise KeyError(sha)
            for k, v in fields.items():
                setattr(row, k, v)
            s.add(row)
            s.commit()

    def save_approved(self, sha: str, rows: pd.DataFrame, approved: dict, by: str | None, note: str) -> None:
        """The rows and result after a person's review: kept beside the parsed result, shown instead of it."""
        rows.astype({c: "string" for c in rows.columns if rows[c].dtype == object}).to_parquet(
            self.root / "results" / f"{sha}.approved.parquet", index=False)
        self.update_document(sha, status="approved", approved=jsonable(approved), reviewed_at=time.time(),
                             reviewed_by=by, reviewer_note=note or None)
        self.rewrite("review", [dict(r, open=False) if r.get("sha") == sha else r for r in self.read("review")])

    def delete_document(self, sha: str) -> None:
        """The result, its rows, its journal entries and the uploaded file. Specs and their regression links stay."""
        with self.session() as s:
            s.exec(delete(DocumentRow).where(col(DocumentRow.sha256) == sha))  # type: ignore[call-overload]
            s.exec(delete(JournalRow).where(col(JournalRow.sha256) == sha))  # type: ignore[call-overload]
            s.commit()
        for p in [*(self.root / "results").glob(f"{sha}.*"), *(self.root / "docs").glob(f"{sha}.*"),
                  *(self.root / "parts").glob(f"{sha}.*")]:
            p.unlink(missing_ok=True)

    # ---------------- the contract's parts (contract.py): doc, gold, meta, bundle
    def save_part(self, sha: str, name: str, text: str | None) -> None:
        path = self.root / "parts" / f"{sha}.{name}.json"
        if text is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(text)

    def part(self, sha: str, name: str) -> str | None:
        path = self.root / "parts" / f"{sha}.{name}.json"
        return path.read_text() if path.exists() else None

    # ---------------- runs (one per parse: what it cost and gave)
    def add_run(self, run: dict) -> int:
        with self.session() as s:
            row = JournalRow(stream="runs", sha256=run.get("sha256"), ts=run.get("ts") or time.time(), data=jsonable(run))
            s.add(row)
            s.commit()
            return int(row.id or 0)

    def update_run(self, run_id: int, **fields: Any) -> None:
        with self.session() as s:
            row = s.get(JournalRow, run_id)
            if row is not None:
                row.data = jsonable({**row.data, **fields})
                s.add(row)
                s.commit()

    def runs(self, sha: str | None = None) -> list[dict]:
        """Runs oldest first (all of them, or one file's), each with its id and time."""
        with self.session() as s:
            q = select(JournalRow).where(col(JournalRow.stream) == "runs")
            if sha is not None:
                q = q.where(col(JournalRow.sha256) == sha)
            rows = s.exec(q.order_by(col(JournalRow.id))).all()
        return [{**r.data, "id": r.id, "ts": r.ts} for r in rows]

    # ---------------- agent jobs
    def enqueue_agent_job(self, layout_key: str, sha: str, name: str, kind: str, spec_id: str | None = None) -> int | None:
        """A new queued job; this layout's job when a person queued it again (retry_agent_job); else None (the layout
        had its job)."""
        with self.session() as s:
            old = s.exec(select(AgentJobRow).where(col(AgentJobRow.layout_key) == layout_key)).first()
            if old is not None:
                if old.status != "queued":
                    return None
                old.sha256, old.name, old.updated = sha, name, time.time()    # run it on this file
                s.add(old)
                s.commit()
                return old.id
            now = time.time()
            job = AgentJobRow(layout_key=layout_key, kind=kind, sha256=sha, name=name, status="queued",
                              spec_id=spec_id, created=now, updated=now)
            s.add(job)
            s.commit()
            return job.id

    def agent_job(self, job_id: int) -> dict | None:
        with self.session() as s:
            job = s.get(AgentJobRow, job_id)
        return job.model_dump() if job else None

    def agent_jobs(self, status: str | None = None) -> list[dict]:
        with self.session() as s:
            q = select(AgentJobRow).order_by(col(AgentJobRow.id).desc())
            if status:
                q = q.where(col(AgentJobRow.status) == status)
            return [j.model_dump() for j in s.exec(q).all()]

    def update_agent_job(self, job_id: int, **fields: Any) -> None:
        with self.session() as s:
            job = s.get(AgentJobRow, job_id)
            if job is None:
                raise KeyError(job_id)
            for k, v in fields.items():
                setattr(job, k, jsonable(v) if k == "report" else v)
            job.updated = time.time()
            s.add(job)
            s.commit()

    def agent_job_for(self, sha: str) -> dict | None:
        """The latest job written for this file."""
        with self.session() as s:
            job = s.exec(select(AgentJobRow).where(col(AgentJobRow.sha256) == sha)
                         .order_by(col(AgentJobRow.updated).desc())).first()
        return job.model_dump() if job else None

    def recover_agent_jobs(self) -> int:
        """Jobs a stopped process left running end as errors (their file keeps the result it had)."""
        n = 0
        for j in self.agent_jobs():
            if j["status"] in ("running", "stopping"):
                self.update_agent_job(j["id"], status="error", error="Interrupted: the API stopped during the job. "
                                      "Retry it to run it again.")
                n += 1
        return n

    def retry_agent_job(self, job_id: int) -> bool:
        job = self.agent_job(job_id)
        if job is None or job["status"] in ("queued", "running"):
            return False
        self.update_agent_job(job_id, status="queued", error=None)
        return True

    # ---------------- learning + review logs
    def append(self, name: str, record: dict) -> None:
        with self.session() as s:
            s.add(JournalRow(stream=name, sha256=record.get("sha"), ts=record.get("ts") or time.time(),
                             data=jsonable(record)))
            s.commit()

    def read(self, name: str, sha: str | None = None) -> list[dict]:
        """A journal stream oldest first: all of it, or one file's entries."""
        with self.session() as s:
            q = select(JournalRow).where(col(JournalRow.stream) == name)
            if sha is not None:
                q = q.where(col(JournalRow.sha256) == sha)
            rows = s.exec(q.order_by(col(JournalRow.id))).all()
        return [r.data for r in rows]

    def rewrite(self, name: str, records: list[dict]) -> None:
        with self.session() as s:
            s.exec(delete(JournalRow).where(col(JournalRow.stream) == name))  # type: ignore[call-overload]
            for r in records:
                s.add(JournalRow(stream=name, sha256=r.get("sha"), ts=r.get("ts") or time.time(), data=jsonable(r)))
            s.commit()
