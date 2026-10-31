"""Extract first, then derive a spec.

For a layout nothing understands, the LLM reads the rows directly. We then search
for a deterministic spec that reproduces those rows exactly: which column holds the
amounts, which holds the labels, or which line pattern matches. The derived spec is
kept only if it reproduces the LLM's rows, so the next file in this layout needs no LLM.
"""

from __future__ import annotations

import re
from collections import Counter

from .docmodel import Document
from .layout import _row_to_cols, build_columns, find_header, norm_h
from .mapper import LINE_PATTERNS, TOTAL_FIELD_ANCHOR, _spec_from_mapping
from .spec import run
from .transforms import parse_number
from .validate import clean_rows


def agreement(spec_amounts: list[float], llm_amounts: list[float]) -> float:
    """F1 between two multisets of amounts rounded to cents."""
    a = Counter(round(x, 2) for x in spec_amounts)
    b = Counter(round(x, 2) for x in llm_amounts)
    if not a or not b:
        return 0.0
    hit = sum((a & b).values())
    p, r = hit / sum(a.values()), hit / sum(b.values())
    return 0.0 if hit == 0 else 2 * p * r / (p + r)


def derive_spec(doc: Document, llm_rows: list[dict]) -> tuple[dict | None, float]:
    amounts = [x for x in (parse_number(r.get("billed_cost")) for r in llm_rows) if x is not None]
    labels = [norm_h(r.get("service_raw") or r.get("description") or "") for r in llm_rows]
    if not amounts:
        return None, 0.0
    candidates: list[dict] = []

    for region in doc.regions:
        if region.scanned:
            continue
        if region.is_frame:
            samples = {str(c): region.df[c].tolist() for c in region.df.columns}
            candidates += _from_columns(samples, amounts, labels, "frame")
        elif region.rows:
            hdr = find_header(region)
            if hdr is None:
                continue
            cols = build_columns(region, hdr)
            samples = {c.name: [] for c in cols}
            for row in region.rows[hdr + 1:]:
                for k, v in _row_to_cols(row, cols, region.kind).items():
                    samples[k].append(v)
            candidates += _from_columns(samples, amounts, labels, "grid")

    if doc.file_type == "pdf":
        for pat in LINE_PATTERNS:
            candidates.append({
                "source": "lines", "lines": [{"pattern": pat}], "detect": {},
                "meta": {"kind": "cost_lines", "cost_basis": "billed", "layout": "lines"},
                "columns": {"service_raw": {"from": ["service_raw"]}, "billed_cost": {"from": ["billed_cost"]}},
                "fields": {"invoice_total": {"anchor": TOTAL_FIELD_ANCHOR, "take": "right_of",
                                             "regex": r"([\d,]+\.\d{2})"}},
                "totals": {"from_fields": ["invoice_total"]},
                "filters": [{"drop_if_match": {"field": "service_raw",
                                               "regex": TOTAL_FIELD_ANCHOR + r"|^\s*(sub)?total\b"}}]})

    best, best_score = None, 0.0
    for spec in candidates:
        try:
            ext = run(spec, doc)
            clean_rows(ext)                                    # drop total rows before scoring
        except Exception:                                      # noqa: BLE001  a bad candidate just loses
            continue
        if ext.rows.empty or "billed_cost" not in ext.rows:
            continue
        score = agreement(ext.rows["billed_cost"].dropna().tolist(), amounts)
        if score > best_score:
            best, best_score = spec, score
    return best, round(best_score, 3)


def _from_columns(samples: dict[str, list], amounts: list[float], labels: list[str], source: str) -> list[dict]:
    want = Counter(round(a, 2) for a in amounts)
    amount_scores = {}
    for h, vals in samples.items():
        got = Counter(round(x, 2) for x in (parse_number(v) for v in vals) if x is not None)
        if got:
            amount_scores[h] = sum((got & want).values()) / sum(want.values())
    if not amount_scores:
        return []
    cost_col = max(amount_scores, key=amount_scores.get)
    if amount_scores[cost_col] < 0.5:
        return []
    label_set = {lab for lab in labels if lab}
    text_scores = {}
    for h, vals in samples.items():
        if h == cost_col:
            continue
        normed = {norm_h(v) for v in vals if v}
        if normed:
            text_scores[h] = sum(1 for lab in label_set if any(lab and (lab in n or n in lab) for n in normed if n)) / max(1, len(label_set))
    chosen = {"billed_cost": cost_col}
    if text_scores:
        lab_col = max(text_scores, key=text_scores.get)
        if text_scores[lab_col] > 0:
            chosen["description"] = lab_col
    spec = _spec_from_mapping(chosen, source=source, headers=list(samples), groups=(source == "grid"))
    variants = [spec]
    if source == "grid":                                       # also try "label column is the service"
        alt = _spec_from_mapping({k if k != "description" else "service_raw": v for k, v in chosen.items()},
                                 source=source, headers=list(samples), groups=True)
        variants.append(alt)
    return variants


def looks_like_total(label: str) -> bool:
    return bool(re.search(r"\b(sub)?total\b|amount due|balance due", label or "", re.I))
