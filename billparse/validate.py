"""Validation produces evidence, not a single pass/fail.

Checks: row presence, line math, stated totals (found anywhere), hidden total rows,
row coverage, dual-extraction agreement, plausibility, service resolution, and the price checks: the catalog's list
prices (pricing.py) as an independent reference that a parse which adds up but read the wrong column or unit fails.
Each check adds or removes confidence. Files without totals are still accepted,
marked "unreconciled", with wider forecast ranges downstream.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from prices.units import normalize as unit_of

from .pricing import flagged
from .spec import Extraction
from .transforms import map_unique, parse_number
from .tree import bound

TOTAL_LABEL = re.compile(r"^\s*(?:grand\s+)?(?:sub)?\s*total\b|\b(?:amount|balance)\s+due\b|\binvoice\s+total\b", re.I)
TAX_LABEL = re.compile(r"\b(?:sales\s+tax|tax|vat|gst|hst)\b", re.I)
CREDIT_LABEL = re.compile(r"\b(?:credit|discount|refund|promotion|adjustment)\b", re.I)
SAVINGS_PLANS_FEE = re.compile(r"^\s*savings\s+plans?\s+for\b", re.I)


@dataclass
class Check:
    name: str
    status: str            # pass | fail | warn | skip
    detail: str
    weight: float = 0.0    # confidence delta applied


# ---------------------------------------------------------------- cleanup (runs for every route)
def clean_rows(ext: Extraction, chunk: bool = False) -> list[str]:
    """Tag tax/credit lines and pull total rows out of the line items. chunk: the rows are one chunk of a large
    file (bigfile.py), so a row equal to the sum of the others is no total."""
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
    # a Savings Plans "service" is the plan's fee (AWS: "Savings Plans for AWS Compute usage"), not usage it covers
    if "service_raw" in df:
        fee = df["service_raw"].astype("string").str.contains(SAVINGS_PLANS_FEE).fillna(False).astype(bool)
        df.loc[fee & df["charge_type"].isna(), "charge_type"] = "Purchase"

    # group headers that name a provider ("Amazon Web Services") belong in provider, not service
    if "service_raw" in df:
        prov = map_unique(df["service_raw"], _provider_of)
        is_prov = prov.notna()
        if is_prov.any():
            if "provider" not in df:
                df["provider"] = None
            df.loc[is_prov, "provider"] = df.loc[is_prov, "provider"].where(df.loc[is_prov, "provider"].notna(), prov[is_prov])
            df.loc[is_prov, "service_raw"] = df.loc[is_prov, "description"] if "description" in df else None
            notes.append(f"{int(is_prov.sum())} rows had a provider name as service; used the description instead")
            label = _label_series(df)

    if ext.evidence:                                # the tree of sums already separated totals from lines
        return notes
    # a "Total" row is a stated total, not spend. Safety net for totals in other languages
    # ("Gesamtbetrag", "Totale"): any single row equal to the sum of all the others.
    is_tot = label.str.contains(TOTAL_LABEL).fillna(False).astype(bool)
    if not is_tot.any() and len(df) >= 3 and not chunk:
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


# ---------------------------------------------------------------- what the checks read from the rows
PRICED = ("exact", "attributes", "assumed")
# a price ratio near one of these: a unit or column mix-up (GB vs MB, seconds vs hours, days vs hours, months vs hours,
# days vs months)
MIXUPS = (1000.0, 1 / 1000, 3600.0, 1 / 3600, 24.0, 1 / 24, 730.0, 1 / 730, 730 / 24, 24 / 730)
_LOG_MIXUPS = np.log(np.array(MIXUPS))


def _nearest_mixup(r: pd.Series) -> pd.Series:
    """Each ratio's nearest mix-up factor (in log scale): a line counts for one factor only."""
    logs = np.log(r.clip(lower=1e-12).to_numpy(dtype=float))
    return pd.Series(np.array(MIXUPS)[np.abs(logs[:, None] - _LOG_MIXUPS[None, :]).argmin(axis=1)], index=r.index)
MAX_EXAMPLES = 5
FAR = 10.0      # a price more than 10x off the list price: no discount explains it (quantity and price swapped)
MIN_GROUNDED = 5   # catalog_grounding skips with fewer lines tested (one guessed line must not fail a file)


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    return df[name] if name in df else pd.Series(None, index=df.index, dtype=object)


def _usage(df: pd.DataFrame) -> pd.Series:
    ct = _col(df, "charge_type")
    return ct.isna() | ct.eq("Usage")


def _flag(df: pd.DataFrame, name: str | None) -> pd.Series:
    """A flag column of pricing (True, 1, "True", 1.0) as bool; absent: False."""
    if not name or name not in df:
        return pd.Series(False, index=df.index)
    return flagged(df[name])


def _product_label(df: pd.DataFrame) -> pd.Series:
    """What a line named as its product: "m5.xlarge in eu-west-9", a usage type, or a SKU."""
    it, region = _col(df, "instance_type").astype(object), _col(df, "region").astype(object)
    ut, sku = _col(df, "usage_type").astype(object), _col(df, "sku_id").astype(object)
    out = ut.where(ut.notna(), sku).astype(object)
    named = it.notna()
    out[named] = it[named].astype(str) + " in " + region[named].astype(str)
    return out.fillna("?").astype(str)


