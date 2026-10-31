"""Find tables, header fields and list-style lines in grid regions (Excel sheets, PDF pages).

Columns are located by header text and x-position, never fixed indexes, so a moved
or added column doesn't break a spec. Output is a plain DataFrame plus row labels
and any stated totals found along the way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

from .docmodel import Region, Row, header_score, is_numberish
from .rowclass import classify, features

PDF_TOL = 25.0     # points: max distance from a column's range for a PDF cell to still join it


@dataclass
class Col:
    name: str
    x0: float
    x1: float


@dataclass
class GridTable:
    df: pd.DataFrame
    headers: list[str]
    stated_totals: list[dict] = field(default_factory=list)   # {"label","amount","kind","src"}
    labels: list[dict] = field(default_factory=list)          # {"src","label","features"} for learning
    amount_like_rows: int = 0                                 # rows that carried a number in the amount column
    regions_used: list[str] = field(default_factory=list)


def norm_h(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


# ---------------------------------------------------------------- header location
def find_header(region: Region, anchor: str | None = None, expect: list[str] | None = None,
                start: int = 0, max_scan: int = 80) -> int | None:
    rows = region.rows
    if anchor:
        rx = re.compile(anchor, re.I)
        for i in range(start, len(rows)):
            if rx.search(rows[i].text):
                return i
        return None
    expect_n = {norm_h(e) for e in (expect or [])}
    best, best_i = 0.0, None
    for i in range(start, min(len(rows), start + max_scan)):
        texts = [c.text for c in rows[i].cells]
        s = header_score(texts)
        if expect_n:
            hits = sum(1 for t in texts if norm_h(t) in expect_n)
            s += 3 * hits
            if hits == 0:
                continue
        if s > best + 0.25:
            best, best_i = s, i
    return best_i if best >= 2 else None


def build_columns(region: Region, hdr_i: int, header_rows: int = 1) -> list[Col]:
    head = region.rows[hdr_i]
    cols = [Col(c.text, c.x0, c.x1) for c in head.cells if c.text]
    for extra in region.rows[hdr_i + 1: hdr_i + header_rows]:
        for c in extra.cells:                                  # two-row headers: append sub-labels
            col = _match_col(cols, c, region.kind)
            if col is not None and c.text and c.text not in col.name:
                col.name = f"{col.name} {c.text}".strip()
    seen: dict[str, int] = {}
    for col in cols:                                           # merged header cells repeat names
        if col.name in seen:
            seen[col.name] += 1
            col.name = f"{col.name}_{seen[col.name]}"
        else:
            seen[col.name] = 0
    if region.kind == "page":                                  # widen PDF column ranges to midpoints
        cols.sort(key=lambda c: c.x0)
        for a, b in zip(cols, cols[1:]):
            mid = (a.x1 + b.x0) / 2
            a.x1, b.x0 = max(a.x1, mid), min(b.x0, mid)
    return cols


def _match_col(cols: list[Col], cell, kind: str) -> Col | None:
    best, best_ov = None, 0.0
    for col in cols:
        ov = min(col.x1, cell.x1) - max(col.x0, cell.x0)
        if ov > best_ov:
            best, best_ov = col, ov
    if best is not None:
        return best
    if kind == "page":                                         # nearest column within tolerance
        near = min(cols, key=lambda c: min(abs(cell.xc - c.x0), abs(cell.xc - c.x1)), default=None)
        if near and min(abs(cell.xc - near.x0), abs(cell.xc - near.x1)) <= PDF_TOL:
            return near
    return None


# ---------------------------------------------------------------- table extraction
def extract_table(regions: list[Region], tspec: dict, amount_headers: list[str],
                  label_headers: list[str], expect_headers: list[str]) -> GridTable | None:
    sheet_rx = re.compile(tspec["sheet"], re.I) if tspec.get("sheet") else None
    start_anchor, end_anchor = tspec.get("start"), tspec.get("end")
    end_rx = re.compile(end_anchor, re.I) if end_anchor else None
    header_rows = int(tspec.get("header_rows", 1))
    multi = tspec.get("multi_page", True)
    carry = tspec.get("group_header", {}).get("carry_to", "_group")
    merge_cont = tspec.get("continuation", "merge") == "merge"

    records: list[dict] = []
    out = GridTable(df=pd.DataFrame(), headers=[])
    cols: list[Col] | None = None
    header_texts: set[str] = set()
    group: str | None = None
    finished = False

    for region in regions:
        if finished or region.is_frame or not region.rows:
            continue
        if sheet_rx and not sheet_rx.search(region.name):
            continue
        hdr = find_header(region, start_anchor, expect_headers)
        if hdr is None and (cols is None or region.kind == "sheet"):
            continue                       # a sheet without the table's header is another table, not a next page
        if hdr is not None and (cols is None or region.kind == "sheet"):
            cols = build_columns(region, hdr, header_rows)
            header_texts = {c.name.lower() for c in cols}
            out.headers = [c.name for c in cols]
            begin = hdr + header_rows
        elif hdr is not None:                                  # repeated header on a new page
            begin = hdr + header_rows
        else:
            if not multi:
                continue
            begin = 0
        out.regions_used.append(region.name)
        amount_col = _pick(cols, amount_headers)
        label_col = _pick(cols, label_headers)
        xs = [r.first_x for r in region.rows[begin:begin + 60] if r.cells]
        median_x = sorted(xs)[len(xs) // 2] if xs else 0.0
        prev, last_idx, items_seen = None, None, 0

        i = begin
        while i < len(region.rows):
            row = region.rows[i]
            if region.kind == "sheet" and last_idx is not None and row.index - last_idx > 3 and items_seen:
                # big gap on a sheet: this table ended; look for a repeat of the same header below
                nxt = find_header(region, None, list(header_texts), start=i)
                if nxt is None or not _same_header(region.rows[nxt], header_texts):
                    break
                i = nxt + header_rows
                last_idx = region.rows[nxt].index
                continue
            last_idx = row.index
            if end_rx and end_rx.search(row.text) and items_seen:
                amt = _cell_text(row, amount_col, cols, region.kind)
                if is_numberish(amt):
                    out.stated_totals.append({"label": row.text[:80], "amount": amt, "kind": "end_anchor",
                                              "src": f"{region.name}:{row.index}"})
                finished = True
                break
            by_col = _row_to_cols(row, cols, region.kind)
            amt = by_col.get(amount_col.name, "") if amount_col else ""
            lab = by_col.get(label_col.name, "") if label_col else next(iter(by_col.values()), "")
            f = features(row, amt, lab, header_texts, median_x, prev)
            label = classify(f)
            src = f"{region.name}:{row.index}"
            out.labels.append({"src": src, "label": label, "features": f})
            if f["has_amount"]:
                out.amount_like_rows += 1
            if label == "item":
                rec = dict(by_col)
                rec["_group"] = group
                rec["_src"] = src
                if carry != "_group":
                    rec[f"_carry:{carry}"] = group
                records.append(rec)
                items_seen += 1
            elif label == "group_header":
                group = row.text.rstrip(":").strip()
            elif label == "continuation" and merge_cont and records and label_col:
                key = label_col.name
                records[-1][key] = f"{records[-1].get(key, '')} {lab or row.text}".strip()
            elif label in ("subtotal", "total"):
                out.stated_totals.append({"label": row.text[:80], "amount": amt, "kind": label, "src": src})
                if label == "total" and tspec.get("stop_at_total", True):
                    finished = True
                    break
            prev = label
            i += 1

    if not records:
        return None
    out.df = pd.DataFrame(records)
    return out


def _same_header(row: Row, header_texts: set[str]) -> bool:
    texts = {c.text.lower() for c in row.cells if c.text}
    return len(texts & header_texts) >= 0.6 * len(header_texts)


def _pick(cols: list[Col] | None, names: list[str]) -> Col | None:
    if not cols:
        return None
    wanted = [norm_h(n) for n in names]
    for w in wanted:
        for c in cols:
            if norm_h(c.name) == w:
                return c
    for w in wanted:
        for c in cols:
            if w and w in norm_h(c.name):
                return c
    return None


def _row_to_cols(row: Row, cols: list[Col], kind: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for cell in row.cells:
        if not cell.text:
            continue
        col = _match_col(cols, cell, kind)
        if col is None:
            continue
        out[col.name] = f"{out[col.name]} {cell.text}" if col.name in out else cell.text
    return out


def _cell_text(row: Row, col: Col | None, cols: list[Col], kind: str) -> str:
    if col is None:
        nums = [c.text for c in row.cells if is_numberish(c.text)]
        return nums[-1] if nums else ""
    return _row_to_cols(row, cols, kind).get(col.name, "")


# ---------------------------------------------------------------- header fields
def extract_fields(regions: list[Region], fspec: dict) -> dict[str, dict]:
    """{"invoice_total": {"anchor": "Total Due", "take": "right_of", "regex": "..."}}"""
    found: dict[str, dict] = {}
    for name, f in (fspec or {}).items():
        rx = re.compile(f["anchor"], re.I)
        val = None
        for region in regions:
            if region.is_frame:
                continue
            for i, row in enumerate(region.rows):
                for j, cell in enumerate(row.cells):
                    m = rx.search(cell.text)
                    if not m:
                        continue
                    rest = cell.text[m.end():].strip(" :\t")
                    if f.get("take", "right_of") == "right_of":
                        val = rest or next((c.text for c in row.cells[j + 1:] if c.text), None)
                    else:                                      # below
                        below = region.rows[i + 1] if i + 1 < len(region.rows) else None
                        if below:
                            val = next((c.text for c in below.cells
                                        if min(c.x1, cell.x1) - max(c.x0, cell.x0) > 0), None)
                    if val and f.get("regex"):
                        mm = re.search(f["regex"], val)
                        val = mm.group(1) if mm else None
                    if val:
                        found[name] = {"value": val, "src": f"{region.name}:{row.index}"}
                        break
                if name in found:
                    break
            if name in found:
                break
    return found


# ---------------------------------------------------------------- list-style lines
def extract_lines(regions: list[Region], patterns: list[dict]) -> pd.DataFrame:
    """Regex per text line, named groups become columns. For PDFs with no table at all."""
    compiled = [re.compile(p["pattern"]) for p in patterns]
    recs: list[dict] = []
    group = None
    for region in regions:
        if region.is_frame:
            continue
        for row in region.rows:
            text = row.text
            hit = False
            for rx in compiled:
                m = rx.search(text)
                if m:
                    rec = {k: v for k, v in m.groupdict().items() if v is not None}
                    rec["_group"], rec["_src"] = group, f"{region.name}:{row.index}"
                    recs.append(rec)
                    hit = True
                    break
            if not hit and row.bold and not any(is_numberish(c.text) for c in row.cells):
                group = text.rstrip(":")
    return pd.DataFrame(recs)
