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
    "contracted_cost": "Cost after negotiated discounts, before commitments",
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
    "currency_conversion_rate": "Units of the line's currency per one US dollar, as printed (GCP currency_conversion_rate, "
                                "FOCUS x_CurrencyConversionRate): converts the catalog's USD list prices",
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
    "commitment_type": "The provider's commitment kind, as printed (Reserved Instance, Savings Plan, ...)",
    "commitment_status": "used or unused (unused commitment capacity)",
    "coverage": "How the usage was priced: on_demand, reserved, savings_plan, committed_use, spot, unknown",
    "line_type_raw": "The provider's own line type, unchanged (SavingsPlanCoveredUsage, DiscountedUsage, ...)",
    "sku_id": "The provider's SKU of the line, as printed (AWS product/sku, FOCUS SkuId, GCP sku.id)",
    "sku_price_id": "The provider's id of the exact price of the line (AWS pricing/RateCode, FOCUS SkuPriceId)",
    "meter_id": "The provider's meter id (Azure MeterId)",
    "operation": "The provider's operation (AWS lineItem/Operation: RunInstances, CreateDBInstance:0002)",
    "tenancy": "Shared, Dedicated or Host (AWS product/tenancy)",
    "license_model": "License included, bring your own license, no license required (AWS product/licenseModel)",
    "pre_installed_sw": "Software pre-installed on the instance: NA, SQL Std, SQL Web, SQL Ent (AWS product/preInstalledSw)",
    "resource_id": "The resource the line is for (AWS lineItem/ResourceId, Azure ResourceId, FOCUS ResourceId)",
    "pricing_term": "OnDemand or Reserved, as printed (AWS pricing/term)",
    "purchase_option": "All Upfront, Partial Upfront or No Upfront, as printed (AWS pricing/PurchaseOption)",
    "commitment_term": "The commitment's term, as printed (AWS 1yr / 3yr, Azure 12 months)",
    "list_unit_price": "Price of one unit at the public list price (AWS publicOnDemandRate, Azure PayGPrice)",
    "contracted_unit_price": "Price of one unit after negotiated discounts (Azure UnitPrice, FOCUS ContractedUnitPrice)",
}
NUMERIC = {"billed_cost", "effective_cost", "list_cost", "contracted_cost", "credits", "quantity", "unit_price",
           "tax", "list_unit_price", "contracted_unit_price", "currency_conversion_rate"}
DATES = {"charge_start", "charge_end"}

CHARGE_TYPE_MAP = {
    "usage": "Usage", "purchase": "Purchase", "tax": "Tax", "credit": "Credit",
    "refund": "Refund", "fee": "Fee", "support": "Support", "adjustment": "Adjustment",
    "rifee": "Purchase", "savingsplanrecurringfee": "Purchase", "savingsplanupfrontfee": "Purchase",
    "savingsplancoveredusage": "Usage", "savingsplannegation": "Negation",
    "discountedusage": "Usage", "bundleddiscount": "Credit", "edpdiscount": "Credit",
    "privaterate discount": "Credit", "unusedreservation": "Purchase", "unusedsavingsplan": "Purchase",
    "roundingadjustment": "Adjustment", "new": "Purchase", "renew": "Purchase",
    "cycle charge": "Purchase", "regular": "Usage", "commitment": "Purchase",
}

_CURRENCY_JUNK = re.compile(r"[^\d.,\-()]")


# ---------------------------------------------------------------- per distinct value
UNIQUE_MIN = 2048          # below this many rows a plain map is as fast


def _same(a, b) -> bool:
    return a == b or (isinstance(a, float) and isinstance(b, float) and a != a and b != b)


def map_unique(s: pd.Series, f: Callable) -> pd.Series:
    """s.map(f), computing f once per distinct value: the result is the same as s.map(f), row for row. Exports repeat
    the same few values (a line type, a service, a usage type) millions of times. Text columns only: values that
    are not all strings (1 and 1.0 hash alike) take the plain map. The result has the type s.map(f) infers."""
    if len(s) < UNIQUE_MIN:
        return s.map(f)
    if isinstance(s.dtype, pd.StringDtype) and s.dtype.na_value is np.nan:   # text (CSV): no object copy
        codes, uniques = pd.factorize(s)
        values = np.empty(len(uniques) + 1, dtype=object)
        values[:-1] = [f(u) for u in uniques]
        if (codes < 0).any():
            values[-1] = f(s.dtype.na_value)            # code -1: a missing value, as s.map(f) passes it
        return pd.Series(values[codes], index=s.index, name=s.name).infer_objects()
    obj = s.astype(object)
    na = obj.isna().to_numpy()
    filled = obj[~na]
    if len(filled) and pd.api.types.infer_dtype(filled, skipna=False) != "string":
        return s.map(f)
    out = np.empty(len(obj), dtype=object)
    if len(filled):
        codes, uniques = pd.factorize(filled)
        values = np.empty(len(uniques), dtype=object)
        values[:] = [f(u) for u in uniques]
        out[~na] = values[codes]
    if na.any():
        a, b = f(None), f(np.nan)
        if _same(a, b):
            out[na] = a
        else:                                            # f tells None from NaN: decide row by row (missing rows only)
            out[na] = [a if v is None else f(v) for v in obj[na]]
    return pd.Series(out, index=s.index, name=s.name).infer_objects()     # the type s.map(f) would infer


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


def _strip(v):
    return None if v is None or str(v).strip() in ("", "nan") else str(v).strip()