@dataclass
class RowStats:
    """Everything the row-based checks read, as sums and counts: built from a whole frame, or chunk by chunk and
    merged (bigfile.py), so a file of any size is checked by the same functions."""
    rows: int = 0
    cost_rows: int = 0              # lines with a billed cost
    total: float = 0.0
    non_tax: float = 0.0
    label_cost: float = 0.0         # |cost| of lines with a service or description
    label_rows: int = 0             # lines with a cost and a service or description
    abs_cost: float = 0.0
    line_math: bool = False         # the rows have quantity, unit price and cost
    lm_n: int = 0                   # lines line math tests
    lm_ok: int = 0
    ratio_n: int = 0                # cost / (quantity x price) on those lines: count, sum, sum of squares
    ratio_sum: float = 0.0
    ratio_sq: float = 0.0
    dates: bool = False             # the rows have charge dates
    bad_dates: int = 0
    currencies: set = field(default_factory=set)
    charge_types: bool = False      # the rows have a charge type
    negatives: int = 0              # negative lines not marked credit, refund, adjustment or negation
    # the price checks (pricing.py ran on the rows): the catalog was asked for this many AWS usage lines
    catalog_lines: int = 0
    g_priced: list = field(default_factory=lambda: [0, 0.0])       # catalog_grounding: [lines, cost] priced
    g_nosuch: list = field(default_factory=lambda: [0, 0.0])       # ... and no_such_product
    g_conflict: list = field(default_factory=lambda: [0, 0.0])     # ... and exact matches whose printed region, type
    #                                                                or OS is not the product's (price_conflict)
    g_values: dict = field(default_factory=dict)                   # the values not found -> cost (at most 50)
    pl: list = field(default_factory=lambda: [0, 0.0, 0.0])        # printed_list_price: [lines, weight, agreeing]
    pl_mix: dict = field(default_factory=dict)                     # a mix-up factor -> weight
    ip: list = field(default_factory=lambda: [0, 0.0, 0.0, 0.0])   # implied_unit_price: [lines, cost, in 0.5-1.05,
    #                                                                 more than 10x off the list price]
    ip_mix: dict = field(default_factory=dict)
    pu: list = field(default_factory=lambda: [0.0, 0.0])           # price_units: [cost, at unit_mismatch]
    uh: list = field(default_factory=lambda: [0, 0])               # usage_hours: [lines tested, over the window]
    co: list = field(default_factory=lambda: [0, 0])               # cost_order: [lines tested, out of order]
    examples: dict = field(default_factory=dict)                   # check -> up to 5 example rows

    @classmethod
    def of(cls, df: pd.DataFrame) -> RowStats:
        out = cls()
        out.add(df)
        return out

    def add(self, df: pd.DataFrame) -> None:
        self.rows += len(df)
        if df.empty or "billed_cost" not in df:
            return
        self.cost_rows += int(df["billed_cost"].notna().sum())
        cost = df["billed_cost"].fillna(0.0)
        self.total += float(cost.sum())
        self.non_tax += float(cost[df.get("charge_type", pd.Series(index=df.index)).ne("Tax")].sum())
        lab = (_label_series(df).str.len().fillna(0) > 0).astype(bool)
        cabs = df["billed_cost"].abs().fillna(0)
        self.label_cost += float(cabs[lab].sum())
        self.label_rows += int((lab & df["billed_cost"].notna()).sum())
        self.abs_cost += float(cabs.sum())
        need = {"quantity", "unit_price", "billed_cost"}
        if need <= set(df.columns):
            self.line_math = True
            sub = df.dropna(subset=list(need))
            sub = sub[(sub["quantity"] != 0) & (sub["unit_price"] != 0)]
            if "charge_type" in sub:                   # a Savings Plan negation prints quantity x the on-demand
                sub = sub[sub["charge_type"].ne("Negation")]   # rate but its amount cancels the covered usage
            calc = sub["quantity"] * sub["unit_price"]
            ok = (calc - sub["billed_cost"]).abs() <= np.maximum(0.011, 0.01 * sub["billed_cost"].abs())
            self.lm_n += len(sub)
            self.lm_ok += int(ok.sum())
            ratio = (sub["billed_cost"] / calc.replace(0, np.nan)).dropna()
            self.ratio_n += len(ratio)
            self.ratio_sum += float(ratio.sum())
            self.ratio_sq += float((ratio * ratio).sum())
        if "charge_start" in df and df["charge_start"].notna().any():
            self.dates = True
            d = pd.to_datetime(df["charge_start"], errors="coerce", utc=True).dt.tz_localize(None)
            self.bad_dates += int((d.notna() & ((d < "2006-01-01")
                                                | (d > pd.Timestamp.now() + pd.Timedelta(days=62)))).sum())
        if "currency" in df:
            self.currencies |= set(df["currency"].dropna().astype(str).str.upper().unique())
        if "charge_type" in df:
            self.charge_types = True
            neg = df["billed_cost"] < 0
            self.negatives += int((neg & ~df["charge_type"].fillna("").isin(
                ["Credit", "Refund", "Adjustment", "Negation"])).sum())
        if "price_match_level" in df:
            self._prices(df)
        if {"resource_id", "charge_start", "charge_end", "quantity", "unit"} <= set(df.columns):
            self._hours(df)
        if sum(c in df for c in ("list_cost", "contracted_cost", "effective_cost")) >= 2:
            self._order(df)

    def _example(self, check: str, df: pd.DataFrame, mask: pd.Series, what) -> None:
        """Up to 5 example rows per check, from the first chunks: never more."""
        got = self.examples.setdefault(check, [])
        if len(got) >= MAX_EXAMPLES or not mask.any():
            return
        for i in mask[mask].index[: MAX_EXAMPLES - len(got)]:
            got.append({"src": str(df.at[i, "_src"]) if "_src" in df else str(i), "values": what(df.loc[i])})

    def _prices(self, df: pd.DataFrame) -> None:
        level = df["price_match_level"].astype(object)
        usage = _usage(df) & _col(df, "provider").astype(str).str.lower().isin(("aws", "azure", "gcp"))
        asked = usage & level.notna() & level.astype(str).ne("") & level.ne("no_catalog")
        self.catalog_lines += int(asked.sum())
        if not asked.any():
            return
        cost = pd.to_numeric(df["billed_cost"], errors="coerce").abs().fillna(0.0)
        priced = asked & level.isin(PRICED)
        notes = _col(df, "price_assumptions").astype(str)
        # catalog_grounding: priced against no_such_product
        nosuch = asked & level.eq("unmatched") & _col(df, "price_missing").eq("no_such_product")
        conflict = priced & _col(df, "price_conflict").notna() & _col(df, "price_conflict").astype(str).ne("")
        for acc, m in ((self.g_priced, priced), (self.g_nosuch, nosuch), (self.g_conflict, conflict)):
            acc[0] += int(m.sum())
            acc[1] += float(cost[m].sum())
        if conflict.any():
            for v, c in cost[conflict].groupby(_col(df, "price_conflict")[conflict].astype(str)).sum().items():
                if v in self.g_values or len(self.g_values) < 50:
                    self.g_values[v] = self.g_values.get(v, 0.0) + float(c)
            self._example("catalog_grounding", df, conflict, lambda r: str(r["price_conflict"]))
        if nosuch.any():
            label_cols = [c for c in df.columns if c in ("instance_type", "region", "usage_type", "sku_id")]
            for v, c in cost[nosuch].groupby(_product_label(df.loc[nosuch, label_cols])).sum().items():
                if v in self.g_values or len(self.g_values) < 50:
                    self.g_values[v] = self.g_values.get(v, 0.0) + float(c)
            self._example("catalog_grounding", df, nosuch, lambda r: _product_label(r.to_frame().T).iloc[0])
        # printed_list_price: the file's own list unit price against the catalog's (exact matches only)
        printed = pd.to_numeric(_col(df, "list_unit_price"), errors="coerce")
        cat = pd.to_numeric(_col(df, "catalog_unit_price"), errors="coerce")
        qty = pd.to_numeric(_col(df, "quantity"), errors="coerce")
        own = printed.notna() & ~_flag(df, "_list_unit_price_inferred")
        # dates before the catalog's history count too (its history starts at the first import): a price never
        # changes by a mix-up factor, so only those fail; another difference warns (the price may have changed)
        m = asked & level.eq("exact") & own & (cat > 0) & ~notes.str.contains("tiered")
        if m.any():
            r = printed[m] / cat[m]
            w = (qty[m] * printed[m]).abs().fillna(0.0)
            w = w.where(w > 0, 1e-9)
            self.pl[0] += int(m.sum())
            self.pl[1] += float(w.sum())
            self.pl[2] += float(w[(r - 1).abs() <= 0.01].sum())
            near = _nearest_mixup(r)
            hit = (r / near - 1).abs() <= 0.02
            for f, x in w[hit].groupby(near[hit]).sum().items():
                self.pl_mix[float(f)] = self.pl_mix.get(float(f), 0.0) + float(x)
            off = pd.Series(False, index=df.index)
            off[m] = (r - 1).abs() > 0.01
            self._example("printed_list_price", df, off,
                          lambda row: f"printed {row['list_unit_price']} vs catalog {row['catalog_unit_price']} "
                                      f"per {row.get('unit')}")
        # implied_unit_price: what the line paid per unit against the catalog's price (never the printed list price:
        # printed_list_price compares that one, so each failure points at its own column)
        implied = pd.to_numeric(_col(df, "implied_unit_price"), errors="coerce")
        ratio = implied / cat.where(cat > 0)
        cov = _col(df, "coverage")
        first_tier = ~notes.str.contains("tiered")
        m = priced & cov.isin(("on_demand", "unknown")) & first_tier & ratio.notna()
        if m.any():
            r, w = ratio[m], cost[m]
            self.ip[0] += int(m.sum())
            self.ip[1] += float(w.sum())
            self.ip[2] += float(w[(r >= 0.5) & (r <= 1.05)].sum())
            self.ip[3] += float(w[(r > FAR) | (r < 1 / FAR)].sum())
            near = _nearest_mixup(r)
            hit = (r / near >= 0.5) & (r / near <= 1.05)
            for f, x in w[hit].groupby(near[hit]).sum().items():
                self.ip_mix[float(f)] = self.ip_mix.get(float(f), 0.0) + float(x)
            off = pd.Series(False, index=df.index)
            off[m] = (r < 0.5) | (r > 1.05)
            self._example("implied_unit_price", df, off,
                          lambda row: f"ratio {float(row['implied_unit_price']) / float(row['catalog_unit_price']):.4g}"
                                      f": paid {row.get('billed_cost')} for {row.get('quantity')} {row.get('unit')}, "
                                      f"catalog {row.get('catalog_unit_price')}")
        # price_units: lines whose unit the price's cannot be converted to
        self.pu[0] += float(cost[asked].sum())
        bad = asked & level.eq("unit_mismatch")
        self.pu[1] += float(cost[bad].sum())
        self._example("price_units", df, bad, lambda row: f"unit {row.get('unit')!r}, {row.get('usage_type') or ''}")

    def _hours(self, df: pd.DataFrame) -> None:
        """usage_hours: hourly usage of one resource within its line's window."""
        factor = map_unique(df["unit"], lambda u: (n[1] if (n := unit_of(u)) and n[0] == "hour" else None)
                            if isinstance(u, str) else None)
        start = pd.to_datetime(df["charge_start"], errors="coerce", utc=True)
        end = pd.to_datetime(df["charge_end"], errors="coerce", utc=True)
        hours = (end - start).dt.total_seconds() / 3600
        qty = pd.to_numeric(df["quantity"], errors="coerce")
        # hours counted per core or GPU (GCP core SKUs, Azure vCore meters, Fargate vCPU) are hours times the
        # resource's cores: they may exceed the window
        per_unit = pd.Series(False, index=df.index)
        for c in ("description", "usage_type", "meter_name"):
            per_unit |= map_unique(_col(df, c), lambda v: bool(PER_CORE.search(v)) if isinstance(v, str)
                                   else False).astype(bool)
        m = (_usage(df) & factor.notna() & df["resource_id"].notna() & df["resource_id"].astype(str).ne("")
             & hours.gt(0) & qty.notna() & ~per_unit)
        if not m.any():
            return
        used = qty[m] * factor[m].astype(float)
        over = pd.Series(False, index=df.index)
        over[m] = used > hours[m] * 1.01
        self.uh[0] += int(m.sum())
        self.uh[1] += int(over.sum())
        self._example("usage_hours", df, over, lambda row: f"{row['resource_id']}: {row['quantity']} {row['unit']} "
                                                           f"from {row['charge_start']} to {row['charge_end']}")

    def _order(self, df: pd.DataFrame) -> None:
        """cost_order: list >= contracted >= effective on usage lines that print at least two of them."""
        vals = {}
        for c, flag in (("list_cost", "_list_cost_inferred"), ("contracted_cost", "_contracted_cost_inferred"),
                        ("effective_cost", None)):
            v = pd.to_numeric(_col(df, c), errors="coerce")
            v = v.where(v != 0)                                      # 0: not priced in that column, not swapped
            vals[c] = v.where(~_flag(df, flag)) if flag else v       # a cost pricing filled is not the file's
        frame = pd.DataFrame(vals)
        m = _usage(df) & frame.notna().sum(axis=1).ge(2) & (frame.fillna(0) >= 0).all(axis=1)
        if not m.any():
            return
        f = frame[m]
        bad = pd.Series(False, index=f.index)
        for hi, lo in (("list_cost", "contracted_cost"), ("contracted_cost", "effective_cost"),
                       ("list_cost", "effective_cost")):
            bad |= f[hi].notna() & f[lo].notna() & (f[hi] < f[lo] * 0.99 - 0.005)
        self.co[0] += int(m.sum())
        self.co[1] += int(bad.sum())
        out = pd.Series(False, index=df.index)
        out[bad.index] = bad
        self._example("cost_order", df, out, lambda row: f"list {row.get('list_cost')}, contracted "
                                                         f"{row.get('contracted_cost')}, effective {row.get('effective_cost')}")

    def merge(self, o: RowStats) -> None:
        for f in ("rows", "cost_rows", "total", "non_tax", "label_cost", "label_rows", "abs_cost", "lm_n", "lm_ok", "ratio_n",
                  "ratio_sum", "ratio_sq", "bad_dates", "negatives", "catalog_lines"):
            setattr(self, f, getattr(self, f) + getattr(o, f))
        for f in ("g_priced", "g_nosuch", "g_conflict", "pl", "ip", "pu", "uh", "co"):
            setattr(self, f, [a + b for a, b in zip(getattr(self, f), getattr(o, f), strict=True)])
        for mine, theirs in ((self.pl_mix, o.pl_mix), (self.ip_mix, o.ip_mix)):
            for k, v in theirs.items():
                mine[k] = mine.get(k, 0.0) + v
        for k, v in o.g_values.items():
            if k in self.g_values or len(self.g_values) < 50:
                self.g_values[k] = self.g_values.get(k, 0.0) + v
        for k, v in o.examples.items():
            got = self.examples.setdefault(k, [])
            got.extend(v[: MAX_EXAMPLES - len(got)])
        self.line_math |= o.line_math
        self.dates |= o.dates
        self.charge_types |= o.charge_types
        self.currencies |= o.currencies


