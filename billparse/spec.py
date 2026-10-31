"""The one interpreter that runs every spec: built-in, verified, or LLM-generated.

A spec is JSON. It can describe three sources (frame columns, a grid table, or
regex lines), plus header fields and stated totals. See specs/README.md for the format.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import expr
from .docmodel import Document, Region
from .layout import _row_to_cols, build_columns, extract_fields, extract_lines, extract_table, find_header, norm_h
from .mapper import map_columns
from .transforms import CANONICAL, TRANSFORMS, apply_transforms, default_transforms, map_unique, parse_number

ALLOWED_TOP = {"id", "version", "trust", "extends", "detect", "meta", "source", "table", "columns",
               "derive", "reshape", "filters", "fields", "lines", "totals", "fingerprint", "stats",
               "created_at", "parent", "notes", "tree", "context", "maps"}


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
    evidence: dict = field(default_factory=dict)     # tree source: subtotals, views, unexplained amounts
    provider: dict | None = None                     # the document's provider and what said so (provider.py)
    unmapped: dict = field(default_factory=dict)     # the fields guessed for columns the spec does not name
    checks: list = field(default_factory=list)       # more checks validate() adds (candidates_agree), as dicts


# ---------------------------------------------------------------- validation of spec shape
def check_spec(spec: dict) -> None:
    """Structural check for specs from untrusted sources (the LLM)."""
    extra = set(spec) - ALLOWED_TOP
    if extra:
        raise SpecError(f"Unknown spec keys: {sorted(extra)}")
    for fld, c in (spec.get("columns") or {}).items():
        if fld not in CANONICAL and not fld.startswith("_"):      # "_name": an internal field a step reads
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
    maps = spec.get("maps")
    if maps is not None and not (isinstance(maps, dict) and all(isinstance(k, str) and isinstance(v, str)
                                                                for k, v in maps.items())):
        raise SpecError('maps must be {"column": "prefix"}')
    if not (spec.get("columns") or spec.get("lines") or spec.get("source") in ("tree", "iac")):
        raise SpecError("Spec maps no columns")
    if spec.get("tree") is not None:
        if not isinstance(spec["tree"], dict):
            raise SpecError("tree must be an object of options")
        from .tree import check_options
        problems = check_options(spec["tree"])
        if problems:
            raise SpecError("; ".join(problems))
    if spec.get("context") is not None:
        from .context import check_context
        problems = check_context(spec["context"])
        if problems:
            raise SpecError("; ".join(problems))
    post = (spec.get("meta") or {}).get("post") or []
    if post:
        from .costs import POST
        unknown = [p for p in post if p not in POST]
        if unknown:
            raise SpecError(f"Unknown meta.post step(s): {unknown}; known: {sorted(POST)}")
    text_rx = (spec.get("detect") or {}).get("text")
    if text_rx is not None:
        re.compile(text_rx)


def merge(base: dict, child: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in child.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# ---------------------------------------------------------------- detection
def detect_score(spec: dict, headers: list[str], text: str = "") -> float:
    """0 if the spec can't apply to these headers (or this text, for layouts with no table header), else higher =
    better match."""
    d = spec.get("detect") or {}
    if d.get("text"):
        if not text or not re.search(d["text"], text, re.I | re.M):
            return 0.0
        if not (d.get("required") or d.get("any_of") or d.get("any_regex")):
            return 2.0
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
def run(spec: dict, doc: Document, catalog_dir=None) -> Extraction:
    source = spec.get("source", "auto")
    regions = doc.regions
    fields = extract_fields(regions, spec.get("fields") or {})
    if source == "tree":
        return _tree(spec, doc, fields, catalog_dir)
    if source == "iac":                                 # CloudFormation or Terraform: an inventory (iac.py)
        from . import iac
        res = iac.read(doc)
        ext = Extraction(rows=iac.rows(res), fields=fields, spec_id=spec.get("id", ""),
                         evidence={"excluded": res.excluded, "gaps": res.gaps})
        ext.warnings += [f"{g['name']}: {g['message']} ({g['src']})" for g in res.gaps]
        return ext
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

    raw = _expand_maps(spec, raw)
    raw, reshape_totals = _reshape(spec, raw)
    ext.stated_totals += reshape_totals
    if (spec.get("reshape") or {}).get("unpivot"):
        ext.source_rows = len(raw)
    rows = _map_columns(spec, raw, ext)
    rows = _derive(spec, rows, raw)
    rows = _filter(spec, rows)
    meta = spec.get("meta") or {}
    if meta.get("provider") and "provider" not in (spec.get("columns") or {}):
        rows["provider"] = meta["provider"]
    if meta.get("currency") and rows.get("currency") is not None and rows["currency"].isna().all():
        rows["currency"] = meta["currency"]
    ext.rows = rows.reset_index(drop=True)
    if meta.get("post"):
        from .costs import POST
        for step in meta["post"]:
            POST[step](ext, spec.get("_cost_basis"))      # _cost_basis: a large file's, from its first rows
    _totals_from_fields(spec, ext)
    if not ext.stated_totals:
        ext.stated_totals += _auto_totals(doc)
    return ext


def _tree(spec: dict, doc: Document, fields: dict, catalog_dir) -> Extraction:
    """The tree of sums (tree.py): lines with their context, and the evidence that they are complete."""
    from . import context, tree
    t = tree.read(doc.regions, spec.get("tree") or {})
    rows = context.lines_frame(t.lines, catalog_dir, t.currency, spec.get("context") or {})
    for fld, c in (spec.get("columns") or {}).items():             # a spec can still set or override a field
        if "const" in c:
            rows[fld] = c["const"]
        elif c.get("from") and c["from"][0] in rows:
            rows[fld] = rows[c["from"][0]]
    rows = _filter(spec, _derive(spec, rows))
    meta = spec.get("meta") or {}
    if meta.get("provider"):
        rows["provider"] = meta["provider"]
    fields = {**t.facts, **fields}                     # the spec's own fields win over the printed header fields
    ext = Extraction(rows=rows.reset_index(drop=True), fields=fields, source_rows=0,
                     evidence=t.evidence, spec_id=spec.get("id", ""))
    rec = t.evidence.get("reconcile") or {}
    top = rec if rec.get("src") else t.evidence.get("top_total")   # the printed total the lines are proved against
    if top:
        ext.stated_totals.append({"label": top["label"], "amount": top["amount"], "kind": "tree_total",
                                  "src": top["src"], **({"lines": rec["lines"], "rounding": rec["rounding"]}
                                                       if rec.get("src") else {})})
    _totals_from_fields(spec, ext)
    if rows.empty:
        ext.warnings.append("No rows extracted")
    return ext


def _frame(spec: dict, regions: list[Region]) -> tuple[Extraction, pd.DataFrame | None]:
    frames = [r for r in regions if r.is_frame]
    if not frames:
        return Extraction(rows=pd.DataFrame()), None
    scored = sorted(frames, key=lambda r: -detect_score(spec, list(r.df.columns)))
    use = [r for r in scored if detect_score(spec, list(r.df.columns)) > 0] or scored[:1]
    raw = pd.concat([r.df.assign(_src=[f"{r.name}:{i + (r.header_row or 0) + r.row_offset + 2}"
                                       for i in range(len(r.df))])
                     for r in use], ignore_index=True)
    return Extraction(rows=pd.DataFrame(), source_rows=len(raw)), raw


def _inventory_grid(spec: dict, regions: list[Region]) -> tuple[Extraction, pd.DataFrame | None]:
    """An inventory sheet (resources with no cost): every non-empty row below the header row is a row."""
    expect = [h for c in (spec.get("columns") or {}).values() for h in _from_list(c) if not h.startswith("_")]
    records = []
    for region in regions:
        if region.is_frame or not region.rows:
            continue
        hdr = find_header(region, None, expect)
        if hdr is None:
            continue
        cols = build_columns(region, hdr)
        for row in region.rows[hdr + 1:]:
            vals = _row_to_cols(row, cols, region.kind)
            if any(str(v).strip() for v in vals.values()):
                records.append({**vals, "_src": f"{region.name}:{row.index}"})
    raw = pd.DataFrame(records)
    return Extraction(rows=pd.DataFrame(), source_rows=len(raw)), (raw if records else None)


def _grid(spec: dict, regions: list[Region]) -> tuple[Extraction, pd.DataFrame | None]:
    if (spec.get("meta") or {}).get("kind") == "inventory":
        return _inventory_grid(spec, regions)
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


def _map_values(v) -> dict:
    """A map cell as a dict: JSON text ('{"a": "b"}'), Hive/Athena text ('{a=b, c=d}'), a dict, or key/value pairs
    (a Parquet map)."""
    if isinstance(v, dict):
        return v
    if isinstance(v, (list, tuple, np.ndarray)):
        return {str(k): x for k, x in (tuple(pair) for pair in v)}
    if not isinstance(v, str) or not v.strip():
        return {}
    text = v.strip()
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else {}
    except ValueError:
        pass
    if text.startswith("{") and text.endswith("}"):
        return dict(part.split("=", 1) for part in (x.strip() for x in text[1:-1].split(", ")) if "=" in part)
    return {}


def _expand_maps(spec: dict, raw: pd.DataFrame) -> pd.DataFrame:
    """A column of key/value pairs (CUR 2.0 "product": {"operating_system": "Linux", ...}) gives the columns the spec
    names with its prefix ("maps": {"product": "product_"} -> product_operating_system), when the file has no such
    column of its own."""
    maps = spec.get("maps") or {}
    if not maps:
        return raw
    named = {h for c in (spec.get("columns") or {}).values() for h in _from_list(c)}
    for col, prefix in maps.items():
        src = _find_col(raw, [col])
        want = [h for h in named if h.startswith(prefix) and _find_col(raw, [h]) is None]
        if src is None or not want:
            continue
        parsed = map_unique(raw[src], _map_values)
        for h in want:
            key = h[len(prefix):]
            raw[h] = map_unique(parsed, lambda d, key=key: (d or {}).get(key))
    return raw


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
    used: set = set()
    for fld, c in (spec.get("columns") or {}).items():
        if "const" in c:
            out[fld] = c["const"]
            continue
        if c.get("coalesce"):                                   # first non-blank across columns, per row
            srcs = [x for x in (_find_col(raw, [n]) for n in _from_list(c)) if x is not None]
            used.update(srcs)
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
        used.add(src)
        steps = c.get("t") if c.get("t") is not None else default_transforms(fld)
        out[fld] = apply_transforms(raw[src].where(raw[src].notna(), None), steps)
    meta = spec.get("meta") or {}
    ext.unmapped = _keep_unmapped(raw, out, used, guess=not meta.get("exact_columns"), plan=spec.get("_unmapped"),
                                  extra=not (meta.get("exact_columns") or spec.get("_no_extra")))
    for meta_col in ("_src", "_group"):
        if meta_col in raw:
            out[meta_col] = raw[meta_col]
    return out


# money columns: a second cost changes the sum; a price can be list or net
NEVER_ADDED = {"billed_cost", "tax", "unit_price", "list_cost", "contracted_cost", "effective_cost", "list_unit_price",
               "contracted_unit_price"}


def _keep_unmapped(raw: pd.DataFrame, out: pd.DataFrame, used: set, guess: bool = True,
                   plan: dict | None = None, extra: bool = True) -> dict:
    """The columns the spec did not map are not lost: a header the mapper's rules know fills a field the spec left
    empty (never a cost), and every other column goes in the line's extra text ("Header: value | ..."). Returns the
    fields it guessed. guess=False (a format whose every column has one meaning, meta.exact_columns): nothing is
    guessed, and no extra text is kept (a provider export's other columns are product details the spec chose not to
    read). plan: the guesses to use (the same for every chunk of a large file). extra=False: no extra text (a large
    file: it would cost more memory than the rest of the line)."""
    if not guess and not extra:
        return {}
    if plan is not None and not extra:
        return _take(raw, out, {f: c for f, c in plan.items() if c in raw.columns})
    left = [c for c in raw.columns if c not in used and not str(c).startswith("_") and str(c).strip()
            and raw[c].astype(str).str.strip().replace({"nan": "", "None": ""}).ne("").any()]
    if not left:
        return {}
    if not guess:
        chosen = {}
    elif plan is not None:
        chosen = {f: c for f, c in plan.items() if c in raw.columns}
    else:
        # the rules of before task 10 (legacy): a built-in or learned spec's result never changes with the mapper
        chosen, _, _ = map_columns({c: raw[c].head(200).tolist() for c in left}, legacy=True)
    taken = _take(raw, out, chosen)
    left = [c for c in left if c not in taken.values()]
    if not left or not extra:
        return taken
    parts = [map_unique(raw[c], lambda v, c=c: "" if v is None or (isinstance(v, float) and v != v)
                        or str(v).strip() == "" else f"{c}: {str(v).strip()}") for c in left]
    extra = parts[0].str.cat(parts[1:], sep=" | ") if len(parts) > 1 else parts[0]
    extra = extra.str.replace(r"( \| )+", " | ", regex=True).str.strip(" |")
    out["_extra"] = extra.where(extra.ne(""), None)
    return taken


def _take(raw: pd.DataFrame, out: pd.DataFrame, chosen: dict) -> dict:
    """The guessed fields the spec left empty (never a cost), filled from their columns."""
    taken = {}
    for fld, col in chosen.items():
        if fld in NEVER_ADDED or fld in out:
            continue
        out[fld] = apply_transforms(raw[col].where(raw[col].notna(), None), default_transforms(fld))
        taken[fld] = col
    return taken


def needed_columns(spec: dict, header: list[str], unmapped: dict | None = None) -> list[str]:
    """The columns of a file (its header) that a frame spec reads: the ones its columns name (or match by
    header_regex), the ones its derives read with col(), its map columns, and the guessed ones (unmapped). A large
    file reads only these (bigfile.py)."""
    names = [h for c in (spec.get("columns") or {}).values() for h in _from_list(c)]
    names += re.findall(r'col\(\s*"([^"]+)"\s*\)', " ".join(map(str, (spec.get("derive") or {}).values())))
    names += list(spec.get("maps") or {}) + list((unmapped or {}).values())
    frame = pd.DataFrame(columns=header)
    out = {c for c in (_find_col(frame, [n]) for n in names if not n.startswith("_")) if c is not None}
    for c in (spec.get("columns") or {}).values():
        if c.get("header_regex"):
            rx = re.compile(c["header_regex"], re.I)
            out |= {h for h in header if rx.search(str(h))}
    return [h for h in header if h in out]


HOW = "_how:"                                       # per row: was a derived value printed or computed (contract)


def _derive(spec: dict, rows: pd.DataFrame, raw: pd.DataFrame | None = None) -> pd.DataFrame:
    """Each derive expression, with how each value was made: "printed" when the expression copied a printed value
    (it left a mapped value as it was, or a function such as cur_effective selected a printed column), "derived"
    when it computed one."""
    for fld, e in (spec.get("derive") or {}).items():
        before = rows[fld] if fld in rows else None
        out = expr.evaluate(e, rows, raw)
        how = out.attrs.get("how")
        if how is None:
            same = before.astype(object).eq(out.astype(object)) if before is not None else \
                pd.Series(False, index=rows.index)
            how = pd.Series("derived", index=rows.index).where(~same, "printed")
        out = out.copy()
        out.attrs = {}
        if pd.api.types.is_float_dtype(out):            # computed money: no float noise (4.800000000000001)
            out = out.where(how.eq("printed") if isinstance(how, pd.Series) else False, out.round(10))
        rows[fld] = out
        rows[HOW + fld] = pd.Series(how, index=rows.index).where(out.notna(), None)
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
                keep &= ~map_unique(rows[col], lambda v, rx=rx: v is not None and bool(rx.search(str(v)))).astype(bool)
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
