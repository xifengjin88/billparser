"""The vision route (vision.py): a PDF no text-layer route proves is read from its page images as protocol Markdown,
and accepted only when its lines prove to the printed total."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pypdfium2 as pdfium

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from billparse import tree, vision  # noqa: E402
from billparse.docmodel import Cell, Document, Region, Row  # noqa: E402
from billparse.pipeline import Pipeline, Settings  # noqa: E402

PAGE = """Example Cloud Ltd

# Invoice

Invoice number: 220687353

<table>
<tr><th>Service</th><th>Region</th><th>Quantity</th><th>Amount</th></tr>
<tr><td>Amazon Simple Storage Service</td><td>eu-west-1</td><td>702.07</td><td>197,98 €</td></tr>
<tr><td>Amazon Relational Database Service</td><td>ap-northeast-1</td><td>1,166.69</td><td>{rds} €</td></tr>
<tr><td>AWS Lambda</td><td>us-west-2</td><td>745.53</td><td>1.380,65 €</td></tr>
</table>

<table>
<tr><td>Total amount (before tax)</td><td>3.788,57 €</td></tr>
<tr><td>VAT 19%</td><td>719,83 €</td></tr>
<tr><td><strong>TOTAL DUE</strong></td><td><strong>4.508,40 €</strong></td></tr>
</table>
"""


class SeeingLLM:
    """Reads page images: answers with a transcription; any other call fails (a route error)."""

    def __init__(self, markdown: str):
        self.markdown, self.images = markdown, 0

    def complete_images(self, system: str, images: list[bytes], user: str, max_tokens: int = 16000) -> str:
        assert "Document Representation Protocol" in system and images[0][:4] == b"\x89PNG"
        self.images += len(images)
        return self.markdown

    def complete(self, system: str, user: str, max_tokens: int = 4000) -> str:
        raise ValueError("no text answers here")

    def complete_json(self, system: str, user: str, schema: dict, max_tokens: int = 4000) -> dict:
        raise ValueError("no JSON answers here")


def _blank_pdf(path: Path) -> Path:
    """A page with no text layer (as a scan): no text-layer route can read it."""
    pdf = pdfium.PdfDocument.new()
    pdf.new_page(595, 842)
    buf = io.BytesIO()
    pdf.save(buf)
    path.write_bytes(buf.getvalue())
    return path


def _pipe(tmp_path: Path, llm, **kw) -> Pipeline:
    return Pipeline(Settings(store_dir=tmp_path / "store", use_llm=True, use_agent=False, **kw), llm=llm)


def test_markdown_tables_become_rows_the_tree_proves():
    regions = vision.to_regions(PAGE.format(rds="2.209,94"))
    t = tree.read(regions, {})
    rec = t.evidence["reconcile"]
    assert rec["proved"] and rec["amount"] == 4508.4
    assert sorted(line["amount"] for line in t.lines) == [197.98, 719.83, 1380.65, 2209.94]


def test_a_page_read_from_its_image_is_accepted_when_it_proves(tmp_path):
    llm = SeeingLLM(PAGE.format(rds="2.209,94"))
    pipe = _pipe(tmp_path, llm)
    r = pipe.parse_file(_blank_pdf(tmp_path / "scan.pdf"))
    assert r["status"] == "reconciled" and r["spec"]["route"] == "vision", r["checks"]
    assert llm.images == 1
    rows = pipe.store.get_rows(r["sha256"])
    assert round(float(rows["billed_cost"].sum()), 2) == 4508.4
    assert pipe.store.part(r["sha256"], "vision").startswith("Example Cloud Ltd")


def test_a_misread_digit_fails_the_proof_and_is_never_accepted(tmp_path):
    pipe = _pipe(tmp_path, SeeingLLM(PAGE.format(rds="2.209,49")))     # 94 read as 49
    r = pipe.parse_file(_blank_pdf(tmp_path / "scan.pdf"))
    seen = [a for a in r["attempts"] if a["route"] == "vision"]
    assert seen and seen[0]["status"] != "reconciled"
    assert r["status"] == "needs_review"


def test_the_route_is_off_with_its_setting(tmp_path):
    llm = SeeingLLM(PAGE.format(rds="2.209,94"))
    r = _pipe(tmp_path, llm, use_vision=False).parse_file(_blank_pdf(tmp_path / "scan.pdf"))
    assert llm.images == 0 and r["status"] == "unsupported"


def test_only_a_short_pdf_that_prints_a_total_wants_the_vision_route(tmp_path):
    pipe = _pipe(tmp_path, SeeingLLM(""))

    def doc(*lines: str, pages: int = 1, file_type: str = "pdf") -> Document:
        rows = [Row([Cell(t, 30, 500)], i, page=1) for i, t in enumerate(lines)]
        return Document("x.pdf", file_type, "0" * 64,
                        [Region("page", f"page{n + 1}", rows if n == 0 else []) for n in range(pages)])
    assert pipe._wants_vision(doc("IMPORTE TOTAL A PAGAR: 4.508,40 €"))
    assert pipe._wants_vision(doc("合計金額 ¥12,345"))
    assert not pipe._wants_vision(doc("Passwords must be rotated every 90 days."))           # a memo
    assert not pipe._wants_vision(doc("Total due $10.00", pages=vision.MAX_PAGES + 1))     # too long to read
    assert not pipe._wants_vision(doc("Total due $10.00", file_type="xlsx"))
