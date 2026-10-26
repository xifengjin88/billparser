"""Service resolution plus the customer-facing numbers: usage summary, forecast
with ranges, and commitment-eligible spend.

Forecast ranges widen with lower confidence and shorter history. Commitment numbers
are indicative only: monthly bills hide hourly usage, so baselines are conservative.
"""

from __future__ import annotations

import calendar
import re
import threading
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from svcres.catalog import Catalog, build
from svcres.resolver import Resolver
from svcres.seeds import AZURE_FAMILY_TO_CATEGORY, FEE_SERVICES

from .transforms import map_unique

COMMITTABLE = {"Compute", "Databases", "Containers", "Analytics"}
# Negation (AWS SavingsPlanNegation) cancels covered usage printed at the on-demand rate: not usage, not a credit
NON_USAGE = {"Tax", "Credit", "Refund", "Purchase", "Fee", "Support", "Adjustment", "Negation"}


# ---------------------------------------------------------------- services
_RESOLVER: Resolver | None = None
# the resolver's cache and synonym files are not safe to change from two threads (the API parses in a pool)
RESOLVER_LOCK = threading.RLock()


def get_resolver(data_dir: Path) -> Resolver:
    """Load the catalog once per process. Without fetched catalogs, seeds still cover common AWS names."""
    global _RESOLVER
    with RESOLVER_LOCK:
        if _RESOLVER is None:
            _RESOLVER = _load_resolver(data_dir)
    return _RESOLVER


def _load_resolver(data_dir: Path) -> Resolver:
    data_dir.mkdir(parents=True, exist_ok=True)
    cat_path = data_dir / "catalog.msgpack"
    if not cat_path.exists():
        build(data_dir / "raw").save(cat_path)
    return Resolver(Catalog.load(cat_path), data_dir)


def resolve_services(rows: pd.DataFrame, data_dir: Path) -> float | None:
    """Resolve the services of the rows; returns the share of usage cost with a resolved service."""
    resolved, usage = resolve_service_sums(rows, data_dir)
    return resolved / usage if usage else None


def resolve_service_sums(rows: pd.DataFrame, data_dir: Path) -> tuple[float, float]:
    """As resolve_services, but the share's parts (usage cost resolved, usage cost): a large file adds them up
    chunk by chunk (bigfile.py)."""
    with RESOLVER_LOCK:
        return _resolve_services(rows, data_dir)


def _resolve_services(rows: pd.DataFrame, data_dir: Path) -> tuple[float, float]:
    if rows.empty or not ({"service_raw", "product_code", "description"} & set(rows.columns)):
        return 0.0, 0.0
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
        picked[todo] = map_unique(rows.loc[todo, col].astype(str).str.strip(),
                                  lambda v, table=table: table[v] if v in table and table[v].service_key else None)
    if provider is None:                                # one provider by its exact matches: weak matches elsewhere retried
        picked = _one_provider(rows, picked, res)
    got = _fields(picked, ("service", "service_code", "category", "method", "provider"))
    rows["service"] = got["service"]
    rows["service_name"] = rows["service"]                    # the catalog's name: empty when nothing matched
    rows["service_code"] = got["service_code"]
    rows["category"] = got["category"]
    for col in ("service_raw", "description"):          # a line printed by Azure service family ("Compute")
        if col in rows and rows["category"].isna().any():
            fam = map_unique(rows[col], lambda v: AZURE_FAMILY_TO_CATEGORY.get(str(v).strip()) if isinstance(v, str)
                             else None)
            rows["category"] = rows["category"].where(rows["category"].notna(), fam)
    rows["resolved_by"] = got["method"]
    if "provider" not in rows or rows["provider"].isna().all():
        rows["provider"] = got["provider"]
    else:
        rows["provider"] = rows["provider"].where(rows["provider"].notna(), got["provider"])
    # unresolved lines keep their raw label so summaries still show them
    rows["service"] = rows["service"].where(rows["service"].notna(), rows.get("service_raw"))
    _fees_and_areas(rows)
    usage = ~rows.get("charge_type", pd.Series([None] * len(rows), index=rows.index)).isin(NON_USAGE)
    cost = rows["billed_cost"].abs().fillna(0).where(usage, 0)
    return float(cost[picked.notna()].sum()), float(cost.sum())


MARKETPLACE_TEXT = re.compile(r"\bmarketplace\b|\bsold by\b", re.I)
# the cloud area of a commitment that matched no service, by its name ("Compute savings plan")
COMMITMENT_AREAS = [("Compute", r"\bcompute\b|virtual machine|\bvms?\b|\bec2\b|app service"),
                    ("Databases", r"\bdatabases?\b|\bsql\b|\brds\b|cosmos|redis|elasticache"),
                    ("AI + Machine Learning", r"machine learning|sagemaker"),
                    ("Storage", r"\bstorage\b|\bfiles?\b|\bblob\b"),
                    ("Analytics", r"\bfabric\b|synapse|databricks|redshift|opensearch")]


