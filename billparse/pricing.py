"""Price matching: every AWS usage line looked up in the price catalog (prices/, task 05) at the public on-demand list
price, with how it matched and how sure that is. Facts, never conclusions: no savings estimate, no commitment rate,
and coverage and effective cost are never touched.

    stats = match_prices(rows, get_catalog(), PriceDefaults())      # adds the price columns to rows in place
    summary = stats.summary(infer_discount(stats))                  # the document's `pricing`

Each line's level (price_match_level):
  exact          rate code, SKU or usage type + operation, one price
  attributes     instance type, region, OS, ... one price, nothing assumed
  assumed        as attributes, with an assumption (price_assumptions): the line's words (NA software, Shared
                 tenancy for an EC2 invoice line) or a default the request gave
  ambiguous      several prices fit; price_missing names the empty key columns that would tell them apart
  unmatched      no price; price_missing names the missing key columns, or why: service_not_loaded (the catalog
                 has no prices for the service: a catalog gap), no_such_product (no product has this SKU, usage type
                 or instance type in this region: likely read from a wrong column), else catalog_row
  unit_mismatch  a price, in a unit the line's unit cannot be converted to
  no_catalog     an Azure or GCP line: the catalog has no prices for them yet
The catalog is called once per file (a large file: once per chunk, for the keys the KeyCache does not have) with the
distinct keys, never per line. A pricing error never fails the parse: the lines stay unpriced, with a warning.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from prices import normalize as nz
from prices.units import normalize as unit_of

from .attributes import _ec2_instance
from .spec import HOW
from .transforms import map_unique

if TYPE_CHECKING:
    from prices.store import PriceCatalog

log = logging.getLogger(__name__)

LEVELS = ["exact", "attributes", "assumed", "ambiguous", "unmatched", "unit_mismatch", "no_catalog"]
MATCHED = ("exact", "attributes", "assumed")
EXACT_BY = ("rate_code", "sku", "usage")              # the lookups that name the product itself
ATTRIBUTES = ["operating_system", "tenancy", "license_model", "pre_installed_sw", "database_engine",
              "deployment_option"]                    # the columns that tell an instance type's prices apart
KEY_COLUMNS = ["rate_code", "sku", "service_code", "usage_type", "operation", "region", "instance_type", *ATTRIBUTES]
PRICE_COLUMNS = ["price_match_level", "price_missing", "price_assumptions", "implied_unit_price", "price_ratio",
                 "catalog_sku_id", "catalog_unit_price", "price_conflict", "_list_cost_inferred", "_contracted_cost_inferred", "_list_unit_price_inferred"]
FLAGS = ("_list_cost_inferred", "_contracted_cost_inferred", "_list_unit_price_inferred")   # what pricing filled
NO_CATALOG = ("azure", "gcp")
AWS_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9]+$")      # a CUR product code: AmazonEC2, AWSLambda, awskms
# R2: a steady negotiated discount
R2_MIN_LINES, R2_MIN_SERVICES, R2_TOLERANCE, R2_LOW, R2_HIGH = 20, 3, 0.01, 0.5, 0.99
BUCKET = 0.002                                        # ratio statistics kept in buckets: mergeable across chunks
R3_RATIO = 0.5


@dataclass
class PriceDefaults:
    """Explicit, per request; all None = assume nothing. A default fills an attribute only where the line and its
    words leave it empty, and is recorded in price_assumptions ("operating_system=Linux (request)")."""
    operating_system: str | None = None    # "Linux"
    tenancy: str | None = None             # "Shared"
    pre_installed_sw: str | None = None    # "NA"
    license_model: str | None = None
    region: str | None = None              # "us-east-1": invoices that print no region (resellers)

    def given(self) -> dict[str, str]:
        return {k: v for k, v in asdict(self).items() if v}


class KeyCache:
    """Lookup results by (key, date), shared by the chunks of one large file: a key is looked up once per file."""

    def __init__(self) -> None:
        self.results: dict[tuple[str, str], dict | None] = {}
        self.exists: dict[str, bool] = {}                 # key text -> some product has its SKU, usage or type

    def __contains__(self, k: tuple[str, str]) -> bool:
        return k in self.results

    def get(self, k: tuple[str, str]) -> dict | None:
        return self.results.get(k)

    def put(self, k: tuple[str, str], v: dict | None) -> None:
        self.results[k] = v


@dataclass
class PriceStats:
    """Counters for the document's pricing summary, from whole rows or chunk by chunk (merge)."""
    catalog_version: str | None = None
    defaults: dict = field(default_factory=dict)
    levels: dict = field(default_factory=dict)        # level -> [lines, |billed cost|]
    missing: dict = field(default_factory=dict)       # key column -> [lines, |billed cost|] (not matched lines)
    ratios: dict = field(default_factory=dict)        # R2: ratio bucket -> [lines, sum of ratios, {services}]
    below_list: int = 0                               # R3: coverage unknown, far below list price
    warnings: list = field(default_factory=list)
    lookups: int = 0                                  # keys sent to the catalog
    undated_as_of: str | None = None                  # the date of lines without one (period start, or the day priced)

    def note(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)

    def merge(self, other: PriceStats) -> None:
        self.catalog_version = self.catalog_version or other.catalog_version
        self.defaults = self.defaults or other.defaults
        for mine, theirs in ((self.levels, other.levels), (self.missing, other.missing)):
            for k, (n, c) in theirs.items():
                got = mine.setdefault(k, [0, 0.0])
                got[0] += n
                got[1] += c
        for b, (n, s, svc) in other.ratios.items():
            got = self.ratios.setdefault(b, [0, 0.0, set()])
            got[0] += n
            got[1] += s
            got[2] |= svc
        self.below_list += other.below_list
        self.lookups += other.lookups
        self.undated_as_of = self.undated_as_of or other.undated_as_of
        for w in other.warnings:
            self.note(w)

    def summary(self, inferred_discount: float | None = None) -> dict:
        """The document's `pricing`: match rates by cost (and by lines) of priced AWS usage, the lines and cost per
        level, the most common missing key columns, the inferred discount (an estimate) and warnings."""
        priced = {k: v for k, v in self.levels.items() if k != "no_catalog"}
        cost = sum(c for _, c in priced.values())
        lines = sum(n for n, _ in priced.values())

        def share(levels: tuple[str, ...], i: int, total: float) -> float | None:
            return round(sum(priced.get(k, [0, 0.0])[i] for k in levels) / total, 4) if total else None
        warnings = list(self.warnings)
        if self.below_list:
            warnings.append(f"{self.below_list:,} lines cost far below list price and the file does not say why "
                            "(possible commitment coverage)")
        missing = sorted(self.missing.items(), key=lambda kv: (-kv[1][1], -kv[1][0]))[:10]
        return {
            "catalog_version": self.catalog_version,
            "defaults": self.defaults,
            "undated_as_of": self.undated_as_of,
            "match_rate": {"sure": share(("exact", "attributes"), 1, cost), "assumed": share(("assumed",), 1, cost),
                           "sure_lines": share(("exact", "attributes"), 0, lines),
                           "assumed_lines": share(("assumed",), 0, lines)},
            "levels": {k: {"lines": n, "cost": round(c, 2)} for k, (n, c) in sorted(
                self.levels.items(), key=lambda kv: LEVELS.index(kv[0]) if kv[0] in LEVELS else 99)},
            "missing": [{"field": k, "lines": n, "cost": round(c, 2)} for k, (n, c) in missing],
            "inferred_discount": None if inferred_discount is None else {
                "value": round(inferred_discount, 4), "estimate": True,
                "note": "A steady ratio of the billed to the list price on on-demand lines (rule R2): an estimate"},
            "warnings": warnings,
        }


