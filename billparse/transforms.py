"""Canonical row schema and the fixed list of transforms specs may use.

Specs can only reference transforms registered here. The LLM can't invent new
ones; your team adds them in code (see CUSTOM_TRANSFORMS) when a pattern recurs.
"""

from __future__ import annotations

import calendar
import json
import re
from collections.abc import Callable

import numpy as np
import pandas as pd

# Canonical fields, loosely FOCUS-shaped. Everything is optional except a cost.
CANONICAL = {
    "service_raw": "Service or product label as written in the file",
    "product_code": "Provider product / meter / service code",
    "description": "Line description (SKU, meter name, usage type)",
    "billed_cost": "Cost charged for the line",
    "effective_cost": "Amortized cost, if the file has it",
    "list_cost": "Cost at list / on-demand price",
    "credits": "Credits applied to the line (negative)",
    "quantity": "Usage quantity",
    "unit": "Unit of measure",
    "unit_price": "Price per unit",
    "charge_start": "Usage or charge start date",
    "charge_end": "Usage or charge end date",
    "region": "Region / location",
    "account_id": "Account, subscription or project ID",
    "account_name": "Account, subscription or project name",
    "currency": "Currency code",
    "charge_type": "Usage, Purchase, Tax, Credit, Refund, Fee, Support, Adjustment",
    "invoice_id": "Invoice number",
    "provider": "aws, azure, gcp, or other",
    "tax": "Tax amount on the line",
    "instance_type": "Instance or VM size (m5.xlarge, db.r6g.large, Standard_D4s_v5, n2-standard-8)",
    "database_engine": "Database engine (MySQL, Aurora PostgreSQL, SQL Server, ...)",
    "operating_system": "Operating system (Linux, Windows, RHEL, ...)",
    "deployment_option": "Deployment (Single-AZ, Multi-AZ)",
    "pricing_model": "On Demand, Reserved, Savings Plan, Spot, Commitment",
    "usage_type": "Provider usage type (USE1-BoxUsage:m5.xlarge, APS3-TimedStorage-ByteHrs)",
    "commitment_id": "Reservation, savings plan or commitment ID",
}
NUMERIC = {"billed_cost", "effective_cost", "list_cost", "credits", "quantity", "unit_price", "tax"}
DATES = {"charge_start", "charge_end"}

CHARGE_TYPE_MAP = {
    "usage": "Usage", "purchase": "Purchase", "tax": "Tax", "credit": "Credit",
    "refund": "Refund", "fee": "Fee", "support": "Support", "adjustment": "Adjustment",
    "rifee": "Purchase", "savingsplanrecurringfee": "Purchase", "savingsplancoveredusage": "Usage",
    "discountedusage": "Usage", "bundleddiscount": "Credit", "edpdiscount": "Credit",
    "privaterate discount": "Credit", "unusedreservation": "Purchase", "unusedsavingsplan": "Purchase",
    "roundingadjustment": "Adjustment", "new": "Purchase", "renew": "Purchase",
    "cycle charge": "Purchase", "regular": "Usage", "commitment": "Purchase",
}

_CURRENCY_JUNK = re.compile(r"[^\d.,\-()]")


# ---------------------------------------------------------------- scalar parsers
def parse_number(v: object, decimal: str = ".") -> float | None:
    """'$1,234.50' -> 1234.5, '(12.00)' -> -12, '12.00-' -> -12, '1.234,50' (decimal=',')."""
    if v is None:
        return None
    if isinstance(v, (int, float, np.number)) and not isinstance(v, bool):
        return None if (isinstance(v, float) and np.isnan(v)) else float(v)
    s = str(v).strip()
    if not s or s.lower() in {"nan", "none", "null", "-", "—", "n/a"}:
        return None
    neg = s.startswith("(") and s.endswith(")") or s.endswith("-") or s.startswith("-")
    s = _CURRENCY_JUNK.sub("", s).strip("()-")
    if not s:
        return None
    if decimal == ",":
        s = s.replace(".", "").replace(",", ".")
    else:
        # "1.234,56" pattern auto-detect even without a hint
        if re.match(r"^\d{1,3}(\.\d{3})+,\d{1,2}$", s) or re.match(r"^\d+,\d{1,2}$", s):
            s = s.replace(".", "").replace(",", ".")          # "1.234,56" or "276,48" (decimal comma)
        else:
            s = s.replace(",", "")
    try:
        f = float(s)
    except ValueError:
        return None
    return -f if neg else f


_UNIT_BLOCK = re.compile(r"^\s*([\d,]+(?:\.\d+)?)\s+(.+)$")


def parse_block_unit(v: object) -> tuple[float, str] | None:
    """Azure-style '10 Hours' / '1000000 Requests' -> (10.0, 'Hours')."""
    if v is None:
        return None
    s = str(v).strip()
    m = _UNIT_BLOCK.match(s)
    if m:
        return float(m.group(1).replace(",", "")), m.group(2).strip()
    return (1.0, s) if s else None


# ---------------------------------------------------------------- vectorized transforms
def t_number(s: pd.Series, decimal: str = ".") -> pd.Series:
    num = pd.to_numeric(s, errors="coerce")
    need = num.isna() & s.notna()
    if need.any():
        num = num.astype(float)
        num[need] = s[need].map(lambda v: parse_number(v, decimal)).astype(float)   # None -> NaN
    return num.astype(float)


