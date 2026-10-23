"""The one interpreter that runs every spec: built-in, verified, or LLM-generated.

A spec is JSON. It can describe three sources (frame columns, a grid table, or
regex lines), plus header fields and stated totals. See specs/README.md for the format.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

import pandas as pd

from . import expr
from .docmodel import Document, Region
from .layout import extract_fields, extract_lines, extract_table, norm_h
from .transforms import CANONICAL, TRANSFORMS, apply_transforms, default_transforms, parse_number

ALLOWED_TOP = {"id", "version", "trust", "extends", "detect", "meta", "source", "table", "columns",
               "derive", "reshape", "filters", "fields", "lines", "totals", "fingerprint", "stats",
               "created_at", "parent", "notes"}


class SpecError(ValueError):
    pass


@dataclass
class Extraction:
    rows: pd.DataFrame                      # canonical columns + _src, _group
    stated_totals: list[dict] = field(default_factory=list)
    fields: dict = field(default_factory=dict)
    labels: list[dict] = field(default_factory=list)
    amount_like_rows: int | None = None     # for row-coverage check on grid tables
    source_rows: int = 0
    warnings: list[str] = field(default_factory=list)
    spec_id: str = ""


# ---------------------------------------------------------------- validation of spec shape
def check_spec(spec: dict) -> None:
    """Structural check for specs from untrusted sources (the LLM)."""
    extra = set(spec) - ALLOWED_TOP
    if extra:
        raise SpecError(f"Unknown spec keys: {sorted(extra)}")
    for fld, c in (spec.get("columns") or {}).items():
        if fld not in CANONICAL:
            raise SpecError(f"Unknown canonical field: {fld}")
        for step in c.get("t", []) or []:
            name = step if isinstance(step, str) else next(iter(step))
            if name not in TRANSFORMS:
                raise SpecError(f"Unknown transform: {name}")
    for fld, e in (spec.get("derive") or {}).items():
        if fld not in CANONICAL:
            raise SpecError(f"Unknown derived field: {fld}")
        expr.validate(e)
    for p in spec.get("lines") or []:
        re.compile(p["pattern"])
    if not (spec.get("columns") or spec.get("lines")):
        raise SpecError("Spec maps no columns")


def merge(base: dict, child: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in child.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# ---------------------------------------------------------------- detection
def detect_score(spec: dict, headers: list[str]) -> float:
    """0 if the spec can't apply to these headers, else higher = better match."""
    d = spec.get("detect") or {}
    hn = {norm_h(h) for h in headers}
    req = [norm_h(h) for h in d.get("required", [])]
    if any(r not in hn for r in req):
        return 0.0
    for group in d.get("any_of", []):
        if not any(norm_h(h) in hn for h in group):
            return 0.0
    if any(norm_h(h) in hn for h in d.get("forbid", [])):
        return 0.0
    ar = d.get("any_regex")
    if ar:
        rx = re.compile(ar["pattern"], re.I)
        if sum(1 for h in headers if rx.search(str(h))) < ar.get("min", 1):
            return 0.0
    if not req and not d.get("any_of") and not ar:
        return 0.0
    # more specific specs (more required columns) win ties
    return 1.0 + len(req) + 0.5 * len(d.get("any_of", []))


