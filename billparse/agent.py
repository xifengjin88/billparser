"""The agent: an LLM that reviews a file no spec proved, writes or changes a spec for its layout, and proves it.

The LLM never reads a bill into lines. It writes a spec (JSON that the interpreter runs). Only the code decides:

  evidence report (code)  ->  author (Claude with tools on the real file)  ->  verify (code)  ->  regression gate (code)
                                   ^                                              |
                                   +---------- the failed checks, as feedback ----+   (at most AGENT_MAX_ROUNDS)

  proved (the checks say reconciled, and the gate passed): the spec is saved as a candidate and the file parsed
  again with it; every later file of that layout uses it with no LLM call.
  not proved after the last round: the file goes to a person, with the agent's note and the rows involved.

Tools (they run on the real file, as the interpreter reads it): list_regions, read_rows, row_shapes, find_rows,
test_regex, find_service, check_spec (the interpreter and every check on the whole file), submit_spec.

Limits per job (AGENT_* in example.env): rounds, tool calls per round, estimated cost, wall time, output tokens per
call. A limit ends the job with the best result so far. Every request, tool call and check is written to a trace
(<data dir>/agent/<job>.jsonl) that the web app shows.

The model and its key come from the environment (BILLPARSE_AGENT_MODEL, ANTHROPIC_API_KEY): nothing runs without a
key, and tests use a fake client with the same shape.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import coverage
from .attributes import extract as extract_attributes
from .docmodel import Document, load_bytes
from .llm import PRICES, SPEC_GUIDE, cost_of, redact  # noqa: F401  PRICES: the web API lists them
from .spec import Extraction, SpecError, check_spec, run
from .store import Store
from .tree import money_in
from .validate import clean_rows, validate

MODEL = "claude-opus-5-5"
MAX_TOOL_TEXT = 12000


@dataclass
class Limits:
    rounds: int = 3
    tool_calls: int = 40
    max_cost_usd: float = 3.0
    max_seconds: float = 900.0
    max_tokens: int = 16000
    model: str = MODEL
    effort: str = "high"

    @classmethod
    def from_env(cls) -> Limits:
        e = os.environ.get
        return cls(rounds=int(e("AGENT_MAX_ROUNDS", 3)), tool_calls=int(e("AGENT_TOOL_CALLS", 40)),
                   max_cost_usd=float(e("AGENT_MAX_COST_USD", 3.0)), max_seconds=float(e("AGENT_MAX_SECONDS", 900)),
                   max_tokens=int(e("AGENT_MAX_TOKENS", 16000)), model=e("BILLPARSE_AGENT_MODEL") or MODEL,
                   effort=e("AGENT_EFFORT", "high"))


def enabled() -> bool:
    return os.environ.get("AGENT_ENABLED", "0") == "1" and bool(os.environ.get("ANTHROPIC_API_KEY"))


# ---------------------------------------------------------------- the model
class AgentLLM(Protocol):
    def create(self, system: str, messages: list[dict], tools: list[dict], limits: Limits) -> dict:
        """One Messages API call. Returns {"content": [blocks as dicts], "stop_reason": str, "usage": {...}}."""
        ...


class ClaudeClient:
    """The Messages API through the anthropic SDK, with the server-side fallback on refusals."""

    def __init__(self) -> None:
        import anthropic
        self.client = anthropic.Anthropic()

    def create(self, system: str, messages: list[dict], tools: list[dict], limits: Limits) -> dict:
        r = self.client.beta.messages.create(
            model=limits.model, max_tokens=limits.max_tokens, system=system, messages=messages, tools=tools,
            tool_choice={"type": "auto"}, output_config={"effort": limits.effort},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        usage = r.usage.model_dump(mode="json") if r.usage else {}
        return {"content": [b.model_dump(mode="json", exclude_none=True) for b in r.content],
                "stop_reason": r.stop_reason, "usage": usage, "model": r.model}



# ---------------------------------------------------------------- the file, as the tools see it
def _tool(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": props, "required": required, "additionalProperties": False}}


TOOLS = [
    _tool("list_regions", "The file's pages, sheets and tables: name, kind, number of rows.", {}, []),
    _tool("read_rows", "Rows of one region, in order (at most 120): index, text, x position, bold, the amounts found.",
          {"region": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}},
          ["region", "start", "end"]),
    _tool("row_shapes", "Every kind of row in the whole file (indent, bold, number of amounts, has words), how many "
          "rows of each, and where examples are. Start here.", {}, []),
    _tool("find_rows", "Rows whose text matches a regex (case-insensitive; at most 60), with region and index.",
          {"regex": {"type": "string"}}, ["regex"]),
    _tool("test_regex", "What a regex matches on every text row, with its named groups (at most 40 matches).",
          {"regex": {"type": "string"}}, ["regex"]),
    _tool("find_service", "Catalog services for a label (what a line's service must resolve to).",
          {"label": {"type": "string"}}, ["label"]),
    _tool("check_spec", "Run the real interpreter and every check on the whole file with this spec. Returns the "
          "status, completeness, the failed checks with the rows involved, totals, and sample lines.",
          {"spec": {"type": "object"}}, ["spec"]),
    _tool("submit_spec", "Your answer for this round: the spec, and one sentence on what is still wrong (or 'none').",
          {"spec": {"type": "object"}, "note": {"type": "string"}}, ["spec", "note"]),
]


class Workspace:
    def __init__(self, doc: Document, catalog_dir: Path | None):
        self.doc = doc
        self.catalog_dir = catalog_dir
        self.text_rows = [(reg.name, i, row) for reg in doc.regions if not reg.is_frame for i, row in enumerate(reg.rows)]

    def call(self, name: str, args: dict) -> tuple[str, bool]:
        """(result text, is_error)."""
        try:
            out = getattr(self, f"t_{name}")(**args)
        except (TypeError, KeyError, ValueError, re.error, SpecError) as e:
            return f"{type(e).__name__}: {e}", True
        text = out if isinstance(out, str) else json.dumps(out, default=str)
        return redact(text[:MAX_TOOL_TEXT] + (" ...(cut)" if len(text) > MAX_TOOL_TEXT else "")), False

    def t_list_regions(self) -> list[dict]:
        return [{"name": r.name, "kind": r.kind, "rows": len(r.df) if r.is_frame else len(r.rows),
                 **({"columns": [str(c) for c in r.df.columns][:60]} if r.is_frame else {})} for r in self.doc.regions]

    def t_read_rows(self, region: str, start: int, end: int) -> list[dict]:
        reg = next(r for r in self.doc.regions if r.name == region)
        if reg.is_frame:
            return reg.df.iloc[start:min(end, start + 120)].astype(str).to_dict("records")
        return [self._row(i, row) for i, row in enumerate(reg.rows[start:min(end, start + 120)], start)]

    def _row(self, i: int, row) -> dict:
        return {"i": i, "text": row.text, "x": round(row.first_x, 1), "bold": row.bold,
                "amounts": [v for v, *_ in money_in(row.text)]}

    def t_row_shapes(self) -> list[dict]:
        shapes: dict[tuple, list] = {}
        for region, i, row in self.text_rows:
            amounts = money_in(row.text)
            words = bool(re.search(r"[A-Za-z]{2,}", re.sub(r"[\d.,$€£()\-]", " ", row.text)))
            key = (round(row.first_x / 3) * 3, row.bold, min(len(amounts), 3), words)
            shapes.setdefault(key, []).append((region, i, row.text[:90]))
        out = [{"x": k[0], "bold": k[1], "amounts": k[2], "words": k[3], "rows": len(v),
                "examples": [f"{r}#{i}: {t}" for r, i, t in v[:2] + v[len(v) // 2:len(v) // 2 + 1] + v[-1:]]}
               for k, v in shapes.items()]
        return sorted(out, key=lambda s: -s["rows"])[:40]

    def t_find_rows(self, regex: str) -> list[dict]:
        rx = re.compile(regex, re.I)
        return [{"region": r, **self._row(i, row)} for r, i, row in self.text_rows if rx.search(row.text)][:60]

    def t_test_regex(self, regex: str) -> list[dict]:
        rx = re.compile(regex)
        out = []
        for r, i, row in self.text_rows:
            m = rx.search(row.text)
            if m:
                out.append({"region": r, "i": i, "match": m.group(0)[:120], "groups": m.groupdict()})
                if len(out) >= 40:
                    break
        return out

    def t_find_service(self, label: str) -> list[dict]:
        from .analyze import RESOLVER_LOCK, get_resolver
        if self.catalog_dir is None:
            return []
        with RESOLVER_LOCK:
            res = get_resolver(Path(self.catalog_dir))
            r = res.resolve(label)
        keys = [r.service_key] if r.service_key else [k for k, _ in (r.candidates or [])]
        return [{"key": k, **res.cat.services.get(k, {}), "how": r.method} for k in keys[:8]]

    def t_check_spec(self, spec: dict) -> dict:
        return check(spec, self.doc, self.catalog_dir)[0]

    def t_submit_spec(self, spec: dict, note: str) -> str:
        return "received"


def check(spec: dict, doc: Document, catalog_dir: Path | None) -> tuple[dict, Extraction | None, dict]:
    """Run a spec on the whole file with every check: (a short report for the agent, the extraction, the verdict)."""
    check_spec(spec)
    ext = run(spec, doc, catalog_dir)
    clean_rows(ext)
    extract_attributes(ext.rows)
    coverage.apply(ext.rows)
    kind = (spec.get("meta") or {}).get("kind", "cost_lines")
    verdict = validate(ext) if kind == "cost_lines" else {"status": "auxiliary", "confidence": 1.0, "checks": [],
                                                           "completeness": "consistent", "feedback": []}
    rows = ext.rows
    total = float(rows["billed_cost"].sum()) if "billed_cost" in rows and len(rows) else 0.0
    ev = ext.evidence or {}
    sample_cols = [c for c in ("description", "service_raw", "billed_cost", "charge_type", "account_id", "region",
                               "instance_type", "_path", "_src") if c in rows]
    report = {
        "status": verdict["status"], "completeness": verdict.get("completeness"), "confidence": verdict["confidence"],
        "rows": len(rows), "sum_of_lines": round(total, 2),
        "stated_totals": [{k: t.get(k) for k in ("label", "amount", "kind")} for t in ext.stated_totals][:6],
        "checks": [{k: c[k] for k in ("name", "status", "detail")} for c in verdict.get("checks", [])],
        "warnings": ext.warnings[:5],
        **({"tree": {k: ev.get(k) for k in ("groups", "subtotals_failed", "unexplained", "informational")}} if ev else {}),
        "sample_lines": rows[sample_cols].head(12).astype(str).to_dict("records") if len(rows) else [],
    }
    return report, ext, verdict


def evidence_report(doc: Document, summary: dict) -> str:
    """What the code knows about the file before the agent starts."""
    parts = [f"File: {doc.name} ({doc.file_type}), regions: {len(doc.regions)}",
             f"Current result: status {summary.get('status')}, completeness {summary.get('completeness')}, "
             f"{summary.get('rows')} lines, read by {(summary.get('spec') or {}).get('route')}"]
    failed = [c for c in summary.get("checks", []) if c["status"] in ("fail", "warn")]
    if failed:
        parts.append("Checks that failed or warned:\n" + "\n".join(f"- {c['name']}: {c['detail']}" for c in failed))
    ev = summary.get("evidence") or {}
    if ev.get("unexplained"):
        parts.append("Amounts nothing explains: " + json.dumps(ev["unexplained"][:10]))
    if ev.get("subtotals_failed"):
        parts.append("Groups that do not add up: " + json.dumps(ev["subtotals_failed"][:10]))
    tried = [f"{a['route']} {a.get('spec_id') or ''}: {a.get('status')} ({a.get('confidence')})"
             for a in summary.get("attempts", [])]
    if tried:
        parts.append("Specs tried: " + "; ".join(tried))
    return redact("\n".join(parts))


AGENT_GUIDE = SPEC_GUIDE.replace("Return ONLY the JSON object.", "") + """

