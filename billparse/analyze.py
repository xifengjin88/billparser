"""Service resolution plus the customer-facing numbers: usage summary, forecast
with ranges, and commitment-eligible spend.

Forecast ranges widen with lower confidence and shorter history. Commitment numbers
are indicative only: monthly bills hide hourly usage, so baselines are conservative.
"""

from __future__ import annotations

import calendar
from pathlib import Path

import numpy as np
import pandas as pd

from svcres.catalog import Catalog, build
from svcres.resolver import Resolver

COMMITTABLE = {"Compute", "Databases", "Containers", "Analytics"}
NON_USAGE = {"Tax", "Credit", "Refund", "Purchase", "Fee", "Support", "Adjustment"}


# ---------------------------------------------------------------- services
_RESOLVER: Resolver | None = None


def get_resolver(data_dir: Path) -> Resolver:
    """Load the catalog once per process. Without fetched catalogs, seeds still cover common AWS names."""
    global _RESOLVER
    if _RESOLVER is None:
        data_dir.mkdir(parents=True, exist_ok=True)
        cat_path = data_dir / "catalog.msgpack"
        if not cat_path.exists():
            build(data_dir / "raw").save(cat_path)
        _RESOLVER = Resolver(Catalog.load(cat_path), data_dir)
    return _RESOLVER


def resolve_services(rows: pd.DataFrame, data_dir: Path) -> float | None:
    if rows.empty or not ({"service_raw", "product_code", "description"} & set(rows.columns)):
        return None
    res = get_resolver(data_dir)
    provider = None
    if "provider" in rows and rows["provider"].notna().any():
        top = rows["provider"].dropna().astype(str).str.lower().value_counts()
        provider = top.index[0] if top.iloc[0] / top.sum() > 0.9 and top.index[0] in ("aws", "azure", "gcp") else None
    picked = pd.Series([None] * len(rows), index=rows.index, dtype=object)
    for col in ("product_code", "service_raw", "description"):
        if col not in rows:
            continue
        todo = picked.isna() & rows[col].notna()
        if not todo.any():
            continue
        table = res.resolve_many(rows.loc[todo, col].astype(str).str.strip().unique(), provider)
        picked[todo] = rows.loc[todo, col].astype(str).str.strip().map(
            lambda v: table[v] if v in table and table[v].service_key else None)
    rows["service"] = picked.map(lambda r: r.service if r else None)
    rows["category"] = picked.map(lambda r: r.category if r else None)
    rows["resolved_by"] = picked.map(lambda r: r.method if r else None)
    if "provider" not in rows or rows["provider"].isna().all():
        rows["provider"] = picked.map(lambda r: r.provider if r else None)
    else:
        rows["provider"] = rows["provider"].where(rows["provider"].notna(), picked.map(lambda r: r.provider if r else None))
    # unresolved lines keep their raw label so summaries still show them
    rows["service"] = rows["service"].where(rows["service"].notna(), rows.get("service_raw"))
    usage = ~rows.get("charge_type", pd.Series([None] * len(rows), index=rows.index)).isin(NON_USAGE)
    cost = rows["billed_cost"].abs().fillna(0).where(usage, 0)
    return float(cost[picked.notna()].sum() / cost.sum()) if cost.sum() else None


# ---------------------------------------------------------------- analysis
def analyze(rows: pd.DataFrame, confidence: float, status: str, fields: dict) -> dict:
    if rows.empty or "billed_cost" not in rows:
        return {}
    df = rows.copy()
    if "service" not in df:
        df["service"] = df["service_raw"] if "service_raw" in df else df.get("description")
    df["charge_type"] = df.get("charge_type", pd.Series([None] * len(df), index=df.index))
    usage = df[~df["charge_type"].isin(NON_USAGE)]
    total = float(df["billed_cost"].sum())
    usage_total = float(usage["billed_cost"].sum())
    currency = _mode(df.get("currency"))

    by_service = (usage.groupby(usage["service"].fillna("Unknown"))["billed_cost"].sum()
                  .sort_values(ascending=False))
    by_category = (usage.groupby(usage.get("category", pd.Series(index=usage.index)).fillna("Unresolved"))["billed_cost"]
                   .sum().sort_values(ascending=False))
    by_provider = usage.groupby(usage.get("provider", pd.Series(index=usage.index)).fillna("unknown"))["billed_cost"].sum()

    period = _period(df, fields)
    monthly = _monthly(df)
    forecast = _forecast(usage_total, period, monthly, confidence, status)
    commit = _commitments(usage, by_category, monthly, confidence)

    return {
        "currency": currency, "total_billed": round(total, 2), "usage_spend": round(usage_total, 2),
        "non_usage": {k: round(float(v), 2) for k, v in df[df["charge_type"].isin(NON_USAGE)]
                      .groupby("charge_type")["billed_cost"].sum().items()},
        "period": period,
        "by_provider": {k: round(float(v), 2) for k, v in by_provider.items()},
        "by_category": {k: round(float(v), 2) for k, v in by_category.items()},
        "top_services": [{"service": k, "cost": round(float(v), 2),
                          "share": round(float(v / usage_total), 3) if usage_total else None}
                         for k, v in by_service.head(15).items()],
        "monthly": {k: round(float(v), 2) for k, v in monthly.items()} if monthly is not None else None,
        "forecast": forecast,
        "commitment_opportunity": commit,
    }