def _blank(rows: pd.DataFrame) -> None:
    """The price columns, empty (object: stored rows read back as text must take numbers and flags), and the costs
    pricing may fill as numbers. Rows priced before (an attempt, then the final pass) lose what that pricing filled,
    so a catalog value is never taken for a printed one."""
    for flag, col in (("_list_cost_inferred", "list_cost"), ("_contracted_cost_inferred", "contracted_cost"),
                      ("_list_unit_price_inferred", "list_unit_price")):
        if flag in rows and col in rows:
            rows[col] = rows[col].astype(object)
            rows.loc[flagged(rows[flag]), col] = None
    for c in PRICE_COLUMNS:
        rows[c] = False if c in FLAGS else None
        rows[c] = rows[c].astype(object)
    for c in ("list_unit_price", "list_cost", "contracted_cost"):
        if c in rows:
            rows[c] = pd.to_numeric(rows[c], errors="coerce")


def _col(rows: pd.DataFrame, name: str) -> pd.Series:
    return rows[name] if name in rows else pd.Series(None, index=rows.index, dtype=object)


def _text(s: pd.Series) -> pd.Series:
    """Text with empty values as None (the parsed columns hold None, NaN or "")."""
    s = s.astype(object)
    return s.where(s.notna() & s.astype(str).str.strip().ne(""), None)


