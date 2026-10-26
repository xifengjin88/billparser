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
import logging
import multiprocessing
import os
import re
import threading
import zipfile
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)

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
    row_offset: int = 0            # frame regions: data rows before this one (a chunk of a large file, bigfile.py)

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
    if data[:4] == b"PK\x03\x04" and lname.endswith((".docx", ".pptx")):
        doc.file_type = lname.rsplit(".", 1)[-1]
        doc.regions += _read_office(data, prefix, doc.file_type)
        return
    if data[:4] == b"PK\x03\x04" and not lname.endswith((".xlsx", ".xlsm")):
        try:
            z = zipfile.ZipFile(io.BytesIO(data))             # zip of exports
        except zipfile.BadZipFile:
            doc.file_type = doc.file_type or "zip"
            doc.notes.append(f"{name}: the archive is damaged (no central directory); nothing could be read")
            return
        with z:
            for info in z.infolist():
                if not info.is_dir() and not info.filename.startswith("__MACOSX") \
                        and not Path(info.filename).name.startswith("._"):
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
    elif lname.endswith(TEXT_EXT) and not _tabular(_decode(data)):
        doc.file_type = doc.file_type or "txt"
        doc.regions.append(_read_text(_decode(data), prefix))
    else:
        doc.file_type = "csv"
        doc.regions.append(_read_csv(data, prefix))


# ---------------------------------------------------------------- text, Word, PowerPoint
TEXT_EXT = (".txt", ".text", ".md", ".log", ".yaml", ".yml", ".tf", ".hcl")


def _tabular(text: str) -> bool:
    """A delimited table saved as text: most lines split into the same 3+ fields."""
    lines = [ln for ln in text.splitlines()[:200] if ln.strip()]
    if len(lines) < 3:
        return False
    for sep in (",", "\t", ";", "|"):
        counts = Counter(ln.count(sep) for ln in lines)
        n, freq = counts.most_common(1)[0]
        if n >= 2 and freq >= 0.8 * len(lines):
            return True
    return False


def _read_text(text: str, prefix: str, name: str = "page1") -> Region:
    """Text lines as one page: one cell per line, the indent (in characters) as its x position."""
    rows = []
    for i, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        rows.append(Row(cells=[Cell(value=line.strip(), x0=indent * 6.0, x1=(indent + len(line.strip())) * 6.0)],
                        index=i + 1, top=float(i), page=1))
    return Region(kind="page", name=prefix + name, rows=rows)


def _read_office(data: bytes, prefix: str, kind: str) -> list[Region]:
    """Word and PowerPoint text and tables (their XML, no extra library): a page per slide, one for a document.
    Each table row is one line with its cells; each paragraph is one line."""
    import xml.etree.ElementTree as ET
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
          "a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
    regions: list[Region] = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if kind == "docx":
            parts = ["word/document.xml"]
        else:
            parts = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                           key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[-1]).group(1)))
        for k, part in enumerate(parts, 1):
            root = ET.fromstring(z.read(part))
            lines: list[str] = []
            if kind == "docx":
                body = root.find("w:body", ns)
                for el in list(body) if body is not None else []:
                    if el.tag.endswith("}tbl"):
                        for tr in el.iter(f"{{{ns['w']}}}tr"):
                            cells = ["".join(t.text or "" for t in tc.iter(f"{{{ns['w']}}}t")).strip()
                                     for tc in tr.iter(f"{{{ns['w']}}}tc")]
                            lines.append("    ".join(c for c in cells if c))
                    elif el.tag.endswith("}p"):
                        lines.append("".join(t.text or "" for t in el.iter(f"{{{ns['w']}}}t")))
            else:
                for tbl in root.iter(f"{{{ns['a']}}}tbl"):
                    for tr in tbl.iter(f"{{{ns['a']}}}tr"):
                        cells = ["".join(t.text or "" for t in tc.iter(f"{{{ns['a']}}}t")).strip()
                                 for tc in tr.iter(f"{{{ns['a']}}}tc")]
                        lines.append("    ".join(c for c in cells if c))
                for para in root.iter(f"{{{ns['a']}}}p"):
                    text = "".join(t.text or "" for t in para.iter(f"{{{ns['a']}}}t"))
                    if text.strip() and not any(text.strip() in ln for ln in lines):
                        lines.append(text)
            regions.append(_read_text("\n".join(lines), prefix, name=f"{'slide' if kind == 'pptx' else 'page'}{k}"))
    return regions


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
        computed = _evaluate_formulas(ws, wsf)            # formulas saved without a result (files written by programs)
        rows: list[Row] = []
        for r in range(1, (ws.max_row or 0) + 1):
            cells: list[Cell] = []
            for c in range(1, (ws.max_column or 0) + 1):
                cell = ws.cell(r, c)
                val = merged.get((r, c), cell.value)
                fv = wsf.cell(r, c).value
                formula = fv if isinstance(fv, str) and fv.startswith("=") else None
                if val is None and formula:
                    val = computed.get((r, c))
                if val is None:
                    continue
                cells.append(Cell(value=val, x0=c, x1=c + 1,
                                  bold=bool(cell.font and cell.font.bold),
                                  indent=float(cell.alignment.indent or 0) if cell.alignment else 0.0,
                                  formula=formula))
            if cells:
                rows.append(Row(cells=cells, index=r))
        regions.append(Region(kind="sheet", name=prefix + title, rows=rows))
    return regions


