"""Run every file of a corpus folder through the pipeline and report what each one gave (phase 0 of docs/PLAN.md).

    uv run python scripts/corpus.py                         # CORPUS_DIR or ~/projects/cost_engine_inputs
    uv run python scripts/corpus.py --compare               # also show what changed since the last report
    uv run python scripts/corpus.py --only aws-             # files whose name contains the text
    uv run python scripts/corpus.py --agent                 # also run the agent on every file not proved
                                                            # (AGENT_ENABLED=1 and ANTHROPIC_API_KEY; costs money)

With --agent (or --store DIR) the store is kept in store/corpus/store, so the specs the agent saves are used by the
next run: the second run of a layout needs no agent.

The files stay where they are: each run parses them into a new scratch store, with the LLM off. The report goes to
store/corpus/<time>.json (store/ is not in git). Expected results written by hand go in store/corpus/expected.json:
{"file name": {"total": 13417.90, "status": "reconciled", "min_rows": 700}}; a file that does not meet them is
marked. A file that is not a bill or is bad data gets "known": the reason, with the status it should keep
({"status": "needs_review", "known": "not a bill: a savings proposal"}): while it keeps that status, the report lists
it apart, not as an open problem. The corpus is customer data: never copy it into the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import traceback
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from billparse import agent  # noqa: E402
from billparse.pipeline import Pipeline, Settings  # noqa: E402
from billparse.settings import load_env  # noqa: E402

OUT = ROOT / "store" / "corpus"
ATTRS = ("service", "region", "account_id", "instance_type", "database_engine", "operating_system", "usage_type",
         "quantity", "unit_price")
ACCEPTED = ("reconciled", "unreconciled", "consistent", "auxiliary")


def run_file(pipe: Pipeline, path: Path) -> dict:
    t0 = time.perf_counter()
    try:
        r = pipe.parse_file(path)
    except Exception as e:  # noqa: BLE001  the harness reports every failure and goes on
        return {"file": path.name, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc(limit=4),
                "seconds": round(time.perf_counter() - t0, 2)}
    # a large file's rows are not read whole: its rolled-up lines (sums and attribute shares by line kind)
    rows = pipe.store.get_rolled(r["sha256"]) if r.get("large") else pipe.store.get_rows(r["sha256"])
    n = 0 if rows is None else len(rows)
    coverage = {}
    if n:
        for a in ATTRS:
            if a in rows:
                coverage[a] = round(float(rows[a].notna().mean()), 3)
    a = r.get("analysis") or {}
    total = a.get("total_billed")
    if total is None and n and "billed_cost" in rows:
        total = round(float(rows["billed_cost"].sum()), 2)
    sums, lines = (sums_of(rows), lines_key(rows)) if n else ({}, None)
    return {"sums": sums, "lines_key": lines, "file": path.name, "sha256": r["sha256"], "file_type": r.get("file_type"), "kind": r.get("kind"), "status": r["status"],
            "completeness": r.get("completeness"), "confidence": r.get("confidence"), "rows": n, "total": total,
            "route": (r.get("spec") or {}).get("route"), "spec": (r.get("spec") or {}).get("id"),
            "failed": [c["name"] for c in r.get("checks", []) if c["status"] == "fail"],
            "passed": [c["name"] for c in r.get("checks", []) if c["status"] == "pass"],
            "stated": [t.get("amount") for t in r.get("stated_totals", [])][:3],
            "coverage": coverage, "seconds": round(time.perf_counter() - t0, 2)}


KIND = {"Tax": "tax", "Credit": "credit", "Refund": "credit", "Purchase": "commitment_fee",
        "Negation": "commitment_negation"}
MAX_KEYS = 300                                                   # a huge export keeps its largest keys and the rest


def sums_of(rows) -> dict:
    """The money of a file by service, account, region and kind: a wrong line under the right total shows here."""
    import pandas as pd
    cost = pd.to_numeric(rows["billed_cost"], errors="coerce").fillna(0.0) if "billed_cost" in rows else None
    if cost is None:
        return {}
    keys = {"service": rows["service"] if "service" in rows else rows.get("service_raw"),
            "account": rows.get("account_id"), "region": rows.get("region"),
            "kind": rows["charge_type"].map(lambda t: KIND.get(t, "usage") if isinstance(t, str) else "usage")
            if "charge_type" in rows else None}
    out = {}
    for name, key in keys.items():
        if key is None or key.isna().all():
            continue
        g = cost.groupby(key.fillna("(none)").astype(str)).sum().round(2)
        if len(g) > MAX_KEYS:
            top = g.abs().sort_values(ascending=False).index[:MAX_KEYS]
            g = pd.concat([g[top], pd.Series({"(the rest)": round(float(g.drop(top).sum()), 2)})])
        out[name] = {k: float(v) for k, v in g.items()}
    return out


def lines_key(rows) -> str | None:
    """A short fingerprint of which printed rows became lines and their amounts (order does not matter)."""
    import hashlib

    import pandas as pd
    if "_src" not in rows:
        return None
    cost = pd.to_numeric(rows["billed_cost"], errors="coerce").fillna(0.0) if "billed_cost" in rows else [0.0] * len(rows)
    pairs = sorted(f"{s}|{round(float(c), 6)}" for s, c in zip(rows["_src"].astype(str), cost))
    return hashlib.sha256("\n".join(pairs).encode()).hexdigest()[:16]


RANK = {"reconciled": 3, "consistent": 2, "unchecked": 1, "contradicted": 0}


def compare(base: dict, cur: dict) -> list[tuple[str, str]]:
    """What changed in one file since its baseline: ("worse" | "better" | "changed", what). A number that moves
    is "changed" until a person accepts it, even when the status stays."""
    if "error" in cur:
        return [("worse", f"error: {cur['error'][:80]}")]
    if "error" in base:
        return [("better", "no error now")]
    out = []
    rb, rc = RANK.get(base.get("completeness") or "", 1), RANK.get(cur.get("completeness") or "", 1)
    if rc != rb:
        out.append(("better" if rc > rb else "worse", f"completeness {base.get('completeness')} -> {cur.get('completeness')}"))
    if base.get("status") != cur.get("status") and rc == rb:
        out.append(("changed", f"status {base.get('status')} -> {cur.get('status')}"))
    if (base.get("total") is None) != (cur.get("total") is None) or \
            (base.get("total") is not None and abs(base["total"] - cur["total"]) > 0.005):
        out.append(("changed", f"total {base.get('total')} -> {cur.get('total')}"))
    if base.get("rows") != cur.get("rows"):
        out.append(("changed", f"lines {base.get('rows')} -> {cur.get('rows')}"))
    for dim, bsum in (base.get("sums") or {}).items():
        csum = (cur.get("sums") or {}).get(dim, {})
        moved = [k for k in set(bsum) | set(csum) if abs(bsum.get(k, 0.0) - csum.get(k, 0.0)) > 0.005]
        if moved:
            ex = "; ".join(f"{k[:30]} {bsum.get(k, 0):,.2f} -> {csum.get(k, 0):,.2f}" for k in sorted(moved)[:3])
            out.append(("changed", f"by {dim}: {len(moved)} key(s) moved ({ex})"))
    for dim in set(cur.get("sums") or {}) - set(base.get("sums") or {}):
        out.append(("better", f"new breakdown by {dim}"))
    if base.get("lines_key") and cur.get("lines_key") and base["lines_key"] != cur["lines_key"] and \
            not any(k == "changed" for k, _ in out):
        out.append(("changed", "the same totals from other printed rows"))
    for attr in set(base.get("coverage") or {}) | set(cur.get("coverage") or {}):
        b, c = (base.get("coverage") or {}).get(attr, 0.0), (cur.get("coverage") or {}).get(attr, 0.0)
        if abs(c - b) > 0.02:
            out.append(("better" if c > b else "worse", f"{attr} filled {b:.0%} -> {c:.0%}"))
    if len(cur.get("expected_failures") or []) > len(base.get("expected_failures") or []):
        out.append(("worse", "expected: " + "; ".join(cur["expected_failures"])))
    return out


def check_expected(res: dict, exp: dict) -> list[str]:
    bad = []
    if "total" in exp and (res.get("total") is None or abs(res["total"] - exp["total"]) > exp.get("tol", 0.011)):
        bad.append(f"total {res.get('total')} != {exp['total']}")
    if "status" in exp and res.get("status") != exp["status"]:
        bad.append(f"status {res.get('status')} != {exp['status']}")
    if "min_rows" in exp and (res.get("rows") or 0) < exp["min_rows"]:
        bad.append(f"rows {res.get('rows')} < {exp['min_rows']}")
    return bad


def with_agent(pipe: Pipeline, path: Path, res: dict) -> dict:
    """Run the agent on a file that is not proved, then measure it again; the job's outcome goes in the report."""
    summary = pipe.store.get_result(res["sha256"]) or {}
    if not agent.needs_agent(summary):
        return res
    job_id = agent.enqueue_for(pipe, summary)
    if job_id is None:                                          # this layout was reviewed before: run it again
        job_id = next(j["id"] for j in pipe.store.agent_jobs() if j["sha256"] == res["sha256"]
                      or j["layout_key"].endswith(summary.get("sha256", "")[:12]))
        pipe.store.retry_agent_job(job_id)
    job = agent.run_job(pipe, job_id)
    after = run_file(pipe, path)
    after["before"] = {k: res.get(k) for k in ("status", "completeness", "rows", "total")}
    after["agent"] = {k: job.get(k) for k in ("id", "status", "rounds", "tool_calls", "cost_usd", "spec_id", "note")}
    print(f"    agent job {job['id']}: {job['status']}, {job['rounds']} round(s), ${job['cost_usd']:.2f}, "
          f"spec {job.get('spec_id') or '-'}")
    return after


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, default=Path(os.environ.get("CORPUS_DIR", Path.home() / "projects/cost_engine_inputs")))
    ap.add_argument("--only", default="")
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--agent", action="store_true", help="run the agent on files that are not proved")
    ap.add_argument("--store", type=Path, help="keep the store here (default with --agent: store/corpus/store)")
    ap.add_argument("--check", action="store_true", help="compare with the accepted baseline; exit 1 on any worse "
                                                          "or changed file")
    ap.add_argument("--accept", action="store_true", help="accept this run as the baseline (the --only files, or all)")
    a = ap.parse_args()
    load_env()                                                  # ANTHROPIC_API_KEY and AGENT_* from .env
    if a.agent and not agent.enabled():
        raise SystemExit("--agent calls the Claude API and costs money: set AGENT_ENABLED=1 and ANTHROPIC_API_KEY.")
    keep = a.store or (OUT / "store" if a.agent else None)
    files = sorted(p for p in a.dir.iterdir() if p.is_file() and not p.name.startswith(".") and a.only in p.name)
    OUT.mkdir(parents=True, exist_ok=True)
    expected = json.loads((OUT / "expected.json").read_text()) if (OUT / "expected.json").exists() else {}
    previous = sorted(OUT.glob("2*.json"))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ.pop("DATABASE_URL", None)                    # never the real database
        store_dir = keep or Path(tmp)
        # the agent step runs here only with --agent (with_agent), so its outcome is measured on its own
        pipe = Pipeline(Settings(store_dir=store_dir, use_llm=False, retain_docs=keep is not None, use_agent=False))
        results = []
        for p in files:
            res = run_file(pipe, p)
            if a.agent and "error" not in res:
                res = with_agent(pipe, p, res)
            if p.name in expected:
                res["expected_failures"] = check_expected(res, expected[p.name])
            results.append(res)
            flag = "  !" + "; ".join(res["expected_failures"]) if res.get("expected_failures") else ""
            if "error" in res:
                print(f"{p.name[:46]:46} ERROR {res['error'][:90]}")
            else:
                print(f"{p.name[:46]:46} {res['file_type'] or '':8} {res['status']:13} {str(res.get('completeness') or ''):12}"
                      f" {res['rows']:6} rows  total={res['total'] if res['total'] is not None else '':<12} "
                      f"route={res['route']}{flag}")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (OUT / f"{stamp}.json").write_text(json.dumps(results, indent=1, default=str))
    ok = [r for r in results if "error" not in r]
    print(f"\n{len(results)} files: {Counter(r['status'] for r in ok)}  errors: {len(results) - len(ok)}")
    print("completeness:", dict(Counter(r.get("completeness") for r in ok)))
    print("accepted:", sum(r["status"] in ACCEPTED for r in ok), " expected failures:",
          sum(bool(r.get("expected_failures")) for r in results), f"of {sum(r['file'] in expected for r in results)}")
    known = [r for r in ok if (expected.get(r["file"]) or {}).get("known") and not r.get("expected_failures")]
    if known:                                                   # not bills or bad data, with the status they should keep
        print(f"known ({len(known)}, not open problems):")
        for r in known:
            print(f"  {r['file'][:50]:50} {r['status']:13} {expected[r['file']]['known']}")
    open_ = [r for r in ok if r["status"] not in ACCEPTED and r not in known]
    print(f"open: {len(open_)}" + "".join(f"\n  {r['file'][:50]:50} {r['status']}" for r in open_))
    if a.compare and previous:
        before = {r["file"]: r for r in json.loads(previous[-1].read_text())}
        print(f"\nchanges since {previous[-1].name}:")
        for r in results:
            b = before.get(r["file"])
            if b and (b.get("status"), b.get("rows"), b.get("total")) != (r.get("status"), r.get("rows"), r.get("total")):
                print(f"  {r['file'][:46]:46} {b.get('status')}/{b.get('rows')}/{b.get('total')} -> "
                      f"{r.get('status')}/{r.get('rows')}/{r.get('total')}")
    failed = check_baseline(results, a.only) if a.check else False
    if a.accept:
        accept_baseline(results)
    jobs = [r["agent"] for r in results if r.get("agent")]
    if jobs:
        print(f"agent: {len(jobs)} job(s) {dict(Counter(j['status'] for j in jobs))}, "
              f"cost ${sum(j['cost_usd'] or 0 for j in jobs):.2f}")
    print(f"report: {OUT / (stamp + '.json')}")
    if failed:
        raise SystemExit(1)


