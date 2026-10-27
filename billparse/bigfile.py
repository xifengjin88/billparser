"""Large CUR, FOCUS, Azure and GCP exports: read in chunks, with bounded memory.

A file is large when it is more than LARGE_FILE_MB uncompressed. Its path (Pipeline.parse_path):

  classify   the format and the uncompressed size (a gzip is decompressed only until it passes LARGE_FILE_MB)
  sample     the header and the first SAMPLE_ROWS rows as a Document: the routes run on it as on a small file
  scan       the whole file in chunks of about CHUNK_ROWS rows. Each chunk goes through the accepted spec and the
             steps of a small file (clean, regions, attributes, coverage, services); its rows are appended to one
             Parquet file, and only sums are kept: the statistics the checks read (validate.RowStats) and the
             lines rolled up by contract.ROLLUP_KEYS, service, category and month
  result     the verdict from the statistics (validate.validate_stats); the analysis and the contract from the
             rolled-up lines, whose sums by service, category, provider and month are exact

Large files are CSV (plain, in gzip or in zip) and Parquet (plain or in zip). Excel, PDF and JSON files are read whole
and are refused above LARGE_FILE_MB. The code that reads a large file stays here: a worker process (task 09) calls it.
"""

from __future__ import annotations

import codecs
import csv
import gzip
import hashlib
import io
import logging
import os
import resource
import sys
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from . import contract, coverage
from .analyze import resolve_service_sums
from .attributes import extract as extract_attributes
from .attributes import normalize_regions
from .contract import any_flag, shared_text, shared_value
from .docmodel import HEADER_SCAN_ROWS, TEXT_EXT, Document, Region, _frame, _tabular, _unique_headers, detect_header_row
from .pricing import FLAGS as PRICE_FLAGS
from .pricing import PRICE_COLUMNS, KeyCache, PriceDefaults, PriceStats, match_prices, recompute
from .spec import HOW, needed_columns, run
from .transforms import CANONICAL, DATES, NUMERIC
from .validate import RowStats, clean_rows

log = logging.getLogger(__name__)

MB = 1024 * 1024


def setting(name: str, default: float) -> float:
    """A size or count from the environment, read at each use (tests change them)."""
    return float(os.environ.get(name, default))


def large_file_bytes() -> int:
    return int(setting("LARGE_FILE_MB", 100) * MB)              # larger (uncompressed): the large path


def max_uncompressed_bytes() -> int:
    return int(setting("MAX_UNCOMPRESSED_GB", 50) * 1024 * MB)  # larger: refused


class TooLarge(ValueError):
    pass


def too_large_message(size: int | None = None) -> str:
    limit = f"{setting('MAX_UNCOMPRESSED_GB', 50):g} GB"
    head = f"The file is {size / 1024 / MB:,.1f} GB uncompressed; the limit is {limit}" if size is not None else \
        f"The file is more than {limit} uncompressed, the limit"
    return (f"{head} (MAX_UNCOMPRESSED_GB). Split the export (for example one file per month or per account) and "
            "upload each part.")


def export_csv_message(fmt: str, size: int) -> str:
    return (f"Export this report as CSV to parse a file this large: this {fmt} file is {size / MB:,.0f} MB, and "
            f"{fmt} files are read whole, up to {setting('LARGE_FILE_MB', 100):g} MB (LARGE_FILE_MB).")


# ---------------------------------------------------------------- classify
@dataclass
class FileInfo:
    path: Path
    name: str
    large: bool
    fmt: str = "csv"                # csv | parquet (large files); other formats are refused when large
    compression: str = "none"       # none | gzip | zip
    member: str | None = None       # the zip member read
    size: int = 0                   # uncompressed bytes (a large gzip: at least this many)
    refused: str | None = None      # why the large path cannot read the file

    @property
    def region_name(self) -> str:
        """The frame's name, as docmodel names it for the same file."""
        prefix = f"{self.member}:" if self.member else ""
        return prefix + ("parquet" if self.fmt == "parquet" else "csv")


