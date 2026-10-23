"""Validation produces evidence, not a single pass/fail.

Checks: row presence, line math, stated totals (found anywhere), hidden total rows,
row coverage, dual-extraction agreement, plausibility, service resolution.
Each check adds or removes confidence. Files without totals are still accepted,
marked "unreconciled", with wider forecast ranges downstream.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from .spec import Extraction
from .transforms import parse_number

TOTAL_LABEL = re.compile(r"^\s*(?:grand\s+)?(?:sub)?\s*total\b|\b(?:amount|balance)\s+due\b|\binvoice\s+total\b", re.I)
TAX_LABEL = re.compile(r"\b(?:sales\s+tax|tax|vat|gst|hst)\b", re.I)
CREDIT_LABEL = re.compile(r"\b(?:credit|discount|refund|promotion|adjustment)\b", re.I)


@dataclass
class Check:
    name: str
    status: str            # pass | fail | warn | skip
    detail: str
    weight: float = 0.0    # confidence delta applied


# ---------------------------------------------------------------- cleanup (runs for every route)
def clean_rows(ext: Extraction) -> list[str]:
    """Tag tax/credit lines and pull total rows out of the line items."""
    notes: list[str] = []
    df = ext.rows
    if df.empty or "billed_cost" not in df:
        return notes
    label = _label_series(df)
    if "charge_type" not in df:
        df["charge_type"] = None
    tax = label.str.contains(TAX_LABEL).fillna(False).astype(bool) & df["charge_type"].isna()
    df.loc[tax, "charge_type"] = "Tax"
    credit = label.str.contains(CREDIT_LABEL).fillna(False).astype(bool) & df["charge_type"].isna() & (df["billed_cost"] < 0)
    df.loc[credit, "charge_type"] = "Credit"

    # group headers that name a provider ("Amazon Web Services") belong in provider, not service
    if "service_raw" in df:
        prov = df["service_raw"].map(_provider_of)
        is_prov = prov.notna()
        if is_prov.any():
            if "provider" not in df:
                df["provider"] = None
            df.loc[is_prov, "provider"] = df.loc[is_prov, "provider"].where(df.loc[is_prov, "provider"].notna(), prov[is_prov])
            df.loc[is_prov, "service_raw"] = df.loc[is_prov, "description"] if "description" in df else None
            notes.append(f"{int(is_prov.sum())} rows had a provider name as service; used the description instead")
            label = _label_series(df)

    # a "Total" row is a stated total, not spend. Safety net for totals in other languages
    # ("Gesamtbetrag", "Totale"): any single row equal to the sum of all the others.
    is_tot = label.str.contains(TOTAL_LABEL).fillna(False).astype(bool)
    if not is_tot.any() and len(df) >= 3:
        cost_all = df["billed_cost"].fillna(0.0)
        grand = cost_all.sum()
        eq = ((grand - cost_all) - cost_all).abs() <= np.maximum(0.011, 0.0005 * cost_all.abs())
        eq &= cost_all.abs() > 0
        if int(eq.sum()) == 1:
            is_tot = eq
    if is_tot.any():
        others = df.loc[~is_tot, "billed_cost"].sum()
        for idx in df.index[is_tot]:
            amt = df.at[idx, "billed_cost"]
            ext.stated_totals.append({"label": label[idx][:80], "amount": amt, "kind": "total_row",
                                      "src": df.at[idx, "_src"] if "_src" in df else None})
        notes.append(f"Moved {int(is_tot.sum())} total row(s) out of line items (sum of others {others:,.2f})")
        ext.rows = df.loc[~is_tot].reset_index(drop=True)
    return notes


PROVIDER_NAMES = {"amazon web services": "aws", "aws": "aws", "amazon": "aws", "microsoft azure": "azure",
                  "azure": "azure", "microsoft": "azure", "google cloud": "gcp", "google cloud platform": "gcp",
                  "gcp": "gcp", "google": "gcp"}


def _provider_of(v) -> str | None:
    if v is None:
        return None
    k = re.sub(r"\s+", " ", re.sub(r"[^a-z ]+", " ", str(v).lower())).strip()
    return PROVIDER_NAMES.get(k) or PROVIDER_NAMES.get(re.sub(r"\s+(charges|usage|subscription|plan)$", "", k))


def _label_series(df: pd.DataFrame) -> pd.Series:
    parts = [df[c].astype("string").fillna("") for c in ("service_raw", "description") if c in df]
    if not parts:
        return pd.Series([""] * len(df), index=df.index, dtype="string")
    lab = parts[0]
    for p in parts[1:]:
        lab = lab + " " + p
    return lab.str.strip()


# ---------------------------------------------------------------- checks
def validate(ext: Extraction, alt: Extraction | None = None, resolved_share: float | None = None,
             kind: str = "cost_lines") -> dict:
    df = ext.rows
    checks: list[Check] = []
    n = len(df)
    if n == 0 or "billed_cost" not in df or df["billed_cost"].notna().sum() == 0:
        checks.append(Check("rows", "fail", "No cost rows extracted", -1.0))
        return _summarize(checks, 0.0, reconciled=False, ext=ext)
    checks.append(Check("rows", "pass", f"{n} rows", 0.1))
    cost = df["billed_cost"].fillna(0.0)
    total = float(cost.sum())
    non_tax = float(cost[df.get("charge_type", pd.Series(index=df.index)).ne("Tax")].sum())

    checks.append(_labels(df))
    checks.append(_line_math(df))
    tot_check, reconciled = _stated_totals(ext, total, non_tax)
    checks.append(tot_check)
    checks.append(_coverage(ext))
    if alt is not None:
        checks.append(_dual(ext, alt))
    checks += _plausibility(df)
    if resolved_share is not None:
        st = "pass" if resolved_share >= 0.9 else "warn" if resolved_share >= 0.6 else "fail"
        checks.append(Check("service_resolution", st, f"{resolved_share:.0%} of cost has a resolved service",
                            {"pass": 0.05, "warn": -0.05, "fail": -0.15}[st]))
    base = 0.55
    conf = float(np.clip(base + sum(c.weight for c in checks), 0.0, 1.0))
    return _summarize(checks, conf, reconciled, ext)


def _labels(df: pd.DataFrame) -> Check:
    """Spend we can't attribute to anything is not useful: require a label on most of the cost."""
    lab = _label_series(df).str.len().fillna(0) > 0
    cost = df["billed_cost"].abs().fillna(0)
    share = float(cost[lab.astype(bool)].sum() / cost.sum()) if cost.sum() else 0.0
    if share >= 0.9:
        return Check("labels", "pass", f"{share:.0%} of cost has a service or description", 0.0)
    return Check("labels", "fail", f"Only {share:.0%} of cost has a service or description label", -0.35)