BASELINE = OUT / "baseline.json"


def check_baseline(results: list[dict], only: str) -> bool:
    """Print every file that differs from its accepted baseline; True when one is worse or changed."""
    base = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    if not base:
        print("\nno baseline yet: run with --accept after you have checked this run")
        return False
    groups: dict[str, list[str]] = {"worse": [], "changed": [], "better": []}
    seen = set()
    for r in results:
        seen.add(r["file"])
        if r["file"] not in base:
            groups["changed"].append(f"{r['file'][:50]}: new file, not in the baseline")
            continue
        for kind, what in compare(base[r["file"]], r):
            groups[kind].append(f"{r['file'][:50]}: {what}")
    for name in sorted(set(base) - seen):
        if only in name:
            groups["worse"].append(f"{name[:50]}: in the baseline, not in this run")
    for kind in ("worse", "changed", "better"):
        if groups[kind]:
            print(f"\n{kind.upper()} ({len(groups[kind])}):")
            for line in groups[kind]:
                print(f"  {line}")
    bad = bool(groups["worse"] or groups["changed"])
    print("\nbaseline check: " + ("FAILED (accept the changes that are right: --accept --only <file>)" if bad else
                                   "passed, nothing changed" if not groups["better"] else "passed, only better"))
    return bad


def accept_baseline(results: list[dict]) -> None:
    base = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    keep = ("status", "completeness", "rows", "total", "sums", "lines_key", "coverage", "expected_failures", "error")
    for r in results:
        base[r["file"]] = {k: r[k] for k in keep if k in r}
    BASELINE.write_text(json.dumps(base, indent=1, sort_keys=True, default=str))
    print(f"baseline: {len(results)} file(s) accepted into {BASELINE} ({len(base)} in all)")


if __name__ == "__main__":
    main()