More spec keys:
- "source": "tree" reads pages and text lines by their amounts: totals, subtotals and lines found from the sums (a
  row whose amount equals the sum of the rows under it is a subtotal). Options in "tree" (all optional):
  {"ignore": regex of rows to drop, "skip_from": regex of the row from which the rest of the file is left out (a
   later part that prints the same money again by another dimension, e.g. per account after a by-service detail),
   "section": regex of rows that start a section, "auto_sections": bool,
   "repeated_header": regex of a column header printed again above items, "informational": regex of rows that are
   information not money ("Highest service spend"), "trailer": regex of text rows that belong to the amount row above
   (a period), "currency": "USD", "amount": "last"|"first", "furniture_share": 0.6, "furniture_top": 3,
   "furniture_bottom": 12, "heading_bold": bool, "heading_max_amounts": 1, "table_row_amounts": 3, "min_total": 1.0,
   "link_runs": bool, "max_combo": 3}
- "context" (tree source): {"account": regex with named groups id and name, "region": regex of region headings,
  "not_service": regex of headings that are not services, "label_split": " / " for labels printed in two languages,
  "charge_types": [["Tax", regex], ["Credit", regex], ...] matched on a line's label, then the labels above it}
- "detect": {"text": regex} matches the text of the first pages (use it for PDFs and text files, which have no
  table header): a spec is used for a later file only if its detect matches.

How to work:
1. Call row_shapes and read_rows to see the layout. Find the printed totals and how the lines nest under them.
2. Write a spec. For PDFs and text, start from {"source": "tree"} and add options only for what the checks show is
   wrong; use "lines" or "grid" when the layout is a plain list or table. Always set "detect" and "meta".
3. Call check_spec. The goal is completeness "reconciled": the lines add up to a printed total, every group adds up,
   and every amount is a line, a total or a repeat. Read the failed checks and the rows they name, change the spec,
   check again.
4. Call submit_spec with the spec that checks best and one sentence on what is still wrong.
Never put a value of this one file (an account number, a date, an amount) into a spec: a spec describes the layout,
so it must work on every bill of this layout."""


# ---------------------------------------------------------------- one job
@dataclass
class Outcome:
    status: str                                 # proved | unproved | limit | error
    spec: dict | None = None
    report: dict | None = None
    note: str = ""
    rounds: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    trace: list[dict] = field(default_factory=list)


def author(doc: Document, summary: dict, llm: AgentLLM, limits: Limits, catalog_dir: Path | None,
           examples: list[dict], start_spec: dict | None = None, trace=None, should_stop=None) -> Outcome:
    """Run the agent on one file until a spec proves it or a limit stops it. Nothing is saved here."""
    t0 = time.time()
    ws = Workspace(doc, catalog_dir)
    out = Outcome("unproved")
    log = trace or (lambda **e: out.trace.append(e))
    best: tuple[int, dict, dict] | None = None              # (score, spec, report)
    intro = evidence_report(doc, summary)
    if examples:
        intro += "\n\nSaved specs of similar files (examples, not answers):\n" + "\n".join(json.dumps(e)[:3000] for e in examples)
    if start_spec:
        intro += "\n\nThe current spec for this layout (improve it; keep what works on other files):\n" + json.dumps(start_spec)
    messages: list[dict] = [{"role": "user", "content": intro + "\n\nWrite a spec for this file's layout."}]
    for rnd in range(1, limits.rounds + 1):
        out.rounds = rnd
        submitted: dict | None = None
        last = messages[-1]["content"]
        log(event="round", round=rnd, task=last if isinstance(last, str) else "Continue: fix what the checks name")
        calls_this_round = 0
        while submitted is None:
            if should_stop is not None and should_stop():
                out.status, out.note = "stopped", "Stopped by a person"
                return _best(out, best)
            if out.cost_usd >= limits.max_cost_usd or time.time() - t0 >= limits.max_seconds:
                out.status, out.note = "limit", f"Stopped at the job's limit (${out.cost_usd:.2f}, {time.time() - t0:.0f} s)"
                return _best(out, best)
            t_call = time.time()
            resp = llm.create(AGENT_GUIDE, messages, TOOLS, limits)
            call_cost = cost_of(resp.get("usage") or {}, resp.get("model") or limits.model)
            out.cost_usd += call_cost
            log(event="response", round=rnd, stop_reason=resp.get("stop_reason"), usage=resp.get("usage"),
                cost_usd=round(call_cost, 5), ms=int((time.time() - t_call) * 1000), model=resp.get("model"),
                blocks=[_shown(b) for b in resp["content"] if b.get("type") in ("text", "tool_use", "thinking")])
            messages.append({"role": "assistant", "content": resp["content"]})
            if resp.get("stop_reason") == "refusal":
                out.status, out.note = "error", "The model declined the request"
                return _best(out, best)
            uses = [b for b in resp["content"] if b.get("type") == "tool_use"]
            if not uses:
                messages.append({"role": "user", "content": "Call check_spec, then submit_spec with your spec."})
                calls_this_round += 1
                if calls_this_round > limits.tool_calls:
                    break
                continue
            results = []
            for u in uses:
                out.tool_calls += 1
                calls_this_round += 1
                text, err = ws.call(u["name"], u.get("input") or {})
                log(event="tool", round=rnd, id=u["id"], name=u["name"], error=err, result=text[:4000])
                if u["name"] == "submit_spec" and not err:
                    submitted = (u.get("input") or {}).get("spec")
                    out.note = (u.get("input") or {}).get("note", "")
                    log(event="author", round=rnd, note=out.note)
                results.append({"type": "tool_result", "tool_use_id": u["id"], "content": text,
                                **({"is_error": True} if err else {})})
            if calls_this_round >= limits.tool_calls and submitted is None:
                results.append({"type": "text", "text": "Tool call limit for this round: call submit_spec now."})
            messages.append({"role": "user", "content": results})
        if submitted is None:
            continue
        # verify: only the code's own checks decide
        try:
            report, _, verdict = check(submitted, doc, catalog_dir)
        except (SpecError, ValueError, KeyError, re.error) as e:
            report, verdict = {"error": str(e)}, {"completeness": None, "status": "error", "confidence": 0.0}
        gates = [{"gate": c["name"], "ok": c["status"] != "fail"} for c in report.get("checks", [])] \
            or [{"gate": "runs", "ok": False}]
        log(event="verify", round=rnd, ok=verdict.get("completeness") == "reconciled", gates=gates,
            completeness=verdict.get("completeness"), report=report)
        score = _score(verdict)
        if best is None or score > best[0]:
            best = (score, submitted, report)
        if verdict.get("completeness") == "reconciled":
            out.status, out.spec, out.report = "proved", submitted, report
            return out
        messages.append({"role": "user", "content": "The code's checks on your submitted spec:\n"
                         + json.dumps(report, default=str)[:MAX_TOOL_TEXT]
                         + "\nIt is not reconciled. Fix what the checks name and submit again."})
    return _best(out, best)


def _shown(block: dict) -> dict:
    """A reply block as the trace keeps it: text, a tool call (name, input), or a reasoning summary."""
    if block.get("type") == "tool_use":
        return {"type": "tool_use", "id": block.get("id"), "name": block.get("name"), "input": block.get("input")}
    if block.get("type") == "thinking":
        return {"type": "thinking", "text": (block.get("thinking") or "")[:4000]}
    return {"type": "text", "text": (block.get("text") or "")[:4000]}


def _score(verdict: dict) -> int:
    return {"reconciled": 3, "consistent": 2, "unchecked": 1}.get(verdict.get("completeness") or "", 0)


def _best(out: Outcome, best) -> Outcome:
    if best is not None:
        out.spec, out.report = best[1], best[2]
    return out


# ---------------------------------------------------------------- jobs (store, trace, decision)
def trace_path(store: Store, job_id: int) -> Path:
    p = Path(store.root) / "agent"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{job_id}.jsonl"


def read_trace(store: Store, job_id: int) -> list[dict]:
    p = trace_path(store, job_id)
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []


def run_job(pipeline, job_id: int, llm: AgentLLM | None = None, limits: Limits | None = None) -> dict:
    """Run one queued job: author, verify, gate, decide, save. Returns the job as a dict."""
    from .fingerprint import fingerprint
    from .learn import regression_gate
    store: Store = pipeline.store
    job = store.agent_job(job_id)
    if job is None:
        raise KeyError(job_id)
    limits = limits or Limits.from_env()
    loaded = store.load_doc(job["sha256"])
    if loaded is None:
        store.update_agent_job(job_id, status="error", error="The uploaded file is not stored")
        return store.agent_job(job_id)
    data, name = loaded
    doc = load_bytes(data, name)
    summary = store.get_result(job["sha256"]) or {}
    start_spec = store.get_spec(job["spec_id"]) if job.get("spec_id") else None
    t0 = time.time()
    store.update_agent_job(job_id, status="running", started=t0, attempts=(job.get("attempts") or 0) + 1)
    path = trace_path(store, job_id)
    path.write_text("")

    def log(**event):
        with path.open("a") as f:
            f.write(json.dumps({"ts": time.time(), **event}, default=str) + "\n")

    def should_stop() -> bool:
        j = store.agent_job(job_id)
        return j is not None and j["status"] == "stopping"

    try:
        llm = llm or ClaudeClient()
        fp = fingerprint(doc)
        examples = [e for _, e in pipeline._examples(fp)][:3]
        log(event="job", harness=f"billparse agent ({limits.model}, tools)", kind=job["kind"], model=limits.model,
            effort=limits.effort,
            limits={k: getattr(limits, k) for k in ("rounds", "tool_calls", "max_cost_usd", "max_seconds", "max_tokens")},
            examples=len(examples), start_spec=start_spec, evidence=evidence_report(doc, summary))
        out = author(doc, summary, llm, limits, pipeline.s.catalog_dir, examples, start_spec, trace=log,
                     should_stop=should_stop)
    except Exception as e:  # noqa: BLE001  a job never takes the API down; the error is kept on the job
        log(event="error", error=f"{type(e).__name__}: {e}")
        store.update_agent_job(job_id, status="error", error=f"{type(e).__name__}: {e}", seconds=time.time() - t0)
        return store.agent_job(job_id)
    fields = {"rounds": out.rounds, "tool_calls": out.tool_calls, "cost_usd": round(out.cost_usd, 4),
              "note": out.note[:2000], "seconds": round(time.time() - t0, 1), "report": out.report}
    log(event="finish", outcome=out.status, rounds=out.rounds, cost_usd=round(out.cost_usd, 4), note=out.note)
    if out.status != "proved" or out.spec is None:
        log(event="decision", status=out.status, note=out.note)
        store.update_agent_job(job_id, status=out.status, **fields)
        return store.agent_job(job_id)
    spec_id = job.get("spec_id") or f"agent-{doc.file_type}-{fp['hash'][:10]}"
    spec = {k: v for k, v in out.spec.items() if k not in ("id", "version", "trust", "created_at", "stats")}
    spec = dict(spec, id=spec_id, trust="candidate", fingerprint=fp,
                stats={"created_by": "agent", "job_id": job_id, "model": limits.model})
    if start_spec is not None:                         # a new version must still read every file the spec read
        gate = regression_gate(pipeline, spec, spec_id)
        log(event="gate", gate=gate)
        if not gate["passed"]:
            store.update_agent_job(job_id, status="rejected", **fields,
                                   error=f"Regression gate: {gate['failures'][:3]}")
            return store.agent_job(job_id)
    saved = store.save_spec(spec)
    log(event="decision", status="proved", spec_id=spec_id, version=saved["version"])
    store.update_agent_job(job_id, status="proved", spec_id=spec_id, spec_version=saved["version"], **fields)
    pipeline.parse_bytes(data, name, force=True, agent=False, run=False)  # read again: the learned route finds it
    return store.agent_job(job_id)


def needs_agent(result: dict) -> bool:
    """A result the agent should review: cost lines that a check contradicts, or that nothing could test (nothing read
    included), and that no person approved. A consistent result (the file prints no total to test, and every internal
    check passes) is not sent: no spec can prove a total the file does not print, so a job would only cost money."""
    seen = {result.get("completeness"), (result.get("contract") or {}).get("completeness")} - {None}
    open_ = not seen or bool(seen & {"contradicted", "unchecked"})
    return result.get("kind") in (None, "cost_lines") and result.get("status") not in ("unsupported", "approved") \
        and open_ and not result.get("cached")


def enqueue_for(pipeline, result: dict) -> int | None:
    """Queue a job for this result's layout (one job per layout fingerprint, or per spec version for a repair)."""
    store: Store = pipeline.store
    spec = result.get("spec") or {}
    learned = spec.get("route") == "learned" and spec.get("id")
    return store.enqueue_agent_job(job_key(store, result), result["sha256"], result["file"],
                                   kind="repair" if learned else "new", spec_id=spec["id"] if learned else None)


def job_key(store: Store, result: dict) -> str:
    """The key of this result's job: its layout fingerprint, or the spec version it repairs."""
    spec = result.get("spec") or {}
    if spec.get("route") == "learned" and spec.get("id"):
        return f"repair:{spec['id']}@v{spec.get('version')}:{result['sha256'][:12]}"
    return f"layout:{_layout_key(store, result)}"


def _layout_key(store: Store, result: dict) -> str:
    rec = next(iter(reversed(store.read("records", sha=result["sha256"]))), None)
    fp = (rec or {}).get("fingerprint") or {}
    return fp.get("hash") or result["sha256"][:16]