# ---------------------------------------------------------------- checks
def validate(ext: Extraction, alt: Extraction | None = None, resolved_share: float | None = None,
             kind: str = "cost_lines", text: str | None = None) -> dict:
    """text: the document's text, for rows an LLM wrote itself (route llm_extract_direct): their instance types and
    regions must be printed there (grounding_values)."""
    stats = RowStats.of(ext.rows)
    if not stats.cost_rows:
        return _summarize([Check("rows", "fail", "No cost rows extracted", -1.0)], 0.0, reconciled=False)
    checks = [Check("rows", "pass", f"{stats.rows} rows", 0.1), _labels(stats), _line_math(stats)]
    tot_check, reconciled = _stated_totals(ext.stated_totals, ext.evidence, stats.total, stats.non_tax, stats.rows)
    checks.append(tot_check)
    checks.append(_coverage(ext.amount_like_rows, ext.source_rows, stats.rows, ext.stated_totals))
    if ext.evidence:
        checks += _tree_checks(ext.evidence, stats.total)
    if alt is not None:
        checks.append(_dual(ext, alt))
    if (cur := _converted(ext)) is not None:
        checks.append(cur)
    checks += _plausibility(stats)
    checks += _resolution(resolved_share)
    checks += price_checks(stats, ext.rows, text)
    checks += [Check(**c) for c in ext.checks]                 # e.g. candidates_agree (pipeline._llm_spec)
    return _summarize(checks, _confidence(checks), reconciled)