def match_prices(rows: pd.DataFrame, catalog: PriceCatalog | None, defaults: PriceDefaults | None = None,
                 cache: KeyCache | None = None, period_start: str | None = None) -> PriceStats:
    """Adds the price columns to rows in place; returns counters for the document summary (mergeable across
    chunks). period_start: the document's printed billing period start, the date of lines that have none."""
    defaults = defaults or PriceDefaults()
    stats = PriceStats(defaults=defaults.given())
    if rows.empty:
        return stats
    _blank(rows)
    ct = _col(rows, "charge_type")
    usage = ct.isna() | ct.eq("Usage")
    provider = map_unique(_col(rows, "provider"), lambda v: v.lower() if isinstance(v, str) else None)
    cost = pd.to_numeric(_col(rows, "billed_cost"), errors="coerce").fillna(0.0).abs()
    other = usage & provider.isin(NO_CATALOG)
    if other.any():
        rows.loc[other, "price_match_level"] = "no_catalog"
        stats.levels["no_catalog"] = [int(other.sum()), float(cost[other].sum())]
    qty = pd.to_numeric(_col(rows, "quantity"), errors="coerce")
    priced = usage & provider.eq("aws") & qty.notna()
    if not priced.any():
        return stats
    try:
        version = catalog.version if catalog is not None else None
    except Exception as e:                       # noqa: BLE001  a database error must not fail the parse
        stats.note(f"The price catalog could not be read ({type(e).__name__}); the lines are not priced")
        return stats
    if version is None:
        stats.note("No price catalog is loaded (make prices); the lines are not priced")
        return stats
    stats.catalog_version = version
    try:
        _price(rows, priced, qty, cost, catalog, defaults, cache or KeyCache(), stats, period_start)
    except Exception as e:                       # noqa: BLE001  the parse never fails because of pricing
        log.exception("price matching failed")
        for c in PRICE_COLUMNS:
            rows.loc[priced, c] = False if c in FLAGS else None
        stats.levels = {k: v for k, v in stats.levels.items() if k == "no_catalog"}
        stats.missing, stats.ratios, stats.below_list = {}, {}, 0
        stats.note(f"Price matching failed ({type(e).__name__}: {e}); the lines are not priced")
    return stats


def _keys(p: pd.DataFrame, defaults: PriceDefaults) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """(catalog keys, assumptions as text, whether an attribute was assumed) of the priced lines."""
    keys = pd.DataFrame(index=p.index)
    keys["rate_code"] = _text(_col(p, "sku_price_id"))
    keys["sku"] = _text(_col(p, "sku_id"))
    code = _text(_col(p, "product_code"))
    code = code.where(map_unique(code, lambda v: bool(AWS_CODE.match(v)) if isinstance(v, str) else False)
                      .astype(bool), None)
    keys["service_code"] = code.where(code.notna(), _text(_col(p, "service_code")))
    for c in ["usage_type", "operation", "region", "instance_type", *ATTRIBUTES]:
        keys[c] = _text(_col(p, c))
    notes: list[pd.Series] = []
    # the line's own words: an EC2 line with no operation and no SKU (an invoice) that names an OS and no SQL Server
    # runs no software; one that does not say Dedicated or Host is Shared (the parser fills them when it says so)
    ec2 = (map_unique(keys["instance_type"], _ec2_instance).astype(bool) & keys["operation"].isna()
           & keys["sku"].isna() & keys["rate_code"].isna())
    def assume(m: pd.Series, col: str, value: str, why: str) -> None:
        keys.loc[m, col] = value
        notes.append(pd.Series(np.where(m, f"{col}={value} ({why})", ""), index=p.index))
    assume(ec2 & keys["tenancy"].isna(), "tenancy", "Shared", "text")
    # the request's defaults: lines identified by their attributes only
    open_ = keys["instance_type"].notna() & keys[["rate_code", "sku", "usage_type"]].isna().all(axis=1)
    for col, value in defaults.given().items():
        assume(open_ & keys[col].isna(), col, value, "request")
    # an OS (named, or the request's) and no SQL Server in the words: no pre-installed software
    assume(ec2 & keys["pre_installed_sw"].isna() & keys["operating_system"].notna(), "pre_installed_sw", "NA", "text")
    text = pd.Series("", index=p.index)
    for n in notes:
        text = text.str.cat(n, sep="; ").str.strip("; ")
    return keys, text, text.ne("")


def _dates(p: pd.DataFrame, period_start: str | None, stats: PriceStats) -> pd.Series:
    """The date each line is priced as of: its start, else the printed billing period, else today (a warning; the
    summary keeps the date, so a reprice uses the same one). Converted once per distinct value."""
    codes, uniques = pd.factorize(_col(p, "charge_start"), use_na_sentinel=True)
    d = pd.to_datetime(pd.Series(uniques, dtype=object), errors="coerce", utc=True)
    text = d.dt.strftime("%Y-%m-%d").astype(object).where(d.notna(), None).to_numpy()
    out = pd.Series(np.where(codes >= 0, text[np.maximum(codes, 0)] if len(text) else None, None), index=p.index,
                    dtype=object)
    if out.isna().any():
        fallback = period_start
        if fallback is None:
            fallback = dt.date.today().isoformat()
            stats.note(f"{int(out.isna().sum()):,} priced lines have no date and the file prints no billing period: "
                       f"priced as of today ({fallback})")
        stats.undated_as_of = fallback
        out = out.fillna(fallback)
    return out


