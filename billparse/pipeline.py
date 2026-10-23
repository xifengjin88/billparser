"""The pipeline: intake -> read -> route -> extract -> clean -> validate -> resolve -> analyze -> store.

Routing tries the cheapest lane first and stops at the first acceptable result:
  1. builtin spec   (known provider export, detected by headers)
  2. learned spec   (seen this layout before: header match or fingerprint similarity)
  3. rule mapper    (header words + value patterns, month pivots, line patterns)
  4. LLM spec       (retrieval of similar verified specs as examples, up to N repair rounds)
  5. LLM extract    (read rows directly, then derive a deterministic spec from them)
Every route's result is validated the same way.
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from . import llm as llm_mod
from .analyze import analyze, resolve_services
from .docmodel import Document, load_bytes
from .fingerprint import fingerprint, similarity, table_headers
from .induce import derive_spec
from .mapper import propose
from .rowclass import set_model
from .spec import Extraction, SpecError, check_spec, detect_score, run
from .store import Store
from .validate import clean_rows, validate

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Settings:
    store_dir: Path = ROOT / "store"
    catalog_dir: Path = ROOT / "data"
    use_llm: bool = True
    max_repairs: int = 3
    accept_confidence: float = 0.7
    builtin_accept: float = 0.5
    reuse_similarity: float = 0.8
    retain_docs: bool = True
    examples_k: int = 3


@dataclass
class Attempt:
    route: str
    spec: dict | None
    ext: Extraction | None = None
    verdict: dict = field(default_factory=dict)
    error: str | None = None
    llm_calls: int = 0

    @property
    def confidence(self) -> float:
        return self.verdict.get("confidence", 0.0)

    def log(self) -> dict:
        return {"route": self.route, "spec_id": (self.spec or {}).get("id"), "confidence": self.confidence,
                "status": self.verdict.get("status"), "feedback": self.verdict.get("feedback", [])[:5],
                "error": self.error}


class Pipeline:
    def __init__(self, settings: Settings | None = None, llm: llm_mod.LLM | None = None):
        self.s = settings or Settings()
        self.store = Store(self.s.store_dir, retain_docs=self.s.retain_docs)
        self.llm = llm if llm is not None else (llm_mod.default_llm() if self.s.use_llm else None)
        _load_row_model(self.s.store_dir)

    # ------------------------------------------------------------ public
    def parse_file(self, path: str | Path, force: bool = False) -> dict:
        p = Path(path)
        return self.parse_bytes(p.read_bytes(), p.name, force=force)

    def parse_bytes(self, data: bytes, name: str, force: bool = False) -> dict:
        t0 = time.perf_counter()
        doc = load_bytes(data, name)
        if not force and (cached := self.store.get_result(doc.sha256)):
            return {**cached, "cached": True}
        self.store.save_doc(doc.sha256, data, name)

        if doc.regions and all(r.scanned or (not r.is_frame and not r.rows) for r in doc.regions):
            return self._finish(doc, None, [], {"status": "unsupported", "confidence": 0.0, "checks": [],
                                                "feedback": ["Scanned PDF: needs OCR (not in this prototype)"]},
                                {}, None, t0)

        fp = fingerprint(doc)
        headers = table_headers(doc)
        attempts: list[Attempt] = []
        best = None
        for route_fn in (self._builtin, self._learned, self._mapper, self._llm_spec, self._llm_extract):
            for att in route_fn(doc, fp, headers, attempts):
                attempts.append(att)
                if self._accept(att):
                    best = att
                    break
            if best:
                break
        if best is None:
            ok = [a for a in attempts if a.ext is not None and not a.ext.rows.empty]
            best = max(ok, key=lambda a: a.confidence) if ok else None

        if best is None:
            verdict = {"status": "needs_review", "confidence": 0.0, "checks": [],
                       "feedback": ["No route produced rows"]}
            return self._finish(doc, None, attempts, verdict, fp, None, t0)

        # final pass: resolve services, re-validate with resolution share, analyze
        rows = best.ext.rows
        share = resolve_services(rows, self.s.catalog_dir)
        alt = self._alt_extraction(doc, best)
        verdict = validate(best.ext, alt=alt, resolved_share=share)
        return self._finish(doc, best, attempts, verdict, fp, share, t0)

    # ------------------------------------------------------------ routes (generators of attempts)
    def _builtin(self, doc, fp, headers, attempts):
        scored = sorted(((detect_score(s, headers), s) for s in self.store.builtin_specs()), key=lambda x: -x[0])
        if scored and scored[0][0] > 0:
            yield self._try("builtin", scored[0][1], doc)

    def _learned(self, doc, fp, headers, attempts):
        cands: dict[str, float] = {}
        for spec in self.store.learned_specs():
            d = detect_score(spec, headers)
            if d > 0:
                cands[spec["id"]] = max(cands.get(spec["id"], 0), 1.0 + d / 10)
        for sid, sfp in self.store.fingerprints():
            if sfp["hash"] == fp["hash"]:
                cands[sid] = max(cands.get(sid, 0), 2.0)
            else:
                sim = similarity(fp, sfp)
                if sim >= self.s.reuse_similarity:
                    cands[sid] = max(cands.get(sid, 0), sim)
        for sid, _ in sorted(cands.items(), key=lambda x: -x[1])[:3]:
            spec = self.store.get_spec(sid)
            if spec and spec.get("trust") != "builtin":
                yield self._try("learned", spec, doc)

    def _mapper(self, doc, fp, headers, attempts):
        spec, conf = propose(doc)
        if spec:
            att = self._try("mapper", spec, doc)
            att.verdict["mapper_confidence"] = conf
            yield att

    def _llm_spec(self, doc, fp, headers, attempts):
        if not self.llm:
            return
        preview = build_preview(doc)
        examples = self._examples(fp)
        spec, feedback, calls = None, None, 0
        for _ in range(self.s.max_repairs):
            calls += 1
            try:
                spec = llm_mod.generate_spec(self.llm, preview, examples, feedback, spec)
                check_spec(spec)
            except Exception as e:                              # noqa: BLE001  bad JSON / bad spec -> repair
                feedback = [f"Spec rejected: {e}"]
                yield Attempt("llm_spec", spec, error=str(e), llm_calls=1)
                continue
            att = self._try("llm_spec", spec, doc)
            att.llm_calls = 1
            yield att
            if self._accept(att):
                return
            feedback = att.verdict.get("feedback") or [att.error or "No rows extracted"]

    def _llm_extract(self, doc, fp, headers, attempts):
        if not self.llm:
            return
        try:
            out = llm_mod.extract_rows(self.llm, build_preview(doc, max_rows=400))
        except Exception as e:                                  # noqa: BLE001
            yield Attempt("llm_extract", None, error=str(e), llm_calls=1)
            return
        rows = out.get("rows") or []
        spec, agree = derive_spec(doc, rows)
        if spec and agree >= 0.9:
            spec.setdefault("notes", f"Derived from LLM extraction (agreement {agree:.0%})")
            att = self._try("llm_extract_derived", spec, doc)
            att.llm_calls = 1
            yield att
            return
        # no deterministic spec reproduces the rows: use the LLM rows directly (never reused)
        df = pd.DataFrame(rows)
        if not df.empty and "billed_cost" in df:
            df["billed_cost"] = pd.to_numeric(df["billed_cost"], errors="coerce")
            ext = Extraction(rows=df, stated_totals=([{"label": "llm total", "amount": out["total"], "kind": "field"}]
                                                     if out.get("total") is not None else []))
            clean_rows(ext)
            att = Attempt("llm_extract_direct", None, ext=ext, llm_calls=1)
            att.verdict = validate(ext)
            att.verdict["confidence"] = min(att.verdict["confidence"], 0.65)   # never auto-accept
            yield att

    # ------------------------------------------------------------ helpers
    def _try(self, route: str, spec: dict, doc: Document) -> Attempt:
        att = Attempt(route, spec)
        try:
            att.ext = run(spec, doc)
            clean_rows(att.ext)
            kind = (spec.get("meta") or {}).get("kind", "cost_lines")
            if kind != "cost_lines":
                att.verdict = {"status": "auxiliary", "confidence": 1.0 if not att.ext.rows.empty else 0.0,
                               "checks": [], "feedback": [], "kind": kind}
            else:
                att.verdict = validate(att.ext)
        except (SpecError, Exception) as e:                     # noqa: BLE001
            att.error = f"{type(e).__name__}: {e}"
            att.verdict = {"status": "error", "confidence": 0.0, "checks": [], "feedback": [att.error]}
            if not isinstance(e, (SpecError, ValueError, KeyError)):
                att.verdict["trace"] = traceback.format_exc(limit=3)
        return att

    def _accept(self, att: Attempt) -> bool:
        if att.ext is None or att.verdict.get("status") in ("error", None):
            return False
        if att.verdict.get("status") == "auxiliary":
            return att.confidence > 0
        if att.verdict.get("status") == "needs_review":
            return False
        floor = self.s.builtin_accept if att.route == "builtin" else self.s.accept_confidence
        return att.confidence >= floor

    def _alt_extraction(self, doc: Document, best: Attempt) -> Extraction | None:
        """Independent second extraction for the dual-extraction check."""
        if best.route in ("builtin", "mapper", "llm_extract_direct") or best.spec is None:
            return None
        if (best.spec.get("meta") or {}).get("kind", "cost_lines") != "cost_lines" or best.spec.get("reshape"):
            return None
        spec, _ = propose(doc)
        if not spec:
            return None
        try:
            ext = run(spec, doc)
            clean_rows(ext)
            return ext if not ext.rows.empty else None
        except Exception:                                       # noqa: BLE001
            return None

    def _examples(self, fp: dict) -> list[tuple[str, dict]]:
        """Most similar verified/candidate specs, as few-shot examples for the LLM."""
        scored = []
        for spec in self.store.learned_specs():
            ex = (spec.get("stats") or {}).get("example_preview")
            sfp = spec.get("fingerprint")
            if ex and sfp:
                bonus = 0.1 if spec.get("trust") == "verified" else 0.0
                scored.append((similarity(fp, sfp) + bonus, ex, _public(spec)))
        scored.sort(key=lambda x: -x[0])
        out = [(ex, sp) for _, ex, sp in scored[: self.s.examples_k]]
        if len(out) < self.s.examples_k:                        # pad with built-ins as header-only examples
            for spec in self.store.builtin_specs()[: self.s.examples_k - len(out)]:
                out.append(("headers: " + ", ".join(h for c in spec["columns"].values() for h in c.get("from", [])[:1]),
                            _public(spec)))
        return out

    def _finish(self, doc: Document, best: Attempt | None, attempts: list[Attempt], verdict: dict,
                fp: dict, share: float | None, t0: float) -> dict:
        rows = best.ext.rows if best and best.ext is not None else pd.DataFrame()
        spec_info = None
        accepted = verdict.get("status") in ("reconciled", "unreconciled", "auxiliary")
        if best and best.spec is not None:
            spec = best.spec
            if best.route in ("mapper", "llm_spec", "llm_extract_derived") and accepted:
                spec = dict(spec, id=f"{doc.file_type}-{fp['hash'][:10]}", trust="candidate", fingerprint=fp,
                            stats={"example_preview": llm_mod.render_preview(build_preview(doc, max_rows=20))[:4000],
                                   "created_by": best.route})
                spec = self.store.save_spec(spec)
            spec_info = {"id": spec.get("id"), "version": spec.get("version"), "trust": spec.get("trust"),
                         "route": best.route, "format": (spec.get("meta") or {}).get("format")}
            if accepted and spec.get("id"):
                total = float(rows["billed_cost"].sum()) if "billed_cost" in rows else 0.0
                self.store.link(spec["id"], doc.sha256, fp,
                                {"rows": len(rows), "total": round(total, 2), "confidence": verdict["confidence"]})

        kind = verdict.get("kind") or ((best.spec or {}).get("meta") or {}).get("kind", "cost_lines") if best else None
        analysis = {}
        if kind == "cost_lines" and accepted and not rows.empty:
            analysis = analyze(rows, verdict["confidence"], verdict["status"], best.ext.fields if best else {})
        stated = best.ext.stated_totals if best and best.ext else []
        summary = {
            "file": doc.name, "sha256": doc.sha256, "file_type": doc.file_type, "kind": kind,
            "status": verdict["status"], "confidence": verdict["confidence"],
            "spec": spec_info, "rows": len(rows),
            "service_resolution": round(share, 3) if share is not None else None,
            "stated_totals": [{k: v for k, v in t.items() if k != "src"} for t in stated][:10],
            "header_fields": {k: v["value"] for k, v in (best.ext.fields.items() if best and best.ext else [])},
            "checks": verdict.get("checks", []),
            "warnings": _warnings(best),
            "analysis": analysis,
            "attempts": [a.log() for a in attempts],
            "llm_calls": sum(a.llm_calls for a in attempts),
            "seconds": round(time.perf_counter() - t0, 3),
        }
        self.store.save_result(doc.sha256, summary, rows)
        self.store.append("records", {"ts": time.time(), "sha": doc.sha256, "file": doc.name, "fingerprint": fp,
                                      "route": spec_info and spec_info["route"], "spec": spec_info,
                                      "status": summary["status"], "confidence": summary["confidence"],
                                      "attempts": summary["attempts"], "llm_calls": summary["llm_calls"]})
        if best and best.ext and best.ext.labels and summary["status"] == "reconciled":
            for lab in best.ext.labels:                         # silver labels for the row classifier
                self.store.append("labels", {"sha": doc.sha256, **lab})
        if summary["status"] in ("needs_review", "unsupported"):
            self.store.append("review", {"ts": time.time(), "sha": doc.sha256, "file": doc.name,
                                         "status": summary["status"], "open": True,
                                         "feedback": verdict.get("feedback", [])[:8]})
        return summary


# ---------------------------------------------------------------- previews
def build_preview(doc: Document, max_rows: int = 40, max_regions: int = 4) -> dict:
    regions = []
    for r in doc.regions[:max_regions]:
        if r.is_frame:
            rows = [list(map(str, r.df.columns))] + r.preview(max_rows)
            regions.append({"name": r.name, "kind": "frame", "rows": rows})
        elif r.rows:
            rows = [[c.text for c in row.cells] for row in r.rows[:max_rows]]
            bold = [i for i, row in enumerate(r.rows[:max_rows]) if row.bold]
            regions.append({"name": r.name, "kind": r.kind, "rows": rows, "bold_rows": bold})
    return {"file_type": doc.file_type, "regions": regions}


def _warnings(best: Attempt | None) -> list[str]:
    if best is None:
        return []
    out = list(best.ext.warnings) if best.ext else []
    note = ((best.spec or {}).get("meta") or {}).get("notes")
    if note:
        out.append(note)
    return out


def _public(spec: dict) -> dict:
    return {k: v for k, v in spec.items() if k not in ("stats", "fingerprint", "created_at", "version")}


def _load_row_model(store_dir: Path) -> None:
    p = Path(store_dir) / "models" / "rowclass.joblib"
    if p.exists():
        try:
            from .learn import load_row_model
            set_model(load_row_model(p))
        except Exception:                                       # noqa: BLE001  fall back to rules
            set_model(None)
