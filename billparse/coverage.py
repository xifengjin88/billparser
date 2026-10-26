"""Coverage: how each line's usage was priced (on_demand, reserved, savings_plan, committed_use, spot, unknown).

A spec maps a printed column to `coverage` (line type, pricing model, FOCUS PricingCategory, GCP credit types) with
the `coverage` or `commitment_credit` transform. apply() then settles what one column cannot, for every route:

  * Spot in AWS CUR: the line type of Spot usage is plain "Usage"; its usage type says Spot ("USE1-SpotUsage:...").
  * FOCUS: PricingCategory "Committed" is refined by CommitmentDiscountType (a Savings Plan or a reservation).
  * A line type that names a commitment (Azure UnusedReservation, a fee line) gives coverage when no column did.
  * Files with no coverage column (invoices, custom files): the words of a usage line ("Savings Plan", "Reserved",
    "Spot", "CUD", "On Demand") give it, as an inferred value. A column always wins over text. No evidence: unknown.
  * Lines that are not usage and not commitment lines (tax, credit, refund, fee, ...) have no coverage: None. The
    question does not apply to them, so they are never "unknown".

The way each value was made is kept per row in "_how:coverage": "printed" (from a column) or "text" (from words).
"""

from __future__ import annotations

import re

import pandas as pd

from .spec import HOW
from .transforms import coverage_of, map_unique

COMMITMENTS = {"reserved", "savings_plan", "committed_use"}
TEXT_RULES = [("on_demand", re.compile(r"pay[\s-]*as[\s-]*you[\s-]*go|\bnot\s+applied\b", re.I)),   # before the
              #   commitment words: "reserved capacity not applied, billed at pay-as-you-go rates" is on demand
              ("savings_plan", re.compile(r"savings\s*plans?", re.I)),
              ("reserved", re.compile(r"\breserved\b|\breservations?\b|\bRI\b")),
              ("committed_use", re.compile(r"\bCUD\b|committed\s+use", re.I)),
              ("spot", re.compile(r"\bspot\b", re.I)),
              ("on_demand", re.compile(r"\bon[\s-]*demand\b", re.I))]
SPOT_USAGE = re.compile(r"SpotUsage", re.I)
COMMITMENT_LINES = {"Purchase", "Negation"}       # commitment fee and negation lines keep a commitment coverage


def _text_coverage(text: str) -> str | None:
    return next((cov for cov, rx in TEXT_RULES if rx.search(text)), None)


def _is_spot(v) -> bool:
    return bool(SPOT_USAGE.search(v)) if isinstance(v, str) else False


def _status(v) -> str | None:
    return v.strip().lower() if isinstance(v, str) and v.strip() else None


def _line_kind(v) -> str:
    return re.sub(r"[\s_-]+", "", v.lower()) if isinstance(v, str) else ""


def _col(rows: pd.DataFrame, name: str) -> pd.Series:
    return rows[name] if name in rows else pd.Series([None] * len(rows), index=rows.index, dtype=object)


def apply(rows: pd.DataFrame) -> None:
    if rows.empty:
        return
    ct = _col(rows, "charge_type")
    usage = ct.isna() | ct.eq("Usage")
    cov = _col(rows, "coverage").astype(object).where(lambda s: s.notna(), None)
    how = pd.Series("printed", index=rows.index, dtype=object).where(cov.notna(), None)

    # a line type that names a commitment, when no column gave one (Azure UnusedReservation, CUR fee lines)
    from_type = map_unique(_col(rows, "line_type_raw"), coverage_of)
    fill = (cov.isna() | cov.eq("unknown")) & from_type.isin(COMMITMENTS)
    cov[fill], how[fill] = from_type[fill], "printed"

    # FOCUS: a commitment discount of a known kind
    ctype = map_unique(_col(rows, "commitment_type"), lambda v: str(v).lower() if isinstance(v, str) else "")
    committed = cov.eq("committed_use")
    cov[committed & ctype.str.contains("saving")] = "savings_plan"
    cov[committed & ctype.str.contains("reserv")] = "reserved"

    # Spot in the usage type
    spot_type = map_unique(_col(rows, "usage_type"), _is_spot).astype(bool)
    spot = usage & (cov.isna() | cov.isin({"on_demand", "unknown"})) & spot_type
    how[spot & cov.isna()] = "text"
    cov[spot] = "spot"

    # usage lines with no coverage column: the line's words
    todo = usage & cov.isna()
    if todo.any():
        parts = [map_unique(_col(rows, c), lambda v: str(v) if isinstance(v, str) else "")
                 for c in ("description", "service_raw", "usage_type", "_path")]
        text = parts[0]
        for p in parts[1:]:
            text = text + " " + p
        found = map_unique(text[todo], _text_coverage)
        hit = found.notna()
        cov[found[hit].index], how[found[hit].index] = found[hit], "text"
    cov[usage & cov.isna()] = "unknown"                   # usage the file does not say how it was priced

    # a commitment fee line whose printed commitment type names it (FOCUS purchases have no PricingCategory)
    fee = ct.isin(COMMITMENT_LINES) & cov.isna()
    cov[fee & ctype.str.contains("saving")], how[fee & ctype.str.contains("saving")] = "savings_plan", "printed"
    cov[fee & ctype.str.contains("reserv")], how[fee & ctype.str.contains("reserv")] = "reserved", "printed"

    # not usage: only commitment fee and negation lines keep a commitment coverage
    other = ~usage & ~(ct.isin(COMMITMENT_LINES) & cov.isin(COMMITMENTS))
    cov[other], how[other] = None, None

    rows["coverage"] = cov
    rows[HOW + "coverage"] = how

    # commitment status: covered usage used its commitment; an unused-commitment line says unused
    status = map_unique(_col(rows, "commitment_status"), _status)
    kind = map_unique(_col(rows, "line_type_raw"), _line_kind)
    status_how = pd.Series("printed", index=rows.index, dtype=object).where(status.notna(), None)
    unused = status.isna() & kind.str.startswith("unused")
    used = status.isna() & usage & cov.isin(COMMITMENTS) & how.eq("printed")
    status[unused], status_how[unused] = "unused", "rule"     # from the line type, not a status column
    status[used], status_how[used] = "used", "rule"           # covered usage used its commitment
    rows["commitment_status"] = status
    rows[HOW + "commitment_status"] = status_how