def _lookup(catalog: PriceCatalog, pairs: list[tuple[str, str]], key_rows: dict[str, dict], cache: KeyCache,
            stats: PriceStats) -> None:
    """Look up the (key, date) pairs the cache does not have, in one call."""
    todo = [k for k in dict.fromkeys(pairs) if k not in cache]
    if not todo:
        return
    frame = pd.DataFrame([{**key_rows[k], "as_of": d, "key_id": i, "term_type": "on_demand"}
                          for i, (k, d) in enumerate(todo)])
    stats.lookups += len(todo)
    found = catalog.lookup(frame)
    got = {} if found.empty else {int(r["key_id"]): r for r in found.to_dict("records")}
    for i, k in enumerate(todo):
        cache.put(k, got.get(i))


def _same(a: dict | None, b: dict | None) -> bool:
    if a is None or b is None:
        return a is b
    return (a.get("rate_id"), a.get("candidates"), a.get("unit_price")) == (b.get("rate_id"), b.get("candidates"),
                                                                         b.get("unit_price"))


SOURCE_COLUMNS = ["sku_price_id", "sku_id", "product_code", "service_code", "usage_type", "operation", "region",
                  "instance_type", *ATTRIBUTES,       # the line columns a key is made of, and how the identifiers
                  HOW + "usage_type", HOW + "instance_type"]     # were made (a value guessed from the words)
GUESSED = ("text", "rule")                            # origins of a value read from a line's words or set by a rule
REGION_CODE = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-\d+$")


def _price(rows: pd.DataFrame, priced: pd.Series, qty: pd.Series, cost: pd.Series, catalog: PriceCatalog,
           defaults: PriceDefaults, cache: KeyCache, stats: PriceStats, period_start: str | None) -> None:
    # the priced lines, with only the columns read here (a copy of every column of a large file costs seconds)
    p = rows.loc[priced, [c for c in rows.columns if c in SOURCE_COLUMNS or c == "charge_start"]]
    # the distinct keys, from the line columns as they are: everything that depends only on the key (cleaning, the
    # line's words, the request's defaults, what is missing) is done once per key and spread back by arrays
    raw = pd.DataFrame({c: _col(p, c) for c in SOURCE_COLUMNS}, index=p.index).astype(object)
    codes = raw.groupby(SOURCE_COLUMNS, dropna=False, sort=False).ngroup().to_numpy()
    first = pd.Series(np.arange(len(raw))).groupby(codes).first().to_numpy()
    uniq = raw.iloc[first].reset_index(drop=True)
    keys, notes, assumed = _keys(uniq, defaults)
    # a key whose identifier (usage type, else instance type) the parser guessed from the words: never no_such_product
    how_usage = uniq[HOW + "usage_type"].isin(GUESSED) & keys["usage_type"].notna()
    how_type = uniq[HOW + "instance_type"].isin(GUESSED) & keys["usage_type"].isna()
    guessed = (keys["sku"].isna() & keys["rate_code"].isna() & (how_usage | how_type)).to_numpy()
    dates = _dates(p, period_start, stats)
    # each key looked up at its first and last date, and at every date in between only when those two differ (a
    # price changed inside the file's period)
    texts = keys[KEY_COLUMNS].astype(object).where(keys[KEY_COLUMNS].notna(), "").astype(str).agg(
        "\x1f".join, axis=1).tolist()
    key_rows = {t: {c: (None if pd.isna(v) else v) for c, v in r.items()}
                for t, r in zip(texts, keys[KEY_COLUMNS].to_dict("records"), strict=True)}
    pairs = pd.DataFrame({"code": codes, "date": dates.to_numpy()})
    span = pairs.groupby("code")["date"].agg(["min", "max"]).reindex(range(len(texts)))
    lo, hi = span["min"].tolist(), span["max"].tolist()
    _lookup(catalog, [(texts[c], d) for c in range(len(texts)) for d in (lo[c], hi[c])], key_rows, cache, stats)
    split = [c for c in range(len(texts)) if not _same(cache.get((texts[c], lo[c])), cache.get((texts[c], hi[c])))]
    per_key = pd.DataFrame([cache.get((texts[c], lo[c])) or {} for c in range(len(texts))])
    for c in ("matched_by", "candidates", "unit_price", "unit", "sku", "effective_from", "tiered", "differs"):
        if c not in per_key:
            per_key[c] = None
    per_key = _any_region(catalog, keys, texts, key_rows, per_key, lo, guessed, cache, stats)
    reasons = _reasons(catalog, keys, texts, per_key["matched_by"].isna().to_numpy(), cache, guessed)
    res = per_key.take(codes).set_axis(p.index)
    if split:
        mask = pairs["code"].isin(split).to_numpy()
        both = pairs[mask].drop_duplicates()
        _lookup(catalog, [(texts[c], d) for c, d in both.itertuples(index=False)], key_rows, cache, stats)
        got = pd.DataFrame([{**(cache.get((texts[c], d)) or {}), "code": c, "date": d}
                            for c, d in both.itertuples(index=False)])
        rows_split = pairs[mask].merge(got, on=["code", "date"], how="left").reindex(columns=per_key.columns)
        res = res.astype(object)
        res.loc[p.index[mask], per_key.columns] = rows_split.to_numpy()
    _apply(rows, p.index, keys, codes, notes.to_numpy()[codes], assumed.to_numpy()[codes], dates, res,
           qty[p.index], cost[p.index], stats, reasons)