def _line_math(df: pd.DataFrame) -> Check:
    need = {"quantity", "unit_price", "billed_cost"}
    if not need <= set(df.columns):
        return Check("line_math", "skip", "No quantity and unit price columns")
    sub = df.dropna(subset=list(need))
    sub = sub[(sub["quantity"] != 0) & (sub["unit_price"] != 0)]
    if len(sub) < 3:
        return Check("line_math", "skip", "Too few lines with quantity and unit price")
    calc = sub["quantity"] * sub["unit_price"]
    ok = (calc - sub["billed_cost"]).abs() <= np.maximum(0.011, 0.01 * sub["billed_cost"].abs())
    share = float(ok.mean())
    if share >= 0.95:
        return Check("line_math", "pass", f"{share:.0%} of {len(sub)} lines: qty x price = amount", 0.15)
    ratio = (sub["billed_cost"] / calc.replace(0, np.nan)).dropna()
    if len(ratio) and ratio.std() / max(1e-9, abs(ratio.mean())) < 0.01:
        return Check("line_math", "warn",
                     f"Consistent ratio {ratio.mean():.4g} between amount and qty x price "
                     "(unit block size, discount or currency conversion)", 0.0)
    return Check("line_math", "fail", f"Only {share:.0%} of {len(sub)} lines match qty x price", -0.15)


def _stated_totals(ext: Extraction, total: float, non_tax: float) -> tuple[Check, bool]:
    cands = []
    for t in ext.stated_totals:
        amt = parse_number(t.get("amount"))
        if amt is not None and t.get("kind") != "subtotal":
            cands.append((t, amt))
    if not cands:
        subs = [parse_number(t.get("amount")) for t in ext.stated_totals if t.get("kind") == "subtotal"]
        subs = [x for x in subs if x is not None]
        if subs and abs(sum(subs) - non_tax) <= max(0.05, 0.0005 * abs(non_tax)):
            return Check("stated_total", "pass", f"{len(subs)} stated subtotals sum to the extracted lines "
                         f"({sum(subs):,.2f}); no grand total on the document", 0.25), True
        detail = "No stated total found" + (f" ({len(subs)} subtotals seen, sum {sum(subs):,.2f})" if subs else "")
        return Check("stated_total", "skip", detail + "; marked unreconciled", 0.0), False
    tol = max(0.05, 0.0005 * abs(total))
    tax_rows = [parse_number(t.get("amount")) or 0 for t in ext.stated_totals if re.search(r"tax", t.get("label", ""), re.I)]
    targets = {"sum of lines": total, "sum before tax": non_tax, "sum plus stated tax": total + sum(tax_rows)}
    for t, amt in sorted(cands, key=lambda x: -abs(x[1])):
        for name, val in targets.items():
            if abs(amt - val) <= tol:
                return Check("stated_total", "pass", f"Stated {t['label']!r} {amt:,.2f} = {name} {val:,.2f}", 0.35), True
    t, amt = max(cands, key=lambda x: abs(x[1]))
    return Check("stated_total", "fail", f"Stated {t['label']!r} {amt:,.2f} vs extracted {total:,.2f} "
                 f"(diff {total - amt:,.2f})", -0.35), False