# ---------------------------------------------------------------- run
def run(spec: dict, doc: Document) -> Extraction:
    source = spec.get("source", "auto")
    regions = doc.regions
    fields = extract_fields(regions, spec.get("fields") or {})
    if source == "lines" or (source == "auto" and spec.get("lines") and not spec.get("table")):
        raw = extract_lines(regions, spec["lines"])
        ext = Extraction(rows=pd.DataFrame(), fields=fields, source_rows=len(raw))
    elif source == "grid" or (source == "auto" and spec.get("table") is not None):
        ext, raw = _grid(spec, regions)
        ext.fields = fields
    else:
        ext, raw = _frame(spec, regions)
        ext.fields = fields
    ext.spec_id = spec.get("id", "")
    if raw is None or raw.empty:
        ext.rows = pd.DataFrame(columns=list(CANONICAL) + ["_src", "_group"])
        ext.warnings.append("No rows extracted")
        _totals_from_fields(spec, ext)
        return ext

    raw, reshape_totals = _reshape(spec, raw)
    ext.stated_totals += reshape_totals
    if (spec.get("reshape") or {}).get("unpivot"):
        ext.source_rows = len(raw)
    rows = _map_columns(spec, raw, ext)
    rows = _derive(spec, rows)
    rows = _filter(spec, rows)
    meta = spec.get("meta") or {}
    if meta.get("provider") and "provider" not in (spec.get("columns") or {}):
        rows["provider"] = meta["provider"]
    if meta.get("currency") and rows.get("currency") is not None and rows["currency"].isna().all():
        rows["currency"] = meta["currency"]
    ext.rows = rows.reset_index(drop=True)
    _totals_from_fields(spec, ext)
    if not ext.stated_totals:
        ext.stated_totals += _auto_totals(doc)
    return ext


def _frame(spec: dict, regions: list[Region]) -> tuple[Extraction, pd.DataFrame | None]:
    frames = [r for r in regions if r.is_frame]
    if not frames:
        return Extraction(rows=pd.DataFrame()), None
    scored = sorted(frames, key=lambda r: -detect_score(spec, list(r.df.columns)))
    use = [r for r in scored if detect_score(spec, list(r.df.columns)) > 0] or scored[:1]
    raw = pd.concat([r.df.assign(_src=[f"{r.name}:{i + (r.header_row or 0) + 2}" for i in range(len(r.df))])
                     for r in use], ignore_index=True)
    return Extraction(rows=pd.DataFrame(), source_rows=len(raw)), raw


def _grid(spec: dict, regions: list[Region]) -> tuple[Extraction, pd.DataFrame | None]:
    cols = spec.get("columns") or {}
    amount_h = _from_list(cols.get("billed_cost")) or ["amount", "cost", "total", "charges", "price"]
    label_h = (_from_list(cols.get("service_raw")) + _from_list(cols.get("description"))) or ["description"]
    expect = [h for c in cols.values() for h in _from_list(c) if not h.startswith("_")]
    amount_h = [h for h in amount_h if not h.startswith("_")] or ["amount", "cost", "total", "charges", "price"]
    label_h = [h for h in label_h if not h.startswith("_")] or ["description", "service", "item"]
    gt = extract_table(regions, spec.get("table") or {}, amount_h, label_h, expect)
    if gt is None:
        # grid spec on a frame-only document (e.g. CSV): fall back to frame behavior
        return _frame(spec, regions)
    ext = Extraction(rows=pd.DataFrame(), stated_totals=gt.stated_totals, labels=gt.labels,
                     amount_like_rows=gt.amount_like_rows, source_rows=len(gt.df))
    return ext, gt.df


def _from_list(c: dict | None) -> list[str]:
    if not c:
        return []
    f = c.get("from", [])
    return [f] if isinstance(f, str) else list(f)


