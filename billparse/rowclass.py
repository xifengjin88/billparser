"""Label each row of a located table: item, group_header, continuation, subtotal,
total, header_repeat, note, blank.

Rules first. Every labeled row also yields a feature dict, saved to the learning
store so a classifier can replace these rules once there are enough labels.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .docmodel import Row, is_numberish

TOTAL_RX = re.compile(r"\b(grand\s+total|total\s+(due|amount|charges|cost|usd|eur)|amount\s+due|"
                      r"balance\s+due|invoice\s+total|total)\b", re.I)
SUBTOTAL_RX = re.compile(r"\b(sub[\s-]?total|total\s+for|section\s+total)\b", re.I)
TAX_RX = re.compile(r"\b(tax|vat|gst|hst)\b", re.I)

LABELS = ("item", "group_header", "continuation", "subtotal", "total", "header_repeat", "note", "blank")


@dataclass
class RowLabel:
    label: str
    features: dict


def features(row: Row, amount_text: str, label_text: str, header_texts: set[str],
             median_x: float, prev_label: str | None) -> dict:
    texts = [c.text for c in row.cells if c.text]
    lowered = {t.lower() for t in texts}
    return {
        "n_cells": len(texts),
        "has_amount": bool(amount_text) and is_numberish(amount_text),
        "has_label": bool(label_text),
        "bold": row.bold,
        "indent_delta": round(row.first_x - median_x, 1),
        "total_kw": bool(TOTAL_RX.search(row.text)),
        "subtotal_kw": bool(SUBTOTAL_RX.search(row.text)),
        "tax_kw": bool(TAX_RX.search(label_text or row.text)),
        "has_formula_sum": any(_vertical_sum(c.formula) for c in row.cells if c.formula),
        "header_overlap": (len(lowered & header_texts) / max(1, len(header_texts))) if header_texts else 0.0,
        "ends_colon": row.text.rstrip().endswith(":"),
        "numeric_share": sum(is_numberish(t) for t in texts) / max(1, len(texts)),
        "prev": prev_label or "",
    }


_SUM_RANGE = re.compile(r"SUM\(\$?([A-Z]{1,3})\$?(\d+):\$?([A-Z]{1,3})\$?(\d+)\)", re.I)


def _vertical_sum(formula: str) -> bool:
    """=SUM(E7:E9) sums rows above (a subtotal). =SUM(B4:G4) sums across a row (a row total)."""
    return any(a.upper() == c.upper() and b != d for a, b, c, d in _SUM_RANGE.findall(formula or ""))


def classify(f: dict) -> str:
    """Rule-based labeler. Swap for a trained model via set_model()."""
    if _MODEL is not None:
        return _MODEL(f)
    if f["n_cells"] == 0:
        return "blank"
    if f["header_overlap"] >= 0.6:
        return "header_repeat"
    if f["has_amount"] and (f["subtotal_kw"] or (f["has_formula_sum"] and not f["total_kw"])):
        return "subtotal"
    if f["has_amount"] and f["total_kw"] and not f["tax_kw"]:
        return "total"
    if f["has_amount"] and f["has_label"]:
        return "item"
    if f["has_amount"] and not f["has_label"]:
        return "item" if f["numeric_share"] < 1 else "note"
    # no amount from here on
    if f["has_label"] and (f["bold"] or f["ends_colon"] or f["indent_delta"] < -2):
        return "group_header"
    if f["has_label"] and f["prev"] in ("item", "continuation") and f["n_cells"] <= 2:
        return "continuation"
    if f["has_label"] and f["prev"] in ("", "header_repeat", "group_header", "subtotal") and f["n_cells"] == 1:
        return "group_header"
    return "note"


_MODEL = None


def set_model(predict_fn) -> None:
    """Install a trained classifier: predict_fn(features: dict) -> label."""
    global _MODEL
    _MODEL = predict_fn
