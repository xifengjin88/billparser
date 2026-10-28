"""The pipeline: intake -> read -> route -> extract -> clean -> validate -> resolve -> analyze -> store.

Routing tries the cheapest lane first and stops at the first acceptable result:
  1. builtin spec   (known provider export, detected by headers)
  2. learned spec   (seen this layout before: header match or fingerprint similarity)
  3. rule mapper    (header words + value patterns, month pivots, line patterns)
  4. LLM spec       (retrieval of similar verified specs as examples, up to N repair rounds)
  5. LLM extract    (read rows directly, then derive a deterministic spec from them)
Every route's result is validated the same way.

When no route proves the file (its cost lines do not reconcile) and the agent is on (AGENT_ENABLED=1 and a key),
the parse goes on with the agent step (agent.py): the agent writes a spec for the file's layout, the code checks
it, and a proved spec is saved and the file is read again with it. Callers see one parse and its final result.
"""

from __future__ import annotations

import contextvars
import json
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from . import agent as agent_mod
from . import bigfile, contract, coverage, jobs
from . import headers as headers_mod
from . import llm as llm_mod
from .analyze import analyze, resolve_services
from .attributes import extract as extract_attributes
from .attributes import normalize_regions
from .docmodel import Document, load_bytes
from .fingerprint import fingerprint, similarity, table_headers
from .induce import derive_spec
from .mapper import MapContext, propose, propose_all
from .pricing import KeyCache, PriceDefaults, PriceStats, apply_discount, drop_prices, infer_discount, match_prices
from .settings import catalog_dir, data_dir
from .spec import Extraction, SpecError, check_spec, detect_score, run
from .store import Store
from .transforms import CANONICAL, period_of
from .validate import CRITICAL, PRICE_CHECKS, REVIEW_BELOW, clean_rows, validate, validate_stats


@dataclass
class Settings:
    store_dir: Path = field(default_factory=data_dir)          # PARSER_DATA_DIR
    catalog_dir: Path = field(default_factory=catalog_dir)     # CATALOG_DIR
    use_llm: bool = True
    max_repairs: int = 3
    accept_confidence: float = 0.7
    builtin_accept: float = 0.5
    reuse_similarity: float = 0.8
    retain_docs: bool = True
    examples_k: int = 3
    use_agent: bool = field(default_factory=agent_mod.enabled)  # AGENT_ENABLED=1 and ANTHROPIC_API_KEY
    price_defaults: PriceDefaults = field(default_factory=PriceDefaults)   # what pricing may assume (none)
    # LLM spec candidates in the first round, generated in parallel (1: one at a time, as before)
    spec_candidates: int = field(default_factory=lambda: max(1, int(os.environ.get("BILLPARSE_SPEC_CANDIDATES") or 2)))
    # the agent step runs in this parse (the tests, with a fake agent model); else it is a job of the learn queue
    learn_inline: bool = False


@dataclass
class _PriceContext:
    """What pricing uses during one parse: the catalog, the parse's key cache and the request's defaults."""
    catalog: object
    cache: KeyCache
    defaults: PriceDefaults


_PRICING: contextvars.ContextVar[_PriceContext | None] = contextvars.ContextVar("billparse_pricing", default=None)


@dataclass
class Attempt:
    route: str
    spec: dict | None
    ext: Extraction | None = None
    verdict: dict = field(default_factory=dict)
    error: str | None = None
    llm_calls: int = 0
    chosen: bool = True             # False: a spec candidate another one beat (logged, never accepted)
    settled: dict | None = None     # the mapper's columns mapped with a score of 3.0 or more: field -> header

    @property
    def confidence(self) -> float:
        return self.verdict.get("confidence", 0.0)

    def log(self) -> dict:
        return {"route": self.route, "spec_id": (self.spec or {}).get("id"), "confidence": self.confidence,
                "status": self.verdict.get("status"), "feedback": self.verdict.get("feedback", [])[:5],
                "error": self.error}