def t_date(s: pd.Series, fmt: str | None = None, dayfirst: bool = False) -> pd.Series:
    if fmt:
        out = pd.to_datetime(s, format=fmt, errors="coerce")
    else:
        num = pd.to_numeric(s, errors="coerce")
        serial = num.between(20000, 80000)                    # Excel serial dates
        out = pd.to_datetime(s.where(~serial), errors="coerce", dayfirst=dayfirst, format="mixed")
        if serial.any():
            out[serial] = pd.to_datetime("1899-12-30") + pd.to_timedelta(num[serial], unit="D")
    try:
        out = out.dt.tz_localize(None)
    except (TypeError, AttributeError):
        pass
    return out.dt.strftime("%Y-%m-%d").where(out.notna(), None)


def t_strip(s: pd.Series) -> pd.Series:
    return s.map(lambda v: None if v is None or str(v).strip() in ("", "nan") else str(v).strip())


def t_regex(s: pd.Series, pattern: str, group: int = 1) -> pd.Series:
    rx = re.compile(pattern)

    def f(v):
        m = rx.search(str(v)) if v is not None else None
        return m.group(group) if m else None
    return s.map(f)


def t_map(s: pd.Series, mapping: dict, default: str | None = "keep") -> pd.Series:
    low = {str(k).lower(): v for k, v in mapping.items()}

    def f(v):
        if v is None:
            return None
        hit = low.get(str(v).strip().lower())
        return hit if hit is not None else (v if default == "keep" else default)
    return s.map(f)


def t_charge_type(s: pd.Series) -> pd.Series:
    def f(v):
        if v is None:
            return None
        k = re.sub(r"[\s_-]+", "", str(v).lower())
        return CHARGE_TYPE_MAP.get(k) or CHARGE_TYPE_MAP.get(str(v).lower()) or str(v)
    return s.map(f)


def t_json_sum(s: pd.Series, key: str = "amount") -> pd.Series:
    """Sum a key across a JSON array cell, e.g. GCP credits [{"amount": -1.2}, ...]."""
    def f(v):
        if v is None or v == "":
            return 0.0
        try:
            arr = json.loads(v) if isinstance(v, str) else v
            return float(sum(float(x.get(key, 0) or 0) for x in arr if isinstance(x, dict)))
        except (ValueError, TypeError, AttributeError):
            return None
    return s.map(f)


def t_upper(s: pd.Series) -> pd.Series:
    return s.map(lambda v: str(v).upper() if v is not None else None)


def t_strip_suffix(s: pd.Series, suffix: str) -> pd.Series:
    return s.map(lambda v: str(v)[: -len(suffix)].strip() if v is not None and str(v).endswith(suffix) else v)


def t_scale(s: pd.Series, factor: float) -> pd.Series:
    return s * factor


TRANSFORMS: dict[str, Callable] = {
    "number": t_number, "currency": t_number, "date": t_date, "strip": t_strip,
    "regex": t_regex, "map": t_map, "charge_type": t_charge_type, "json_sum": t_json_sum,
    "upper": t_upper, "strip_suffix": t_strip_suffix, "scale": t_scale,
}

# Hand-written escape hatches, referenced by name from specs. The LLM may not create these.
CUSTOM_TRANSFORMS: dict[str, Callable] = {}


def apply_transforms(s: pd.Series, steps: list) -> pd.Series:
    for step in steps or []:
        if isinstance(step, str):
            name, kw = step, {}
        else:
            (name, arg), = step.items()
            primary = {"regex": "pattern", "strip_suffix": "suffix", "scale": "factor", "json_sum": "key",
                       "map": "mapping", "date": "fmt", "number": "decimal", "currency": "decimal"}.get(name)
            if isinstance(arg, dict) and not (name == "map" and "mapping" not in arg):
                kw = arg                                       # {"regex": {"pattern": "...", "group": 2}}
            else:
                kw = {primary: arg} if primary else {}
        fn = TRANSFORMS.get(name) or CUSTOM_TRANSFORMS.get(name)
        if fn is None:
            raise ValueError(f"Unknown transform: {name}")
        s = fn(s, **kw)
    return s


def default_transforms(field: str) -> list:
    if field in NUMERIC:
        return ["number"]
    if field in DATES:
        return ["date"]
    if field == "charge_type":
        return ["strip", "charge_type"]
    return ["strip"]


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"], 1)}
_PERIOD = re.compile(r"^\s*(?:(?P<y>\d{4})[-/](?P<m>\d{1,2})|(?P<mn>[A-Za-z]{3})[a-z]*[\s-]+(?P<y2>\d{4}))\s*$")


def period_of(text) -> tuple[str, str] | None:
    """A month written as a label ("2026-01", "2026/1", "Jan 2026", "January-2026") -> (first day, last day)."""
    if text is None or not isinstance(text, str):
        return None
    m = _PERIOD.match(text)
    if not m:
        return None
    year = int(m["y"] or m["y2"])
    month = int(m["m"]) if m["m"] else _MONTHS.get((m["mn"] or "").lower()[:3])
    if not month or not 1 <= month <= 12:
        return None
    last = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last:02d}"