def _reshape(spec: dict, raw: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    up = (spec.get("reshape") or {}).get("unpivot")
    if not up:
        return raw, []
    idc = up.get("id")
    id_col = raw.columns[0] if idc in (None, "first") else _find_col(raw, [idc])
    excl = re.compile(up["exclude"], re.I) if up.get("exclude") else None
    value_cols = [c for c in raw.columns if c not in (id_col, "_src", "_group") and not (excl and excl.search(c))]
    totals: list[dict] = []
    if up.get("total_row"):
        trx = re.compile(up["total_row"], re.I)
        is_tot = raw[id_col].astype(str).str.match(trx)
        for _, r in raw[is_tot].iterrows():
            amt = sum(parse_number(r[c]) or 0.0 for c in value_cols)
            totals.append({"label": str(r[id_col]), "amount": amt, "kind": "total", "src": r.get("_src")})
        raw = raw[~is_tot]
    long = raw.melt(id_vars=[id_col, "_src"] if "_src" in raw else [id_col], value_vars=value_cols,
                    var_name="_var", value_name="_value")
    long = long.rename(columns={id_col: "_id"})
    return long, totals


def _find_col(df: pd.DataFrame, names: list[str]) -> str | None:
    lookup = {norm_h(c): c for c in df.columns}
    for n in names:
        if n in df.columns:
            return n
        hit = lookup.get(norm_h(n))
        if hit is not None:
            return hit
    return None


def _map_columns(spec: dict, raw: pd.DataFrame, ext: Extraction) -> pd.DataFrame:
    out = pd.DataFrame(index=raw.index)
    for fld, c in (spec.get("columns") or {}).items():
        if "const" in c:
            out[fld] = c["const"]
            continue
        if c.get("coalesce"):                                   # first non-blank across columns, per row
            srcs = [x for x in (_find_col(raw, [n]) for n in _from_list(c)) if x is not None]
            if srcs:
                merged = raw[srcs[0]].where(raw[srcs[0]].astype(str).str.strip().ne(""), None)
                for x in srcs[1:]:
                    merged = merged.where(merged.notna(), raw[x])
                steps = c.get("t") if c.get("t") is not None else default_transforms(fld)
                out[fld] = apply_transforms(merged.where(merged.notna(), None), steps)
            continue
        src = _find_col(raw, _from_list(c))
        if src is None and c.get("header_regex"):
            rx = re.compile(c["header_regex"], re.I)
            src = next((col for col in raw.columns if rx.search(str(col))), None)
        if src is None:
            if c.get("required"):
                ext.warnings.append(f"Required column for {fld} not found: {_from_list(c)}")
            continue
        steps = c.get("t") if c.get("t") is not None else default_transforms(fld)
        out[fld] = apply_transforms(raw[src].where(raw[src].notna(), None), steps)
    for meta_col in ("_src", "_group"):
        if meta_col in raw:
            out[meta_col] = raw[meta_col]
    return out


def _derive(spec: dict, rows: pd.DataFrame) -> pd.DataFrame:
    for fld, e in (spec.get("derive") or {}).items():
        rows[fld] = expr.evaluate(e, rows)
    return rows


def _filter(spec: dict, rows: pd.DataFrame) -> pd.DataFrame:
    keep = pd.Series(True, index=rows.index)
    for f in spec.get("filters") or []:
        if "drop_if_blank" in f:
            col = f["drop_if_blank"]
            if col in rows:
                keep &= rows[col].notna()
        elif "drop_if_match" in f:
            col, rx = f["drop_if_match"]["field"], re.compile(f["drop_if_match"]["regex"], re.I)
            if col in rows:
                keep &= ~rows[col].map(lambda v: v is not None and bool(rx.search(str(v)))).astype(bool)
        elif "keep_if_in" in f:
            col, vals = f["keep_if_in"]["field"], {str(v).lower() for v in f["keep_if_in"]["values"]}
            if col in rows:
                keep &= rows[col].astype(str).str.lower().isin(vals)
    return rows[keep]


AUTO_TOTAL = re.compile(r"^\s*(?:grand\s*total|total\s*(?:due|amount|charges)?|amount\s*due|balance\s*due|"
                        r"invoice\s*total|grand)\s*:?\s*$", re.I)


def _auto_totals(doc: Document) -> list[dict]:
    """Totals the spec didn't ask for: label cell followed by an amount (incl. JSON metadata)."""
    out = []
    for region in doc.regions:
        if region.is_frame:
            continue
        for row in region.rows:
            for j, cell in enumerate(row.cells):
                key = cell.text.split(".")[-1].replace("_", " ")
                if AUTO_TOTAL.match(key):
                    nxt = next((c.text for c in row.cells[j + 1:] if c.text), "")
                    if parse_number(nxt) is not None:
                        out.append({"label": cell.text, "amount": nxt, "kind": "auto", "src": f"{region.name}:{row.index}"})
    return out


def _totals_from_fields(spec: dict, ext: Extraction) -> None:
    for name in (spec.get("totals") or {}).get("from_fields", []):
        if name in ext.fields:
            ext.stated_totals.append({"label": name, "amount": ext.fields[name]["value"],
                                      "kind": "field", "src": ext.fields[name]["src"]})
