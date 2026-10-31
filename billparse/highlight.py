"""The original file with every line (or table row) colored by what the parser made of it, as in
invoice-parser-viewer: charges, totals, section headings, facts, gaps, flags; section bars at the left margin."""
from __future__ import annotations

import re
from pathlib import Path

from .schema import Cite, Document

# role -> (fill RGBA, outline RGBA, outline width); the first role of a line in ORDER wins
STYLES = {
    "flagged": ((220, 38, 38, 35), (220, 38, 38, 220), 2),
    "gap": ((147, 51, 234, 40), (147, 51, 234, 220), 2),
    "total": ((234, 88, 12, 40), (234, 88, 12, 220), 2),
    "charge": ((37, 99, 235, 40), (37, 99, 235, 210), 2),
    "not_counted": ((100, 116, 139, 35), (100, 116, 139, 200), 1),
    "section": ((217, 119, 6, 35), (217, 119, 6, 200), 1),
    "fact": ((13, 148, 136, 30), (13, 148, 136, 170), 1),
    "line": ((0, 150, 136, 14), (0, 150, 136, 70), 1),
}
ORDER = ["flagged", "gap", "total", "charge", "not_counted", "section", "fact"]
# table rows: a background per role (the same colors, light)
ROW_COLORS = {"flagged": "#fde2e2", "gap": "#efe3fc", "total": "#fde9d9", "charge": "#e3ecfd",
              "not_counted": "#eceff3", "section": "#fbefd5", "fact": "#dff3f1"}
BAND_COLORS = [(37, 99, 235), (217, 119, 6), (5, 150, 105), (190, 24, 93), (124, 58, 237)]
LEGEND = ("Blue: charge · Orange: printed total · Amber: section heading · Teal: fact · Grey: not counted · "
          "Purple: gap · Red: flagged · Faint green: other text · Left bars: sections (color = depth)")
_LABEL = re.compile(r"^p(\d+) · line (\d+)")


def _key(cite: Cite | None):
    """(page, line) of a text cite, ("table", sheet, row) of a table cite, else None."""
    if cite is None:
        return None
    if cite.table is not None and cite.row is not None:
        return ("table", cite.table, cite.row)
    if cite.page is not None and cite.line is not None:
        return (cite.page, cite.line)
    return None


def roles(doc: Document, meta: dict) -> dict:
    """Every cited line or row -> the set of roles the parser gave it."""
    out: dict = {}

    def mark(cite, role):
        if (k := _key(cite)) is not None:
            out.setdefault(k, set()).add(role)

    for c in doc.charges:
        mark(c.cite, "charge" if c.counted else "not_counted")
    for t in doc.stated_totals:
        mark(t.amount.cite, "total")
    for s in doc.sections:
        if s.name:
            mark(s.name.cite, "section" if s.counted else "not_counted")
    for f in doc.stated_facts:
        mark(f.cite, "fact")
    for g in doc.gaps:
        mark(g.cite, "gap")
    for f in meta.get("flags") or []:
        m = _LABEL.match(str(f.get("cite") or ""))
        if m:
            out.setdefault((int(m.group(1)), int(m.group(2))), set()).add("flagged")
    return out


def style(found: set) -> str:
    return next((r for r in ORDER if r in found), "line")


def section_bands(doc: Document, page: int, line_box: dict) -> list[tuple[int, str, float, float]]:
    """(depth, section name, top, bottom) of each section with lines on this page: its heading and its charges."""
    by_id = {s.id: s for s in doc.sections}

    def depth(s) -> int:
        d, node = 0, s
        while node.parent_id and node.parent_id in by_id and d < 12:
            d, node = d + 1, by_id[node.parent_id]
        return d
    members: dict[str, list] = {}
    for c in doc.charges:
        if c.section_id and (k := _key(c.cite)) and k[0] == page and k[1] in line_box:
            sid = c.section_id
            while sid:                                   # a charge is in its section and every section above
                members.setdefault(sid, []).append(line_box[k[1]])
                sid = by_id[sid].parent_id if sid in by_id else None
    for s in doc.sections:
        if s.name and (k := _key(s.name.cite)) and k[0] == page and k[1] in line_box:
            members.setdefault(s.id, []).append(line_box[k[1]])
    out = []
    for sid, boxes in members.items():
        s = by_id.get(sid)
        if s is not None:
            out.append((depth(s), str(s.name.value) if s.name else sid, min(b[1] for b in boxes), max(b[3] for b in boxes)))
    return sorted(out)


SCALE = 1.6            # page image pixels per PDF point (the web app scrolls to a line with it)
FOCUS = ((255, 221, 0, 85), (220, 38, 38, 255), 4)      # the clicked line: yellow (the text stays readable), thick red border


def render_page(path: Path, page: int, page_lines: list[dict], doc: Document, meta: dict, scale: float = SCALE,
                focus: tuple | None = None):
    """The page as an image: every line boxed in its role's style, section bars, and `focus` (page, line) in yellow."""
    import pypdfium2 as pdfium
    from PIL import Image, ImageDraw, ImageFont

    pdf = pdfium.PdfDocument(str(path))
    try:
        if page - 1 >= len(pdf):
            return None
        image = pdf[page - 1].render(scale=scale).to_pil().convert("RGBA")  # pyright: ignore[reportArgumentType]  # pypdfium2 types scale as int; it takes a float
    finally:
        pdf.close()
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    found = roles(doc, meta)
    line_box = {ln["line"]: ln["bbox"] for ln in page_lines if ln.get("bbox")}
    for d, _name, top, bottom in section_bands(doc, page, line_box):
        color = BAND_COLORS[d % len(BAND_COLORS)]
        x = 4 + d * 7
        draw.rectangle((x, top * scale - 2, x + 4, bottom * scale + 2), fill=(*color, 170))
    try:
        font = ImageFont.load_default(size=max(9, int(6 * scale)))
    except TypeError:                                   # Pillow before 10.1: one size
        font = ImageFont.load_default()
    for line_no, (x0, top, x1, bottom) in line_box.items():
        fill, outline, width = STYLES[style(found.get((page, line_no), set()))]
        focused = focus == (page, line_no)
        if focused:
            fill, outline, width = FOCUS
        draw.rectangle((x0 * scale - 3, top * scale - 2, x1 * scale + 3, bottom * scale + 2),
                       fill=fill, outline=outline, width=width)
        # the line number in the left margin ("p33 · line 14" is found at a glance)
        label = str(line_no)
        tx = max(2, x0 * scale - 8 - draw.textlength(label, font=font))
        draw.text((tx, top * scale - 1), label, font=font,
                  fill=(220, 38, 38, 255) if focused else (100, 116, 139, 230))
        if focused:                                     # a marker in the margin, easy to see when scrolling
            draw.rectangle((0, top * scale - 4, 6, bottom * scale + 4), fill=(220, 38, 38, 255))
    return Image.alpha_composite(image, layer).convert("RGB")


def row_styler(found: dict, sheet: str):
    """A pandas Styler function: each table row's background by its role."""
    def color(row):
        role = style(found.get(("table", sheet, row.name), set()))
        bg = ROW_COLORS.get(role)
        return [f"background-color: {bg}" if bg else ""] * len(row)
    return color
