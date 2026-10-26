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
from pathlib import Path

from .pipeline import Pipeline, Settings
from .settings import configure_logging, load_env


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
        e = a.get("commitment_eligible_usage") or {}
        for cat, split in (e.get("by_category") or {}).items():
            parts = ", ".join(f"{k.replace('_', ' ')} {v:,.2f}" for k, v in split.items() if k != "total")
            print(f"  commitment-eligible {cat} ({e.get('cost_type')} cost) {split['total']:,.2f}: {parts}")
        u = e.get("unused_commitment_spend") or {}
        if u.get("total") is not None:
            print(f"  unused commitment {u['total']:,.2f}: " + ", ".join(f"{k.replace('_', ' ')} {v:,.2f}"
                                                                      for k, v in (u.get("by_type") or {}).items()))
        elif u.get("lines"):
            print(f"  unused commitment: {u['lines']} line(s), amount not in the file")


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
    ag = sub.add_parser("agent", help="the agent that writes specs (needs AGENT_ENABLED=1 and ANTHROPIC_API_KEY)")
    ag.add_argument("action", choices=["list", "run", "drain", "retry", "trace"])
    ag.add_argument("target", nargs="?", help="run: a file to parse and review; retry/trace: a job id")
    a = ap.parse_args(argv)

    load_env()
    configure_logging()
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
    elif a.cmd == "agent":
        _agent(pipe, a)


def _agent(pipe: Pipeline, a) -> None:
    from . import agent
    st = pipe.store
    if a.action == "list":
        for j in st.agent_jobs():
            print(f"{j['id']:5} {j['status']:9} {j['kind']:6} rounds={j['rounds']} ${j['cost_usd']:.2f}  "
                  f"{j['name'][:50]}  {j.get('spec_id') or ''}  {(j.get('note') or j.get('error') or '')[:60]}")
        return
    if a.action == "trace":
        for e in agent.read_trace(st, int(a.target)):
            print(json.dumps(e, default=str)[:400])
        return
    if a.action == "retry":
        print("queued" if st.retry_agent_job(int(a.target)) else "not queued (no such job, or it is running)")
        return
    if not agent.enabled():
        raise SystemExit("The agent calls the Claude API and costs money: set AGENT_ENABLED=1 and ANTHROPIC_API_KEY.")
    if a.action == "run":
        path = Path(a.target)                       # agent=False: the job below is its only run (no second paid run)
        r = pipe.parse_path(path, force=True, agent=False)
        if not agent.needs_agent(r):
            print(f"{r['file']}: {r['completeness']}, no review needed")
            return
        key = agent.job_key(st, r)                  # the layout's job may have been made for another file
        job_id = agent.enqueue_for(pipe, r) or next(j["id"] for j in st.agent_jobs() if j["layout_key"] == key)
        st.retry_agent_job(job_id)
        _print_job(agent.run_job(pipe, job_id))
    elif a.action == "drain":
        for j in reversed(st.agent_jobs("queued")):
            _print_job(agent.run_job(pipe, j["id"]))


def _print_job(j: dict) -> None:
    print(f"job {j['id']} {j['name']}: {j['status']} after {j['rounds']} round(s), {j['tool_calls']} tool calls, "
          f"${j['cost_usd']:.2f}; spec {j.get('spec_id') or '-'}; {(j.get('note') or j.get('error') or '')[:200]}")


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