def _fees_and_areas(rows: pd.DataFrame) -> None:
    """The kind and the area of the fees. A support charge is a support fee on every bill (not usage); a line the
    bill marks as Marketplace has the area Marketplace when no service gave one; a commitment that matched no
    service has the area its name says. The kind (usage, tax, credit, fee) stays apart from the area."""
    if "charge_type" not in rows:
        rows["charge_type"] = None
    ct, cat = rows["charge_type"], rows["category"]
    text = pd.Series("", index=rows.index)
    for col in ("service_raw", "description", "service"):
        if col in rows:
            text = text + " " + rows[col].fillna("").astype(str)
    support = cat.eq("Support") & (ct.isna() | ct.eq("Usage"))
    rows.loc[support, "charge_type"] = "Support"
    if "service_code" in rows:                          # a program or membership fee (the APN yearly fee)
        rows.loc[rows["service_code"].isin(FEE_SERVICES) & (ct.isna() | ct.eq("Usage")), "charge_type"] = "Fee"
    rows.loc[rows["charge_type"].eq("Support") & cat.isna(), "category"] = "Support"
    # a line the bill marks as sold through a Marketplace is Marketplace money, whatever service its name matches
    usage = rows["charge_type"].isna() | rows["charge_type"].eq("Usage")
    rows.loc[text.str.contains(MARKETPLACE_TEXT) & usage, "category"] = "Marketplace"
    for area, rx in COMMITMENT_AREAS:
        todo = rows["charge_type"].eq("Purchase") & rows["category"].isna()
        rows.loc[todo & text.str.contains(rx, case=False, regex=True), "category"] = area


EXACT_METHODS = ("code", "synonym", "name")


def _fields(picked: pd.Series, names: tuple[str, ...]) -> dict[str, pd.Series]:
    """Fields of the resolution on each row, read once per distinct resolution (a file has a few hundred services
    on millions of lines)."""
    vals = picked.to_numpy(dtype=object)
    codes, _ = pd.factorize(np.fromiter(map(id, vals), dtype=np.int64, count=len(vals)))
    _, first = np.unique(codes, return_index=True)
    objs = vals[first]
    out = {}
    for name in names:
        per = np.array([getattr(o, name) if o is not None else None for o in objs] + [None], dtype=object)
        out[name] = pd.Series(per[codes], index=picked.index).infer_objects()
    return out


def _one_provider(rows: pd.DataFrame, picked: pd.Series, res: Resolver) -> pd.Series:
    """A file with no provider column whose exact matches (code, synonym, name) are 80% or more one provider's: a
    prefix or fuzzy match to another provider ("Cloud Composer" -> Azure "Cloud Services" in a GCP export) is tried
    again within that provider, and left unmatched when it finds nothing there."""
    got = _fields(picked, ("method", "provider"))
    how = got["method"].map(lambda m: m.split(":")[-1] if isinstance(m, str) else None)
    is_exact = how.isin(EXACT_METHODS)
    if not is_exact.any():
        return picked
    top, n = Counter(got["provider"][is_exact]).most_common(1)[0]
    if n < 0.8 * int(is_exact.sum()):
        return picked
    weak = picked.notna() & got["provider"].ne(top) & ~is_exact
    if "provider" in rows:                              # a provider the file prints is kept (a multi-cloud bill)
        weak &= rows["provider"].isna()
    if not weak.any():
        return picked
    picked = picked.copy()
    picked[weak] = None
    for col in ("product_code", "service_raw", "description"):
        todo = weak & picked.isna() & rows[col].notna() if col in rows else None
        if todo is None or not todo.any():
            continue
        table = res.resolve_many(rows.loc[todo, col].astype(str).str.strip().unique(), top)
        picked[todo] = map_unique(rows.loc[todo, col].astype(str).str.strip(),
                                  lambda v, table=table: table[v] if v in table and table[v].service_key
                                  and table[v].provider == top else None)
    return picked


# ---------------------------------------------------------------- analysis
TYPE_OF_CHARGE = {"Tax": "tax", "Credit": "credit", "Refund": "credit", "Purchase": "commitment_fee",
                  "Support": "support_fee", "Fee": "fee", "Negation": "commitment_negation"}


def _kinds(df: pd.DataFrame) -> pd.Series:
    """Each line's kind: usage, tax, credit, commitment_fee, support_fee, fee, marketplace, other."""
    t = df["charge_type"]
    cats = df["category"] if "category" in df else pd.Series(None, index=df.index, dtype=object)
    usage = (t.isna() | t.eq("Usage")).to_numpy()
    other = t.map(TYPE_OF_CHARGE).fillna("other").to_numpy(dtype=object)
    return pd.Series(np.where(usage, np.where(cats.eq("Marketplace").to_numpy(), "marketplace", "usage"), other),
                     index=df.index)


def _by_type(df: pd.DataFrame, key: pd.Series, name: str) -> list[dict]:
    table = df.assign(_kind=_kinds(df)).pivot_table(index=key, columns="_kind", values="billed_cost",
                                                    aggfunc="sum", fill_value=0.0)
    out = []
    for k, r in table.iterrows():
        item = {name: k, **{c: round(float(v), 2) for c, v in r.items() if abs(v) >= 0.005}}
        item["total"] = round(float(r.sum()), 2)
        out.append(item)
    return sorted(out, key=lambda x: -abs(x["total"]))


def _by_service_and_type(df: pd.DataFrame) -> list[dict]:
    """Each service's money by kind: usage, tax, credit, commitment fee, support fee, Marketplace. The kinds stay
    apart; a tax or a credit printed for a service is under that service."""
    return _by_type(df, df["service"].fillna("Unknown"), "service")


def _by_category_and_type(df: pd.DataFrame) -> list[dict]:
    """Each area's money by kind (Compute: usage, commitment fee, credit, tax...): what an area really costs."""
    cats = df["category"] if "category" in df else pd.Series(None, index=df.index)
    return _by_type(df, cats.fillna("Unresolved"), "category")


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
        "by_service_and_type": _by_service_and_type(df),
        "by_category_and_type": _by_category_and_type(df),
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
        # rolled-up lines (bigfile.py) keep each group's first start and, apart, its last
        d_max = pd.to_datetime(df["_last_start"], errors="coerce").max() if "_last_start" in df else d.max()
        start, last = d.min(), max(d_max, end) if has_end else d_max
        # end dates are exclusive (next day 00:00, as in CUR) unless they fall on the last start day
        inclusive = not has_end or end.normalize() == d_max.normalize()
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
