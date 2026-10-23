"""Turn any uploaded file into one document model.

Two kinds of regions:
- Grid regions (Excel sheets, PDF pages): rows of cells with position and formatting
  (bold, indent, formula). Needed for messy layouts: group headers, subtotals,
  wrapped lines, tables anywhere on the sheet.
- Frame regions (CSV, Parquet, JSON records, very large sheets): a pandas DataFrame.
  Native exports can have millions of rows, so they skip per-cell objects.

Every spec and validator downstream works on this model, never on raw files.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

MAX_GRID_ROWS = 20_000           # bigger sheets are read as frames (no formatting)
HEADER_SCAN_ROWS = 40
HEADER_WORDS = {"cost", "amount", "service", "date", "description", "quantity", "qty",
                "price", "total", "product", "meter", "usage", "unit", "region",
                "account", "subscription", "charge", "sku", "currency", "rate", "item"}


# ---------------------------------------------------------------- model
@dataclass
class Cell:
    value: Any
    x0: float
    x1: float
    bold: bool = False
    indent: float = 0.0
    formula: str | None = None

    @property
    def text(self) -> str:
        return "" if self.value is None else str(self.value).strip()

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class Row:
    cells: list[Cell]
    index: int                     # Excel row number, or PDF line number on the page
    top: float = 0.0               # PDF y position
    page: int = 0

    @property
    def text(self) -> str:
        return " ".join(c.text for c in self.cells if c.text)

    @property
    def bold(self) -> bool:
        filled = [c for c in self.cells if c.text]
        return bool(filled) and all(c.bold for c in filled)

    @property
    def first_x(self) -> float:
        filled = [c for c in self.cells if c.text]
        return filled[0].x0 + filled[0].indent if filled else 0.0


@dataclass
class Region:
    kind: str                      # "sheet" | "page" | "frame"
    name: str
    rows: list[Row] = field(default_factory=list)
    df: pd.DataFrame | None = None # frame regions: raw frame, header NOT yet applied
    header_row: int | None = None  # frame regions: detected header row offset
    scanned: bool = False          # PDF page with no text layer

    @property
    def is_frame(self) -> bool:
        return self.df is not None

    def preview(self, n: int = 15) -> list[list[str]]:
        """First n rows as text, for fingerprints and LLM prompts."""
        if self.is_frame:
            return self.df.head(n).fillna("").astype(str).values.tolist()
        return [[c.text for c in r.cells] for r in self.rows[:n]]


@dataclass
class Document:
    name: str
    file_type: str
    sha256: str
    regions: list[Region]
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- entry point
def load(path: str | Path, name: str | None = None) -> Document:
    path = Path(path)
    data = path.read_bytes()
    return load_bytes(data, name or path.name)


def load_bytes(data: bytes, name: str) -> Document:
    sha = hashlib.sha256(data).hexdigest()
    doc = Document(name=name, file_type="", sha256=sha, regions=[])
    _load_into(doc, data, name)
    return doc


def _load_into(doc: Document, data: bytes, name: str, prefix: str = "") -> None:
    lname = name.lower()
    if data[:2] == b"\x1f\x8b":                               # gzip
        inner = name[:-3] if lname.endswith(".gz") else name + ".csv"
        return _load_into(doc, gzip.decompress(data), inner, prefix)
    if data[:4] == b"PK\x03\x04" and not lname.endswith((".xlsx", ".xlsm")):
        with zipfile.ZipFile(io.BytesIO(data)) as z:          # zip of exports
            for info in z.infolist():
                if not info.is_dir() and not info.filename.startswith("__MACOSX"):
                    _load_into(doc, z.read(info), info.filename, prefix=info.filename + ":")
        doc.file_type = doc.file_type or "zip"
        return
    if data[:5] == b"%PDF-":
        doc.file_type = "pdf"
        doc.regions += _read_pdf(data, prefix)
    elif data[:4] == b"PK\x03\x04" or lname.endswith((".xlsx", ".xlsm")):
        doc.file_type = "xlsx"
        doc.regions += _read_xlsx(data, prefix)
    elif data[:4] == b"PAR1" or lname.endswith(".parquet"):
        doc.file_type = "parquet"
        doc.regions.append(_frame(pd.read_parquet(io.BytesIO(data)), prefix + "parquet", header_in_df=True))
    elif lname.endswith(".json") or data.lstrip()[:1] in (b"{", b"["):
        doc.file_type = "json"
        records, meta = _read_json(data, prefix)
        doc.regions.append(records)
        if meta.rows:
            doc.regions.append(meta)
    else:
        doc.file_type = "csv"
        doc.regions.append(_read_csv(data, prefix))


# ---------------------------------------------------------------- CSV
def _read_csv(data: bytes, prefix: str) -> Region:
    text = _decode(data)
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        sep = dialect.delimiter
    except csv.Error:
        sep = ","
    head = list(csv.reader(io.StringIO(sample), delimiter=sep))[:HEADER_SCAN_ROWS]
    hdr = detect_header_row(head)
    df = pd.read_csv(io.StringIO(text), sep=sep, header=None, skiprows=hdr, dtype=str,
                     keep_default_na=False, na_values=[""], engine="python" if sep != "," else "c",
                     on_bad_lines="skip")
    df.columns = _unique_headers(df.iloc[0].tolist())
    df = df.iloc[1:].reset_index(drop=True)
    reg = Region(kind="frame", name=prefix + "csv", df=df, header_row=hdr)
    return reg


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- JSON
def _read_json(data: bytes, prefix: str) -> tuple[Region, Region]:
    text = _decode(data).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:                               # JSON lines
        obj = [json.loads(line) for line in text.splitlines() if line.strip()]
    records = _find_records(obj)
    flat = [_flatten(r) for r in records]
    df = pd.DataFrame(flat)
    return _frame(df, prefix + "json", header_in_df=True), _json_meta(obj, records, prefix)


def _json_meta(obj: Any, records: list[dict], prefix: str) -> Region:
    """Scalars outside the record list (invoice number, totals) as a key/value grid."""
    rec_ids = {id(r) for r in records}
    rows: list[Row] = []

    def walk(o: Any, path: str) -> None:
        if id(o) in rec_ids:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(o, list):
            if any(id(x) in rec_ids for x in o):
                return
            for i, x in enumerate(o[:50]):
                walk(x, f"{path}[{i}]")
        elif o is not None and path:
            rows.append(Row(cells=[Cell(path, 0, 1), Cell(o, 1, 2)], index=len(rows)))

    walk(obj, "")
    return Region(kind="sheet", name=prefix + "json-meta", rows=rows)


def _find_records(obj: Any) -> list[dict]:
    """The largest list of dicts anywhere in the JSON is the line-item table."""
    best: list[dict] = []

    def walk(o: Any) -> None:
        nonlocal best
        if isinstance(o, list):
            dicts = [x for x in o if isinstance(x, dict)]
            if len(dicts) > len(best) and len(dicts) >= 0.8 * len(o):
                best = dicts
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)

    walk(obj)
    if not best and isinstance(obj, dict):
        best = [obj]
    return best


def _flatten(d: dict, parent: str = "") -> dict:
    out: dict = {}
    for k, v in d.items():
        key = f"{parent}.{k}" if parent else str(k)
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        elif isinstance(v, list):
            out[key] = json.dumps(v)                           # e.g. GCP credits array
        else:
            out[key] = v
    return out


def _frame(df: pd.DataFrame, name: str, header_in_df: bool) -> Region:
    df = df.copy()
    df.columns = _unique_headers([str(c) for c in df.columns])
    return Region(kind="frame", name=name, df=df.astype(object).where(df.notna(), None), header_row=0)


# ---------------------------------------------------------------- Excel
def _read_xlsx(data: bytes, prefix: str) -> list[Region]:
    import openpyxl

    regions: list[Region] = []
    wb_vals = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    big = {ws.title for ws in wb_vals.worksheets if (ws.max_row or 0) > MAX_GRID_ROWS}
    for ws in wb_vals.worksheets:
        if ws.title in big:                                    # huge sheet: frame, no formatting
            rows = list(ws.iter_rows(values_only=True))
            head = [[("" if v is None else str(v)) for v in r] for r in rows[:HEADER_SCAN_ROWS]]
            hdr = detect_header_row(head)
            df = pd.DataFrame(rows[hdr + 1:], columns=_unique_headers([str(v) if v is not None else "" for v in rows[hdr]]))
            regions.append(Region(kind="frame", name=prefix + ws.title, df=df.astype(object), header_row=hdr))
    wb_vals.close()

    small = [n for n in openpyxl.load_workbook(io.BytesIO(data), read_only=True).sheetnames if n not in big]
    if not small:
        return regions
    wb_v = openpyxl.load_workbook(io.BytesIO(data), data_only=True)      # cached values + formatting
    wb_f = openpyxl.load_workbook(io.BytesIO(data), data_only=False)     # formulas
    for title in small:
        ws, wsf = wb_v[title], wb_f[title]
        merged: dict[tuple[int, int], Any] = {}
        for rng in ws.merged_cells.ranges:                    # fill merged ranges with top-left value
            v = ws.cell(rng.min_row, rng.min_col).value
            for r in range(rng.min_row, rng.max_row + 1):
                for c in range(rng.min_col, rng.max_col + 1):
                    merged[(r, c)] = v
        rows: list[Row] = []
        for r in range(1, (ws.max_row or 0) + 1):
            cells: list[Cell] = []
            for c in range(1, (ws.max_column or 0) + 1):
                cell = ws.cell(r, c)
                val = merged.get((r, c), cell.value)
                if val is None:
                    continue
                fv = wsf.cell(r, c).value
                formula = fv if isinstance(fv, str) and fv.startswith("=") else None
                cells.append(Cell(value=val, x0=c, x1=c + 1,
                                  bold=bool(cell.font and cell.font.bold),
                                  indent=float(cell.alignment.indent or 0) if cell.alignment else 0.0,
                                  formula=formula))
            if cells:
                rows.append(Row(cells=cells, index=r))
        regions.append(Region(kind="sheet", name=prefix + title, rows=rows))
    return regions


# ---------------------------------------------------------------- PDF
def _read_pdf(data: bytes, prefix: str) -> list[Region]:
    import pdfplumber

    regions: list[Region] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            words = page.extract_words(extra_attrs=["fontname", "size"], keep_blank_chars=False,
                                       use_text_flow=False)
            reg = Region(kind="page", name=f"{prefix}page{pno}")
            if not words:
                reg.scanned = bool(page.images)
                regions.append(reg)
                continue
            reg.rows = _words_to_rows(words, pno)
            regions.append(reg)
    return regions


def _words_to_rows(words: list[dict], page: int, y_tol: float = 3.0) -> list[Row]:
    words = sorted(words, key=lambda w: (round(w["top"]), w["x0"]))
    lines: list[list[dict]] = []
    for w in words:
        if lines and abs(lines[-1][0]["top"] - w["top"]) <= y_tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    rows: list[Row] = []
    for i, line in enumerate(lines):
        line.sort(key=lambda w: w["x0"])
        cells: list[Cell] = []
        for w in line:                                         # merge words into phrases by gap
            gap_limit = max(w.get("size", 10) * 0.6, 3.0)
            if cells and w["x0"] - cells[-1].x1 <= gap_limit:
                prev = cells[-1]
                prev.value = f"{prev.value} {w['text']}"
                prev.x1 = w["x1"]
                prev.bold = prev.bold and "bold" in w.get("fontname", "").lower()
            else:
                cells.append(Cell(value=w["text"], x0=w["x0"], x1=w["x1"],
                                  bold="bold" in w.get("fontname", "").lower()))
        rows.append(Row(cells=cells, index=i, top=line[0]["top"], page=page))
    return rows


# ---------------------------------------------------------------- shared helpers
_NUM = re.compile(r"^[\s$€£¥(+-]*[\d.,]+\s*[)%]?\s*(?:[€$£¥]|[A-Z]{1,3})?\s*-?$")


def is_numberish(s: str) -> bool:
    return bool(s) and bool(_NUM.match(s.strip())) and any(ch.isdigit() for ch in s)


def header_score(cells: list[str]) -> float:
    filled = [c.strip() for c in cells if c and str(c).strip()]
    if len(filled) < 2:
        return 0.0
    texty = sum(1 for c in filled if not is_numberish(c)) / len(filled)
    vocab = sum(1 for c in filled if any(w in c.lower() for w in HEADER_WORDS))
    short = sum(1 for c in filled if len(c) <= 40) / len(filled)
    return texty * short * (len(filled) ** 0.5) + 1.5 * vocab


def detect_header_row(rows: list[list[str]]) -> int:
    """Index of the most header-like row among the first rows."""
    if not rows:
        return 0
    width = max(len([c for c in r if str(c).strip()]) for r in rows)
    best, best_i = -1.0, 0
    for i, r in enumerate(rows):
        filled = len([c for c in r if str(c).strip()])
        if filled < max(2, 0.5 * width):
            continue
        s = header_score([str(c) for c in r])
        if s > best + 0.5:                                     # prefer earlier rows on ties
            best, best_i = s, i
    return best_i


def _unique_headers(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for i, n in enumerate(names):
        n = (str(n).strip() if n is not None and str(n) != "nan" else "") or f"col_{i + 1}"
        if n in seen:
            seen[n] += 1
            n = f"{n}_{seen[n]}"
        else:
            seen[n] = 0
        out.append(n)
    return out