class Pipeline:
    def __init__(self, settings: Settings | None = None, llm: llm_mod.LLM | None = None, store: Store | None = None,
                 agent_llm: agent_mod.AgentLLM | None = None):
        self.s = settings or Settings()
        self.agent_llm = agent_llm                       # tests give a fake; None: the Claude API
        self.agent_limits: agent_mod.Limits | None = None   # this pipeline's agent model and effort (None: from env)
        self.choice_note: str | None = None              # the model choices of this run, recorded with it
        self.store = store or Store(self.s.store_dir, retain_docs=self.s.retain_docs)
        self.llm = llm if llm is not None else (llm_mod.default_llm() if self.s.use_llm else None)

    # ------------------------------------------------------------ public
    def parse_file(self, path: str | Path, force: bool = False) -> dict:
        return self.parse_path(path, force=force)

    def parse_path(self, path: str | Path, name: str | None = None, force: bool = False, on_step=None,
                   agent: bool = True, user: str | None = None, run: bool = True, move: bool = False,
                   sha: str | None = None, info: bigfile.FileInfo | None = None,
                   price_defaults: PriceDefaults | None = None) -> dict:
        """Parse a file on disk. A small file is read into memory (parse_bytes); a large one (more than LARGE_FILE_MB
        uncompressed) is read in chunks (bigfile.py), and its agent job, when it needs one, is queued, never run here.
        move: the file is a temporary upload, moved into the store when kept. info: the file's classify() result, when
        the caller has it. price_defaults: what pricing may assume for this request (else Settings.price_defaults)."""
        p = Path(path)
        name = name or p.name
        info = info or bigfile.classify(p, name)
        if not info.large:
            return self.parse_bytes(p.read_bytes(), name, force=force, on_step=on_step, agent=agent, user=user,
                                    run=run, price_defaults=price_defaults)
        t0 = time.perf_counter()
        result = self._parse_large(info, force, on_step, move, sha, price_defaults)
        if agent and self.s.use_agent and not result.get("cached") and agent_mod.needs_agent(result):
            self.queue_learning(agent_mod.enqueue_for(self, result))     # on the sample, never inline
        if run and not result.get("cached"):
            self._record_run(result, None, int((time.perf_counter() - t0) * 1000), user)
        return result

    def parse_bytes(self, data: bytes, name: str, force: bool = False, on_step=None, agent: bool = True,
                    user: str | None = None, run: bool = True, price_defaults: PriceDefaults | None = None) -> dict:
        """Parse one file and return its result. on_step(name) is told when the parse starts a step that can be
        long ("learning": the agent writes a spec for the layout). agent=False: never run the agent step.
        run=False: no run is recorded (the agent's own parse of the file it proved)."""
        t0 = time.perf_counter()
        spent = getattr(self.llm, "spent", None)         # what this thread's LLM calls cost, before and after
        before = spent() if spent else 0.0
        result = self._parse(data, name, force, price_defaults)
        if spent and spent() > before:
            result["claude_cost_usd"] = round(spent() - before, 6)
        job = None
        if agent and self.s.use_agent and not result.get("cached") and agent_mod.needs_agent(result):
            job_id = agent_mod.enqueue_for(self, result)    # one job per layout (a retried job is queued again)
            if job_id is not None and self.s.learn_inline:
                if on_step is not None:
                    on_step("learning")
                job = agent_mod.run_job(self, job_id, llm=self.agent_llm, limits=self.agent_limits)
                if job["status"] == "proved":           # else the first pass stands
                    result = self.store.get_result(result["sha256"]) or result
            elif job_id is not None:
                result["learn_job_id"] = self.queue_learning(job_id)
        if run and not result.get("cached"):
            self._record_run(result, job, int((time.perf_counter() - t0) * 1000), user)
        return result

    def queue_learning(self, agent_job_id: int | None) -> int | None:
        """A learn job (worker.run_learn) for this agent job: the parse ends now, the learning runs on the learn queue.
        Its parent is the parse job that runs this parse, so a proved spec sends that job's webhook again."""
        if agent_job_id is None:
            return None
        aj = self.store.agent_job(agent_job_id)
        if aj is None or aj["status"] != "queued":
            return None
        job, _ = jobs.enqueue(self.store, "learn", "learn", name=aj["name"], sha256=aj["sha256"],
                              options={"agent_job_id": agent_job_id}, dedupe_key=f"learn:{agent_job_id}:{aj.get('attempts') or 0}",
                              parent_id=jobs.current_job.get())
        return job["id"]

    def _save_contract(self, doc: Document, summary: dict, rows: pd.DataFrame) -> dict:
        """The result in invoice-parser's contract (contract.py), kept beside the lines; returns what the lists show."""
        out, meta, bundle = contract.build(summary, rows, doc)
        sha = doc.sha256
        self.store.save_part(sha, "doc", out.model_dump_json(exclude_none=True))
        self.store.save_part(sha, "meta", json.dumps(meta, default=str))
        self.store.save_part(sha, "bundle", json.dumps(bundle, default=str))
        return {"lane": meta["lane"], "completeness": out.completeness,
                "confidence": out.confidence.level if out.confidence else None, "charges": len(out.charges),
                "gaps": len(out.gaps), "counted_total": str(out.counted_total()),
                "money_at_risk": float(contract.money_at_risk(out))}

    def _record_run(self, result: dict, job: dict | None, total_ms: int, user: str | None) -> None:
        c = result.get("contract") or {}
        self.store.add_run({
            "sha256": result["sha256"], "name": result.get("file"), "elapsed_ms": int((result.get("seconds") or 0) * 1000),
            "total_ms": total_ms, "model_calls": int(result.get("llm_calls") or 0), "lane": c.get("lane"),
            "status": contract.status_of(result.get("status")), "completeness": c.get("completeness"),
            "version": f"billparse · {self.choice_note}" if self.choice_note else "billparse",
            "jev_cost_usd": None, "claude_cost_usd": result.get("claude_cost_usd"),
            "learn_cost_usd": job["cost_usd"] if job else None, "learn_job_id": job["id"] if job else None,
            "parsed_by": user})

    def _parse(self, data: bytes, name: str, force: bool, price_defaults: PriceDefaults | None = None) -> dict:
        t0 = time.perf_counter()
        doc = load_bytes(data, name)
        if not force and (cached := self.store.get_result(doc.sha256)):
            return {**cached, "cached": True}
        self.store.save_doc(doc.sha256, data, name)

        if doc.regions and all(r.scanned or (not r.is_frame and not r.rows) for r in doc.regions):
            return self._finish(doc, None, [], {"status": "unsupported", "confidence": 0.0, "checks": [],
                                                "feedback": ["Scanned PDF: needs OCR (not in this prototype)"]},
                                {}, None, t0)

        # the attempts and the final pass are priced with one key cache (each key looked up once per parse)
        token = _PRICING.set(_PriceContext(self.catalog(), KeyCache(), price_defaults or self.s.price_defaults))
        try:
            fp = fingerprint(doc)
            attempts, best = self._route(doc, fp)
            if best is None:
                verdict = {"status": "needs_review", "confidence": 0.0, "checks": [],
                           "feedback": ["No route produced rows"]}
                return self._finish(doc, None, attempts, verdict, fp, None, t0)

            # final pass: resolve services, re-validate with resolution share, analyze
            rows = best.ext.rows
            share = resolve_services(rows, self.s.catalog_dir)
            if best.verdict.get("status") == "auxiliary":           # an inventory, a price sheet: not cost lines
                return self._finish(doc, best, attempts, dict(best.verdict, completeness="consistent"), fp, share, t0)
            pricing = self._price(rows, best.ext.fields, price_defaults)
            alt = self._alt_extraction(doc, best)
            verdict = validate(best.ext, alt=alt, resolved_share=share,
                               text=doc_text(doc) if best.route == "llm_extract_direct" else None)
            if best.route == "llm_extract_direct" and verdict.get("completeness") == "consistent":
                verdict["completeness"] = "unchecked"           # rows the LLM wrote, no printed total tested them
            return self._finish(doc, best, attempts, verdict, fp, share, t0, pricing=pricing)
        finally:
            _PRICING.reset(token)

    # ------------------------------------------------------------ prices
    def catalog(self):
        """The price catalog in the store's database (prices.store), or None when it cannot be opened."""
        from prices.store import get_catalog
        try:
            return get_catalog(self.store.url)
        except Exception:                                       # noqa: BLE001  pricing never fails a parse
            return None

    def _price(self, rows: pd.DataFrame, fields: dict, defaults: PriceDefaults | None) -> dict:
        """Price the usage lines (pricing.py) in place; returns the document's pricing summary."""
        period = contract.printed_period({k: v.get("value") for k, v in (fields or {}).items()})
        ctx = _PRICING.get()
        stats = match_prices(rows, ctx.catalog if ctx else self.catalog(), defaults or self.s.price_defaults,
                             cache=ctx.cache if ctx else None, period_start=period[0] if period else None)
        return self._price_summary(stats, rows)

    @staticmethod
    def _price_attempt(ext: Extraction) -> None:
        """An attempt's lines priced (the parse's cache), so the price checks steer which route wins and what the
        LLM repairs. Lines without a provider yet (a mapper or LLM spec) are priced in the final pass."""
        ctx = _PRICING.get()
        if ctx is None or ctx.catalog is None or ext.rows.empty:
            return
        period = contract.printed_period({k: v.get("value") for k, v in (ext.fields or {}).items()})
        try:
            match_prices(ext.rows, ctx.catalog, ctx.defaults, ctx.cache, period[0] if period else None)
        except Exception:                                       # noqa: BLE001  pricing never fails an attempt
            pass

    def reprice(self, sha: str, price_defaults: PriceDefaults | None = None) -> dict:
        """Price a stored result again with the current catalog, never parsing the file again: the stored rows (a
        large file: its Parquet rows, in batches) without the values pricing filled, priced, then the analysis and
        the contract rebuilt and saved. The route, the rows and the checks stay as they were (pricing changes no
        billed cost). price_defaults: None keeps the ones the result was priced with. Returns the new summary."""
        t0 = time.perf_counter()
        summary = self.store.get_result(sha)
        if summary is None:
            raise KeyError(sha)
        if summary.get("kind") not in (None, "cost_lines"):
            return summary                                  # an inventory or a price sheet: no cost lines
        defaults = price_defaults or PriceDefaults(**((summary.get("pricing") or {}).get("defaults") or {}))
        header = summary.get("header_fields") or {}
        period = contract.printed_period(header)
        # lines without a date: the date the first pricing used (else a reprice on another day would price them
        # differently)
        kept = (summary.get("pricing") or {}).get("undated_as_of")
        start = period[0] if period else kept
        doc = bigfile.stored_doc(self.store, sha) or Document(summary.get("file") or "", "csv", sha, [])
        rows_path = None
        if summary.get("large"):
            src = self.store.root / "results" / f"{sha}.parquet"
            if not src.exists():
                return summary
            rows_path = self.store.temp_path(".parquet")
            scan = bigfile.reprice(src, rows_path, self.catalog(), defaults, start)
            rows = scan.rolled
            pricing = self._price_summary(scan.pricing, rows) if scan.pricing is not None else None
        else:
            stored = self.store.get_rows(sha)
            if stored is None or stored.empty:
                return summary
            rows = drop_prices(stored)
            stats = match_prices(rows, self.catalog(), defaults, period_start=start)
            pricing = self._price_summary(stats, rows)
        if pricing is not None and kept and not period and pricing.get("undated_as_of"):
            pricing["warnings"].append(f"Lines without a date are priced as of {kept}, the date of the first pricing")
        if summary.get("analysis"):
            fields = {k: {"value": v, "src": None} for k, v in header.items()}
            summary["analysis"] = analyze(rows, summary.get("confidence") or 0.0, summary["status"], fields)
        summary["pricing"] = pricing
        summary["repriced"] = {"at": time.time(), "seconds": round(time.perf_counter() - t0, 3)}
        summary["contract"] = self._save_contract(doc, summary, rows)
        self.store.save_result(sha, summary, rows, rows_path=rows_path)
        if rows_path is not None:
            self.store.save_rolled(sha, rows)
        return summary

    @staticmethod
    def _price_summary(stats: PriceStats, rows: pd.DataFrame) -> dict:
        """R2 over the whole file (a large file: its rolled-up lines), then the summary."""
        discount = infer_discount(stats)
        apply_discount(rows, discount)
        return stats.summary(discount)

    def _route(self, doc: Document, fp: dict) -> tuple[list[Attempt], Attempt | None]:
        """Every route, cheapest first, until one is accepted; else the most confident attempt with rows."""
        headers = table_headers(doc)
        attempts: list[Attempt] = []
        for route_fn in (self._builtin, self._learned, self._mapper, self._llm_spec, self._llm_extract):
            for att in route_fn(doc, fp, headers, attempts):
                attempts.append(att)
                if self._accept(att):
                    return attempts, att
        ok = [a for a in attempts if a.ext is not None and not a.ext.rows.empty]
        return attempts, (max(ok, key=lambda a: a.confidence) if ok else None)

    def _parse_large(self, info: bigfile.FileInfo, force: bool, on_step, move: bool, sha: str | None,
                     price_defaults: PriceDefaults | None = None) -> dict:
        """A large file: the routes on its sample, then the accepted spec over the whole file in chunks. The attempts
        and the chunks are priced with one key cache."""
        token = _PRICING.set(_PriceContext(self.catalog(), KeyCache(), price_defaults or self.s.price_defaults))
        try:
            return self._read_large(info, force, on_step, move, sha)
        finally:
            _PRICING.reset(token)

    def _read_large(self, info: bigfile.FileInfo, force: bool, on_step, move: bool, sha: str | None) -> dict:
        ctx = _PRICING.get()
        assert ctx is not None
        t0 = time.perf_counter()
        sha = sha or bigfile.sha256_of(info.path)
        if not force and (cached := self.store.get_result(sha)):
            self._keep(info, sha, move)
            return {**cached, "cached": True}

        def unsupported(doc: Document, why: str, best: Attempt | None = None, attempts=(), fp=None) -> dict:
            verdict = {"status": "unsupported", "confidence": 0.0, "checks": [], "feedback": [why]}
            out = self._finish(doc, best, list(attempts), verdict, fp or {}, None, t0, big=bigfile.Scan(warnings=[why]))
            self._keep(info, sha, move)
            return out
        if info.refused:
            return unsupported(Document(info.name, info.fmt, sha, []), info.refused)
        try:
            sample = bigfile.sample_doc(info, sha)
        except bigfile.TooLarge as e:
            return unsupported(Document(info.name, info.fmt, sha, []), str(e))
        fp = fingerprint(sample)
        attempts, best = self._route(sample, fp)
        if best is None:
            verdict = {"status": "needs_review", "confidence": 0.0, "checks": [],
                       "feedback": ["No route produced rows (on the first rows of the file)"]}
            out = self._finish(sample, None, attempts, verdict, fp, None, t0)
            self._keep(info, sha, move)
            return out
        if (why := bigfile.unsupported_spec(best.route, best.spec)):
            return unsupported(sample, why, None, attempts, fp)
        if on_step is not None:
            on_step("reading")
        out_path = self.store.temp_path(".parquet")
        try:
            scan = bigfile.scan(best.spec, info, sample, self.s.catalog_dir, out_path, on_progress=on_step,
                                unmapped=best.ext.unmapped if best.ext is not None else None,
                                cost_basis=((best.ext.fields if best.ext is not None else {}).get("cost_basis")
                                            or {}).get("value"),
                                prices=ctx.catalog, price_defaults=ctx.defaults, cache=ctx.cache)
        except bigfile.TooLarge as e:
            out_path.unlink(missing_ok=True)
            return unsupported(Document(info.name, info.fmt, sha, []), str(e))
        except BaseException:
            out_path.unlink(missing_ok=True)
            raise
        kind = (best.spec.get("meta") or {}).get("kind", "cost_lines")
        if kind != "cost_lines":
            verdict = {"status": "auxiliary", "confidence": 1.0 if scan.rows else 0.0, "checks": [], "feedback": [],
                       "kind": kind, "completeness": "consistent"}
        else:
            verdict = validate_stats(scan.stats, scan.stated_totals, scan.source_rows, scan.share, scan.skipped)
            if self._accept(best) and verdict["status"] not in ("reconciled", "unreconciled"):
                scan.note(f"The first {len(sample.regions[0].df):,} rows passed the checks with {best.route} spec "
                          f"{best.spec.get('id') or ''}; the whole file did not: "
                          + "; ".join(verdict.get("feedback") or [])[:300])
        pricing = self._price_summary(scan.pricing, scan.rolled) if scan.pricing is not None else None
        out = self._finish(sample, best, attempts, verdict, fp, scan.share if kind == "cost_lines" else None, t0,
                           big=scan, pricing=pricing)
        self._keep(info, sha, move)
        return out

    def _keep(self, info: bigfile.FileInfo, sha: str, move: bool) -> None:
        self.store.save_doc_path(sha, info.path, info.name, move=move)

    # ------------------------------------------------------------ routes (generators of attempts)
    def _builtin(self, doc, fp, headers, attempts):
        text = doc_text(doc)
        scored = sorted(((detect_score(s, headers, text), s) for s in self.store.builtin_specs(include_disabled=False)),
                        key=lambda x: -x[0])
        if scored and scored[0][0] > 0:
            yield self._try("builtin", scored[0][1], doc)

    def _learned(self, doc, fp, headers, attempts):
        cands: dict[str, float] = {}
        text = doc_text(doc)
        for spec in self.store.learned_specs():
            d = detect_score(spec, headers, text)
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
            if spec and spec.get("trust") not in ("builtin", "disabled"):   # a disabled spec is kept, never used
                yield self._try("learned", spec, doc)

    @staticmethod
    def _printed_totals(attempts) -> list[dict]:
        """The document's printed total that the tree of sums found: it checks every other reading of the same file
        (a mapper proposal, an LLM spec, the LLM's own rows), so none of them passes with no total to test."""
        return [t for a in attempts if a.ext is not None for t in a.ext.stated_totals if t.get("kind") == "tree_total"]

    @staticmethod
    def _all_printed_totals(attempts) -> list[dict]:
        """Every total that a route read from the file (a total row, a header field, the tree's total), each once:
        rows the LLM wrote itself are tested against them, so the LLM cannot pass a total every spec failed."""
        out, seen = [], set()
        for a in attempts:
            for t in (a.ext.stated_totals if a.ext is not None else []):
                if t.get("kind") in ("subtotal", "document_total", "llm_claim"):
                    continue
                key = (str(t.get("label")), str(t.get("amount")))
                if key not in seen:
                    seen.add(key)
                    out.append(dict(t, kind="document_total"))
        return out

    def map_context(self) -> MapContext:
        """What the mapper may use beyond the headers: the header dictionary and the price catalog (a catalog that
        cannot be read gives none: the detectors of instance types then use their pattern, SKUs none)."""
        try:
            catalog = self.catalog()
            catalog = catalog if catalog is not None and catalog.version else None    # loaded: a current snapshot
        except Exception:                                       # noqa: BLE001
            catalog = None
        return MapContext(dictionary=headers_mod.dictionary(self.store.engine), catalog=catalog)

    def _mapper(self, doc, fp, headers, attempts):
        # a built-in spec that recognized the file and read rows keeps it: the mapper's guess at a known provider
        # export is never better (and a built-in result must not change with the mapper)
        if any(a.route == "builtin" and a.ext is not None and not a.ext.rows.empty for a in attempts):
            return
        ctx = self.map_context()
        for spec, conf, m in propose_all(doc, ctx):
            found = self._printed_totals(attempts)
            att = self._try("mapper", spec, doc, extra_totals=found)
            att.verdict["mapper_confidence"] = conf
            if m is not None:
                att.settled = m.settled
                if att.ext is not None:
                    att.ext.warnings.extend(m.warnings)
            yield att

    def _llm_spec(self, doc, fp, headers, attempts):
        """The LLM writes a spec, the interpreter runs it and the checks judge it; up to max_repairs rounds. Round 1
        has spec_candidates candidates, generated in parallel (only the LLM calls run in threads: each candidate is
        run and checked here, in the parse's thread, with its price context); the next rounds repair the best one."""
        if not self.llm:
            return
        preview = build_preview(doc)
        examples = self._examples(fp)
        settled = _settled(attempts)
        spec, feedback = None, None
        rounds = self.s.max_repairs
        if self.s.spec_candidates > 1 and rounds > 0:
            best, feedback, spec = yield from self._spec_candidates(doc, preview, examples, attempts, settled)
            if best is not None and self._accept(best):
                return
            rounds -= 1
        for _ in range(rounds):
            try:
                spec = llm_mod.generate_spec(self.llm, preview, examples, feedback, spec, settled=settled)
                check_spec(spec)
            except Exception as e:                              # noqa: BLE001  bad JSON / bad spec -> repair
                feedback = [f"Spec rejected: {e}"]
                yield Attempt("llm_spec", spec, error=str(e), llm_calls=1)
                continue
            att = self._try("llm_spec", spec, doc, extra_totals=self._printed_totals(attempts))
            att.llm_calls = 1
            _note_settled(att, settled)
            yield att
            if self._accept(att):
                return
            feedback = att.verdict.get("feedback") or [att.error or "No rows extracted"]

    def _spec_candidates(self, doc, preview, examples, attempts, settled: dict | None = None):
        """Round 1: spec_candidates specs at once, each from another order of the examples, the later ones told to read
        the layout on their own. Every candidate is an attempt in the log; the best (accepted; else no failed critical
        check; else the fewest failed price checks; else the most confident) is the only one that can be accepted.
        Two candidates that read the same rows add candidates_agree to it; where they differ, the repair is told.
        Returns (the best attempt or None, the feedback for the next round, the spec it repairs). When no candidate
        ran (each failed to parse or check_spec), the next round repairs the first one that has a spec, told why it was
        rejected, as a single candidate's next round is."""
        n = self.s.spec_candidates
        spent = getattr(self.llm, "spent", None)

        def generate(i: int):
            before = spent() if spent else 0.0
            try:
                out, err = llm_mod.generate_spec(self.llm, preview, _example_variant(examples, i),
                                                 note=CANDIDATE_NOTE if i else None, settled=settled), None
            except Exception as e:                              # noqa: BLE001  a failed candidate is an attempt
                out, err = None, e
            return out, err, (spent() - before) if spent else 0.0
        with ThreadPoolExecutor(max_workers=n, thread_name_prefix="spec-candidate") as pool:
            made = list(pool.map(generate, range(n)))
        if hasattr(self.llm, "charge"):                         # the workers' cost belongs to this parse
            self.llm.charge(sum(c for _, _, c in made))
        atts: list[Attempt] = []
        for spec, err, _ in made:
            if err is None:
                try:
                    check_spec(spec)
                except Exception as e:                          # noqa: BLE001
                    err = e
            if err is not None:
                atts.append(Attempt("llm_spec", spec, error=str(err), llm_calls=1))
                continue
            att = self._try("llm_spec", spec, doc, extra_totals=self._printed_totals(attempts))
            att.llm_calls = 1
            _note_settled(att, settled)
            atts.append(att)
        ran = sorted((a for a in atts if a.ext is not None), key=self._candidate_rank)
        best = ran[0] if ran else None
        disagree: list[str] = []
        with_rows = [a for a in ran if not a.ext.rows.empty]
        if best is not None and len(with_rows) >= 2 and best in with_rows \
                and best.verdict.get("status") != "auxiliary":
            other = next(a for a in with_rows if a is not best)
            disagree = _candidates_differ(best, other)
            if not disagree:
                best.ext.checks.append({"name": "candidates_agree", "status": "pass", "weight": 0.05,
                                        "detail": f"{len(with_rows)} spec candidates read the same {len(best.ext.rows):,} "
                                                  "rows, total and column values"})
                best.verdict = validate(best.ext)
        for a in sorted(atts, key=lambda a: a is best):        # the best last: _route stops at an accepted one
            a.chosen = a is best
            yield a
        if best is None:
            first = next((a for a in atts if a.spec is not None), None)
            rejected = [f"Spec rejected: {a.error}" for a in ([first] if first else []) + [a for a in atts if a is not first]
                        if a.error]
            return None, rejected[:3] or ["No rows extracted"], first.spec if first else None
        return best, (best.verdict.get("feedback") or [best.error or "No rows extracted"]) + disagree, best.spec

    def _candidate_rank(self, att: Attempt) -> tuple:
        checks = att.verdict.get("checks", [])
        critical = any(c["status"] == "fail" and c["name"] in CRITICAL for c in checks)
        price_fails = sum(c["status"] == "fail" and c["name"] in PRICE_CHECKS for c in checks)
        return (not self._accept(att), critical, price_fails, -att.confidence)

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
            att = self._try("llm_extract_derived", spec, doc, extra_totals=self._printed_totals(attempts))
            att.llm_calls = 1
            yield att
            return
        # no deterministic spec reproduces the rows: use the LLM rows directly (never reused)
        df = _periods_out_of_descriptions(pd.DataFrame(rows))
        if not df.empty and "billed_cost" in df:
            df["billed_cost"] = pd.to_numeric(df["billed_cost"], errors="coerce")
            # the LLM's own sum is not printed: it proves nothing (kept as a claim); the tree's printed total checks it
            ext = Extraction(rows=df, stated_totals=self._all_printed_totals(attempts)
                             + ([{"label": "llm total", "amount": out["total"], "kind": "llm_claim"}]
                                if out.get("total") is not None else []))
            clean_rows(ext)
            coverage.apply(ext.rows)
            att = Attempt("llm_extract_direct", None, ext=ext, llm_calls=1)
            self._price_attempt(ext)
            att.verdict = validate(ext, text=doc_text(doc))
            att.verdict["confidence"] = min(att.verdict["confidence"], 0.65)   # never auto-accept
            yield att

    # ------------------------------------------------------------ helpers
    def _try(self, route: str, spec: dict, doc: Document, extra_totals: list[dict] | None = None) -> Attempt:
        att = Attempt(route, spec)
        try:
            att.ext = run(spec, doc, self.s.catalog_dir)
            if extra_totals and spec.get("source") != "tree":
                att.ext.stated_totals += [dict(t, kind="document_total") for t in extra_totals]
            clean_rows(att.ext)
            normalize_regions(att.ext.rows)
            extract_attributes(att.ext.rows, att.ext.warnings)
            coverage.apply(att.ext.rows)
            kind = (spec.get("meta") or {}).get("kind", "cost_lines")
            if kind != "cost_lines":
                att.verdict = {"status": "auxiliary", "confidence": 1.0 if not att.ext.rows.empty else 0.0,
                               "checks": [], "feedback": [], "kind": kind}
            else:
                self._price_attempt(att.ext)
                att.verdict = validate(att.ext)
        except (SpecError, Exception) as e:                     # noqa: BLE001
            att.error = f"{type(e).__name__}: {e}"
            att.verdict = {"status": "error", "confidence": 0.0, "checks": [], "feedback": [att.error]}
            if not isinstance(e, (SpecError, ValueError, KeyError)):
                att.verdict["trace"] = traceback.format_exc(limit=3)
        return att

    def _accept(self, att: Attempt) -> bool:
        if att.ext is None or att.verdict.get("status") in ("error", None) or not att.chosen:
            return False
        if att.verdict.get("status") == "auxiliary":
            return att.confidence > 0
        # a built-in spec is judged without its price checks: a failed one (a SKU the catalog does not have yet or no
        # longer has: an old bill, a SKU newer than the last price download) lowers the result's confidence, and may
        # make it need review, but never hands the file to another route, whose reading would replace the built-in's
        builtin = att.route == "builtin"
        if att.verdict.get("status") == "needs_review" and not (builtin and self._review_only_for_prices(att)):
            return False
        floor = self.s.builtin_accept if builtin else self.s.accept_confidence
        # a failed price check (a unit or column mix-up, products that do not exist) is not accepted at once: the next
        # routes run, and the most confident attempt wins (a wrong reading that adds up must not stop the search). Not
        # for a built-in spec: it reads a known export, so the failure is the data's, and the next routes (an LLM)
        # would cost money on every parse of the file
        if not builtin and any(c["status"] == "fail" and c["name"] in PRICE_CHECKS
                               for c in att.verdict.get("checks", [])):
            return False
        return (_without_price_fails(att) if builtin else att.confidence) >= floor

    @staticmethod
    def _review_only_for_prices(att: Attempt) -> bool:
        """The attempt needs review only because of its failed price checks: no critical check failed, and its
        confidence without them is at least what a result without review needs."""
        checks = att.verdict.get("checks", [])
        if any(c["status"] == "fail" and c["name"] in CRITICAL for c in checks):
            return False
        return _without_price_fails(att) >= REVIEW_BELOW

    def _alt_extraction(self, doc: Document, best: Attempt) -> Extraction | None:
        """Independent second extraction for the dual-extraction check."""
        if best.route in ("builtin", "mapper", "llm_extract_direct") or best.spec is None:
            return None
        if (best.spec.get("meta") or {}).get("kind", "cost_lines") != "cost_lines" or best.spec.get("reshape"):
            return None
        spec, _ = propose(doc, legacy=True)
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
                fp: dict, share: float | None, t0: float, big: bigfile.Scan | None = None,
                pricing: dict | None = None) -> dict:
        """Save the spec, the result and its contract. big: a large file read in chunks; doc is its sample, and the
        lines are the scan's rolled-up lines (the rows are in its Parquet file)."""
        rows = best.ext.rows if best and best.ext is not None else pd.DataFrame()
        fields = best.ext.fields if best and best.ext is not None else {}
        stated = best.ext.stated_totals if best and best.ext else []
        n_rows = len(rows)
        if big is not None:
            rows, fields, stated, n_rows = big.rolled, big.fields, big.stated_totals, big.rows
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
                # the price checks' statuses: the regression gate rejects a version that fails one this one passed
                checks = {c["name"]: c["status"] for c in verdict.get("checks", []) if c["name"] in PRICE_CHECKS}
                self.store.link(spec["id"], doc.sha256, fp,
                                {"rows": n_rows, "total": round(total, 2), "confidence": verdict["confidence"],
                                 "checks": checks})

        kind = verdict.get("kind") or ((best.spec or {}).get("meta") or {}).get("kind", "cost_lines") if best else None
        analysis = {}
        if kind == "cost_lines" and accepted and not rows.empty:
            analysis = analyze(rows, verdict["confidence"], verdict["status"], fields)
        summary = {
            "file": doc.name, "sha256": doc.sha256, "file_type": doc.file_type, "kind": kind,
            "status": verdict["status"], "confidence": verdict["confidence"],
            "completeness": verdict.get("completeness"),
            "evidence": (best.ext.evidence if best and best.ext and big is None else {}),
            "spec": spec_info, "rows": n_rows,
            "service_resolution": round(share, 3) if share is not None else None,
            "stated_totals": [{k: (v if k != "src" or v is None else str(v)) for k, v in t.items()} for t in stated][:10],
            "header_fields": {k: v["value"] for k, v in fields.items()},
            "header_field_src": {k: str(v["src"]) for k, v in fields.items() if v.get("src") is not None},
            "checks": verdict.get("checks", []),
            "warnings": _warnings(best, big),
            "analysis": analysis,
            "pricing": pricing,
            "attempts": [a.log() for a in attempts],
            "llm_calls": sum(a.llm_calls for a in attempts),
            "seconds": round(time.perf_counter() - t0, 3),
        }
        if big is not None:
            summary["large"] = {"source_rows": big.source_rows, "chunks": big.chunks, "rolled_lines": len(rows),
                                "skipped_rows": big.skipped}
        summary["contract"] = self._save_contract(doc, summary, rows)
        self.store.save_result(doc.sha256, summary, rows, rows_path=big.path if big and big.path
                               and big.path.exists() else None)
        if big is not None:
            self.store.save_rolled(doc.sha256, rows)
        self.store.append("records", {"ts": time.time(), "sha": doc.sha256, "file": doc.name, "fingerprint": fp,
                                      "route": spec_info and spec_info["route"], "spec": spec_info,
                                      "status": summary["status"], "confidence": summary["confidence"],
                                      "attempts": summary["attempts"], "llm_calls": summary["llm_calls"]})
        if summary["status"] in ("needs_review", "unsupported"):
            self.store.append("review", {"ts": time.time(), "sha": doc.sha256, "file": doc.name,
                                         "status": summary["status"], "open": True,
                                         "feedback": verdict.get("feedback", [])[:8]})
        return summary