def _kind_of(head: bytes, name: str) -> str:
    """The format of a file from its first bytes and name, as docmodel._load_into decides it."""
    lname = name.lower()
    if head[:4] == b"PK\x03\x04" and lname.endswith((".docx", ".pptx")):
        return lname.rsplit(".", 1)[-1]
    if head[:4] == b"PK\x03\x04" and not lname.endswith((".xlsx", ".xlsm")):
        return "zip"
    if head[:5] == b"%PDF-":
        return "pdf"
    if head[:4] == b"PK\x03\x04" or lname.endswith((".xlsx", ".xlsm")):
        return "xlsx"
    if head[:4] == b"PAR1" or lname.endswith(".parquet"):
        return "parquet"
    if lname.endswith(".json") or head.lstrip()[:1] in (b"{", b"["):
        return "json"
    if lname.endswith(TEXT_EXT) and not _tabular(_text(head)):
        return "txt"
    return "csv"


def _members(z: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    return [i for i in z.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX")
            and not Path(i.filename).name.startswith("._")]


def classify(path: str | Path, name: str | None = None) -> FileInfo:
    """Small or large, and how a large file is read. The cost is bounded: a gzip is decompressed only until it passes
    LARGE_FILE_MB; its full size is checked while it is read (scan)."""
    path = Path(path)
    name = name or path.name
    limit = large_file_bytes()
    size = path.stat().st_size
    with path.open("rb") as f:
        head = f.read(64 * 1024)
    if head[:2] == b"\x1f\x8b":                                 # gzip: the size is known only by reading it
        n, first = 0, b""
        with gzip.open(path, "rb") as g:
            while n <= limit:
                b = g.read(8 * MB)
                if not b:
                    break
                first = first or b[:64 * 1024]
                n += len(b)
        inner = name[:-3] if name.lower().endswith(".gz") else name + ".csv"
        info = FileInfo(path, name, n > limit, compression="gzip", size=n)
        kind = _kind_of(first, inner)
    elif _kind_of(head, name) == "zip":
        try:
            with zipfile.ZipFile(path) as z:
                members = _members(z)
        except zipfile.BadZipFile:
            return FileInfo(path, name, False, fmt="zip", compression="zip", size=size)
        total = sum(i.file_size for i in members)
        info = FileInfo(path, name, total > limit, compression="zip", size=total)
        if not info.large:
            return info
        if len(members) != 1:
            info.refused = (f"This zip holds {len(members)} files ({total / MB:,.0f} MB uncompressed). A zip this "
                            "large is read when it holds one CSV or Parquet file: upload each file on its own.")
            return info
        info.member = members[0].filename
        with zipfile.ZipFile(path) as z, z.open(members[0]) as m:
            kind = _kind_of(m.read(64 * 1024), info.member)
    else:
        info = FileInfo(path, name, size > limit, size=size)
        kind = _kind_of(head, name)
    info.fmt = kind
    if not info.large:
        return info
    if info.size > max_uncompressed_bytes():
        info.refused = too_large_message(info.size)
    elif kind not in ("csv", "parquet"):
        info.refused = export_csv_message({"xlsx": "Excel", "pdf": "PDF", "json": "JSON"}.get(kind, kind), info.size)
    elif kind == "parquet" and info.compression == "gzip":
        info.refused = "A Parquet file in gzip cannot be read in parts: upload the Parquet file itself, or in a zip."
    return info


def sha256_of(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda: f.read(8 * MB), b""):
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------- reading
class _Counted(io.RawIOBase):
    """A stream that counts the bytes read through it, and stops past a limit (a gzip that grows past
    MAX_UNCOMPRESSED_GB)."""

    def __init__(self, f, limit: int | None = None):
        self.f, self.n, self.limit = f, 0, limit

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        n = self.f.readinto(b)
        self.n += n or 0
        if self.limit is not None and self.n > self.limit:
            raise TooLarge(too_large_message())
        return n

    def close(self) -> None:
        self.f.close()
        super().close()


class _Recode(io.RawIOBase):
    """Bytes in any encoding as UTF-8, a character that does not decode replaced by U+FFFD: the encoding is told from
    the first 64 KB, and a stray byte later in the file (a name, a tag) must not stop the parse."""

    def __init__(self, f, encoding: str):
        self.f = f
        self.dec = codecs.getincrementaldecoder("utf-8-sig" if encoding == "utf8" else encoding)(errors="replace")
        self.buf = b""
        self.replaced = False
        self.done = False

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        while not self.buf and not self.done:
            raw = self.f.read(4 * MB)
            text = self.dec.decode(raw, final=not raw)
            self.done = not raw
            self.replaced = self.replaced or "\ufffd" in text
            self.buf = text.encode("utf-8")
        n = min(len(b), len(self.buf))
        b[:n], self.buf = self.buf[:n], self.buf[n:]
        return n

    def close(self) -> None:
        self.f.close()
        super().close()


class Source:
    """A large file opened for reading: its bytes (decompressed), how far through the file the reading is, and the
    CSV dialect and header, read as docmodel._read_csv reads them."""

    def __init__(self, info: FileInfo, tmp_dir: Path | None = None):
        self.info = info
        self.tmp_dir = tmp_dir
        self.raw: _Counted | None = None
        self.extracted: Path | None = None          # Parquet in a zip, written out (Parquet needs to seek)
        if info.fmt == "csv":
            self._dialect()

    def open(self) -> io.BufferedReader:
        """The decompressed bytes, from the start. self.raw counts the stored bytes read (a gzip, a plain file) or the
        uncompressed bytes (a zip member, whose size is known), for the progress."""
        info = self.info
        if info.compression == "zip":
            z = zipfile.ZipFile(info.path)
            self.raw = _Counted(z.open(info.member or ""), max_uncompressed_bytes())
            self.total = info.size
            return io.BufferedReader(self.raw, 8 * MB)
        self.raw = _Counted(info.path.open("rb", buffering=0))
        self.total = info.path.stat().st_size
        if info.compression == "gzip":
            g = gzip.GzipFile(fileobj=io.BufferedReader(self.raw, 1 * MB))
            return io.BufferedReader(_Counted(g, max_uncompressed_bytes()), 8 * MB)
        return io.BufferedReader(self.raw, 8 * MB)

    @property
    def fraction(self) -> float:
        """How much of the file was read."""
        return min(1.0, self.raw.n / self.total) if self.raw and getattr(self, "total", 0) else 0.0

    def _dialect(self) -> None:
        with self.open() as f:
            head = f.read(64 * 1024)
        self.encoding = _encoding(head)
        text = head.decode(self.encoding, errors="ignore")
        if text.startswith("﻿"):
            text = text[1:]
        sample = text[:20000]
        try:
            self.sep = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            self.sep = ","
        rows = list(csv.reader(io.StringIO(sample), delimiter=self.sep))[:HEADER_SCAN_ROWS]
        self.header_row = detect_header_row(rows)
        self.columns = _unique_headers(rows[self.header_row] if self.header_row < len(rows) else [])

    @property
    def header_names(self) -> list[str]:
        """The file's column names (the header row of a CSV, the schema of a Parquet file)."""
        if self.info.fmt == "csv":
            return list(self.columns)
        return _unique_headers([str(c) for c in pq.ParquetFile(self._parquet_path()).schema_arrow.names])

    def frames(self, chunk_rows: Callable[[], int], columns: list[str] | None = None) -> Iterator[pd.DataFrame]:
        """The data rows as frames of about chunk_rows() rows (asked again before each frame), header applied, values as
        the small path reads them: text for CSV (blank = missing), as stored for Parquet. columns: read only these
        (the others are never parsed)."""
        self.skipped = 0
        if self.info.fmt == "parquet":
            yield from self._parquet(chunk_rows, columns)
            return

        def bad(row) -> str:
            self.skipped += 1
            return "skip"
        keep = columns if columns is not None else self.columns
        with self.open() as f:
            self.recode = _Recode(f, self.encoding)
            reader = pacsv.open_csv(
                pa.PythonFile(io.BufferedReader(self.recode, 8 * MB), mode="r"),
                read_options=pacsv.ReadOptions(skip_rows=self.header_row + 1, column_names=self.columns,
                                               encoding="utf8", block_size=8 * MB),
                parse_options=pacsv.ParseOptions(delimiter=self.sep, newlines_in_values=True, invalid_row_handler=bad),
                convert_options=pacsv.ConvertOptions(column_types={c: pa.string() for c in keep},
                                                     include_columns=keep, null_values=[""],
                                                     strings_can_be_null=True, quoted_strings_can_be_null=True))
            yield from _batched(reader, chunk_rows, lambda t: t.to_pandas())

    @property
    def replaced(self) -> bool:
        return bool(getattr(getattr(self, "recode", None), "replaced", False))

    def _parquet_path(self) -> Path:
        """The Parquet file; one in a zip is written out first (Parquet needs to seek)."""
        if self.info.compression != "zip":
            return self.info.path
        if self.extracted is None or not self.extracted.exists():
            path = self.info.path
            self.extracted = (self.tmp_dir or path.parent) / f"{path.name}.{os.getpid()}.{id(self)}.parquet"
            with zipfile.ZipFile(path) as z, z.open(self.info.member or "") as m, self.extracted.open("wb") as out:
                while (b := m.read(8 * MB)):
                    out.write(b)
        return self.extracted

    def _parquet(self, chunk_rows: Callable[[], int], columns: list[str] | None) -> Iterator[pd.DataFrame]:
        self.raw = _Counted(io.BytesIO())
        pf = pq.ParquetFile(self._parquet_path())
        self.total = pf.metadata.num_rows or 1
        names = dict(zip(_unique_headers([str(c) for c in pf.schema_arrow.names]), pf.schema_arrow.names, strict=True))
        read = None if columns is None else [names[c] for c in columns if c in names]
        try:
            for batch in pf.iter_batches(batch_size=max(1, chunk_rows()), columns=read):
                self.raw.n += batch.num_rows
                df = batch.to_pandas()
                yield _frame(df, "", header_in_df=True).df   # as docmodel reads a Parquet file
        finally:
            if self.extracted is not None:
                self.extracted.unlink(missing_ok=True)

    @property
    def header(self) -> int:
        return self.header_row if self.info.fmt == "csv" else 0


def _encoding(head: bytes) -> str:
    """The encoding docmodel._decode would find, from the first bytes (a UTF-16 file has its byte order mark)."""
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16"
    for cut in range(4):                                 # the last character can be cut in two
        try:
            head[:len(head) - cut].decode("utf-8")
            return "utf8"
        except UnicodeDecodeError:
            continue
    try:
        head.decode("cp1252")
        return "cp1252"
    except UnicodeDecodeError:
        return "latin-1"


def _text(head: bytes) -> str:
    return head.decode(_encoding(head), errors="ignore")


def _batched(reader, chunk_rows: Callable[[], int], to_frame) -> Iterator[pd.DataFrame]:
    """The reader's blocks cut into frames of exactly chunk_rows() rows (the last one smaller)."""
    batches, n = [], 0
    for batch in reader:
        if batch.num_rows == 0:
            continue
        batches.append(batch)
        n += batch.num_rows
        while n >= (k := max(1, chunk_rows())):
            table = pa.Table.from_batches(batches)
            yield to_frame(table.slice(0, k))
            rest = table.slice(k)
            batches, n = rest.to_batches(), rest.num_rows
    if n:
        yield to_frame(pa.Table.from_batches(batches))


# ---------------------------------------------------------------- the sample
def sample_doc(info: FileInfo, sha: str, rows: int | None = None) -> Document:
    """The header and the first rows (SAMPLE_ROWS, or fewer to stay within SAMPLE_CELLS values) as the Document a
    small file of them would give: the routes, the fingerprint, the agent and the source viewer work on it."""
    doc = Document(name=info.name, file_type=info.fmt, sha256=sha, regions=[])
    if info.refused:
        return doc
    src = Source(info)
    # SAMPLE_ROWS rows, fewer for a wide file: at most SAMPLE_CELLS values (every route reads every column)
    width = max(1, len(src.header_names))
    n = int(rows or min(setting("SAMPLE_ROWS", 50_000), max(1_000, setting("SAMPLE_CELLS", 2_500_000) // width)))
    frames = src.frames(lambda: n)
    df = next(frames, None)
    frames.close()
    if df is not None:
        doc.regions.append(Region(kind="frame", name=info.region_name, df=df.head(n).reset_index(drop=True),
                                  header_row=src.header))
    return doc


def stored_doc(store, sha: str) -> Document | None:
    """A stored file as a Document: the whole file when it is small, the sample when it is large (a large file is never
    read whole into memory)."""
    from .docmodel import load_bytes
    path = store.file_path(sha)
    if path is None:
        return None
    name = store.doc_name(sha) or path.name
    info = classify(path, name)
    if info.large:
        return sample_doc(info, sha)
    return load_bytes(path.read_bytes(), name)


# ---------------------------------------------------------------- the scan
# columns the steps add to a spec's canonical fields (spec._keep_unmapped, attributes, coverage, resolve_services)
STEP_COLUMNS = ["service", "service_name", "service_code", "category", "resolved_by", "region_name", "price_per",
                "commitment_status", "meter_name", HOW + "coverage", "_extra", "_src", "_group", *PRICE_COLUMNS]
PRICE_NUMBERS = {"implied_unit_price", "price_ratio", *PRICE_FLAGS}     # Parquet numbers (a flag: 1.0 or 0.0)
ROLL_KEYS = [*contract.ROLLUP_KEYS, "service", "category", "_month"]
HOW_RANK = contract.HOW_RANK                  # a rolled-up line keeps its lines' least certain way


@dataclass
class Scan:
    """What a scan of a large file keeps: sums, never the rows."""
    stats: RowStats = field(default_factory=RowStats)
    rolled: pd.DataFrame = field(default_factory=pd.DataFrame)
    stated_totals: list[dict] = field(default_factory=list)
    fields: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    source_rows: int = 0
    resolved: float = 0.0               # usage cost with a resolved service, and all usage cost
    usage: float = 0.0
    chunks: int = 0
    skipped: int = 0                    # CSV rows with a different number of fields than the header
    path: Path | None = None            # the rows, as Parquet
    bases: set = field(default_factory=set)
    pricing: PriceStats | None = None   # the price match of every chunk, merged (None: not priced)

    @property
    def rows(self) -> int:
        return self.stats.rows

    @property
    def total(self) -> float:
        return round(self.stats.total, 2)

    @property
    def share(self) -> float | None:
        return self.resolved / self.usage if self.usage else None

    def note(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)


def scan(spec: dict, info: FileInfo, sample: Document, catalog_dir: Path | None = None, out: Path | None = None,
         full: bool = True, on_progress: Callable[[str], None] | None = None, unmapped: dict | None = None,
         cost_basis: str | None = None, prices=None, price_defaults: PriceDefaults | None = None) -> Scan:
    """Read the whole file in chunks with the spec. full: the steps of a parse (attributes, coverage, services, the
    rows to `out` as Parquet, the rolled-up lines); else only the spec, the cleaning and the statistics (the
    regression gate). unmapped: the fields guessed on the sample for columns the spec does not name, used for every
    chunk (else each chunk would guess from its own first rows). Raises TooLarge when the file grows past
    MAX_UNCOMPRESSED_GB. cost_basis: the export's basis told from the sample (Azure actual or amortized), for chunks
    whose own rows cannot tell it. prices: the price catalog (pricing.py): each chunk's usage lines are priced, the
    keys looked up once for the file (a KeyCache)."""
    spec = dict(spec, _no_extra=True, _cost_basis=cost_basis,
                **({"_unmapped": unmapped} if unmapped is not None else {}))
    pa.set_cpu_count(max(1, int(setting("LARGE_IO_THREADS", 2))))
    pa.set_io_thread_count(max(1, int(setting("LARGE_IO_THREADS", 2))))
    res = Scan(path=out)
    src = Source(info, tmp_dir=out.parent if out else None)
    budget = setting("LARGE_MEMORY_MB", 2048) * MB
    columns = needed_columns(spec, src.header_names, unmapped)
    # CHUNK_ROWS rows, fewer for a wide file: the columns read plus about 50 the steps add, at most CHUNK_CELLS values
    size = {"rows": min(int(setting("CHUNK_ROWS", 250_000)),
                        max(1_000, int(setting("CHUNK_CELLS", 12_000_000)) // (len(columns) + 50)))}
    writer: pq.ParquetWriter | None = None
    schema: pa.Schema | None = None
    pending: list[pd.DataFrame] = []
    kind = (spec.get("meta") or {}).get("kind", "cost_lines")
    cache = KeyCache()
    base = rss()
    try:
        for chunk in src.frames(lambda: size["rows"], columns):
            region = Region(kind="frame", name=info.region_name, df=chunk, header_row=src.header,
                            row_offset=res.source_rows)
            ext = run(spec, Document(info.name, info.fmt, sample.sha256, [region]), catalog_dir)
            res.source_rows += ext.source_rows
            del chunk, region
            clean_rows(ext, chunk=True)
            rows = ext.rows
            if full:
                normalize_regions(rows)
                extract_attributes(rows, ext.warnings)
                coverage.apply(rows)
                if catalog_dir is not None and kind == "cost_lines":
                    r, u = resolve_service_sums(rows, catalog_dir)
                    res.resolved += r
                    res.usage += u
                if kind == "cost_lines" and prices is not None:
                    stats = match_prices(rows, prices, price_defaults, cache)
                    if res.pricing is None:
                        res.pricing = stats
                    else:
                        res.pricing.merge(stats)
            res.stats.add(rows)
            _merge_meta(res, ext)
            if full and not rows.empty:
                if writer is None:
                    schema = _schema(rows, spec)
                    writer = pq.ParquetWriter(str(out), schema, compression="snappy")
                writer.write_table(_table(rows, schema, res))
                pending.append(roll(rows))
                if len(pending) >= 10:                  # merged every 10 chunks
                    pending = [_cap(roll(pd.concat(pending, ignore_index=True)), res, 4)]
            res.chunks += 1
            del rows, ext
            if res.chunks == 1:
                size["rows"] = _chunk_size(size["rows"], rss() - base, budget)
            if on_progress is not None:
                est = res.source_rows / src.fraction if src.fraction else 0
                on_progress(f"reading: {res.source_rows / 1e6:.1f}M of ~{max(est, res.source_rows) / 1e6:.1f}M rows")
    finally:
        if writer is not None:
            writer.close()
    res.skipped = getattr(src, "skipped", 0)
    if res.skipped:
        res.note(f"{res.skipped:,} rows have a different number of fields than the header; they were not read")
    if src.replaced:
        res.note("Some characters of the file are not valid in its encoding; they were replaced by \ufffd")
    if pending:
        res.rolled = _cap(roll(pd.concat(pending, ignore_index=True)), res, 1)
        res.rolled = finish_rolled(res.rolled)
    if len(res.bases) == 1:
        res.fields["cost_basis"] = {"value": next(iter(res.bases)), "src": None}
    elif res.bases:
        res.fields["cost_basis"] = {"value": "unknown", "src": None}
        res.note(f"The chunks of this file read as different cost bases ({', '.join(sorted(res.bases))}); "
                 "the cost basis is unknown")
    return res


def _merge_meta(res: Scan, ext) -> None:
    """A chunk's fields, totals and warnings into the scan's. The cost basis of each chunk is kept apart (an Azure
    export: each chunk is told actual or amortized by its own commitment rows; chunks with none cannot tell)."""
    basis = (ext.fields or {}).get("cost_basis")
    for k, v in (ext.fields or {}).items():
        if k != "cost_basis":
            res.fields.setdefault(k, v)
    if basis and basis.get("value") in ("actual", "amortized"):
        res.bases.add(basis["value"])
    elif basis and "cost_basis" not in res.fields:
        res.fields["cost_basis"] = basis
    for t in ext.stated_totals:
        if len(res.stated_totals) < 50 and t not in res.stated_totals:
            res.stated_totals.append(t)
    for w in ext.warnings:
        res.note(w)


def _chunk_size(rows: int, used: int, budget: float) -> int:
    """After the first chunk: the chunk size that keeps one chunk and its copies within about half of the budget (the
    rolled-up lines and the reader's buffers need the rest)."""
    if used <= 0:
        return rows
    fit = int(rows * 0.5 * budget / used)
    return min(rows, max(10_000, fit))


def rss() -> int:
    """This process's memory now (Linux), or its peak (elsewhere), in bytes."""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except OSError:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak if sys.platform == "darwin" else peak * 1024


# ---------------------------------------------------------------- Parquet rows
def _schema(rows: pd.DataFrame, spec: dict) -> pa.Schema:
    """The Parquet columns of every chunk, fixed by the first: its columns, then every canonical field and step column
    and the derived fields' "how" columns. Money and quantities are numbers, dates are timestamps, the rest text."""
    names = list(rows.columns)
    for c in [*CANONICAL, *STEP_COLUMNS, *(HOW + f for f in CANONICAL)]:     # how each value was made, any chunk
        if c not in names:
            names.append(c)
    out = []
    for c in names:
        s = rows[c] if c in rows else None
        if c in NUMERIC or c in PRICE_NUMBERS:
            out.append(pa.field(c, pa.float64()))
        elif c in DATES or (s is not None and pd.api.types.is_datetime64_any_dtype(s)):
            out.append(pa.field(c, pa.timestamp("ns")))
        else:
            out.append(pa.field(c, pa.string()))
    return pa.schema(out)


def _table(rows: pd.DataFrame, schema: pa.Schema, res: Scan) -> pa.Table:
    cols = {}
    for f in schema:
        s = rows[f.name] if f.name in rows else pd.Series(None, index=rows.index, dtype=object)
        if pa.types.is_floating(f.type):
            s = pd.to_numeric(s, errors="coerce").astype("float64")
        elif pa.types.is_timestamp(f.type):
            s = pd.to_datetime(s, errors="coerce")
            s = s.dt.tz_localize(None) if getattr(s.dt, "tz", None) is not None else s
            s = s.astype("datetime64[ns]")
        else:
            s = s.astype("string")
        cols[f.name] = s
    for c in rows.columns:
        if c not in cols:
            res.note(f"Column {c} appeared after the first chunk and is not in the stored rows")
    return pa.Table.from_pandas(pd.DataFrame(cols, index=rows.index), schema=schema, preserve_index=False)


# ---------------------------------------------------------------- rolled-up lines
def roll(df: pd.DataFrame) -> pd.DataFrame:
    """Lines alike in contract.ROLLUP_KEYS, service, category and month as one line, as contract._rollup adds them up
    (cost and quantity added, list, contracted and effective cost added or left empty, the first source and
    description, the first start and last end, "derived" when any line's value was). Takes lines or rolled-up lines."""
    if df.empty or "billed_cost" not in df:
        return pd.DataFrame()
    df = df.copy()
    if "_month" not in df:
        df["_month"] = contract.month_of(df["charge_start"]) if "charge_start" in df else ""
    if "_lines" not in df:
        df["_lines"] = 1
    keys = [k for k in ROLL_KEYS if k in df]
    for k in keys:
        df[k] = df[k].astype("string").fillna("")
    df["billed_cost"] = pd.to_numeric(df["billed_cost"], errors="coerce").fillna(0.0)
    g = df.groupby(keys, sort=False, dropna=False)
    parts = [g[["billed_cost", "_lines"]].sum()]
    if "quantity" in df:
        df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")
        parts.append(g[["quantity"]].sum())
    costs = [c for c in contract.COST_FIELDS if c in df]
    for c in costs:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if costs:
        parts.append(g[costs].sum(min_count=1))
    first = [c for c in ("_src", "description") if c in df]
    if first:
        parts.append(g[first].first())
    if "charge_start" in df:
        parts.append(g[["charge_start"]].min())
        last = "_last_start" if "_last_start" in df else "charge_start"     # the analysis's period ends there
        parts.append(g[[last]].max().rename(columns={last: "_last_start"}))
    if "charge_end" in df:
        parts.append(g[["charge_end"]].max())
    prices = [c for c in contract.UNIT_PRICES if c in df]
    if prices:
        parts.append(pd.DataFrame({c: shared_value(df, [df[k] for k in keys], c) for c in prices}))
    texts = [c for c in contract.SHARED_TEXT if c in df]
    if texts:
        parts.append(pd.DataFrame({c: shared_text(df, [df[k] for k in keys], c) for c in texts}))
    flags = [c for c in PRICE_FLAGS if c in df]
    if flags:
        parts.append(pd.DataFrame({c: any_flag(df, [df[k] for k in keys], c) for c in flags}))
    hows = [c for c in df.columns if str(c).startswith(HOW)]
    if hows:
        rank = pd.DataFrame({c: df[c].map(HOW_RANK) for c in hows}, index=df.index)
        top = rank.groupby([df[k] for k in keys], sort=False, dropna=False).max()
        names = {v: k for k, v in HOW_RANK.items()}
        parts.append(top.apply(lambda s: s.map(names)))
    return recompute(pd.concat(parts, axis=1).reset_index())


def _cap(rolled: pd.DataFrame, res: Scan, factor: int) -> pd.DataFrame:
    """More rolled-up lines than factor x CONTRACT_MAX_CHARGES: the largest by cost stay, the rest are one "other" line
    per service, charge type, coverage and month (the analysis's sums stay exact)."""
    limit = contract.MAX_CHARGES * factor
    if len(rolled) <= limit:
        return rolled
    order = rolled["billed_cost"].abs().sort_values(ascending=False).index
    keep, rest = rolled.loc[order[:contract.MAX_CHARGES - 1000]], rolled.loc[order[contract.MAX_CHARGES - 1000:]]
    same = ["service", "category", "charge_type", "provider", "currency", "coverage", "commitment_status", "_month"]
    rest = rest.copy()
    for k in [k for k in ROLL_KEYS if k in rest and k not in same]:
        rest[k] = ""
    rest["description"] = "Other lines of this service (rolled up)"
    res.note(f"More than {limit:,} kinds of lines: the smaller ones are one line per service, charge type, coverage "
             "and month (description 'Other lines of this service')")
    return pd.concat([keep, roll(rest)], ignore_index=True)


def reprice(src: Path, out: Path, prices, price_defaults: PriceDefaults | None = None,
            period_start: str | None = None, batch_rows: int | None = None) -> Scan:
    """A large file's stored rows (Parquet) priced again with the current catalog, never parsed again: read in
    batches, the values pricing filled before dropped (pricing.drop_prices), priced with one KeyCache, written to
    `out`, and rolled up as the scan does. The Scan has the rolled-up lines and the merged price statistics."""
    from .pricing import drop_prices
    res = Scan(path=out)
    cache = KeyCache()
    writer: pq.ParquetWriter | None = None
    schema: pa.Schema | None = None
    pending: list[pd.DataFrame] = []
    try:
        for batch in pq.ParquetFile(str(src)).iter_batches(batch_size=batch_rows or int(setting("CHUNK_ROWS", 250_000))):
            rows = drop_prices(batch.to_pandas())
            stats = match_prices(rows, prices, price_defaults, cache, period_start)
            if res.pricing is None:
                res.pricing = stats
            else:
                res.pricing.merge(stats)
            res.stats.add(rows)
            if writer is None:
                schema = _schema(rows, {})
                writer = pq.ParquetWriter(str(out), schema, compression="snappy")
            writer.write_table(_table(rows, schema, res))
            pending.append(roll(rows))
            if len(pending) >= 10:
                pending = [_cap(roll(pd.concat(pending, ignore_index=True)), res, 4)]
            res.chunks += 1
    finally:
        if writer is not None:
            writer.close()
    if pending:
        res.rolled = finish_rolled(_cap(roll(pd.concat(pending, ignore_index=True)), res, 1))
    return res


def finish_rolled(rolled: pd.DataFrame) -> pd.DataFrame:
    """Rolled-up lines as the contract and the analysis read them: empty keys as missing, largest cost first."""
    out = rolled.drop(columns=["_month"], errors="ignore")
    for k in [k for k in ROLL_KEYS if k in out]:
        out[k] = out[k].astype(object).where(out[k].notna() & out[k].ne(""), None)
    return out.sort_values("billed_cost", key=lambda s: -s.abs()).reset_index(drop=True)


def unsupported_spec(route: str, spec: dict | None) -> str | None:
    """Why the accepted reading of the sample cannot be applied chunk by chunk, or None."""
    if spec is None:
        return f"The rows were read by the model ({route}), not by a spec: a file this large needs a spec"
    if spec.get("reshape"):
        return "The spec reshapes the table (unpivot): a file this large is read in parts, which a reshape cannot do"
    if spec.get("source", "auto") not in ("auto", "frame") or spec.get("table") is not None or spec.get("lines"):
        return f"The spec reads a {spec.get('source') or 'grid'} source: a file this large is read as a table only"
    return None