def validate_stats(stats: RowStats, stated_totals: list[dict], source_rows: int,
                   resolved_share: float | None = None, unread_rows: int = 0) -> dict:
    """The verdict of a file read in chunks (bigfile.py): the same checks, names, statuses and weights as validate(),
    from the merged statistics. Exports have no tree of sums and no second reading. unread_rows: rows of the file the
    reader could not read (a different number of fields than the header): they can carry money, so the row coverage
    check fails."""
    if not stats.cost_rows:
        return _summarize([Check("rows", "fail", "No cost rows extracted", -1.0)], 0.0, reconciled=False)
    checks = [Check("rows", "pass", f"{stats.rows} rows", 0.1), _labels(stats), _line_math(stats)]
    tot_check, reconciled = _stated_totals(stated_totals, {}, stats.total, stats.non_tax, stats.rows)
    checks.append(tot_check)
    if unread_rows:
        checks.append(Check("row_coverage", "fail", f"{unread_rows:,} rows of the file could not be read (a different "
                            f"number of fields than the header); {source_rows:,} rows were read", -0.25))
    else:
        checks.append(_coverage(None, source_rows, stats.rows, stated_totals))
    checks += _plausibility(stats)
    checks += _resolution(resolved_share)
    checks += price_checks(stats)
    return _summarize(checks, _confidence(checks), reconciled)