def _periods_out_of_descriptions(df: pd.DataFrame) -> pd.DataFrame:
    """Rows the LLM wrote from a table with a column per month sometimes carry the month as their description:
    the month goes to charge_start / charge_end, and the description is left empty."""
    if df.empty or "description" not in df:
        return df
    found = df["description"].map(period_of)
    hit = found.notna() & (df["charge_start"].isna() if "charge_start" in df else True)
    if not hit.any():
        return df
    df = df.copy()
    for col, i in (("charge_start", 0), ("charge_end", 1)):
        if col not in df:
            df[col] = None
        df.loc[hit, col] = found[hit].map(lambda p, i=i: p[i])
    df.loc[hit, "description"] = None
    return df


CANDIDATE_NOTE = ("Write your own reading of this layout: another approach may have mapped columns wrong (quantity and "
                  "unit price, the cost column, dates, the region). Check each column mapping against the header and "
                  "the rows of the preview.")
AGREE_SAMPLE = 50                   # rows compared between two spec candidates


def _example_variant(examples: list, i: int) -> list:
    """Candidate i's examples: the same ones, in another order (the first candidate: as given)."""
    if not examples or i == 0:
        return examples
    k = i % len(examples)
    return list(reversed(examples[k:] + examples[:k]))


def _candidates_differ(a: Attempt, b: Attempt) -> list[str]:
    """Where two candidates' readings differ: the row count, the total (to the cent), and the values of each
    canonical column both map, on the first AGREE_SAMPLE rows. [] when they agree."""
    ra, rb = a.ext.rows, b.ext.rows
    if len(ra) != len(rb):
        return [f"the candidates disagree on the number of rows ({len(ra):,} vs {len(rb):,})"]
    ta = round(float(pd.to_numeric(ra.get("billed_cost"), errors="coerce").sum()), 2) if "billed_cost" in ra else 0.0
    tb = round(float(pd.to_numeric(rb.get("billed_cost"), errors="coerce").sum()), 2) if "billed_cost" in rb else 0.0
    if ta != tb:
        return [f"the candidates disagree on the total ({ta:,.2f} vs {tb:,.2f})"]
    mapped = set((a.spec or {}).get("columns") or {}) & set((b.spec or {}).get("columns") or {}) & set(CANONICAL)
    out = []
    for col in sorted(c for c in mapped if c in ra and c in rb):
        x, y = ra[col].head(AGREE_SAMPLE).reset_index(drop=True), rb[col].head(AGREE_SAMPLE).reset_index(drop=True)
        nx, ny = pd.to_numeric(x, errors="coerce"), pd.to_numeric(y, errors="coerce")
        numeric = nx.notna() & ny.notna()
        same = (numeric & ((nx - ny).abs() <= 1e-6)) | (~numeric & (x.astype(str).str.strip() == y.astype(str).str.strip()))
        if not same.all():
            out.append(f"the candidates disagree on column {col}")
    return out


