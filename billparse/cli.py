"""Command line.

    python -m billparse parse invoice.pdf [--json] [--no-llm] [--force]
    python -m billparse nightly
    python -m billparse specs [list | show ID | approve ID | rollback ID VERSION | test ID]
    python -m billparse review
    python -m billparse metrics
    python -m billparse synonym "VM Hrs" "azure:Virtual Machines"
"""

from __future__ import annotations

import argparse
import json
import sys

from .pipeline import Pipeline, Settings


def _print_summary(r: dict) -> None:
    a = r.get("analysis") or {}
    spec = r.get("spec") or {}
    print(f"\n{r['file']}  [{r['file_type']}]  {'(cached)' if r.get('cached') else ''}")
    print(f"  status: {r['status']}   confidence: {r['confidence']}   rows: {r['rows']}   "
          f"time: {r.get('seconds')}s   llm calls: {r.get('llm_calls', 0)}")
    if spec:
        print(f"  spec: {spec.get('id')} v{spec.get('version')} ({spec.get('trust')}) via {spec.get('route')}"
              + (f"  [{spec['format']}]" if spec.get("format") else ""))
    for c in r.get("checks", []):
        mark = {"pass": "+", "fail": "x", "warn": "!", "skip": "-"}[c["status"]]
        print(f"    {mark} {c['name']}: {c['detail']}")
    for w in r.get("warnings", []):
        print(f"    ! {w}")
    if a:
        cur = a.get("currency") or ""
        print(f"  total billed {a['total_billed']:,.2f} {cur} | usage {a['usage_spend']:,.2f}"
              + (f" | other {a['non_usage']}" if a.get("non_usage") else ""))
        if r.get("service_resolution") is not None:
            print(f"  services resolved: {r['service_resolution']:.0%} of cost")
        for s in a.get("top_services", [])[:8]:
            print(f"    {s['service'][:40]:40} {s['cost']:>12,.2f}  {s['share'] or 0:>5.0%}")
        f = a.get("forecast", {})
        if f:
            nm = f["next_month"]
            print(f"  next month: {nm['expected']:,.2f} ({nm['low']:,.2f} to {nm['high']:,.2f})  [{f['method']}]")
        c = a.get("commitment_opportunity", {})
        if c.get("eligible_monthly_spend"):
            lo, hi = c["indicative_monthly_savings"]
            print(f"  commitment-eligible spend {c['eligible_monthly_spend']:,.2f}/mo; "
                  f"indicative savings {lo:,.2f} to {hi:,.2f}/mo")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="billparse")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("parse")
    p.add_argument("files", nargs="+")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--force", action="store_true")
    sub.add_parser("nightly")
    sp = sub.add_parser("specs")
    sp.add_argument("action", nargs="?", default="list", choices=["list", "show", "approve", "rollback", "test"])
    sp.add_argument("id", nargs="?")
    sp.add_argument("version", nargs="?", type=int)
    sub.add_parser("review")
    sub.add_parser("metrics")
    sy = sub.add_parser("synonym")
    sy.add_argument("raw")
    sy.add_argument("service_key")
    a = ap.parse_args(argv)

    pipe = Pipeline(Settings(use_llm=not getattr(a, "no_llm", False)))
    if a.cmd == "parse":
        for f in a.files:
            r = pipe.parse_file(f, force=a.force)
            print(json.dumps(r, indent=1, default=str)) if a.json else _print_summary(r)
    elif a.cmd == "nightly":
        from .learn import run_nightly
        print(json.dumps(run_nightly(pipe), indent=1, default=str))
    elif a.cmd == "specs":
        _specs(pipe, a)
    elif a.cmd == "review":
        for r in pipe.store.read("review"):
            if r.get("open"):
                print(f"{r['file']}  {r['status']}  {'; '.join(r['feedback'][:2])}")
    elif a.cmd == "metrics":
        from .learn import metrics
        print(json.dumps(metrics(pipe.store.read("records")), indent=1))
    elif a.cmd == "synonym":
        from .analyze import get_resolver
        get_resolver(pipe.s.catalog_dir).add_synonym(a.raw, a.service_key)
        print(f"Saved {a.raw!r} -> {a.service_key}")


def _specs(pipe: Pipeline, a) -> None:
    st = pipe.store
    if a.action == "list":
        for s in st.builtin_specs():
            print(f"builtin   {s['id']:40} {s['meta'].get('format', '')}")
        for s in st.learned_specs():
            docs = len(st.spec_docs(s["id"]))
            print(f"{s.get('trust', '?'):9} {s['id']:40} v{s['version']}  {docs} docs  "
                  f"({(s.get('stats') or {}).get('created_by', '')})")
    elif a.action == "show":
        print(json.dumps(st.get_spec(a.id, a.version), indent=1))
    elif a.action == "approve":
        s = st.get_spec(a.id)
        print(st.save_spec(dict(s, trust="verified"))["version"])
    elif a.action == "rollback":
        st.set_current(a.id, a.version)
        print(f"{a.id} -> v{a.version}")
    elif a.action == "test":
        from .learn import regression_gate
        print(json.dumps(regression_gate(pipe, st.get_spec(a.id), a.id), indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