def _confidence(checks: list[Check]) -> float:
    return float(np.clip(0.55 + sum(c.weight for c in checks), 0.0, 1.0))


def _resolution(resolved_share: float | None) -> list[Check]:
    if resolved_share is None:
        return []
    st = "pass" if resolved_share >= 0.9 else "warn" if resolved_share >= 0.6 else "fail"
    return [Check("service_resolution", st, f"{resolved_share:.0%} of cost has a resolved service",
                  {"pass": 0.05, "warn": -0.05, "fail": -0.15}[st])]


def _labels(s: RowStats) -> Check:
    """Spend we can't attribute to anything is not useful: require a label on most of the cost. When every line costs
    0 (free Marketplace usage, a month with no charge), on most of the lines."""
    if not s.abs_cost and s.cost_rows:
        share = s.label_rows / s.cost_rows
        if share >= 0.9:
            return Check("labels", "pass", f"Every line costs 0; {share:.0%} of the lines have a service or "
                         "description", 0.0)
        return Check("labels", "fail", f"Every line costs 0, and only {share:.0%} of the lines have a service or "
                     "description label", -0.35)
    share = s.label_cost / s.abs_cost if s.abs_cost else 0.0
    if share >= 0.9:
        return Check("labels", "pass", f"{share:.0%} of cost has a service or description", 0.0)
    return Check("labels", "fail", f"Only {share:.0%} of cost has a service or description label", -0.35)


def _line_math(s: RowStats) -> Check:
    if not s.line_math:
        return Check("line_math", "skip", "No quantity and unit price columns")
    if s.lm_n < 3:
        return Check("line_math", "skip", "Too few lines with quantity and unit price")
    share = s.lm_ok / s.lm_n
    if share >= 0.95:
        return Check("line_math", "pass", f"{share:.0%} of {s.lm_n} lines: qty x price = amount", 0.15)
    if s.ratio_n:
        mean = s.ratio_sum / s.ratio_n
        var = (s.ratio_sq - s.ratio_n * mean * mean) / (s.ratio_n - 1) if s.ratio_n > 1 else float("nan")
        if max(var, 0.0) ** 0.5 / max(1e-9, abs(mean)) < 0.01:
            return Check("line_math", "warn",
                         f"Consistent ratio {mean:.4g} between amount and qty x price "
                         "(unit block size, discount or currency conversion)", 0.0)
    return Check("line_math", "fail", f"Only {share:.0%} of {s.lm_n} lines match qty x price", -0.15)


def _stated_totals(stated_totals: list[dict], evidence: dict, total: float, non_tax: float,
                   n_rows: int) -> tuple[Check, bool]:
    rec = (evidence or {}).get("reconcile") or {}
    if (dup := _duplicates(evidence or {})) is not None:
        return dup, False
    if rec.get("src"):
        return _tree_total(rec, total)
    cands = []
    for t in stated_totals:
        amt = parse_number(t.get("amount"))
        if amt is not None and t.get("kind") not in ("subtotal", "llm_claim"):   # an LLM's own sum proves nothing
            cands.append((t, amt))
    if not cands:
        subs = [parse_number(t.get("amount")) for t in stated_totals if t.get("kind") == "subtotal"]
        subs = [x for x in subs if x is not None]
        if subs and abs(sum(subs) - non_tax) <= max(0.05, 0.0005 * abs(non_tax)):
            return Check("stated_total", "pass", f"{len(subs)} stated subtotals sum to the extracted lines "
                         f"({sum(subs):,.2f}); no grand total on the document", 0.25), True
        detail = "No stated total found" + (f" ({len(subs)} subtotals seen, sum {sum(subs):,.2f})" if subs else "")
        return Check("stated_total", "skip", detail + "; marked unreconciled", 0.0), False
    # n lines printed to the cent can miss their printed total by half a cent each at most
    tol = min(max(0.05, 0.0005 * abs(total)), bound(n_rows))
    tax_rows = [parse_number(t.get("amount")) or 0 for t in stated_totals if re.search(r"tax", t.get("label", ""), re.I)]
    targets = {"sum of lines": total, "sum before tax": non_tax, "sum plus stated tax": total + sum(tax_rows)}
    for t, amt in sorted(cands, key=lambda x: -abs(x[1])):
        for name, val in targets.items():
            if abs(amt - val) <= tol:
                return Check("stated_total", "pass", f"Stated {t['label']!r} {amt:,.2f} = {name} {val:,.2f}", 0.35), True
    t, amt = max(cands, key=lambda x: abs(x[1]))
    return Check("stated_total", "fail", f"Stated {t['label']!r} {amt:,.2f} vs extracted {total:,.2f} "
                 f"(diff {total - amt:,.2f})", -0.35), False


RATE = re.compile(r"1\s*([A-Z]{3})\s*=\s*(\d+(?:\.\d+)?)\s*([A-Z]{3})")