def _any_region(catalog: PriceCatalog, keys: pd.DataFrame, texts: list[str], key_rows: dict, per_key: pd.DataFrame,
                lo: list, guessed: np.ndarray, cache: KeyCache, stats: PriceStats) -> pd.DataFrame:
    """A key with a printed identifier (rate code, SKU, or a usage type the parser did not guess) that found nothing
    with its region is looked up again by the identifier alone. When one product has it, the line is that product's
    (matched by its identifier) and its printed region, instance type and OS are compared with the product's
    (price_conflict, _conflicts): a wrong region column must fail catalog_grounding, not look like a catalog gap.
    Each such key once more, in one call."""
    todo = [c for c in range(len(texts))
            if pd.isna(per_key.at[c, "matched_by"]) and pd.notna(keys.at[c, "region"]) and not guessed[c]
            and keys.loc[c, ["rate_code", "sku", "usage_type"]].notna().any()]
    if not todo:
        return per_key
    alt = {c: texts[c] + "\x1f(any region)" for c in todo}
    rows = {alt[c]: dict(key_rows[texts[c]], region=None) for c in todo}
    _lookup(catalog, [(alt[c], lo[c]) for c in todo], rows, cache, stats)
    per_key = per_key.astype(object)
    for c in todo:
        got = cache.get((alt[c], lo[c]))
        if got and got.get("matched_by") and got.get("candidates") == 1:
            for k, v in got.items():
                if k not in per_key:
                    per_key[k] = None
                per_key.at[c, k] = v
    return per_key


def _reasons(catalog: PriceCatalog, keys: pd.DataFrame, texts: list[str], unmatched: np.ndarray,
             cache: KeyCache, guessed: np.ndarray) -> dict[int, str]:
    """Why a key that names a product (SKU, usage type, or instance type and region) found no price, per key:
    service_not_loaded when the catalog has no prices for its service at all (a catalog gap), no_such_product when no
    product has its SKU, usage type or instance type in its region (a strong sign of a bad parse), else catalog_row.
    Only printed identifiers make no_such_product: one guessed from the words ("S3" in "Amazon S3 Standard
    Storage") is the guess's fault, not the parse's (catalog_row). One exists() query for the keys the cache does not
    know."""
    todo = [c for c in np.flatnonzero(unmatched)
            if keys.loc[c, ["sku", "usage_type"]].notna().any()
            or (pd.notna(keys.at[c, "instance_type"]) and pd.notna(keys.at[c, "region"]))]
    if not todo:
        return {}
    services = catalog.services()
    out: dict[int, str] = {}
    ask = []
    for c in todo:
        code = keys.at[c, "service_code"]
        if isinstance(code, str) and services and code not in services:
            out[c] = "service_not_loaded"
        elif texts[c] in cache.exists:
            out[c] = "catalog_row" if cache.exists[texts[c]] else "no_such_product"
        else:
            ask.append(c)
    if ask:
        frame = keys.loc[ask, ["sku", "service_code", "usage_type", "instance_type", "region"]].assign(key_id=ask)
        found = catalog.exists(frame)
        for c in ask:
            cache.exists[texts[c]] = c in found
            out[c] = "catalog_row" if c in found else "no_such_product"
    for c, why in out.items():
        if why == "no_such_product" and guessed[c]:
            out[c] = "catalog_row"
    return out