def _mode(s: pd.Series | None):
    if s is None or s.dropna().empty:
        return None
    return s.dropna().astype(str).str.upper().mode().iloc[0]


def _period(df: pd.DataFrame, fields: dict) -> dict:
    if "charge_start" in df and df["charge_start"].notna().any():
        d = pd.to_datetime(df["charge_start"], errors="coerce").dropna()
        end = pd.to_datetime(df["charge_end"], errors="coerce").dropna().max() if "charge_end" in df else None
        has_end = end is not None and not pd.isna(end)
        start, last = d.min(), max(d.max(), end) if has_end else d.max()
        # end dates are exclusive (next day 00:00, as in CUR) unless they fall on the last start day
        inclusive = not has_end or end.normalize() == d.max().normalize()
        days = max(1, (last.normalize() - start.normalize()).days + (1 if inclusive else 0))
        return {"start": start.date().isoformat(), "end": last.date().isoformat(), "days": int(days), "source": "line dates"}
    if "period" in fields:
        return {"label": fields["period"]["value"], "days": None, "source": "invoice header"}
    return {"days": None, "source": "unknown (assumed one month)"}


def _monthly(df: pd.DataFrame) -> pd.Series | None:
    if "charge_start" not in df or df["charge_start"].isna().all():
        return None
    d = pd.to_datetime(df["charge_start"], errors="coerce")
    use = df[d.notna() & ~df["charge_type"].isin(NON_USAGE)]
    if use.empty:
        return None
    m = use.groupby(pd.to_datetime(use["charge_start"]).dt.to_period("M").astype(str))["billed_cost"].sum()
    return m if len(m) >= 1 else None


def _forecast(usage_total: float, period: dict, monthly: pd.Series | None, conf: float, status: str) -> dict:
    today = pd.Timestamp.today()
    nxt = (today + pd.offsets.MonthBegin(1))
    days_next = calendar.monthrange(nxt.year, nxt.month)[1]
    method, base = "single bill run rate", usage_total
    if monthly is not None and len(monthly) >= 2:
        full = monthly.iloc[:-1] if len(monthly) > 2 else monthly       # last month may be partial
        growth = full.pct_change().dropna()
        g = float(np.clip(growth.median(), -0.2, 0.2)) * 0.5 if len(growth) else 0.0   # damped
        base, method = float(full.iloc[-1]) * (1 + g), f"damped trend over {len(full)} months (growth {g:+.1%}/mo)"
    elif period.get("days") and period["days"] < 27:
        base = usage_total / period["days"] * 30.44
        method = f"run rate from {period['days']} days of data"
    next_month = base * days_next / 30.44 if period.get("days") else base
    width = 0.08 + (1 - conf) * 0.25 + (0.07 if status != "reconciled" else 0.0) \
        + (0.05 if monthly is None or len(monthly) < 2 else 0.0)
    return {"method": method, "next_month": _band(next_month, width), "next_quarter": _band(next_month * 3, width * 1.2),
            "range_pct": round(width, 3)}


def _band(x: float, w: float) -> dict:
    return {"low": round(x * (1 - w), 2), "expected": round(x, 2), "high": round(x * (1 + w), 2)}


def _commitments(usage: pd.DataFrame, by_cat: pd.Series, monthly: pd.Series | None, conf: float) -> dict:
    eligible = float(sum(v for k, v in by_cat.items() if k in COMMITTABLE))
    if eligible <= 0:
        return {"eligible_monthly_spend": 0.0, "note": "No compute, database, container or analytics spend identified."}
    stable = monthly is not None and len(monthly) >= 3 and (monthly.std() / monthly.mean() < 0.1)
    lo_base, hi_base = (0.6, 0.8) if stable else (0.45, 0.7)
    lo_base, hi_base = lo_base * (0.8 + 0.2 * conf), hi_base * (0.8 + 0.2 * conf)
    return {
        "eligible_monthly_spend": round(eligible, 2),
        "suggested_baseline_coverage": [round(lo_base, 2), round(hi_base, 2)],
        "coverable_monthly_spend": [round(eligible * lo_base, 2), round(eligible * hi_base, 2)],
        "assumed_discount_range": [0.2, 0.4],
        "indicative_monthly_savings": [round(eligible * lo_base * 0.2, 2), round(eligible * hi_base * 0.4, 2)],
        "note": ("Indicative only. Monthly bills hide hourly usage, so the baseline is conservative. "
                 "Hourly data (CUR, usage exports or connected accounts) narrows this range. "
                 "Insured commitments can cover the uncertain band above the baseline."),
    }
