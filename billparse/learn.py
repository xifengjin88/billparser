"""Nightly improvement loop.

1. Re-try every open review item with the (now larger) spec library and the LLM.
2. Repair learned specs that failed on new files; promote a repaired version only if
   it passes the regression gate on every file that spec parsed before.
3. Promote candidate specs to verified after enough clean files.
4. Retrain the row classifier from rows of reconciled documents, and install it only
   if it beats the rule labels on a holdout set.
5. Write a report.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import llm as llm_mod
from .docmodel import load_bytes
from .spec import check_spec, run
from .validate import clean_rows, validate

PROMOTE_AFTER = 3            # clean files before candidate -> verified
PROMOTE_MIN_CONF = 0.75
MIN_LABELS = 400


# ---------------------------------------------------------------- regression gate
def regression_gate(pipeline, spec: dict, spec_id: str) -> dict:
    """Run a spec version on every doc the spec parsed before; it must match each accepted result."""
    docs = pipeline.store.spec_docs(spec_id)
    failures, checked = [], 0
    for sha, accepted in docs.items():
        loaded = pipeline.store.load_doc(sha)
        if not loaded:
            continue
        checked += 1
        doc = load_bytes(*loaded)
        try:
            ext = run(spec, doc)
            clean_rows(ext)
            v = validate(ext)
        except Exception as e:                                  # noqa: BLE001
            failures.append({"sha": sha, "reason": f"error: {e}"})
            continue
        total = float(ext.rows["billed_cost"].sum()) if "billed_cost" in ext.rows else 0.0
        if abs(len(ext.rows) - accepted["rows"]) > max(1, 0.01 * accepted["rows"]):
            failures.append({"sha": sha, "reason": f"rows {len(ext.rows)} vs {accepted['rows']}"})
        elif abs(total - accepted["total"]) > max(0.05, 0.001 * abs(accepted["total"])):
            failures.append({"sha": sha, "reason": f"total {total:.2f} vs {accepted['total']:.2f}"})
        elif v["confidence"] < accepted["confidence"] - 0.05:
            failures.append({"sha": sha, "reason": f"confidence {v['confidence']} vs {accepted['confidence']}"})
    return {"passed": not failures, "checked": checked, "failures": failures}


# ---------------------------------------------------------------- spec repair
def repair_spec(pipeline, spec_id: str, sha: str) -> dict:
    """Ask the LLM to fix a learned spec for a file it failed on, then gate it."""
    spec = pipeline.store.get_spec(spec_id)
    loaded = pipeline.store.load_doc(sha)
    if not (pipeline.llm and spec and loaded):
        return {"spec": spec_id, "result": "skipped"}
    from .pipeline import build_preview, _public
    doc = load_bytes(*loaded)
    ext = run(spec, doc)
    clean_rows(ext)
    feedback = validate(ext)["feedback"] or ["Spec extracted no usable rows from this file"]
    try:
        new = llm_mod.generate_spec(pipeline.llm, build_preview(doc), pipeline._examples(spec.get("fingerprint", {})
                                    or {"file_type": doc.file_type, "headers": [], "words": []}),
                                    feedback, _public(spec))
        check_spec(new)
    except Exception as e:                                      # noqa: BLE001
        return {"spec": spec_id, "result": f"repair failed: {e}"}
    new = {**spec, **new, "id": spec_id, "trust": "candidate", "parent": spec.get("version")}
    ext2 = run(new, doc)
    clean_rows(ext2)
    v2 = validate(ext2)
    if v2["status"] == "needs_review":
        return {"spec": spec_id, "result": "repair did not fix the failing file", "feedback": v2["feedback"][:3]}
    gate = regression_gate(pipeline, new, spec_id)
    if not gate["passed"]:
        return {"spec": spec_id, "result": "rejected by regression gate", "gate": gate}
    saved = pipeline.store.save_spec(new)
    return {"spec": spec_id, "result": f"promoted v{saved['version']}", "gate": gate}


def _closest_learned(pipeline, fp: dict, min_sim: float = 0.5) -> str | None:
    from .fingerprint import similarity
    best, best_sim = None, min_sim
    for sid, sfp in pipeline.store.fingerprints():
        if not fp.get("hash"):
            continue
        sim = similarity(fp, sfp)
        if sim >= best_sim and pipeline.store.get_spec(sid) and pipeline.store.get_spec(sid).get("trust") != "builtin":
            best, best_sim = sid, sim
    return best


# ---------------------------------------------------------------- promotion
def promote_candidates(pipeline) -> list[dict]:
    out = []
    for spec in pipeline.store.learned_specs():
        if spec.get("trust") != "candidate":
            continue
        docs = pipeline.store.spec_docs(spec["id"])
        good = [d for d in docs.values() if d["confidence"] >= PROMOTE_MIN_CONF]
        if len(good) >= PROMOTE_AFTER:
            saved = pipeline.store.save_spec(dict(spec, trust="verified"))
            out.append({"spec": spec["id"], "promoted_to": "verified", "version": saved["version"], "docs": len(good)})
    return out


# ---------------------------------------------------------------- row classifier
FEATURES = ["n_cells", "has_amount", "has_label", "bold", "indent_delta", "total_kw", "subtotal_kw",
            "tax_kw", "has_formula_sum", "header_overlap", "ends_colon", "numeric_share"]
PREV = ["", "item", "group_header", "continuation", "subtotal", "total", "header_repeat", "note", "blank"]


def _vec(f: dict) -> list[float]:
    return [float(f.get(k, 0) or 0) for k in FEATURES] + [1.0 if f.get("prev", "") == p else 0.0 for p in PREV]


def train_row_model(store_dir: Path, labels: list[dict]) -> dict:
    if len(labels) < MIN_LABELS:
        return {"trained": False, "reason": f"{len(labels)} labels (< {MIN_LABELS})"}
    from sklearn.model_selection import train_test_split
    from sklearn.tree import DecisionTreeClassifier
    import joblib

    X = [_vec(r["features"]) for r in labels]
    y = [r["label"] for r in labels]
    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.2, random_state=7)
    model = DecisionTreeClassifier(max_depth=8, min_samples_leaf=3, random_state=7).fit(Xtr, ytr)
    acc = float((model.predict(Xte) == yte).mean())
    if acc < 0.97:                                              # must agree with verified labels
        return {"trained": False, "reason": f"holdout accuracy {acc:.3f} < 0.97"}
    p = Path(store_dir) / "models"
    p.mkdir(exist_ok=True)
    joblib.dump(model, p / "rowclass.joblib")
    return {"trained": True, "holdout_accuracy": round(acc, 4), "labels": len(labels)}


def load_row_model(path: Path):
    import joblib
    model = joblib.load(path)
    return lambda f: str(model.predict([_vec(f)])[0])


# ---------------------------------------------------------------- nightly entry point
def run_nightly(pipeline) -> dict:
    t0 = time.time()
    report: dict = {"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "review": [], "repairs": [], "promotions": []}
    review = pipeline.store.read("review")
    still_open = []
    for item in review:
        if not item.get("open"):
            still_open.append(item)
            continue
        loaded = pipeline.store.load_doc(item["sha"])
        if not loaded:
            still_open.append(item)
            continue
        # if a learned spec matched but failed, try repairing that spec first
        rec = next((r for r in reversed(pipeline.store.read("records")) if r["sha"] == item["sha"]), None)
        learned_ids = {a["spec_id"] for a in (rec or {}).get("attempts", []) if a["route"] == "learned" and a["spec_id"]}
        if not learned_ids and rec:
            # layout drifted (e.g. renamed column) so no learned spec matched: repair the closest one
            near = _closest_learned(pipeline, rec.get("fingerprint") or {})
            if near:
                learned_ids = {near}
        for sid in learned_ids:
            report["repairs"].append(repair_spec(pipeline, sid, item["sha"]))
        result = pipeline.parse_bytes(*loaded, force=True)
        report["review"].append({"file": item["file"], "was": item["status"], "now": result["status"],
                                 "confidence": result["confidence"], "route": (result.get("spec") or {}).get("route")})
    # parse_bytes re-queued anything still failing (ts > t0); drop the old entries we retried
    pipeline.store.rewrite("review", [r for r in pipeline.store.read("review")
                                      if r["sha"] not in {i["sha"] for i in review if i.get("open")}
                                      or r["ts"] > t0])
    report["promotions"] = promote_candidates(pipeline)
    report["row_model"] = train_row_model(pipeline.s.store_dir, pipeline.store.read("labels"))
    report["metrics"] = metrics(pipeline.store.read("records"))
    report["seconds"] = round(time.time() - t0, 2)
    out = Path(pipeline.s.store_dir) / "nightly"
    out.mkdir(exist_ok=True)
    (out / f"{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(json.dumps(report, indent=1, default=str))
    return report


def metrics(records: list[dict]) -> dict:
    """The numbers that should improve month over month."""
    if not records:
        return {}
    latest: dict[str, dict] = {}
    for r in records:
        latest[r["sha"]] = r
    rs = list(latest.values())
    n = len(rs)
    first_try = sum(1 for r in rs if r["attempts"] and r["attempts"][0].get("status") in ("reconciled", "unreconciled", "auxiliary"))
    no_llm = sum(1 for r in rs if r.get("llm_calls", 0) == 0)
    routes: dict[str, int] = {}
    for r in rs:
        routes[r.get("route") or "none"] = routes.get(r.get("route") or "none", 0) + 1
    return {"documents": n,
            "accepted_share": round(sum(1 for r in rs if r["status"] in ("reconciled", "unreconciled", "auxiliary")) / n, 3),
            "reconciled_share": round(sum(1 for r in rs if r["status"] == "reconciled") / n, 3),
            "first_route_success": round(first_try / n, 3),
            "no_llm_share": round(no_llm / n, 3),
            "avg_llm_calls": round(sum(r.get("llm_calls", 0) for r in rs) / n, 2),
            "routes": routes}