def _apply(rows: pd.DataFrame, idx: pd.Index, keys: pd.DataFrame, codes: np.ndarray, notes: np.ndarray,
           assumed: np.ndarray, dates: pd.Series, res: pd.DataFrame, qty: pd.Series, cost: pd.Series,
           stats: PriceStats, reasons: dict[int, str]) -> None:
    found = res["matched_by"].notna()
    cand = pd.to_numeric(res["candidates"], errors="coerce").fillna(0)
    one = found & cand.eq(1)
    assumed = pd.Series(assumed.astype(bool), index=idx)
    # the line's unit in the price's: (normalized unit, factor), an unknown unit as printed (lower case)
    def at(c: str) -> pd.Series:                      # one column of the priced lines (never a copy of every column)
        return _col(rows, c).loc[idx]
    def line_unit(u) -> tuple[str, float] | None:
        return (unit_of(u) or (str(u).strip().lower(), 1.0)) if isinstance(u, str) and u.strip() else None
    # each computed once per distinct unit text (a map over the rows' tuples was a Python call per row)
    unit = at("unit")
    exact_by = res["matched_by"].isin(EXACT_BY)
    no_unit = map_unique(unit, lambda u: line_unit(u) is None).astype(bool)
    norm = map_unique(unit, lambda u: t[0] if (t := line_unit(u)) else None)
    factor = map_unique(unit, lambda u: t[1] if (t := line_unit(u)) else 1.0).astype(float)
    unit_ok = (no_unit & exact_by) | (~no_unit & norm.eq(res["unit"]))
    level = pd.Series(None, index=idx, dtype=object)
    level[one & unit_ok & exact_by] = "exact"
    level[one & unit_ok & ~exact_by & ~assumed] = "attributes"
    level[one & unit_ok & ~exact_by & assumed] = "assumed"
    level[one & ~unit_ok] = "unit_mismatch"
    level[found & cand.gt(1)] = "ambiguous"
    level[~found] = "unmatched"
    missing = _missing(keys, codes, level, res, reasons)
    # prices: in the line's unit; printed values always win (R1)
    ok = level.isin(MATCHED)
    price = pd.to_numeric(res["unit_price"], errors="coerce") * factor
    fill_price = ok & pd.to_numeric(at("list_unit_price"), errors="coerce").isna()
    fill_list = ok & pd.to_numeric(at("list_cost"), errors="coerce").isna()
    for c in ("list_unit_price", "list_cost"):
        if c not in rows:
            rows[c] = np.nan
    rows.loc[fill_price[fill_price].index, "list_unit_price"] = price[fill_price]
    rows.loc[fill_price[fill_price].index, "_list_unit_price_inferred"] = True
    rows.loc[fill_list[fill_list].index, "list_cost"] = (qty * price)[fill_list]
    rows.loc[fill_list[fill_list].index, "_list_cost_inferred"] = True
    # notes: the line's words and the request's defaults, a first-tier price, a date before the catalog's history
    tiered = (res["tiered"].eq(True) | res["tiered"].eq(1)) & ok
    before = ok & res["effective_from"].notna() & (dates.astype(str) < res["effective_from"].astype(str))
    extra = np.where(tiered & before, "tiered; before_history",
                     np.where(tiered, "tiered", np.where(before, "before_history", "")))
    text = np.where((notes != "") & (extra != ""), np.char.add(np.char.add(notes.astype(str), "; "),
                                                               extra.astype(str)), np.where(notes != "", notes, extra))
    list_unit = pd.to_numeric(rows.loc[idx, "list_unit_price"], errors="coerce")
    eff = pd.to_numeric(at("effective_cost"), errors="coerce")
    billed = pd.to_numeric(at("billed_cost"), errors="coerce")
    paid = eff.where(eff.notna(), billed)
    implied = paid / qty.where(qty.ne(0))
    ratio = implied / list_unit.where(list_unit > 0)
    rows.loc[idx, "price_match_level"] = level
    rows.loc[idx, "price_missing"] = missing
    rows.loc[idx, "price_assumptions"] = pd.Series(np.where(text != "", text, None), index=idx, dtype=object)
    rows.loc[idx, "implied_unit_price"] = implied.astype(object).where(implied.notna(), None)
    rows.loc[idx, "price_ratio"] = ratio.astype(object).where(ratio.notna(), None)
    rows.loc[ok[ok].index, "catalog_sku_id"] = res.loc[ok, "sku"]
    rows.loc[ok[ok].index, "catalog_unit_price"] = price[ok]      # the catalog's, even where the file printed one
    rows.loc[idx, "price_conflict"] = _conflicts(rows.loc[idx, [c for c in rows.columns if c in CONFLICT_COLUMNS]], res,
                                                 level.eq("exact"))
    _count(stats, level, missing, cost, ratio, ok, rows.loc[idx, [c for c in rows.columns if c in COUNT_COLUMNS]],
           billed)


CONFLICT_COLUMNS = [c for col in ("region", "instance_type", "operating_system") for c in (col, HOW + col)]
COUNT_COLUMNS = ["coverage", "service_code"]