def _converted(ext: Extraction) -> Check | None:
    """A bill that prints an exchange rate and amounts in both currencies: each amount x the rate is the other
    printed amount, to the cent."""
    rate = next((RATE.search(str(f.get("value"))) for k, f in (ext.fields or {}).items()
                 if RATE.search(str(f.get("value")))), None)
    rows = (ext.evidence or {}).get("converted") or []
    if rate is None or not rows:
        return None
    src, r, dst = rate.group(1), float(rate.group(2)), rate.group(3)
    test = [x for x in rows if x["other_currency"] == dst]
    bad = [x for x in test if abs(round(x["amount"] * r, 2) - x["other"]) > 0.0101]
    if not test:
        return None
    if bad:
        where = "; ".join(f"{x['label'][:40]} {x['amount']:,.2f} {src} x {r} = {x['amount'] * r:,.2f}, printed "
                          f"{x['other']:,.2f} {dst} ({x['src']})" for x in bad[:3])
        return Check("currency", "fail", f"{len(bad)} of {len(test)} amounts do not convert at the printed rate: "
                     f"{where}", -0.2)
    return Check("currency", "pass", f"{len(test)} printed {dst} amounts = {src} amounts x {r} (the printed rate), "
                 "to the cent", 0.05)


def _duplicates(ev: dict) -> Check | None:
    dup = ev.get("duplicate_lines") or []
    if not dup:
        return None
    where = "; ".join(f"{d['label'][:40]} {d['amount']:,.2f} ({d['src']})" for d in dup[:3])
    return Check("stated_total", "fail", f"{len(dup)} printed row(s) are lines more than once: {where}", -0.35)


def _tree_total(rec: dict, total: float) -> tuple[Check, bool]:
    """The tree's proof (tree._reconcile): printed total = lines + the rounding at each printed total between them,
    to the cent, and no rounding larger than its parts allow."""
    amount, rounding = rec["amount"], rec.get("rounding") or 0.0
    unexplained = round(amount - total - rounding, 2)
    head = f"Printed {rec['label'][:60]!r} {amount:,.2f}"

    def brief(xs: list[dict]) -> str:
        return "; ".join(f"{x['label'][:40]} {x['amount']:,.2f} vs its {x['parts']} part(s) {x['children']:,.2f} "
                         f"({x['src']})" for x in xs[:3])
    over = rec.get("over_bound") or []
    if over:
        return Check("stated_total", "fail", f"{head}: {len(over)} printed total(s) differ from their parts by more "
                     f"than rounding: {brief(over)}", -0.35), False
    if abs(unexplained) >= 0.005:
        return Check("stated_total", "fail", f"{head} vs lines {total:,.2f} + rounding {rounding:,.2f}: "
                     f"{unexplained:,.2f} is not explained (counted twice or not read)", -0.35), False
    detail = f"{head} = lines {total:,.2f}" + (f" + rounding {rounding:,.2f} ({brief(rec.get('rounding_at') or [])})"
                                               if abs(rounding) >= 0.005 else ", to the cent")
    return Check("stated_total", "pass", detail, 0.35), True


def _coverage(amount_like_rows: int | None, source_rows: int, n_rows: int, stated_totals: list[dict]) -> Check:
    if amount_like_rows:                                        # grid tables
        accounted = n_rows + len([t for t in stated_totals
                                  if t.get("kind") in ("subtotal", "total", "total_row", "end_anchor")])
        share = min(1.0, accounted / amount_like_rows)
        st = "pass" if share >= 0.98 else "warn" if share >= 0.9 else "fail"
        return Check("row_coverage", st, f"{accounted} of {amount_like_rows} rows with amounts accounted for",
                     {"pass": 0.1, "warn": -0.05, "fail": -0.25}[st])
    if source_rows:
        moved = len([t for t in stated_totals if t.get("kind") == "total_row"])
        share = min(1.0, (n_rows + moved) / source_rows)
        st = "pass" if share >= 0.9 else "warn" if share >= 0.5 else "fail"
        return Check("row_coverage", st, f"{n_rows + moved} of {source_rows} source rows accounted for",
                     {"pass": 0.05, "warn": -0.05, "fail": -0.2}[st])
    return Check("row_coverage", "skip", "No source row count")


def _dual(ext: Extraction, alt: Extraction) -> Check:
    a = float(ext.rows["billed_cost"].sum())
    b = float(alt.rows["billed_cost"].sum()) if not alt.rows.empty and "billed_cost" in alt.rows else 0.0
    if abs(a - b) <= max(0.05, 0.001 * abs(a)) and abs(len(ext.rows) - len(alt.rows)) <= max(1, 0.02 * len(ext.rows)):
        return Check("dual_extraction", "pass", f"Independent extraction agrees ({b:,.2f}, {len(alt.rows)} rows)", 0.15)
    return Check("dual_extraction", "warn", f"Independent extraction differs: {b:,.2f} / {len(alt.rows)} rows "
                 f"vs {a:,.2f} / {len(ext.rows)} rows", -0.1)


def _tree_checks(ev: dict, total: float) -> list[Check]:
    """The tree of sums' evidence (tree.py): groups that add up or not, amounts nothing explains, views that agree."""
    out = []
    failed, ok = ev.get("subtotals_failed") or [], ev.get("subtotals_ok") or 0
    scale = max(abs(total), 1.0)
    if failed:
        gap = ev.get("gap_amount") or 0.0
        where = "; ".join(f"{f['label'][:40]} {f['amount']:,.2f} vs {f.get('children', 0):,.2f} ({f['src']})"
                          for f in failed[:3])
        st = "fail" if gap > 0.005 * scale else "warn"
        out.append(Check("subtotals", st, f"{len(failed)} group(s) do not add up (difference {gap:,.2f}): {where}",
                         -0.25 if st == "fail" else -0.05))
    elif ok:
        out.append(Check("subtotals", "pass", f"{ok} printed group total(s) equal the sum of their lines", 0.2))
    n, amt = ev.get("unexplained_count") or 0, ev.get("unexplained_amount") or 0.0
    if n == 0:
        out.append(Check("amounts_explained", "pass", f"Every one of {ev.get('records', 0)} amounts is a line, a total "
                         "or a repeat of one", 0.1))
    else:
        where = "; ".join(f"{u['label'][:40]} {u['amount']:,.2f} ({u['src']})" for u in (ev.get("unexplained") or [])[:3])
        st = "warn" if amt <= 0.005 * scale else "fail"
        out.append(Check("amounts_explained", st, f"{n} amount(s) ({amt:,.2f}) are not lines, totals or repeats: "
                         f"{where}", -0.05 if st == "warn" else -0.25))
    if ev.get("views_agree"):
        out.append(Check("views_agree", "pass", f"{len(ev['views_agree'])} total(s) are broken down two ways that "
                         "agree", 0.05))
    return out


