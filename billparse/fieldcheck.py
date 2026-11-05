"""Checks of the field values a consumer gets (the public contract), for the corpus check (scripts/corpus.py).

The pipeline's own checks prove the amounts against the file's totals. These check that each field holds a sensible
value: a currency, a unit price that gives the cost, a region code, a coverage on usage, a service on usage cost, a
reason for every part that failed. Each issue is a code and a short detail; the corpus check fails on an issue the
file's label (store/corpus/expected.json "allow") does not accept.
"""

from __future__ import annotations

import re
from decimal import Decimal

from .export import Document

REGION_CODE = re.compile(r"^[a-z0-9-]+$")
TOTAL_LINE = re.compile(r"^\s*(grand\s+)?total\b", re.I)
USAGE = {"usage", "marketplace_usage"}
MIN_SERVICE_SHARE = 0.5                 # usage cost with a service code, below which the file has an issue


def issues(doc: Document, status: str | None) -> dict[str, str]:
    """{code: detail} for the field values of one parsed file that look wrong."""
    out: dict[str, str] = {}
    ch = doc.charges
    reasons = doc.confidence.reasons if doc.confidence else []
    if not ch:
        if not reasons or reasons[0] == "No charge was read":
            out["no_reason"] = "no lines and no reason why"
        return out
    if status not in ("reconciled", "auxiliary") and not reasons:
        out["no_reason"] = f"status {status} with no reason"
    if doc.doc_type and doc.doc_type.value == "inventory":
        return out | _regions(ch)
    if doc.currency is None and any(c.cost is not None for c in ch):
        out["no_currency"] = "the document has no currency"
    bad = 0
    for c in ch:
        if c.quantity and c.unit_price and c.cost and c.quantity.value and c.unit_price.value:
            tax = Decimal(str(c.attributes.get("tax") or 0))
            net = c.cost.value - tax
            if abs(c.quantity.value * c.unit_price.value - net) > max(Decimal("0.05"), abs(net) * Decimal("0.02")):
                bad += 1
    if bad:
        out["unit_price"] = f"{bad} line(s): quantity x unit price is not the cost"
    out |= _regions(ch)
    usage = [c for c in ch if c.kind in USAGE]
    if (n := sum(1 for c in usage if c.coverage is None)):
        out["coverage"] = f"{n} of {len(usage)} usage line(s) have no coverage"
    cost = sum((abs(c.cost.value) for c in usage if c.cost), Decimal(0))
    coded = sum((abs(c.cost.value) for c in usage if c.cost and c.service_code), Decimal(0))
    if cost and coded / cost < Decimal(str(MIN_SERVICE_SHARE)):
        out["service"] = f"{float(coded / cost):.0%} of usage cost has a service code"
    if (n := sum(1 for c in usage if TOTAL_LINE.match(str(c.description.value)))):     # "Total tax" is a tax line
        out["total_as_line"] = f"{n} line(s) are a total row"
    if (n := sum(1 for c in ch if c.quantity and c.quantity.value < 0 and c.kind in USAGE)):
        out["negative_quantity"] = f"{n} usage line(s) have a negative quantity"
    dates = [str(c.period_start.value) for c in ch if c.period_start]
    if dates and (min(dates)[:4] < "2000" or max(dates)[:4] > "2035"):
        out["dates"] = f"dates {min(dates)} to {max(dates)}"
    return out


def _regions(ch) -> dict[str, str]:
    odd = sorted({str(c.context.region.value) for c in ch if c.context and c.context.region
                  and not REGION_CODE.match(str(c.context.region.value))})
    return {"region": f"not region codes: {', '.join(odd[:4])}"} if odd else {}