def _conflicts(p: pd.DataFrame, res: pd.DataFrame, exact: pd.Series) -> pd.Series:
    """An exact match (rate code, SKU, usage type) whose line prints another region, instance type or OS than the
    matched product: "region eu-north-9 vs us-east-1". Strong evidence of a column read from the wrong place (the
    rate code or SKU matched the product, the other columns did not). Printed values only; a region only when both
    are region codes."""
    out = pd.Series("", index=p.index, dtype=object)
    for col, fn in (("region", nz.region), ("instance_type", nz.instance_type),
                    ("operating_system", nz.operating_system)):
        if col not in p or f"product_{col}" not in res:
            continue
        mine = map_unique(p[col], lambda v, fn=fn: fn(v) if isinstance(v, str) and v.strip() else None)
        theirs = res[f"product_{col}"].astype(object)
        printed = ~(p[HOW + col].isin(GUESSED) if HOW + col in p else pd.Series(False, index=p.index))
        # compared on the exact, printed lines only (the text conversions of every line cost more than the rest)
        pos = np.flatnonzero((exact & printed).to_numpy())
        if not len(pos):
            continue
        m, t = mine.iloc[pos], theirs.iloc[pos]
        both = m.notna() & t.notna() & t.astype(str).ne("NA")
        if col == "region":
            both &= map_unique(m, lambda v: bool(REGION_CODE.match(v)) if isinstance(v, str) else False) \
                & map_unique(t, lambda v: bool(REGION_CODE.match(v)) if isinstance(v, str) else False)
        hit = np.zeros(len(p), dtype=bool)
        hit[pos] = (both & m.astype(str).ne(t.astype(str))).to_numpy(dtype=bool)
        off = pd.Series(hit, index=p.index)
        if off.any():
            text = col + " " + mine[off].astype(str) + " vs " + theirs[off].astype(str)
            out[off] = (out[off] + "; " + text).str.strip("; ")
    return out.where(out.ne(""), None)


def _missing(keys: pd.DataFrame, codes: np.ndarray, level: pd.Series, res: pd.DataFrame,
             reasons: dict[int, str]) -> pd.Series:
    """What would have matched a line, once per distinct (key, level, differs): the empty attributes of an ambiguous
    key that tell its candidates apart (all its empty ones when none does), and the operation of a usage type that has
    none; the instance type and region of an unmatched line named by neither rate code, SKU nor usage type; else why
    (reasons: service_not_loaded, no_such_product, catalog_row); unit for a unit mismatch."""
    differs = res["differs"].astype(object)
    frame = pd.DataFrame({"code": codes, "level": level.fillna("").to_numpy(),
                          "differs": differs.where(differs.notna(), "").astype(str).to_numpy()})
    combo, uniques = pd.factorize(pd.MultiIndex.from_frame(frame))
    named = keys[["rate_code", "sku", "usage_type"]].notna().any(axis=1).to_numpy()
    answers = []
    for code, lv, diff in uniques:
        row = keys.iloc[code]
        if lv == "ambiguous":
            empty = [c for c in ATTRIBUTES if pd.isna(row[c])]
            apart = [c for c in empty if c in diff.split(", ")] or empty
            if pd.notna(row["usage_type"]) and pd.isna(row["operation"]):
                apart = ["operation", *apart]         # an EC2 or RDS usage type: the operation names the OS, engine
            answers.append(", ".join(apart) or None)
        elif lv == "unmatched":
            lacking = "" if named[code] else ", ".join(c for c in ("instance_type", "region") if pd.isna(row[c]))
            answers.append(lacking or reasons.get(code, "catalog_row"))
        elif lv == "unit_mismatch":
            answers.append("unit")
        else:
            answers.append(None)
    return pd.Series(np.array(answers, dtype=object)[combo], index=level.index, dtype=object)


def _count(stats: PriceStats, level: pd.Series, missing: pd.Series, cost: pd.Series, ratio: pd.Series,
           ok: pd.Series, p: pd.DataFrame, billed: pd.Series) -> None:
    for lv, g in cost.groupby(level.fillna("")):
        if lv:
            got = stats.levels.setdefault(lv, [0, 0.0])
            got[0] += len(g)
            got[1] += float(g.sum())
    for text, g in cost.groupby(missing.fillna("")):
        for f in [f for f in str(text).split(", ") if f]:
            got = stats.missing.setdefault(f, [0, 0.0])
            got[0] += len(g)
            got[1] += float(g.sum())
    cov = _col(p, "coverage")
    steady = (level.isin(("exact", "attributes")) & cov.isin(("on_demand", "unknown")) & ratio.notna()
              & ratio.ge(R2_LOW * (1 - R2_TOLERANCE)) & ratio.lt(R2_HIGH * (1 + R2_TOLERANCE)))
    if steady.any():
        r = ratio[steady].astype(float)
        frame = pd.DataFrame({"b": (r / BUCKET).round().astype(int), "r": r,
                              "s": _col(p, "service_code")[steady].astype(str)})
        for b, g in frame.groupby("b"):
            got = stats.ratios.setdefault(int(b), [0, 0.0, set()])
            got[0] += len(g)
            got[1] += float(g["r"].sum())
            got[2] |= set(g["s"].unique())
    stats.below_list += int((ok & cov.eq("unknown") & (ratio.lt(R3_RATIO) | billed.eq(0))).sum())