def _plausibility(s: RowStats) -> list[Check]:
    out = []
    if s.dates and s.bad_dates:
        out.append(Check("dates", "warn", f"{s.bad_dates} dates out of range", -0.05))
    if len(s.currencies) > 1:
        out.append(Check("currency", "warn", f"Multiple currencies: {sorted(s.currencies)[:5]}", -0.05))
    if s.charge_types and s.negatives > max(2, 0.05 * s.rows):
        out.append(Check("negatives", "warn", f"{s.negatives} negative lines not marked as credits", -0.05))
    return out


# ---------------------------------------------------------------- the price checks
PER_CORE = re.compile(r"(?<![a-z])(?:v?cpus?|v?cores?|gpus?)(?![a-z])", re.I)   # usage_hours: per core or GPU
PRICE_CHECKS = ("catalog_grounding", "printed_list_price", "implied_unit_price", "price_units", "usage_hours",
                "cost_order", "grounding_values")


def price_checks(s: RowStats, rows: pd.DataFrame | None = None, text: str | None = None) -> list[Check]:
    """The catalog's list prices as an independent reference: a parse that maps real products, pays sane prices and
    agrees with the printed list prices is almost certainly right. Every check skips (weight 0) unless the catalog was
    asked for some line of the file (no catalog, or no line of a provider whose prices are loaded). Catalog-filled values never
    check themselves: printed_list_price reads printed prices only, implied_unit_price the file's costs."""
    if not s.catalog_lines:
        why = "No line was priced against the catalog (no catalog, or no AWS, Azure or GCP usage lines it has prices for)"
        return [Check(n, "skip", why) for n in PRICE_CHECKS]
    return [_grounding(s), _printed_list(s), _implied(s), _units(s), _hours(s), _order(s), _grounded(rows, text)]


def _examples(s: RowStats, check: str) -> str:
    got = s.examples.get(check) or []
    return ("; rows: " + "; ".join(f"{e['src']} ({e['values']})" for e in got)) if got else ""


def _mixup(mix: dict, base: float, share: float) -> float | None:
    """The mix-up factor carrying more than share of base, or None."""
    if not mix or base <= 0:
        return None
    f, w = max(mix.items(), key=lambda kv: kv[1])
    return f if w > share * base else None


def _times(f: float) -> str:
    return f"×{f:.4g}" if f >= 1 else f"×1/{1 / f:.4g}"


def _grounding(s: RowStats) -> Check:
    """catalog_grounding: of the AWS and Azure usage cost whose service the catalog has, the share whose product no catalog
    row has (no_such_product), or whose exact match prints another region, instance type or OS than the product
    (price_conflict): a value read from a wrong column. At least MIN_GROUNDED lines, else skip."""
    lines = s.g_priced[0] + s.g_nosuch[0]
    if lines < MIN_GROUNDED:
        return Check("catalog_grounding", "skip", f"{lines} line(s) tested against the catalog's products: too few")
    use_cost = (s.g_priced[1] + s.g_nosuch[1]) > 0
    i = 1 if use_cost else 0
    base = s.g_priced[i] + s.g_nosuch[i]
    bad = s.g_nosuch[i] + s.g_conflict[i]
    share = bad / base
    what = "cost" if use_cost else "lines"
    if share < 0.02:
        return Check("catalog_grounding", "pass", f"{1 - share:.0%} of the priced {what} name products the catalog "
                     f"has, as printed ({s.g_priced[0]:,} lines)", 0.10)
    top = ", ".join(v for v, _ in sorted(s.g_values.items(), key=lambda kv: -kv[1])[:5])
    parts = [f"{s.g_nosuch[0]:,} name products no catalog row has" if s.g_nosuch[0] else "",
             f"{s.g_conflict[0]:,} print another region, type or OS than the product their rate code, SKU or usage type "
             "matched"
             if s.g_conflict[0] else ""]
    detail = (f"{share:.0%} of the {what} does not fit the catalog ({'; '.join(p for p in parts if p)}): {top}"
              + _examples(s, "catalog_grounding"))
    if share <= 0.10:
        return Check("catalog_grounding", "warn", detail, 0.0)
    return Check("catalog_grounding", "fail", detail + " (an instance type, region or usage type may be read from "
                 "a wrong column)", -0.15)


def _printed_list(s: RowStats) -> Check:
    """printed_list_price: the file's own list unit prices agree with the catalog's (exact matches, first tiers)."""
    n, w, agree = s.pl
    if not n:
        return Check("printed_list_price", "skip", "The file prints no list unit price on an exactly matched line")
    share = agree / w if w else 0.0
    if share >= 0.95:
        return Check("printed_list_price", "pass", f"{share:.0%} of {n:,} printed list prices agree with the catalog "
                     "within 1%", 0.10)
    if (f := _mixup(s.pl_mix, w - agree, 0.5)) is not None:
        return Check("printed_list_price", "fail", f"printed list prices disagree with the catalog by {_times(f)}: "
                     f"unit or column mix-up ({1 - share:.0%} of {n:,} lines)" + _examples(s, "printed_list_price"),
                     -0.20)
    return Check("printed_list_price", "warn", f"{1 - share:.0%} of {n:,} printed list prices differ from the "
                 "catalog by more than 1%" + _examples(s, "printed_list_price"), -0.05)