def t_strip(s: pd.Series) -> pd.Series:
    return map_unique(s, _strip)


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


def _charge_type(v):
    if v is None:
        return None
    k = re.sub(r"[\s_-]+", "", str(v).lower())
    return CHARGE_TYPE_MAP.get(k) or CHARGE_TYPE_MAP.get(str(v).lower()) or str(v)


def t_charge_type(s: pd.Series) -> pd.Series:
    return map_unique(s, _charge_type)


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


def _json_value(v, key: str):
    """key's value in a cell: a JSON object (text or dict), or a list of {"key": k, "value": v} pairs (GCP labels)."""
    if v is None or (isinstance(v, float) and v != v):
        return None
    if isinstance(v, str):
        if not v.strip():
            return None
        try:
            v = json.loads(v)
        except ValueError:
            return None
    if isinstance(v, dict):
        out = v.get(key)
    elif isinstance(v, (list, tuple, np.ndarray)):
        out = next((x.get("value") for x in v if isinstance(x, dict) and x.get("key") == key), None)
    else:
        return None
    if out is None or isinstance(out, (dict, list)):
        return None if out is None else json.dumps(out)
    out = str(out).strip()
    return out or None


def t_json_get(s: pd.Series, key: str = "") -> pd.Series:
    """One key's value from a JSON object cell, e.g. Azure AdditionalInfo {"ServiceType": "Standard_D4s_v5"}, or from a
    key/value list (GCP system_labels [{"key": "compute.googleapis.com/machine_spec", "value": "n2-standard-4"}])."""
    return map_unique(s, lambda v: _json_value(v, key))


# A provider line type or pricing model -> how the usage was priced. Keys are lower case with spaces, "_" and "-"
# removed. Line types that are not usage (tax, credit, fee, ...) have no coverage: None, never "unknown".
COVERAGE_MAP = {
    "usage": "on_demand", "ondemand": "on_demand", "standard": "on_demand",
    "discountedusage": "reserved", "reservation": "reserved", "reservations": "reserved", "reserved": "reserved",
    "rifee": "reserved", "unusedreservation": "reserved",
    "savingsplancoveredusage": "savings_plan", "savingsplannegation": "savings_plan", "savingsplan": "savings_plan",
    "savingsplanrecurringfee": "savings_plan", "savingsplanupfrontfee": "savings_plan",
    "unusedsavingsplan": "savings_plan",
    "committed": "committed_use", "committedusediscount": "committed_use", "cud": "committed_use",
    "spot": "spot", "dynamic": "spot",
}
NOT_USAGE = {"tax", "credit", "refund", "fee", "discount", "bundleddiscount", "edpdiscount", "privateratediscount",
             "adjustment", "roundingadjustment", "roundingerror", "purchase", "support"}
COVERAGES = ("on_demand", "reserved", "savings_plan", "committed_use", "spot", "unknown")


def coverage_of(v) -> str | None:
    if v is None or (isinstance(v, float) and v != v) or not str(v).strip():
        return None
    k = re.sub(r"[\s_-]+", "", str(v).lower())
    if k in NOT_USAGE:
        return None
    return COVERAGE_MAP.get(k, "unknown")


def t_coverage(s: pd.Series) -> pd.Series:
    """A line type or pricing model as a coverage value (see COVERAGE_MAP)."""
    return map_unique(s, coverage_of)


# GCP credit types that mean the usage was covered by a committed use discount
CUD_CREDITS = {"COMMITTED_USAGE_DISCOUNT", "COMMITTED_USAGE_DISCOUNT_DOLLAR_BASE"}


def _commitment_credit(v):
    if v is None or v == "":
        return None
    try:
        arr = json.loads(v) if isinstance(v, str) else v
    except (ValueError, TypeError):
        return None
    types = {str(x.get("type", "")).upper() for x in arr if isinstance(x, dict)} if isinstance(arr, list) else set()
    return "committed_use" if types & CUD_CREDITS else None


def t_commitment_credit(s: pd.Series) -> pd.Series:
    """A GCP credits cell (JSON list): "committed_use" when one credit is a committed use discount, else None."""
    return map_unique(s, _commitment_credit)


def t_upper(s: pd.Series) -> pd.Series:
    return s.map(lambda v: str(v).upper() if v is not None else None)


def t_strip_suffix(s: pd.Series, suffix: str) -> pd.Series:
    return s.map(lambda v: str(v)[: -len(suffix)].strip() if v is not None and str(v).endswith(suffix) else v)


def t_scale(s: pd.Series, factor: float) -> pd.Series:
    return s * factor


TRANSFORMS: dict[str, Callable] = {
    "number": t_number, "currency": t_number, "date": t_date, "strip": t_strip,
    "regex": t_regex, "map": t_map, "charge_type": t_charge_type, "json_sum": t_json_sum,
    "upper": t_upper, "strip_suffix": t_strip_suffix, "scale": t_scale, "coverage": t_coverage,
    "commitment_credit": t_commitment_credit, "json_get": t_json_get,
}

# Hand-written escape hatches, referenced by name from specs. The LLM may not create these.
CUSTOM_TRANSFORMS: dict[str, Callable] = {}


def apply_transforms(s: pd.Series, steps: list) -> pd.Series:
    for step in steps or []:
        if isinstance(step, str):
            name, kw = step, {}
        else:
            (name, arg), = step.items()
            primary = {"regex": "pattern", "strip_suffix": "suffix", "scale": "factor", "json_sum": "key", "json_get": "key",
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