def _coverage(ext: Extraction) -> Check:
    if ext.amount_like_rows:                                    # grid tables
        accounted = len(ext.rows) + len([t for t in ext.stated_totals if t.get("kind") in ("subtotal", "total", "total_row", "end_anchor")])
        share = min(1.0, accounted / ext.amount_like_rows)
        st = "pass" if share >= 0.98 else "warn" if share >= 0.9 else "fail"
        return Check("row_coverage", st, f"{accounted} of {ext.amount_like_rows} rows with amounts accounted for",
                     {"pass": 0.1, "warn": -0.05, "fail": -0.25}[st])
    if ext.source_rows:
        moved = len([t for t in ext.stated_totals if t.get("kind") == "total_row"])
        share = min(1.0, (len(ext.rows) + moved) / ext.source_rows)
        st = "pass" if share >= 0.9 else "warn" if share >= 0.5 else "fail"
        return Check("row_coverage", st, f"{len(ext.rows) + moved} of {ext.source_rows} source rows accounted for",
                     {"pass": 0.05, "warn": -0.05, "fail": -0.2}[st])
    return Check("row_coverage", "skip", "No source row count")


def _dual(ext: Extraction, alt: Extraction) -> Check:
    a = float(ext.rows["billed_cost"].sum())
    b = float(alt.rows["billed_cost"].sum()) if not alt.rows.empty and "billed_cost" in alt.rows else 0.0
    if abs(a - b) <= max(0.05, 0.001 * abs(a)) and abs(len(ext.rows) - len(alt.rows)) <= max(1, 0.02 * len(ext.rows)):
        return Check("dual_extraction", "pass", f"Independent extraction agrees ({b:,.2f}, {len(alt.rows)} rows)", 0.15)
    return Check("dual_extraction", "warn", f"Independent extraction differs: {b:,.2f} / {len(alt.rows)} rows "
                 f"vs {a:,.2f} / {len(ext.rows)} rows", -0.1)


def _plausibility(df: pd.DataFrame) -> list[Check]:
    out = []
    if "charge_start" in df and df["charge_start"].notna().any():
        d = pd.to_datetime(df["charge_start"], errors="coerce")
        bad = d.notna() & ((d < "2006-01-01") | (d > pd.Timestamp.now() + pd.Timedelta(days=62)))
        if bad.any():
            out.append(Check("dates", "warn", f"{int(bad.sum())} dates out of range", -0.05))
    if "currency" in df:
        cur = df["currency"].dropna().astype(str).str.upper().unique()
        if len(cur) > 1:
            out.append(Check("currency", "warn", f"Multiple currencies: {list(cur)[:5]}", -0.05))
    if "charge_type" in df:
        neg = df["billed_cost"] < 0
        unexplained = neg & ~df["charge_type"].fillna("").isin(["Credit", "Refund", "Adjustment"])
        if unexplained.sum() > max(2, 0.05 * len(df)):
            out.append(Check("negatives", "warn", f"{int(unexplained.sum())} negative lines not marked as credits", -0.05))
    return out


def _summarize(checks: list[Check], conf: float, reconciled: bool, ext: Extraction) -> dict:
    critical_fail = any(c.status == "fail" and c.name in ("rows", "stated_total", "row_coverage", "labels") for c in checks)
    if reconciled and conf >= 0.7 and not critical_fail:
        status = "reconciled"
    elif not critical_fail and conf >= 0.6:
        status = "unreconciled"
    else:
        status = "needs_review"
    return {"status": status, "confidence": round(conf, 2), "checks": [asdict(c) for c in checks],
            "feedback": [f"{c.name}: {c.detail}" for c in checks if c.status in ("fail", "warn")]}