def doc_text(doc: Document, max_chars: int = 20000) -> str:
    """The text of the first pages and sheets, one row per line: what a spec's detect.text is matched against."""
    out, n = [], 0
    for r in doc.regions:
        if r.is_frame:
            continue
        for row in r.rows:
            out.append(row.text)
            n += len(row.text) + 1
            if n >= max_chars:
                return "\n".join(out)
    return "\n".join(out)


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


def _without_price_fails(att: Attempt) -> float:
    """The attempt's confidence without the weight of its failed price checks (their weights are negative)."""
    return att.confidence - sum(float(c.get("weight") or 0.0) for c in att.verdict.get("checks", [])
                                if c["status"] == "fail" and c["name"] in PRICE_CHECKS)


def _settled(attempts: list[Attempt]) -> dict[str, str] | None:
    """The columns the mapper settled (score 3.0 or more), from its most confident attempt that settled any; the LLM
    keeps them and maps only the others."""
    mapped = [a for a in attempts if a.route == "mapper" and a.settled]
    return max(mapped, key=lambda a: a.confidence).settled if mapped else None


def _note_settled(att: Attempt, settled: dict[str, str] | None) -> None:
    """An LLM spec that changed a settled column is kept (the checks decide, as for any candidate), with a warning."""
    if not settled or not att.spec:
        return
    cols = att.spec.get("columns") or {}
    changed = [f for f, h in sorted(settled.items()) if h not in ((cols.get(f) or {}).get("from") or [])[:1]]
    if changed:
        warns = [f"the spec changed a settled column: {f}" for f in changed]
        att.verdict.setdefault("warnings", []).extend(warns)
        if att.ext is not None:
            att.ext.warnings.extend(warns)


def _warnings(best: Attempt | None, big: bigfile.Scan | None = None) -> list[str]:
    if best is None:
        return list(big.warnings) if big is not None else []
    out = list(big.warnings) if big is not None else list(best.ext.warnings) if best.ext else []
    note = ((best.spec or {}).get("meta") or {}).get("notes")
    if note:
        out.append(note)
    return out


def _public(spec: dict) -> dict:
    return {k: v for k, v in spec.items() if k not in ("stats", "fingerprint", "created_at", "version")}