_REF = re.compile(r"\$?([A-Z]{1,3})\$?(\d+)")
_RANGE = re.compile(r"\$?([A-Z]{1,3})\$?(\d+):\$?([A-Z]{1,3})\$?(\d+)")


def _col_number(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def _evaluate_formulas(ws, wsf, rounds: int = 6) -> dict[tuple[int, int], float]:
    """Values of formulas that have no saved result: references, ranges, SUM, numbers and + - * / only, evaluated
    with Python's syntax tree (never eval). Others stay empty."""
    import ast
    import operator
    values: dict[tuple[int, int], float] = {}
    pending: dict[tuple[int, int], str] = {}
    for row in wsf.iter_rows():
        for cell in row:
            v = cell.value
            if isinstance(v, str) and v.startswith("="):
                saved = ws.cell(cell.row, cell.column).value
                if saved is None:
                    pending[(cell.row, cell.column)] = v[1:].upper()
                elif isinstance(saved, (int, float)) and not isinstance(saved, bool):
                    values[(cell.row, cell.column)] = float(saved)   # a formula with its result: a value to others
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                values[(cell.row, cell.column)] = float(v)
    ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}

    def calc(node):
        if isinstance(node, ast.Expression):
            return calc(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            return ops[type(node.op)](calc(node.left), calc(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            v = calc(node.operand)
            return -v if isinstance(node.op, ast.USub) else v
        raise ValueError("unsupported")

    computed: dict[tuple[int, int], float] = {}
    for _ in range(rounds):
        progress = False
        for key, f in list(pending.items()):
            def rng(m: re.Match) -> str:
                c1, r1, c2, r2 = _col_number(m[1]), int(m[2]), _col_number(m[3]), int(m[4])
                cells = [(r, c) for r in range(r1, r2 + 1) for c in range(c1, c2 + 1)]
                if any(k in pending for k in cells):
                    raise KeyError("not ready")
                return repr(sum(values.get(k, 0.0) for k in cells))
            try:
                expr = re.sub(r"SUM\(([^()]*)\)", lambda m: "(" + "+".join(
                    _RANGE.sub(rng, part) if ":" in part else part for part in m[1].split(",")) + ")", f)
                expr = _RANGE.sub(rng, expr)
                expr = _REF.sub(lambda m: _ref_value(m, values, pending), expr)
                v = calc(ast.parse(expr, mode="eval"))
            except (KeyError, ValueError, SyntaxError, ZeroDivisionError, TypeError):
                continue
            values[key] = computed[key] = round(v, 10)
            del pending[key]
            progress = True
        if not progress:
            break
    return computed


def _ref_value(m: re.Match, values: dict, pending: dict) -> str:
    key = (int(m[2]), _col_number(m[1]))
    if key in pending:
        raise KeyError("not ready")
    return repr(values.get(key, 0.0))


# ---------------------------------------------------------------- PDF
# PDF pages are read in parallel processes (pdfminer is pure Python: about a second a page). PDF_WORKERS sets the
# number of processes (default: the CPUs, at most 8; 1 reads in this process). The pool is made once and kept.
PDF_WORKERS = int(os.environ.get("PDF_WORKERS", "0")) or min(os.cpu_count() or 1, 8)
PDF_PARALLEL_PAGES = 4                                     # fewer pages: start-up costs more than it saves
_pool: ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()


def _pdf_pool() -> ProcessPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            # spawn, not fork: the API parses in threads, and a forked child of a threaded process can hang
            _pool = ProcessPoolExecutor(PDF_WORKERS, mp_context=multiprocessing.get_context("spawn"))
        return _pool


def _pdf_words(data: bytes, pages: list[int]) -> list[tuple[int, list[dict], bool]]:
    """(page number, words, has images) for the given 1-based pages. Runs in a worker process."""
    import pdfplumber

    out = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for pno in pages:
            page = pdf.pages[pno - 1]
            words = page.extract_words(extra_attrs=["fontname", "size"], keep_blank_chars=False,
                                       use_text_flow=False)
            out.append((pno, words, bool(page.images) if not words else False))
    return out


def _read_pdf(data: bytes, prefix: str) -> list[Region]:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        n = len(pdf.pages)
    pages = list(range(1, n + 1))
    found: list[tuple[int, list[dict], bool]] | None = None
    if PDF_WORKERS > 1 and n >= PDF_PARALLEL_PAGES:
        chunks = [pages[i::PDF_WORKERS] for i in range(min(PDF_WORKERS, n))]
        try:
            found = [x for part in _pdf_pool().map(_pdf_words, [data] * len(chunks), chunks) for x in part]
        except (BrokenProcessPool, OSError) as e:          # no processes here (a sandbox): read in this process
            log.warning("PDF pages read in one process: %s", e)
            _reset_pool()
    if found is None:
        found = _pdf_words(data, pages)
    regions: list[Region] = []
    for pno, words, images in sorted(found, key=lambda x: x[0]):
        reg = Region(kind="page", name=f"{prefix}page{pno}")
        if not words:
            reg.scanned = images
        else:
            reg.rows = _words_to_rows(words, pno)
        regions.append(reg)
    return regions


def _reset_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None


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
