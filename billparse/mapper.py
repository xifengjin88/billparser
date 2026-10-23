"""Rule-based spec generation, tried before any LLM call.

Scores every (column, canonical field) pair from two kinds of evidence:
header words, and what the values look like (currency, dates, codes, text).
Also recognizes month-pivot sheets (services x months) and list-style PDF lines.

The header -> field scorer is the first thing to replace with a trained model;
every accepted spec adds labeled (header, sample values, field) examples.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd

from .docmodel import Document, Region, is_numberish
from .layout import build_columns, find_header, norm_h, _row_to_cols
from .transforms import parse_number

HEADER_RULES: dict[str, list[tuple[str, float]]] = {
    "billed_cost": [(r"\b(extended|line total|net amount|billed cost|total cost|cost|amount|amt|charges?|subtotal|total)\b", 2.5),
                    (r"\b(value|spend|price total)\b", 1.5)],
    "unit_price": [(r"\b(unit price|unit cost|unit rate|effective price|price per|rate)\b", 3.0), (r"^price$", 2.5)],
    "quantity": [(r"\b(qty|quantity|usage quantity|units used|hours|consumed)\b", 3.0), (r"\busage\b", 1.5)],
    "unit": [(r"\b(unit of measure|uom|usage unit|unit)\b", 2.5)],
    "service_raw": [(r"\b(service name|service|svc|product name|meter category|category|offering)\b", 3.0),
                    (r"\b(product|item)\b", 2.0)],
    "description": [(r"\b(description|desc|details?|line item|item|sku name|sku|meter name|meter|name)\b", 2.5)],
    "product_code": [(r"\b(product code|product id|productid|sku id|part number|service code|meter id|code)\b", 3.0)],
    "charge_start": [(r"\b(usage date|start date|from|date|period|month|billing period)\b", 2.0)],
    "charge_end": [(r"\b(end date|to date|end|through)\b", 2.5)],
    "region": [(r"\b(region|location)\b", 3.0)],
    "account_id": [(r"\b(account id|subscription id|project id|account number|subscription|project)\b", 2.5)],
    "account_name": [(r"\b(account name|subscription name|project name|customer|account)\b", 2.0)],
    "currency": [(r"\b(currency|ccy)\b", 3.0)],
    "charge_type": [(r"\b(charge type|line item type|cost type|type)\b", 2.5)],
    "tax": [(r"\b(tax|vat|gst)\b", 3.0)],
    "invoice_id": [(r"\b(invoice no|invoice number|invoice id|invoice)\b", 2.5)],
}
NEGATIVE = {
    "billed_cost": r"\b(unit|rate|list|tax|qty|quantity|per|code|id|date|currency|type)\b",
    "quantity": r"\b(price|cost|amount|rate)\b",
    "description": r"\b(id|code|date|price|cost|amount)\b",
    "service_raw": r"\b(id|code|price|cost|amount)\b",
    "unit": r"\b(price|cost|rate)\b",
}
NUMERIC_FIELDS = {"billed_cost", "unit_price", "quantity", "tax"}
TEXT_FIELDS = {"service_raw", "description", "unit", "region", "account_name", "charge_type", "currency"}
MONTH_RX = re.compile(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?(\s*['-]?\s*\d{2,4})?$|"
                      r"^\d{4}[-/]\d{1,2}$|^\d{1,2}[-/]\d{4}$", re.I)
CODE_RX = re.compile(r"^[A-Z0-9][A-Z0-9_\-:/]{5,}$")
TOTALISH = re.compile(r"\b(sub\s*total|subtotal|total|amount due|balance due)\b", re.I)


@dataclass
class Profile:
    numeric: float
    currency_sym: float
    date: float
    code: float
    distinct: float
    avg_len: float


def profile(values: list) -> Profile:
    vals = [str(v).strip() for v in values if v is not None and str(v).strip() not in ("", "nan", "None")]
    if not vals:
        return Profile(0, 0, 0, 0, 0, 0)
    n = len(vals)
    num = sum(1 for v in vals if is_numberish(v)) / n
    cur = sum(1 for v in vals if re.search(r"[$€£¥]|\.\d{2}$", v)) / n
    dates = pd.to_datetime(pd.Series(vals), errors="coerce", format="mixed")
    date = float(dates.notna().mean()) if num < 0.9 or any("-" in v or "/" in v for v in vals) else 0.0
    code = sum(1 for v in vals if CODE_RX.match(v) and not is_numberish(v)) / n
    return Profile(num, cur, date, code, len(set(vals)) / n, sum(map(len, vals)) / n)


def header_score(field: str, header: str) -> float:
    h = norm_h(header)
    best = 0.0
    for rx, w in HEADER_RULES[field]:
        if re.search(rx, h):
            best = max(best, w)
    if best and field in NEGATIVE and re.search(NEGATIVE[field], h):
        best -= 2.0
    return best


def value_score(field: str, p: Profile) -> float:
    if field in NUMERIC_FIELDS:
        if p.numeric < 0.6 or p.date > 0.8:
            return -5.0
        bonus = 1.0 if field in ("billed_cost", "unit_price") and p.currency_sym > 0.5 else 0.0
        return 1.0 + bonus
    if field in ("charge_start", "charge_end"):
        return 1.5 if p.date > 0.7 else -5.0
    if field == "product_code":
        return 1.0 if p.code > 0.6 else -2.0
    if field in TEXT_FIELDS:
        if p.numeric > 0.5 or p.date > 0.7:
            return -5.0
        if field == "description":
            return (1.0 if p.avg_len > 6 else 0.0) + (0.5 if p.distinct > 0.8 else 0.0)
        if field == "service_raw":
            return 1.0 if p.distinct < 0.7 else 0.3
        return 0.5
    return 0.0


def map_columns(columns: dict[str, list]) -> tuple[dict[str, str], float, dict]:
    """columns: header -> sample values. Returns (field -> header, confidence, scores)."""
    profiles = {h: profile(v) for h, v in columns.items()}
    pairs = []
    for h, p in profiles.items():
        for f in HEADER_RULES:
            hs = header_score(f, h)
            if hs <= 0:
                continue
            pairs.append((hs + value_score(f, p), f, h))
    pairs.sort(reverse=True)
    chosen: dict[str, str] = {}
    used: set[str] = set()
    scores: dict[str, float] = {}
    for s, f, h in pairs:
        if s < 2.0 or f in chosen or h in used:
            continue
        chosen[f], scores[f] = h, s
        used.add(h)
    if "billed_cost" not in chosen:                            # rightmost money-looking column
        money = [h for h, p in profiles.items() if h not in used and p.numeric > 0.8 and p.currency_sym > 0.5]
        if money:
            chosen["billed_cost"], scores["billed_cost"] = money[-1], 2.0
    conf = 0.0
    if "billed_cost" in chosen:
        conf = 0.5 + 0.25 * bool({"service_raw", "description"} & set(chosen))
        conf += 0.25 * min(1.0, sum(scores.values()) / (4.5 * max(1, len(scores))))
    return chosen, round(conf, 2), scores


# ---------------------------------------------------------------- spec builders
def propose(doc: Document) -> tuple[dict | None, float]:
    """Best rule-based spec for a document, with confidence 0..1."""
    best: tuple[dict | None, float] = (None, 0.0)
    for region in doc.regions:
        if region.scanned:
            continue
        cand = _propose_frame(region) if region.is_frame else _propose_grid(region)
        if cand and cand[1] > best[1]:
            best = cand
    if best[1] < 0.6 and doc.file_type == "pdf":
        lines = propose_lines(doc)
        if lines and lines[1] > best[1]:
            best = lines
    return best


def _samples_frame(region: Region, n: int = 200) -> dict[str, list]:
    df = region.df.head(n)
    return {str(c): df[c].tolist() for c in df.columns}


def _propose_frame(region: Region) -> tuple[dict, float] | None:
    cols = _samples_frame(region)
    pivot = _propose_pivot(cols, source="frame")
    if pivot:
        return pivot
    chosen, conf, _ = map_columns(cols)
    if "billed_cost" not in chosen:
        return None
    return _spec_from_mapping(chosen, source="frame", headers=list(cols)), conf


def _propose_grid(region: Region) -> tuple[dict, float] | None:
    hdr = find_header(region)
    if hdr is None:
        return None
    cols = build_columns(region, hdr)
    samples: dict[str, list] = {c.name: [] for c in cols}
    for row in region.rows[hdr + 1: hdr + 80]:
        if TOTALISH.search(row.text) or (row.bold and not any(is_numberish(c.text) for c in row.cells)):
            continue                                           # subtotal and group rows would skew profiles
        for k, v in _row_to_cols(row, cols, region.kind).items():
            samples[k].append(v)
    pivot = _propose_pivot(samples, source="grid")
    if pivot:
        return pivot
    chosen, conf, _ = map_columns(samples)
    if "billed_cost" not in chosen:
        return None
    has_bold_groups = any(r.bold and not any(is_numberish(c.text) for c in r.cells)
                          for r in region.rows[hdr + 1: hdr + 80])
    spec = _spec_from_mapping(chosen, source="grid", headers=[c.name for c in cols], groups=True)
    if has_bold_groups:
        spec["notes"] = "Group headers detected; carried into service_raw when no service column exists."
    return spec, conf


def _spec_from_mapping(chosen: dict[str, str], source: str, headers: list[str], groups: bool = False) -> dict:
    columns = {f: {"from": [h]} for f, h in chosen.items()}
    if groups and "service_raw" not in chosen:
        # hierarchical invoices: the bold group row is the service, lines below are SKUs
        fallback = [chosen["description"]] if "description" in chosen else []
        columns["service_raw"] = {"from": ["_group", *fallback], "coalesce": True}
    elif groups:
        columns["service_raw"] = {"from": [chosen["service_raw"], "_group"], "coalesce": True}
    required = [chosen["billed_cost"]] + ([chosen[k] for k in ("service_raw", "description") if k in chosen][:1])
    spec = {"source": source, "detect": {"required": required},
            "meta": {"kind": "cost_lines", "cost_basis": "billed"},
            "columns": columns, "filters": [{"drop_if_blank": "billed_cost"}]}
    if source == "grid":
        spec["table"] = {"group_header": {"carry_to": "_group"}, "continuation": "merge", "multi_page": True}
    return spec


def _propose_pivot(cols: dict[str, list], source: str) -> tuple[dict, float] | None:
    months = [h for h in cols if MONTH_RX.match(str(h).strip())]
    if len(months) < 3:
        return None
    label = next((h for h in cols if h not in months and profile(cols[h]).numeric < 0.3), None)
    if label is None:
        return None
    spec = {"source": source, "detect": {"required": [label, *months[:2]]},
            "meta": {"kind": "cost_lines", "cost_basis": "billed", "layout": "month_pivot"},
            "reshape": {"unpivot": {"id": label, "exclude": r"(^|\b)total\b|^col_\d+$",
                                    "total_row": r"^\s*(grand\s+)?total\b"}},
            "columns": {"service_raw": {"from": ["_id"]}, "billed_cost": {"from": ["_value"]},
                        "charge_start": {"from": ["_var"]}},
            "filters": [{"drop_if_blank": "billed_cost"},
                        {"drop_if_match": {"field": "service_raw", "regex": r"^\s*(sub)?total\b"}}]}
    if source == "grid":
        spec["table"] = {"group_header": {"carry_to": "_group"}, "continuation": "drop", "stop_at_total": False}
    return spec, 0.8


# ---------------------------------------------------------------- list-style PDFs
LINE_PATTERNS = [
    r"^(?P<service_raw>[A-Za-z][^$]*?[A-Za-z0-9)])\s*\.{2,}\s*[$€£]?\s*(?P<billed_cost>\(?-?[\d,]+\.\d{2}\)?)$",
    r"^(?P<service_raw>[A-Za-z][^$]*?)\s+(?P<quantity>[\d,]+(?:\.\d+)?)\s+(?P<unit>[A-Za-z][A-Za-z\-]*(?:\s[A-Za-z]+)?)\s+"
    r"[$€£]?\s*(?P<unit_price>[\d,]+\.\d{2,6})\s+[$€£]?\s*(?P<billed_cost>\(?-?[\d,]+\.\d{2}\)?)$",
    r"^(?P<service_raw>[A-Za-z][^$]*?[A-Za-z0-9)])\s+[$€£]\s?(?P<billed_cost>\(?-?[\d,]+\.\d{2}\)?)$",
    r"^(?P<service_raw>[A-Za-z][^$]*?[A-Za-z)])\s+(?P<billed_cost>\(?-?[\d,]+\.\d{2}\)?)\s*(USD|EUR|GBP)?$",
]
TOTAL_FIELD_ANCHOR = r"(total\s+due|amount\s+due|balance\s+due|grand\s+total|invoice\s+total|total\s+charges)"


def propose_lines(doc: Document) -> tuple[dict, float] | None:
    texts = [r.text for reg in doc.regions if not reg.is_frame for r in reg.rows]
    money_lines = [t for t in texts if re.search(r"\d\.\d{2}\)?\s*(USD|EUR|GBP)?$", t)]
    if len(money_lines) < 2:
        return None
    best, best_n = None, 0
    for pat in LINE_PATTERNS:
        rx = re.compile(pat)
        n = sum(1 for t in money_lines if rx.search(t))
        if n > best_n:
            best, best_n = pat, n
    if not best:
        return None
    coverage = best_n / len(money_lines)
    spec = {"source": "lines", "lines": [{"pattern": best}],
            "detect": {}, "meta": {"kind": "cost_lines", "cost_basis": "billed", "layout": "lines"},
            "columns": {"service_raw": {"from": ["service_raw"]}, "billed_cost": {"from": ["billed_cost"]},
                        **({"quantity": {"from": ["quantity"]}, "unit": {"from": ["unit"]},
                            "unit_price": {"from": ["unit_price"]}} if "quantity" in best else {})},
            "fields": {"invoice_total": {"anchor": TOTAL_FIELD_ANCHOR, "take": "right_of",
                                         "regex": r"([\d,]+\.\d{2})"}},
            "totals": {"from_fields": ["invoice_total"]},
            "filters": [{"drop_if_match": {"field": "service_raw", "regex": TOTAL_FIELD_ANCHOR + r"|^\s*(sub)?total\b"}}]}
    return spec, round(min(0.85, 0.45 + 0.4 * coverage), 2)


def amounts_in(values: list) -> list[float]:
    return [x for x in (parse_number(v) for v in values) if x is not None]