def infer_discount(stats: PriceStats) -> float | None:
    """R2: 1 - r when at least 20 on-demand lines (exact or attributes), across at least 3 services, have a price
    ratio within 1% of one value r, 0.5 <= r < 0.99: a steady negotiated discount. Else None."""
    best = None
    for b in stats.ratios:
        center = b * BUCKET
        window = [v for k, v in stats.ratios.items() if abs(k * BUCKET - center) <= R2_TOLERANCE * center]
        n = sum(v[0] for v in window)
        services = set().union(*(v[2] for v in window))
        if n >= R2_MIN_LINES and len(services) >= R2_MIN_SERVICES and (best is None or n > best[0]):
            best = (n, sum(v[1] for v in window) / n)
    if best is None or not (R2_LOW <= best[1] < R2_HIGH):
        return None
    return 1 - best[1]


def apply_discount(rows: pd.DataFrame, discount: float | None) -> int:
    """R2: contracted cost = list cost x (1 - discount) on matched lines where the file printed none; returns the
    lines filled."""
    if discount is None or rows.empty or "price_match_level" not in rows:
        return 0
    lst = pd.to_numeric(_col(rows, "list_cost"), errors="coerce")
    if "contracted_cost" not in rows:
        rows["contracted_cost"] = np.nan
    todo = rows["price_match_level"].isin(MATCHED) & lst.notna() & pd.to_numeric(
        rows["contracted_cost"], errors="coerce").isna()
    rows.loc[todo, "contracted_cost"] = lst[todo] * (1 - discount)
    rows.loc[todo, "_contracted_cost_inferred"] = True
    return int(todo.sum())


def recompute(df: pd.DataFrame) -> pd.DataFrame:
    """implied_unit_price and price_ratio of rolled-up lines, from their sums (in place; returns df)."""
    if df.empty or "quantity" not in df or "price_match_level" not in df:
        return df
    q = pd.to_numeric(df["quantity"], errors="coerce")
    eff = pd.to_numeric(_col(df, "effective_cost"), errors="coerce")
    paid = eff.where(eff.notna(), pd.to_numeric(df["billed_cost"], errors="coerce"))
    lst = pd.to_numeric(_col(df, "list_cost"), errors="coerce")
    priced = df["price_match_level"].notna() & df["price_match_level"].astype(str).ne("")
    implied = (paid / q.where(q.ne(0))).where(priced)
    ratio = (paid / lst.where(lst > 0)).where(priced)
    df["implied_unit_price"] = implied.astype(object).where(implied.notna(), None)
    df["price_ratio"] = ratio.astype(object).where(ratio.notna(), None)
    return df


def is_flag(v) -> bool:
    """One flag value: True, 1, "True" or 1.0 (the Parquet rows keep flags as numbers)."""
    return v is True or (isinstance(v, (int, float)) and not pd.isna(v) and v != 0) \
        or (isinstance(v, str) and v.lower() in ("true", "1", "1.0"))


def flagged(s: pd.Series) -> pd.Series:
    """A flag column as bool: the same as s.map(is_flag), with is_flag called once per distinct (value, type) pair, not
    once per row (a value and its type decide is_flag; a missing value is False)."""
    arr = s.to_numpy(dtype=object)
    out = np.zeros(len(arr), dtype=bool)
    codes, _ = pd.factorize(arr, use_na_sentinel=True)
    kinds, _ = pd.factorize(np.fromiter(map(type, arr), dtype=object, count=len(arr)))
    known = codes >= 0
    if known.any():
        pair = codes[known].astype(np.int64) * (int(kinds.max()) + 1) + kinds[known]
        _, first, inverse = np.unique(pair, return_index=True, return_inverse=True)
        values = np.array([is_flag(v) for v in arr[known][first]], dtype=bool)
        out[known] = values[inverse.ravel()]
    return pd.Series(out, index=s.index, name=s.name)


def drop_prices(rows: pd.DataFrame) -> pd.DataFrame:
    """The rows as parsed: the price columns removed and the values pricing filled emptied (reprice)."""
    rows = rows.copy()
    for flag, col in (("_list_cost_inferred", "list_cost"), ("_contracted_cost_inferred", "contracted_cost"),
                      ("_list_unit_price_inferred", "list_unit_price")):
        if flag in rows and col in rows:
            rows.loc[flagged(rows[flag]), col] = np.nan
    return rows.drop(columns=[c for c in PRICE_COLUMNS if c in rows], errors="ignore")
