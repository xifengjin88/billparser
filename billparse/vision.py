"""The vision route (the Document Representation Protocol's page-image path): a short PDF that no text-layer route
proves (text that overlaps in the PDF, a scan) is read from its page images. The model writes each page as Markdown in
the protocol's way (tables as HTML, numbers exactly as printed, nothing dropped or added); the Markdown becomes page
rows, and the tree of sums reads them like any PDF page. The pipeline accepts the reading only when its lines prove to
a printed total, so a misread digit fails the proof and is never accepted silently.

    images = page_images(pdf_bytes, max_pages=5)       # PNG per page, None for a longer document
    md = llm.complete_images(SYSTEM, images, USER)
    regions = to_regions(md)                          # docmodel page regions, for spec.run with the tree spec
"""

from __future__ import annotations

import io
import os
import re
from html.parser import HTMLParser

from .docmodel import Cell, Region, Row

MAX_PAGES = int(os.environ.get("BILLPARSE_VISION_PAGES") or 5)
SCALE = 2.0                                     # 144 dpi: small print stays readable

SYSTEM = """You transcribe document pages into Markdown, following the Document Representation Protocol.
The page images are the ground truth; any text layer is not.
- Transcribe exactly what each page shows: every word and every number, character for character. Never correct,
  round, normalise or complete a value; keep the page's separators and signs ("1.234,56 €", "(1,204.00)", "-4.50").
- Never drop and never invent: every printed line, row, total and footnote appears once; add nothing the page does
  not print (no headers, labels, totals or placeholders).
- Where two strings are printed over each other, write each one whole: a label and its value stay separate.
- Every table (a grid of rows and columns, also one aligned only by whitespace, also a block of label/amount lines
  such as a totals block) is an HTML <table> with <tr>, <th> and <td>; never a pipe table. Keep every column; leave a
  cell empty where the page prints nothing. When row labels are indented to show nesting, give each label cell
  data-level="0" for the leftmost position, "1" for the next indent, and so on. Wrap bold cell text in <strong>.
- Other text is Markdown paragraphs, one printed block each; a heading is "# ..." only for a real title.
- Separate pages with a line holding only ---.
Output only the Markdown, no comment."""

USER = "Transcribe these pages."
# a line that prints a document total, in the languages of the bills seen (with an amount: tree.money_in)
TOTAL_LINE = re.compile(r"(?i)\b(total|totale|gesamt\w*|importe|montant|valor|summe)\b|合計|总额|总计|応付|应付")


def page_images(pdf: bytes, max_pages: int = MAX_PAGES) -> list[bytes] | None:
    """Each page as a PNG; None when the document has more pages than max_pages (the cost grows with each page)."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf)
    try:
        if len(doc) == 0 or len(doc) > max_pages:
            return None
        out = []
        for page in doc:
            buf = io.BytesIO()
            page.render(scale=SCALE).to_pil().save(buf, format="PNG")
            out.append(buf.getvalue())
        return out
    finally:
        doc.close()


# ---------------------------------------------------------------- Markdown -> page rows
LEFT = 30.0            # the stub's left edge
INDENT = 12.0          # one data-level step
COLUMN = 300.0         # the first column after the stub
STEP = 100.0           # each next column
LINE = 14.0            # the height of a line (a row's top)


class _Table(HTMLParser):
    """An HTML table's rows: each a list of (text, bold, header cell, data-level, colspan)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[tuple[str, bool, bool, int, int]]] = []
        self._cell: dict | None = None
        self._strong = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        a = dict(attrs)
        if tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th"):
            level = a.get("data-level")
            self._cell = {"text": [], "bold": False, "th": tag == "th",
                          "level": int(level) if level and level.isdigit() else 0,
                          "span": int(a["colspan"]) if (a.get("colspan") or "").isdigit() else 1}
        elif tag in ("strong", "b"):
            self._strong += 1
        elif tag == "br" and self._cell is not None:
            self._cell["text"].append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("strong", "b"):
            self._strong = max(0, self._strong - 1)
        elif tag in ("td", "th") and self._cell is not None:
            if not self.rows:
                self.rows.append([])
            c = self._cell
            self.rows[-1].append((re.sub(r"\s+", " ", "".join(c["text"])).strip(), c["bold"], c["th"], c["level"],
                                  c["span"]))
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell["text"].append(data)
            if self._strong and data.strip():
                self._cell["bold"] = True


def _md_text(line: str) -> tuple[str, bool]:
    """A Markdown line as plain text, and whether it is bold (a heading or a wholly bold line)."""
    s = line.strip()
    heading = s.startswith("#")
    s = re.sub(r"^#+\s*", "", s)
    s = re.sub(r"^[*-]\s+", "", s)                      # a list marker
    bold = heading or bool(re.fullmatch(r"\*\*.+\*\*", s))
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"<[^>]+>", "", s)                       # inline HTML (<u>, <sup>, ...)
    s = s.replace("\\", "")
    return s.strip(), bold


def _add(rows: list[Row], cells: list[Cell], page: int) -> None:
    rows.append(Row(cells, len(rows), top=LINE * (len(rows) + 1), page=page))


def to_regions(markdown: str) -> list[Region]:
    """The Markdown of a document's pages as page regions: a table row is a row of cells (the stub at its
    data-level's indent, each other column at its own position); any other line is a row of one cell."""
    pages = re.split(r"(?m)^\s*---\s*$", markdown)
    regions = []
    for n, text in enumerate(pages, start=1):
        rows: list[Row] = []
        for block in re.split(r"(?is)(<table.*?</table>)", text):
            if block.lower().startswith("<table"):
                t = _Table()
                t.feed(block)
                for r in t.rows:
                    cells, col = [], 0
                    for k, (txt, bold, _th, level, span) in enumerate(r):
                        x0 = LEFT + INDENT * level if k == 0 else COLUMN + STEP * (col - 1)
                        if txt:
                            cells.append(Cell(txt, x0, x0 + STEP - 4, bold))
                        col += span
                    if cells:
                        _add(rows, cells, n)
                continue
            for line in block.splitlines():
                if not line.strip() or line.strip().startswith("<!--"):
                    continue
                txt, bold = _md_text(line)
                if txt:
                    _add(rows, [Cell(txt, LEFT, LEFT + 500, bold)], n)
        if rows:
            regions.append(Region(kind="page", name=f"page{n}", rows=rows))
    return regions