def _implied(s: RowStats) -> Check:
    """implied_unit_price: what on-demand lines paid per unit is 0.5 to 1.05 times the list price; a cluster near
    a mix-up ratio (×1000, ×3600, ×24, ×730) is a wrong unit or column."""
    n, w, ok, far = s.ip
    if not n or not w:
        return Check("implied_unit_price", "skip", "No priced on-demand line with a cost and a quantity")
    share = ok / w
    if share >= 0.90:
        return Check("implied_unit_price", "pass", f"{share:.0%} of the on-demand cost ({n:,} lines) is paid at 0.5 "
                     "to 1.05 times the catalog's list price", 0.10)
    if (f := _mixup(s.ip_mix, w, 0.05)) is not None:
        return Check("implied_unit_price", "fail", f"unit or column mix-up: ratio ≈ {_times(f)} on "
                     f"{s.ip_mix[f] / w:.0%} of the on-demand cost" + _examples(s, "implied_unit_price"), -0.20)
    if far > 0.05 * w:
        return Check("implied_unit_price", "fail", f"column mix-up: {far / w:.0%} of the on-demand cost is paid more "
                     f"than {FAR:g} times above or below the list price (quantity, unit price or cost read from the "
                     "wrong column)" + _examples(s, "implied_unit_price"), -0.20)
    return Check("implied_unit_price", "warn", f"{1 - share:.0%} of the on-demand cost is paid outside 0.5 to 1.05 "
                 "times the list price (a large negotiated discount explains it; see pricing.inferred_discount)"
                 + _examples(s, "implied_unit_price"), 0.0)


def _units(s: RowStats) -> Check:
    cost, bad = s.pu
    if not cost:
        return Check("price_units", "skip", "No priced cost")
    share = bad / cost
    if share > 0.05:
        return Check("price_units", "warn", f"{share:.0%} of the priced usage cost has a unit the list price's cannot "
                     "be converted to: the unit column may be wrong" + _examples(s, "price_units"), -0.05)
    return Check("price_units", "pass", f"{1 - share:.0%} of the priced usage cost has a unit the list price's converts "
                 "to", 0.0)


def _hours(s: RowStats) -> Check:
    n, over = s.uh
    if not n:
        return Check("usage_hours", "skip", "No hourly line with a resource and its start and end")
    if over:
        return Check("usage_hours", "fail", f"{over:,} of {n:,} hourly lines use one resource for more hours than "
                     "their window has" + _examples(s, "usage_hours"), -0.10)
    return Check("usage_hours", "pass", f"{n:,} hourly lines fit their window", 0.0)


def _order(s: RowStats) -> Check:
    n, bad = s.co
    if not n:
        return Check("cost_order", "skip", "No usage line prints two of list, contracted and effective cost")
    if bad > 0.02 * n:
        return Check("cost_order", "warn", f"{bad:,} of {n:,} lines are not list >= contracted >= effective: cost "
                     "columns may be swapped" + _examples(s, "cost_order"), -0.05)
    return Check("cost_order", "pass", f"{n - bad:,} of {n:,} lines have list >= contracted >= effective", 0.0)


def _grounded(rows: pd.DataFrame | None, text: str | None) -> Check:
    """grounding_values: rows an LLM wrote itself name only instance types and regions the document prints."""
    if rows is None or text is None:
        return Check("grounding_values", "skip", "Not rows an LLM wrote itself")
    low = text.lower()
    missing = []
    for col in ("instance_type", "region"):
        if col not in rows:
            continue
        shown = rows["region_name"].where(rows["region_name"].notna(), rows[col]) \
            if col == "region" and "region_name" in rows else rows[col]
        for v in shown.dropna().astype(str).unique():
            if v.strip() and v.strip().lower() not in low:
                missing.append(f"{col} {v}")
    if missing:
        return Check("grounding_values", "fail", f"{len(missing)} value(s) are not printed in the document: "
                     + ", ".join(missing[:8]), -0.15)
    return Check("grounding_values", "pass", "Every instance type and region is printed in the document", 0.0)


CRITICAL = ("rows", "stated_total", "row_coverage", "labels", "subtotals", "amounts_explained")
INTERNAL = ("subtotals", "line_math", "row_coverage", "dual_extraction", "amounts_explained", "views_agree",
            "catalog_grounding", "printed_list_price", "implied_unit_price", "usage_hours")


def completeness(checks: list[Check], reconciled: bool) -> str:
    """How well the result is proved: reconciled (a printed number proves it), consistent (nothing printed to test,
    the internal checks pass), contradicted (a check failed), unchecked (nothing could be tested)."""
    if any(c.status == "fail" and c.name in CRITICAL + ("line_math",) for c in checks):
        return "contradicted"
    if reconciled:
        return "reconciled"
    if any(c.status == "pass" and c.name in INTERNAL for c in checks):
        return "consistent"
    return "unchecked"


REVIEW_BELOW = 0.6             # a result less confident than this needs review


def _summarize(checks: list[Check], conf: float, reconciled: bool) -> dict:
    critical_fail = any(c.status == "fail" and c.name in CRITICAL for c in checks)
    if reconciled and conf >= 0.7 and not critical_fail:
        status = "reconciled"
    elif not critical_fail and conf >= REVIEW_BELOW:
        status = "unreconciled"
    else:
        status = "needs_review"
    return {"status": status, "confidence": round(conf, 2), "checks": [asdict(c) for c in checks],
            "completeness": completeness(checks, reconciled),
            "feedback": [f"{c.name}: {c.detail}" for c in checks if c.status in ("fail", "warn")]}
