"""The tree of sums: read a bill's rows as totals, subtotals and line items by their amounts.

Bills print one structure in many layouts: a total, made of subtotals, made of line items, often several times (a
summary, a detail, an allocation by account). This module finds the structure from the amounts, not from words like
"Total", so it works on layouts nobody wrote a rule for.

1. records    Each row with an amount is a record (main amount, label, x position, style). Text rows next to an
              amount row with no words (a product name above its numbers, a period below) join it. Text that
              repeats at the top or bottom of many pages is page furniture and is dropped.
2. forests    A text row that comes when no group is open starts a section. In a section, a row is a child of the
              open row above it when it is indented the same or deeper and its amount fits in what that row still
              lacks. A row whose children add up to it is a subtotal; a group that cannot be completed keeps
              its children and gets a difference line. A row equal to the sum of the rows just before it is a
              total over them.
3. classes    Rows that stand for the same money are one class: the same label and amount printed twice, a total
              over one row, a section that adds up to an earlier row (its breakdown), 2-3 top rows that add up to
              a total.
4. lines      From the largest class, each class is broken down by its breakdown with the most leaves; the leaves
              are the line items, the classes above them their context (account, service, region).
5. evidence   Breakdowns that add up, groups that do not (with their rows), classes with two breakdowns that
              agree, and amounts that nothing explains.

Everything is deterministic. Options (a spec's "tree" key) change the choices; see TreeOptions.
"""

from __future__ import annotations

import itertools
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .docmodel import Region

CURRENCIES = ("USD", "CAD", "EUR", "GBP", "INR", "AUD", "JPY", "NZD", "SGD", "CHF", "BRL", "MXN", "ZAR", "SEK",
              "NOK", "DKK", "HKD", "CNY", "KRW")
SYMBOL = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "¥": "JPY"}
_CUR = "|".join(CURRENCIES)
# an amount: an optional currency, a number with 2 decimals, an optional sign or brackets; never part of a longer
# number, a percent, a date or a version
MONEY = re.compile(rf"(?<![\w.,/:-])(?P<neg>[-(])?\s?(?P<cur>{_CUR})?\s?(?P<sym>[$€£₹¥])?\s?(?P<neg2>-)?\s?"
                   rf"(?P<num>\d{{1,3}}(?:,\d{{3}})+\.\d{{2}}|\d+\.\d{{2}})(?P<close>\))?(?![\d.,]*\d)(?!\s?%)(?![-/]\d)")
DATE_RANGE = re.compile(r"\b\d{1,2}[-/ ][A-Za-z]{3}[-/ ]\d{2,4}\s*-\s*\d{1,2}[-/ ][A-Za-z]{3}[-/ ]\d{2,4}\b|"
                        r"\b\d{4}[-/]\d{2}[-/]\d{2}\s*-\s*\d{4}[-/]\d{2}[-/]\d{2}\b|"
                        r"\b[A-Z][a-z]{2} \d{1,2} ?-\s?[A-Z][a-z]{2} \d{1,2},? \d{4}\b")
NOISE = re.compile(r"^(page\s*)?\d+\s*(/|of)\s*\d+$|^\d{1,4}$", re.I)
ZERO = 0.005


@dataclass
class TreeOptions:
    """A layout's choices, set by its spec (written by a person or by the agent). The defaults are neutral: no word
    lists, so a layout that needs one gets it from its spec."""
    currency: str | None = None             # the currency of the main amounts (default: the most common one)
    amount: str = "last"                    # which amount of a row is its main amount: "last" | "first"
    trailer: str | None = None              # regex: text rows after an amount row that still belong to it (a period)
    ignore: str | None = None               # regex: rows to leave out entirely
    skip_from: str | None = None            # regex: the first row it matches and every row after it are left out
                                            # (a later part that prints the same money again, by another dimension)
    section: str | None = None              # regex: text rows that always start a section
    auto_sections: bool = True              # a text row right before an amount row starts a section when no group is open
    repeated_header: str | None = None      # regex: a column header printed again above items, dropped when seen 3+ times
    informational: str | None = None        # regex: rows that are information, not money ("Highest service spend")
    furniture_share: float = 0.6            # text at a page's top or bottom on this share of the pages is furniture
    furniture_top: int = 3                  # ... within this many rows from the top
    furniture_bottom: int = 12              # ... or this many rows from the bottom
    heading_bold: bool = True               # a bold row can head rows at its own indent
    heading_max_amounts: int = 1            # ... if it has at most this many amounts
    table_row_amounts: int = 3              # a row with this many amounts is a table row: it heads nothing
    min_total: float = 1.0                  # smallest amount that can be a total over the rows before it
    link_runs: bool = True                  # runs of sections that add up to a printed row break it down
    max_combo: int = 3                      # largest set of top rows tried as the breakdown of a total
    blocks: bool = False                    # a charge is the rows up to its trailer row (its period): its text rows
                                            # are its name (in order), a part number its code; a column header row
                                            # starts a new block (a Microsoft invoice by product)

    @classmethod
    def from_spec(cls, d: dict | None) -> TreeOptions:
        problems = check_options(d)
        if problems:
            raise ValueError("; ".join(problems))
        return cls(**(d or {}))


def check_options(d: dict | None) -> list[str]:
    """What is wrong with a spec's tree options (unknown names, wrong types, bad regexes), for the spec checker."""
    out = []
    fields = TreeOptions.__dataclass_fields__
    for k, v in (d or {}).items():
        if k not in fields:
            out.append(f"Unknown tree option {k!r}; known: {', '.join(sorted(fields))}")
            continue
        kind = fields[k].type
        if "str" in str(kind) and v is not None and not isinstance(v, str):
            out.append(f"tree.{k} must be a string")
        elif kind in ("bool",) and not isinstance(v, bool):
            out.append(f"tree.{k} must be true or false")
        elif kind in ("int", "float") and (isinstance(v, bool) or not isinstance(v, (int, float))):
            out.append(f"tree.{k} must be a number")
        if k in ("trailer", "ignore", "skip_from", "section", "repeated_header", "informational", "currency") and isinstance(v, str):
            try:
                re.compile(v)
            except re.error as e:
                out.append(f"tree.{k} is not a valid regex: {e}")
    return out


@dataclass(eq=False)
class Record:
    i: int
    region: str
    row: int                                # the row index of the amount row (page line or sheet row)
    page: int
    x: float
    bold: bool
    label: str
    amount: float | None                    # None: a text row
    amounts: list[float] = field(default_factory=list)
    currency: str | None = None
    extra: list[str] = field(default_factory=list)   # text rows joined to this record
    cells: list[str] = field(default_factory=list)   # the row's cells as printed (a PDF row's text blocks)
    columns: dict[str, str] = field(default_factory=dict)   # cell text by the column header printed above it
    other: list[tuple[float, str]] = field(default_factory=list)   # the same row's amounts in another currency
    children: list[Record] = field(default_factory=list)
    parent: Record | None = None
    views: list[list[Record]] = field(default_factory=list)   # breakdowns found outside its own children
    section: int = 0
    role: str = ""                          # leaf | subtotal | total | text | unbalanced | gap
    gap: Record | None = None               # an unbalanced group's difference line

    @property
    def src(self) -> str:
        return f"{self.region}:{self.row}"

    @property
    def acc(self) -> float:
        return sum(c.amount or 0.0 for c in self.children)

    @property
    def value(self) -> float:
        return self.amount or 0.0


@dataclass
class Section:
    i: int
    title: str
    roots: list[Record] = field(default_factory=list)


@dataclass
class Tree:
    records: list[Record]
    sections: list[Section]
    lines: list[dict]                       # the line items with their context
    evidence: dict
    currency: str | None
    facts: dict[str, dict] = field(default_factory=dict)   # header fields: {name: {"value", "src"}}


# ---------------------------------------------------------------- amounts
BETWEEN_WORDS = re.compile(rf"^\s+(?!(?:{_CUR})\b)[A-Za-z]{{2,}}")


def money_in(text: str) -> list[tuple[float, str | None, int, int]]:
    """Every amount in a text: (value, currency or None, start, end). A bare number between words is not an amount
    (a version: "Ubuntu 22.04 LTS")."""
    out = []
    for m in MONEY.finditer(text):
        bare = not (m["cur"] or m["sym"])
        if bare and re.search(r"[A-Za-z]\s*$", text[:m.start()]) and BETWEEN_WORDS.match(text[m.end():]):
            continue
        v = float(m["num"].replace(",", ""))
        if m["neg"] or m["neg2"]:
            v = -v
        out.append((v, m["cur"] or SYMBOL.get(m["sym"] or ""), m.start(), m.end()))
    return out


def tol(n: int = 1, amount: float = 0.0) -> float:
    """How far a sum may be from its printed total: rounding of the parts (errors of up to half a cent each, which
    grow with the square root of their number), at most 0.50, plus a millionth of the amount."""
    return min(0.011 + 0.006 * max(0, n - 1) ** 0.5, 0.5) + 1e-6 * abs(amount)


def close(a: float, b: float, n: int = 1) -> bool:
    return abs(a - b) <= tol(n, max(abs(a), abs(b)))


def bound(n: int) -> float:
    """The most that n printed parts can differ from their printed total by rounding alone: each part and the total
    are off by at most half a cent. A larger difference is not rounding."""
    return 0.005 * (n + 1) + 1e-9


# ---------------------------------------------------------------- 1. records
def furniture(regions: list[Region], opt: TreeOptions) -> set[str]:
    """Page headers and footers: text (digits masked) with no amount near the top or bottom of a share of the pages
    (options furniture_*; at least 3 pages). A section title repeats too, but only on the pages where its section
    starts, so it stays below the share."""
    seen: dict[str, set[int]] = defaultdict(set)
    pages = [r for r in regions if not r.is_frame and r.kind == "page" and r.rows]
    for p, reg in enumerate(pages):
        n = len(reg.rows)
        for j, row in enumerate(reg.rows):
            if (opt.furniture_top <= j < n - opt.furniture_bottom) or money_in(row.text):
                continue
            if opt.blocks and (CODE.match(row.text.strip()) or DATE_RANGE.fullmatch(row.text.strip())):
                continue                                # a charge's part number and period are content, not furniture
            key = _mask(row.text)
            if key:
                seen[key].add(p)
    need = max(3, int(opt.furniture_share * len(pages) + 0.5))
    return {k for k, ps in seen.items() if len(ps) >= need}


def _mask(text: str) -> str:
    return _key(re.sub(r"\d", "0", text))


def records_from(regions: list[Region], opt: TreeOptions) -> tuple[list[Record], str | None]:
    ignore = re.compile(opt.ignore, re.I) if opt.ignore else None
    header = re.compile(opt.repeated_header, re.I) if opt.repeated_header else None
    furn = furniture(regions, opt)
    rows = [(reg, row, row.text.strip()) for reg in regions if not reg.is_frame and not reg.scanned for row in reg.rows]
    header_of = _headers(rows)
    if opt.skip_from:
        skip = re.compile(opt.skip_from, re.I)
        cut = next((i for i, (_, _, t) in enumerate(rows) if skip.search(t)), None)
        rows = rows if cut is None else rows[:cut]
    # a column header printed again above every item (the spec says what it looks like) is not content
    seen = Counter(_mask(t) for _, _, t in rows if t and not money_in(t))
    repeated = {k for k, n in seen.items() if n >= 3}
    parsed = []
    for reg, row, text in rows:
        if not text or NOISE.match(text) or (ignore and ignore.search(text)) or _mask(text) in furn:
            continue
        if header and _mask(text) in repeated and header.search(text):
            continue
        parsed.append((reg, row, text, money_in(text)))
    # the main currency: the most common one among rows whose amounts are all in one currency
    counts = Counter(f[0][1] for *_, f in parsed if f and f[0][1] and len({x[1] for x in f}) == 1)
    currency = opt.currency or (counts.most_common(1)[0][0] if counts else None)
    out: list[Record] = []
    for reg, row, text, found in parsed:
        use = [f for f in found if currency is None or f[1] in (currency, None)]
        if use:
            pick = use[-1] if opt.amount == "last" else use[0]
            # the label: the text without the main amount (and without other amounts at the end of the row, like
            # the same amount in another currency); a price inside the text stays in it
            cut = min([pick[2]] + [f[2] for f in found if f[2] > pick[2]])
            label = (text[:cut] + " " + text[pick[3]:] if cut == pick[2] else text[:cut]).strip(" :-–|")
            for _, cur, a, b in found:                  # the same amount in another currency is not the label
                if currency and cur and cur != currency:
                    label = label.replace(text[a:b].strip(), " ")
            label = re.sub(r"\s{2,}", " ", label)
            label = re.sub(rf"\s*\b({_CUR})\s*$", "", label).strip(" :-–|")
            rec = Record(0, reg.name, row.index, row.page, row.first_x, row.bold, label, pick[0],
                         [v for v, *_ in use], currency=pick[1] or currency)
        else:
            rec = Record(0, reg.name, row.index, row.page, row.first_x, row.bold, text, None)
        rec.cells = [c.text for c in row.cells if c.text]
        rec.other = [(v, cur) for v, cur, *_ in found if currency and cur and cur != currency]
        rec.columns = _columns(row, header_of.get(id(row)))
        out.append(rec)
    return _join_text(out, opt), currency


def _header_like(row) -> bool:
    """A row of column names: 2+ cells of a few words each, no amount and no number."""
    cells = [c for c in row.cells if c.text]
    return len(cells) >= 2 and all(len(c.text.split()) <= 8 and not re.search(r"\d", c.text) for c in cells) \
        and not money_in(row.text)


def _headers(rows: list) -> dict[int, object]:
    """The column header row each row is printed under: the last header before it (a table that goes on over a page
    break keeps its header). A header printed on several rows ("Unit" / "Price" / "(USD)") is one header: its rows
    are joined column by column (_merge_header)."""
    out, current, block = {}, None, []
    for k, (_, row, _) in enumerate(rows):
        if _header_like(row) or (block and _header_word(row) and _merge_target(row, block[-1])):
            if block and (rows[k - 1][1] is not block[-1] or not _next_line(block[-1], row)):
                block = []
            block.append(row)
            current = _merge_header(block) if len(block) > 1 else row
            continue
        nxt = rows[k + 1][1] if k + 1 < len(rows) else None
        if _header_word(row) and nxt is not None and _header_like(nxt) and _merge_target(row, nxt) \
                and _next_line(row, nxt):
            block = [row]                               # a one-word row above a header's numeric column ("Total")
            continue
        block = []
        if current is not None:
            out[id(row)] = current
    return out


HEADER_LINE_GAP = 14.0                                  # points: the rows of one header are one text line apart


def _next_line(a, b) -> bool:
    """b is printed on the text line just below a (a header's next row, not a title above the table). Rows with no
    page position (a sheet) count as next."""
    if not (a.top or b.top) or a.page != b.page:
        return not (a.top or b.top)
    return 0 < b.top - a.top <= HEADER_LINE_GAP


def _header_word(row) -> bool:
    cells = [c for c in row.cells if c.text]
    return len(cells) == 1 and len(cells[0].text.split()) <= 2 and not re.search(r"\d", cells[0].text)


def _merge_target(row, header) -> bool:
    """A one-word header row sits over a column of the header that is not its first (not a section title)."""
    heads = sorted((c for c in header.cells if c.text), key=lambda c: c.x0)
    cell = next(c for c in row.cells if c.text)
    if len(heads) < 2:
        return False
    nearest = min(heads, key=lambda h: abs(h.xc - cell.xc))
    return nearest is not heads[0] and abs(nearest.xc - cell.xc) <= 60


def _merge_header(block: list):
    """The rows of one header as one row: each cell joins the column of the widest row nearest its center, top to
    bottom ("Total" + "(including" + "Tax)" + "(USD)" -> "Total (including Tax) (USD)")."""
    from .docmodel import Cell, Row
    widest = max(block, key=lambda r: len([c for c in r.cells if c.text]))
    anchors = [c for c in widest.cells if c.text]
    parts: dict[int, list[str]] = {i: [] for i in range(len(anchors))}
    for r in block:
        for c in (c for c in r.cells if c.text):
            i = min(range(len(anchors)), key=lambda i: abs(anchors[i].xc - c.xc))
            parts[i].append(c.text)
    cells = [Cell(" ".join(parts[i]), a.x0, a.x1, a.bold) for i, a in enumerate(anchors)]
    return Row(cells, widest.index, widest.top, widest.page)


def _columns(row, header) -> dict[str, str]:
    """The row's cells by column name: each cell under the header cell nearest its center, when every cell has its
    own header cell (else the header is another table's)."""
    if header is None:
        return {}
    # a rate ("10.20%") is printed under no column name of its own: it is not an amount
    cells = [c for c in row.cells if c.text and not re.fullmatch(r"\s*-?\d+(?:\.\d+)?\s*%\s*", c.text)]
    heads = [c for c in header.cells if c.text]
    if len(cells) < 2 or len(cells) > len(heads):
        return {}
    out: dict[str, str] = {}
    for c in cells:
        h = min(heads, key=lambda h: abs(h.xc - c.xc))
        if h.text in out or (min(h.x1, c.x1) - max(h.x0, c.x0) <= 0 and abs(h.xc - c.xc) > 80):
            return {}
        out[h.text] = c.text
    return out


RATE = re.compile(rf"\b1\s*(?:{_CUR})\s*=\s*\d+(?:\.\d+)?\s*(?:{_CUR})\b")
PERIOD_SENTENCE = re.compile(r"\bbilling period (?:of |is |from )?(?P<a>\d{4}[/-]\d{2}[/-]\d{2})\s*(?:-|to)\s*"
                             r"(?P<b>\d{4}[/-]\d{2}[/-]\d{2})", re.I)


def _field_name(text: str) -> str:
    """'Invoice Number: / Numéro de facture :' -> 'Invoice Number' (the first language, no colon)."""
    first = text.split(" / ")[0] if " / " in text else text
    return first.strip().rstrip(":").strip()


def facts_from(regions: list[Region]) -> dict[str, dict]:
    """Header fields printed as a row of names over one row of values ("Billing period | Account ID" over
    "Apr 1 - Apr 30, 2025 | 572918748930"): {name: {"value", "src"}}. The first printing of a name wins."""
    out: dict[str, dict] = {}
    for reg in regions:
        if reg.is_frame or reg.scanned:
            continue
        for i, row in enumerate(reg.rows):
            text, src = row.text, f"{reg.name}:{row.index}"
            if (m := RATE.search(text)):
                out.setdefault("Exchange rate", {"value": m.group(0), "src": src})
            if (m := PERIOD_SENTENCE.search(text)):
                out.setdefault("Billing period", {"value": f"{m['a']} - {m['b']}", "src": src})
            cells = [c.text for c in row.cells if c.text]
            # "Invoice Number: | CAIN25-2511535", or "Account number :" over "820242939662"
            if cells and (cells[0].rstrip().endswith(":") or cells[0].split(" / ")[0].rstrip().endswith(":")):
                name = _field_name(cells[0])
                if len(cells) == 2 and not cells[1].rstrip().endswith(":"):
                    out.setdefault(name, {"value": cells[1], "src": src})
                elif len(cells) == 1 and i + 1 < len(reg.rows):
                    nxt = [c.text for c in reg.rows[i + 1].cells if c.text]
                    if len(nxt) == 1 and not nxt[0].rstrip().endswith(":") and len(nxt[0].split()) <= 4:
                        out.setdefault(name, {"value": nxt[0], "src": f"{reg.name}:{reg.rows[i + 1].index}"})
        for a, b in zip(reg.rows, reg.rows[1:]):
            if not _header_like(a) or _header_like(b) or money_in(b.text):
                continue
            names, values = [c for c in a.cells if c.text], [c for c in b.cells if c.text]
            # "Currency: / Devise :" over "Account Number:": labels of label-value pairs, not names over values
            if len(names) != len(values) or any(c.text.rstrip().endswith(":") for c in names + values):
                continue
            for n, v in zip(names, values):
                if min(n.x1, v.x1) - max(n.x0, v.x0) > 0 or abs(n.xc - v.xc) <= 40:
                    out.setdefault(n.text, {"value": v.text, "src": f"{reg.name}:{b.index}"})
    return out


CODE = re.compile(r"^[A-Z0-9]{2,6}-[A-Z0-9]{3,8}$")      # a part number printed on its own row ("AAA-21874")


def _header_rec(r: Record) -> bool:
    """A text row of column names: 2+ cells of a few words each and no digits."""
    return r.amount is None and len(r.cells) >= 2 and \
        all(len(c.split()) <= 5 and not re.search(r"\d", c) for c in r.cells)


def _join_blocks(recs: list[Record], trailer: re.Pattern) -> list[Record]:
    """tree.blocks: each charge is the rows up to and with its trailer (its period). A block with one amount row is
    one record: its other text rows are its name in their order (a wrapped name before and after the numbers), a
    part number goes to its extra as its code, the period to its extra. Other blocks stay as they are."""
    out: list[Record] = []
    block: list[Record] = []

    def flush(closed: bool) -> None:
        amounts = [r for r in block if r.amount is not None]
        if not closed or len(amounts) != 1:
            out.extend(block)
            return
        rec, names, xs, bold = amounts[0], [], [], False
        for r in block:
            if r is rec:
                for c in r.cells:                       # a part number printed with the numbers is its code
                    if CODE.match(c.strip()):
                        rec.extra.append(f"Part number: {c.strip()}")
                words = [c for c in r.cells if re.search(r"[A-Za-z]", c) and not money_in(c) and not CODE.match(c.strip())]
                names += words
                if words:
                    xs.append(r.x)
                    bold = bold or r.bold
            elif re.fullmatch(rf"\s*(?:{trailer.pattern})\s*", r.label, re.I):
                rec.extra.append(r.label.strip())
            elif CODE.match(r.label.strip()):
                rec.extra.append(f"Part number: {r.label.strip()}")
            else:
                names.append(r.label.strip())
                xs.append(r.x)
                bold = bold or r.bold
        if names:
            rec.label = " ".join(n for n in names if n)
            rec.x, rec.bold = min(xs), bold
        out.append(rec)

    for r in recs:
        if _header_rec(r):
            flush(False)
            block = []
            out.append(r)
            continue
        block.append(r)
        if r.amount is None and re.fullmatch(rf"\s*(?:{trailer.pattern})\s*", r.label, re.I):
            flush(True)
            block = []
    flush(False)
    return out


def _join_text(recs: list[Record], opt: TreeOptions) -> list[Record]:
    """An amount row with no words takes its name from the text rows above it; text rows that end with the
    trailer (by default a period) before that stay with the record above."""
    trailer = re.compile(opt.trailer, re.I) if opt.trailer else DATE_RANGE
    if opt.blocks:
        recs = _join_blocks(recs, trailer)
        for i, r in enumerate(recs):
            r.i = i
        return recs
    out: list[Record] = []
    pending: list[Record] = []
    for r in recs:
        if r.amount is None:
            pending.append(r)
            continue
        if not re.search(r"[A-Za-z]", r.label) and pending:
            cut = 0
            if out and out[-1].amount is not None:
                for k, t in enumerate(pending):
                    if trailer.search(t.label):
                        cut = k + 1
                out[-1].extra += [t.label for t in pending[:cut]]
            names = pending[cut:]
            # the name is the last text rows at one indent; titles above them (at another indent) stay text
            k = len(names)
            while k > 0 and abs(names[k - 1].x - names[-1].x) <= 2.0:
                k -= 1
            keep, names = names[:k], names[k:]
            out.extend(keep)
            if not names and not keep and out and out[-1].amount is not None and pending[:cut] \
                    and re.search(r"[A-Za-z]", out[-1].label):
                r.label, r.x, r.bold = out[-1].label, out[-1].x, out[-1].bold   # the next charge of one product
            if names:
                if r.label.strip():
                    r.extra.append(r.label.strip())         # a period or a code printed with the numbers
                r.label = " ".join(t.label for t in names)
                r.x, r.bold = min(t.x for t in names), any(t.bold for t in names)
            pending = []
        elif pending and out and out[-1].amount is not None and pending[0].amount is None \
                and trailer.search(pending[0].label):
            out[-1].extra.append(pending.pop(0).label)       # a period line right after a record
        out.extend(pending)
        pending = []
        out.append(r)
    out.extend(pending)
    # an amount row with no words has no reliable indent (its label is missing from the text layer): place it
    # between the row above and the row below, so it heads the rows below it
    for j, r in enumerate(out):
        if r.amount is not None and not re.search(r"[A-Za-z]", r.label):
            prev = next((x for x in reversed(out[:j]) if x.amount is not None and re.search(r"[A-Za-z]", x.label)), None)
            nxt = next((x for x in out[j + 1:] if x.amount is not None), None)
            if prev is not None and nxt is not None:
                r.x = (prev.x + nxt.x) / 2 if nxt.x > prev.x + 4.0 else nxt.x
            r.label = r.label or "(no label)"
    for i, r in enumerate(out):
        r.i = i
    return out


# ---------------------------------------------------------------- 2. forests
def build(recs: list[Record], opt: TreeOptions) -> list[Section]:
    section_rx = re.compile(opt.section, re.I) if opt.section else None
    sections = [Section(0, "")]
    stack: list[Record] = []
    sequence: list[tuple[Section, Record]] = []         # every root so far, in order, with its section
    heads = _exact_heads(recs)

    def settle() -> None:
        while stack:
            _finish(stack.pop(), sections[-1])

    for j, r in enumerate(recs):
        if r.amount is None:
            r.role = "text"
            open_group = any(s.children and not _complete(s) for s in stack)
            # a title has its first row right after it, on the same page
            nxt = recs[j + 1] if j + 1 < len(recs) else None
            titled = nxt is not None and nxt.amount is not None and nxt.region == r.region and r.x <= nxt.x + 2.0
            if (section_rx and section_rx.search(r.label)) or (opt.auto_sections and titled and not open_group):
                settle()
                sections.append(Section(len(sections), r.label))
            r.section = sections[-1].i
            continue
        r.section = sections[-1].i
        while stack:
            top = stack[-1]
            if _fits(r, top, opt, heads):
                break
            _finish(stack.pop(), sections[-1])
        if stack:
            r.parent = stack[-1]
            stack[-1].children.append(r)
        else:
            run = _run_before(r, sequence, opt.min_total)
            if run:                                     # a total over the rows just before it, in any section
                r.views.append([x for _, x in run])
                r.role = "total"
                gone = {id(x) for _, x in run}
                for sec, _ in run:
                    sec.roots = [x for x in sec.roots if id(x) not in gone]
                del sequence[-len(run):]
                sections[-1].roots.append(r)
                sequence.append((sections[-1], r))
                continue                                # a total takes no children
            sections[-1].roots.append(r)
            sequence.append((sections[-1], r))
        stack.append(r)
    settle()
    # a table that goes on over a page break repeats its header: consecutive sections with the same title are one
    merged: list[Section] = []
    for s in sections:
        if merged and s.title and _mask(s.title) == _mask(merged[-1].title) and merged[-1].roots and s.roots \
                and abs(s.roots[0].x - merged[-1].roots[-1].x) <= 2.0:
            for r in _walk(s.roots):
                r.section = merged[-1].i
            merged[-1].roots += s.roots
            continue
        merged.append(s)
    return [s for s in merged if s.roots]


def _complete(r: Record) -> bool:
    if not r.children:
        return False
    if abs(r.value) < ZERO:                             # a $0.00 heading over $0.00 rows, or over rows that cancel
        return all(abs(k.value) < ZERO for k in r.children) or abs(r.acc) < ZERO
    return close(r.acc, r.value, len(r.children))


def _exact_heads(recs: list[Record]) -> set[int]:
    """Rows that the rows right after them add up to, to the cent: the rows at the indent of the next row (deeper
    rows are their parts), until the sum reaches the row. Such a row is a heading, even when its group above is
    already complete within rounding. A $0.00 row is a heading when the rows indented under it cancel out to $0.00
    (a service whose charges a discount or credits cancel: "Amazon Bedrock USD 0.00" over "Charges 0.01" and
    "Discount -0.01")."""
    out: set[int] = set()
    for j, r in enumerate(recs):
        if r.amount is None or j + 1 >= len(recs) or recs[j + 1].amount is None:
            continue
        if abs(r.value) < ZERO:
            if _cancel_under(r, recs[j + 1:j + 400]):
                out.add(id(r))
            continue
        x1, s = recs[j + 1].x, 0.0
        for k in recs[j + 1:j + 400]:
            if k.amount is None:
                continue
            if k.x < x1 - 2.0:
                break
            if abs(k.x - x1) > 2.0:
                continue
            s += k.value
            if abs(s - r.value) < ZERO:
                out.add(id(r))
                break
            if abs(s) > abs(r.value) + ZERO:
                break
    return out


def _cancel_under(r: Record, after: list[Record]) -> bool:
    """The rows indented under r, at the indent of the first one, add up to $0.00 and are not all $0.00."""
    first = next((k for k in after if k.amount is not None), None)
    if first is None or first.x <= r.x + 2.0 or first.region != r.region:
        return False
    total, nonzero = 0.0, False
    for k in after:
        if k.amount is None:
            continue
        if k.x <= r.x + 2.0 or k.region != r.region:
            break
        if abs(k.x - first.x) <= 2.0:
            total += k.value
            nonzero = nonzero or abs(k.value) >= ZERO
    return nonzero and abs(total) < ZERO


def _fits(r: Record, top: Record, opt: TreeOptions, heads: set[int] = frozenset()) -> bool:  # type: ignore[assignment]
    """r may be a child of top: top has an amount, r is indented the same or deeper, and r's amount fits in what
    top still lacks."""
    if r.x < top.x - 2.0 or len(top.amounts) >= opt.table_row_amounts:
        return False                                    # a table row (several amounts) never heads a group
    if abs(top.value) < ZERO:
        if id(top) in heads:                            # a $0.00 heading whose rows cancel out: every row under it
            return r.x > top.x + 2.0
        return abs(r.value) < ZERO and (r.x > top.x + 2.0 or     # else only $0.00 rows indented under it (the
                                        bool(top.children) and r.x >= top.children[-1].x - 2.0)   # sums cannot tell more)
    if top.children and _complete(top):
        if abs(r.value) < ZERO:
            return r.x >= top.children[-1].x - 2.0      # a zero row after a complete group
        # complete within rounding only: a row that brings the sum nearer is still its part when it is indented
        # under top, or when top is a heading that the rows after it add up to exactly (regions at a service's indent)
        nearer = abs(top.acc + r.value - top.value) < abs(top.acc - top.value) - ZERO
        return nearer and (r.x > top.x + 2.0 or (id(top) in heads and abs(r.x - top.x) <= 2.0))
    same = abs(r.x - top.x) <= 2.0
    heading = opt.heading_bold and top.bold and len(top.amounts) <= opt.heading_max_amounts
    if same and not top.children and (not heading or abs(r.value) > abs(top.value) + tol()):
        return False                                    # the same indent: a sibling, unless top is a bold heading
    if not top.children and r.x <= top.x + 2.0 and top.parent is not None and _complete(top.parent) \
            and top.parent.children[-1] is top \
            and (id(top) not in heads or abs(top.parent.acc - top.parent.value) < ZERO):
        return False                                    # top completed its group: it is a leaf (unless the group
        #                                                 is complete within rounding only and the rows after top
        #                                                 add up to it exactly: then top heads them)
    if abs(r.value) < ZERO:
        return True
    if r.x > top.x + 2.0:
        return True                                     # clearly indented under an open group: its child, even if
        #                                                 it passes the total (a discount can follow)
    room = top.value - top.acc
    if (r.value > 0) != (room > 0):
        return same and heading                         # a credit or a negative region under a bold heading
    return abs(r.value) <= abs(room) + tol(len(top.children) + 1, top.value)


def _finish(r: Record, sec: Section) -> None:
    """r leaves the stack: a leaf, a subtotal that adds up, or a group that does not add up. When the children have
    r's own style, r was a leaf that took its next siblings: they become siblings again. Else r is a real group
    that does not add up: it keeps its children and gets a difference line, so the levels above still add up and
    the evidence names it."""
    if not r.children:
        r.role = r.role or "leaf"
        return
    if _complete(r):
        r.role = "subtotal"
        return
    if any(abs(k.x - r.x) > 2.0 or k.bold != r.bold for k in r.children):
        r.role = "unbalanced"
        r.gap = Record(-1, r.region, r.row, r.page, r.x + 1.0, False,
                       f"Difference: the lines under {r.label[:60]!r} do not add up to it", round(r.value - r.acc, 2))
        r.gap.role, r.gap.parent = "gap", r
        return
    kids, r.children = r.children, []
    r.role = "leaf"
    holder = r.parent.children if r.parent else sec.roots
    at = holder.index(r) + 1 if r in holder else len(holder)
    for k in kids:
        k.parent = r.parent
    holder[at:at] = kids


def _run_before(r: Record, sequence: list[tuple[Section, Record]], min_total: float = 1.0) -> list[tuple[Section, Record]] | None:
    """The shortest run of roots just before r (up to 2,000, across sections) that adds up to r: then r is a total
    over them. A total is less indented than its parts (or, over several parts, at their indent), and is at least
    min_total (tiny amounts prove nothing)."""
    if abs(r.value) < min_total or not sequence:
        return None
    acc, least = 0.0, float("inf")
    for k in range(1, min(2000, len(sequence)) + 1):
        x = sequence[-k][1]
        acc += x.value
        least = min(least, x.x)
        placed = r.x < least - 2.0 or (k >= 2 and r.x <= least + 2.0)
        if placed and abs(x.value) >= ZERO and close(acc, r.value, k):
            return sequence[-k:]
    return None


def _walk(rs: list[Record]):
    for r in rs:
        yield r
        yield from _walk(r.children)
        if r.gap is not None:
            yield r.gap
        for v in r.views:
            yield from _walk(v)


# ---------------------------------------------------------------- 3. classes
def _key(label: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", label.lower())).strip()


class Classes:
    """Union-find over records that stand for the same money."""

    def __init__(self, recs: list[Record]):
        self.up = {id(r): id(r) for r in recs}
        self.rec = {id(r): r for r in recs}

    def find(self, r: Record) -> int:
        k = id(r)
        while self.up[k] != k:
            self.up[k] = self.up[self.up[k]]
            k = self.up[k]
        return k

    def join(self, a: Record, b: Record) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.up[max(ra, rb, key=lambda k: -self.rec[k].i)] = min(ra, rb, key=lambda k: -self.rec[k].i)

    def members(self) -> dict[int, list[Record]]:
        out: dict[int, list[Record]] = defaultdict(list)
        for r in self.rec.values():
            out[self.find(r)].append(r)
        return out


def link(sections: list[Section], opt: TreeOptions) -> tuple[Classes, list[Record]]:
    recs = [r for s in sections for r in _walk(s.roots)]
    recs = list({id(r): r for r in recs}.values())
    cls = Classes(recs)
    # a total over one row is that row
    for r in recs:
        for v in r.views:
            if len(v) == 1:
                cls.join(r, v[0])
    # runs of consecutive units (sections, cut after their totals) whose sum is a printed row break that row down:
    # the longest run from each start, before single sections, so a list cut by false titles (a part number above
    # each record) is still one breakdown
    # first the surest: a section whose rows add up, to the cent, to the last row of the section just before it (a
    # linked account's summary, then its detail), before any run of sections can take them by a chance sum
    for prev, sec in zip(sections, sections[1:]):
        if not prev.roots or len(sec.roots) < 2:
            continue
        head, total = prev.roots[-1], sum(r.value for r in sec.roots)
        if abs(total) >= ZERO and abs(head.value - total) < ZERO and not head.views:
            head.views.append(list(sec.roots))
    if opt.link_runs:
        _link_runs(sections, recs, cls)
    # a section whose roots add up to an earlier row breaks that row down
    in_view = {id(x) for r in recs for v in r.views for x in v}
    for s in sections:
        roots = s.roots
        if any(id(x) in in_view for x in roots):
            continue
        total = sum(r.value for r in roots)
        if abs(total) < opt.min_total:                  # a few cents match some other printed row by chance
            continue
        target = _target(recs, s, total, len(roots))
        if target is None:
            continue
        if len(roots) == 1:
            cls.join(target, roots[0])
        else:
            target.views.append(roots)
    _join_repeats(recs, cls)
    # top rows: in no group and no breakdown
    inside = set()
    for r in recs:
        inside.update(id(c) for c in r.children)
        for v in r.views:
            inside.update(id(c) for c in v)
    top = [r for r in recs if id(r) not in inside]
    # top rows that differ only by rounding are views of the same money (a pre-tax total and a grand total with
    # no tax, a total by account and a total by service)
    big = sorted((r for r in top if abs(r.value) >= 1.0), key=lambda r: -abs(r.value))
    for a, b in zip(big, big[1:]):
        if _view_close(a.value, b.value, 1):
            cls.join(a, b)
    # a total printed beside its parts: 2-3 other top rows (of other classes) add up to it
    free = sorted((r for r in top if abs(r.value) >= ZERO), key=lambda r: -abs(r.value))
    ex = Expander(cls)
    for big in free[:40]:
        # a part is smaller than the total by more than rounding (else it is another view of the total)
        cands = [r for r in free if abs(r.value) < abs(big.value) - tol() and cls.find(r) != cls.find(big)
                 and not _view_close(r.value, big.value, 1)][:40]
        best = None
        for k in range(2, opt.max_combo + 1):
            pool = cands if k == 2 else cands[:25]
            for c in itertools.combinations(pool, k):
                if len({cls.find(x) for x in c}) < k:
                    continue
                err = abs(sum(x.value for x in c) - big.value)
                if err <= tol(k, big.value):
                    score = (round(err, 2), -sum(ex.leaves(cls.find(x)) for x in c))
                    if best is None or score < best[0]:
                        best = (score, c)
            if best is not None:
                break
        if best is not None:
            big.views.append(sorted(best[1], key=lambda x: x.i))
    return cls, recs


def _view_close(a: float, b: float, n: int) -> bool:
    """Two views of the same money may differ by their own rounding: a little more than a sum of parts."""
    return abs(a - b) <= tol(n, max(abs(a), abs(b))) + 5e-6 * max(abs(a), abs(b))


def _join_repeats(recs: list[Record], cls: Classes) -> None:
    """The same label and amount printed twice is the same money printed in another place (a summary and its
    detail), but only when the rows it belongs to are the same money too: two linked accounts that each pay
    "AWS Cost Explorer 0.42", or two services that each have "US West (Oregon) 0.24", are different money. A row
    belongs to its parent, else to the total its section breaks down; rows are joined from the top down, so the
    owners are joined first."""
    owner: dict[int, Record] = {}
    for r in recs:
        for v in r.views:
            for x in v:
                owner.setdefault(id(x), r)

    def owner_of(r: Record) -> Record | None:
        return r.parent or owner.get(id(r))

    def depth(r: Record) -> int:
        d, seen, o = 0, {id(r)}, owner_of(r)
        while o is not None and id(o) not in seen:
            seen.add(id(o))
            d, o = d + 1, owner_of(o)
        return d

    def fits(a: Record | None, b: Record | None) -> bool:
        return a is None or b is None or cls.find(a) == cls.find(b)

    # each group of repeats joined so far, with its rows: a row joins a group only when it fits every row in it (a
    # row with no owner must not chain two accounts' rows together)
    groups: dict[tuple[str, float], list[list[Record]]] = defaultdict(list)
    for r in sorted(recs, key=lambda r: (depth(r), r.i)):
        k = (_key(r.label), round(r.value, 2))
        if not k[0] or abs(k[1]) < ZERO:
            continue
        mine = owner_of(r)
        for g in groups[k]:
            if all(fits(mine, owner_of(f)) for f in g):
                cls.join(g[0], r)
                g.append(r)
                break
        else:
            groups[k].append([r])


def _link_runs(sections: list[Section], recs: list[Record], cls: Classes) -> None:
    """Runs of consecutive units (a section, cut after each of its total rows) that are in no breakdown yet and
    whose sum is a printed row break that row down; from each start, the longest such run."""
    in_view = {id(x) for r in recs for v in r.views for x in v}
    units: list[list[Record]] = []
    for sec in sections:
        cur: list[Record] = []
        for r in sec.roots:
            if r.role == "total":                       # a total is a unit of its own
                if cur:
                    units.append(cur)
                units.append([r])
                cur = []
                continue
            cur.append(r)
        if cur:
            units.append(cur)
    loose = [u for u in units if not any(id(x) in in_view for x in u)]
    values = sorted(((r.value, r.i, r) for r in recs if abs(r.value) >= 1.0), key=lambda t: (t[0], t[1]))
    keys = [v for v, _, _ in values]
    i = 0
    while i < len(loose):
        best = None
        total, n = 0.0, 0
        for j in range(i, min(len(loose), i + 600)):
            total += sum(r.value for r in loose[j])
            n += len(loose[j])
            # one row is a run only when it has its own breakdown: the one service of an account's detail is
            # another view of the account's total (else any amount printed twice would be joined)
            if (n < 2 and not (n == 1 and loose[j][0].children)) or abs(total) < 1.0:
                continue
            members = {id(x) for u in loose[i:j + 1] for x in _walk(u)}
            t = _nearest_value(values, keys, total, n, loose[i][0].i, members)
            # the total of a run is printed next to it (a header or a footer): no other loose unit in between,
            # else a far row with the same sum is taken by chance
            if t is not None and t.i < loose[i][0].i and i > 0 and t.i < max(x.i for x in _walk(loose[i - 1])):
                t = None
            if t is not None and t.i > loose[i][0].i and j + 1 < len(loose) and t.i > loose[j + 1][0].i:
                t = None
            if t is not None:
                best = (j, t)
        if best is None:
            i += 1
            continue
        j, target = best
        run = [x for u in loose[i:j + 1] for x in u]
        if len(run) == 1:
            cls.join(target, run[0])                     # a view of one row is that row (as a total over one row)
        else:
            target.views.append(run)
        i = j + 1


def _nearest_value(values, keys, total: float, n: int, first: int, members: set) -> Record | None:
    """The row (not one of the run's) nearest before the run whose amount equals the run's sum."""
    import bisect
    pad = tol(n, total)            # the rounding of the parts only: a long run tried at many ends finds near sums by chance
    lo, hi = bisect.bisect_left(keys, total - pad), bisect.bisect_right(keys, total + pad)
    hits = [r for _, _, r in values[lo:hi] if id(r) not in members]
    before = [r for r in hits if r.i < first]
    if before:
        return max(before, key=lambda r: r.i)
    return min(hits, key=lambda r: r.i) if hits else None


def _target(recs: list[Record], s: Section, total: float, n: int) -> Record | None:
    """The nearest row before the section whose amount its roots add up to (else the nearest after)."""
    first, last = s.roots[0].i, max(r.i for r in _walk(s.roots))
    before = [r for r in recs if r.i < first and r.section != s.i and _view_close(r.value, total, n)]
    if before:
        return max(before, key=lambda r: r.i)
    after = [r for r in recs if r.i > last and r.section != s.i and _view_close(r.value, total, n)]
    return min(after, key=lambda r: r.i) if after else None


def _own_breakdowns(r: Record) -> list[list[Record]]:
    out = []
    if r.children and _complete(r):
        out.append(r.children)
    elif r.role == "unbalanced" and r.gap is not None:
        out.append(r.children + [r.gap])
    out += [v for v in r.views if len(v) >= 2 and _view_close(sum(x.value for x in v), r.value, len(v))]
    return out


def _has_breakdown(r: Record, cls: Classes) -> bool:
    k = cls.find(r)
    return any(_own_breakdowns(m) for m in cls.rec.values() if cls.find(m) == k)


# ---------------------------------------------------------------- 4. lines
class Expander:
    def __init__(self, cls: Classes):
        self.cls = cls
        self.members = cls.members()
        self.memo: dict[int, int] = {}

    def breakdowns(self, k: int) -> list[list[Record]]:
        out, seen = [], set()
        for m in self.members.get(k, []):
            for b in _own_breakdowns(m):
                sig = tuple(sorted(id(x) for x in b))
                if sig not in seen:
                    seen.add(sig)
                    out.append(b)
        return out

    def leaves(self, k: int, path: frozenset = frozenset()) -> int:
        if k in self.memo:
            return self.memo[k]
        if k in path:
            return 1
        bds = [b for b in self.breakdowns(k) if all(self.cls.find(x) not in path | {k} for x in b)]
        # a $0.00 line adds no detail: two views of one total ("Charges, Credits $0.00, Tax" and "service: Charges,
        # Tax") tie on the money they break down, and the tie goes to the view with fewer direct parts (the deeper one)
        own = 1 if abs(self.cls.rec[k].value) >= ZERO else 0
        n = max((sum(self.leaves(self.cls.find(x), path | {k}) for x in b) for b in bds), default=own)
        self.memo[k] = n
        return n

    def expand(self, r: Record, path: list[Record], out: list, used: set, seen: frozenset = frozenset()) -> None:
        k = self.cls.find(r)
        used.add(k)
        bds = [b for b in self.breakdowns(k) if all(self.cls.find(x) not in seen | {k} for x in b)]
        if not bds:
            out.append((r, path))
            return
        # most leaves; then the deeper view: fewer direct parts with money (a $0.00 part adds no detail)
        best = max(bds, key=lambda b: (sum(self.leaves(self.cls.find(x), seen | {k}) for x in b),
                                       -sum(1 for x in b if abs(x.value) >= ZERO), max(x.i for x in b)))
        for x in best:
            self.expand(x, path + [r], out, used, seen | {k})


def label_path(path: list[Record], cls: Classes, members: dict[int, list[Record]]) -> list[str]:
    """The labels of the classes above a line, each class's labels once (a row and its other printings)."""
    return list(path_sources(path, cls, members))


def path_sources(path: list[Record], cls: Classes, members: dict[int, list[Record]]) -> dict[str, str]:
    """label_path's labels, each with where it is printed (its first printing). Other printings of a class count
    only in the section of the row on the path: the same amount in a summary ("Highest service spend") is not a
    heading of the line."""
    out: dict[str, str] = {}
    for r in path:
        alike = members.get(cls.find(r), [r])
        own = [m for m in alike if m.section == r.section] if r.label else alike
        for m in sorted(own or alike, key=lambda m: m.i):
            if m.label and m.label not in out:
                out[m.label] = m.src
    return out


# ---------------------------------------------------------------- 5. the whole thing
def read(regions: list[Region], options: dict | None = None) -> Tree:
    opt = TreeOptions.from_spec(options)
    recs, currency = records_from(regions, opt)
    sections = build(recs, opt)
    cls, linked = link(sections, opt)
    ex = Expander(cls)
    amounts = [r for r in linked if r.amount is not None]
    used: set[int] = set()
    pairs: list[tuple[Record, list[Record]]] = []
    # the main groups: from the largest class with a breakdown down, then any other class that adds money
    order = sorted({cls.find(r): r for r in amounts}.items(), key=lambda kv: (-abs(kv[1].value), -ex.leaves(kv[0])))
    covered: set[int] = set()
    groups: list[dict] = []
    group_rows: list[tuple[set[int], set[int]]] = []    # each group's classes and leaves
    main = None
    for k, r in order:
        if k in covered or not ex.breakdowns(k):
            continue
        if main is not None:
            # another group adds money only when the groups together equal an amount printed on the document (two
            # sellers and their grand total); else it is another view of money already read, or information
            # (the groups' printed totals are the parts: their rounding only, never the rounding of every line)
            so_far = sum(g["amount_used"] for g in groups)
            # (a few cents prove nothing: they match some printed amount by chance; a $0.00 group adds no money,
            # and its lines are kept)
            if ZERO <= abs(r.value) < opt.min_total or _within(k, ex, covered, cls) \
                    or not any(abs(so_far + r.value - a.value) <= bound(len(groups) + 1)
                                                       for a in amounts if abs(a.value) >= 1.0):
                continue
        sub: list[tuple[Record, list[Record]]] = []
        ex.expand(cls.rec[k], [], sub, used)
        if main is not None and abs(r.value) < ZERO and all(not path for _, path in sub):
            continue                                    # a lone $0.00 row ("Total tax 0.00") is a total, not a line
        groups.append({"label": cls.rec[k].label[:60], "amount": cls.rec[k].amount, "src": cls.rec[k].src,
                       "lines": len(sub), "sum": round(sum(x.value for x, _ in sub), 2),
                       **_rounding(sub, cls, ex)})
        pairs += sub
        closure = _closure(k, ex, cls)
        covered |= closure
        group_rows.append((closure, {id(leaf) for leaf, _ in sub}))
        for part in {id(path[1]): path[1] for _, path in sub if len(path) > 1}.values():   # each seller's part
            group_rows.append((_closure(cls.find(part), ex, cls),
                               {id(leaf) for leaf, path in sub if len(path) > 1 and path[1] is part}))
        main = main or cls.rec[k]
    if main is None:                                    # nothing adds up: every amount row is a line
        pairs = [(r, []) for r in amounts if r.parent is None or r.parent.role != "subtotal"]
    leaf_ids = {id(leaf) for leaf, _ in pairs}
    known_rows = [m for m in amounts if cls.find(m) in covered or id(m) in leaf_ids]
    known = {round(m.value * 100) for m in known_rows}
    # a summary over another dimension: the sum of the known rows that share a label ("Highest AWS Region spend" is
    # every "Canada (Central)" subtotal added up), in the whole bill or in one group (one seller's part)
    by_label: dict[tuple, list[float]] = defaultdict(list)
    seen_ids: set[tuple] = set()
    for m in known_rows:
        if not m.label:
            continue
        scopes = [None] + [g for g, (cl, lv) in enumerate(group_rows) if cls.find(m) in cl or id(m) in lv]
        for g in scopes:
            if (g, id(m)) not in seen_ids:
                seen_ids.add((g, id(m)))
                by_label[(g, _key(m.label))].append(m.value)
    sums = [(sum(v), len(v)) for v in by_label.values() if len(v) >= 2]
    unexplained = [r for r in amounts if cls.find(r) not in covered and id(r) not in leaf_ids
                   and abs(r.value) >= ZERO and not _equal_to_known(r, known)
                   and not any(close(total, r.value, n) for total, n in sums)]
    # information printed beside the bill's money (the spec says what it looks like) is not money
    info_rx = re.compile(opt.informational, re.I) if opt.informational else None
    informational = [r for r in unexplained if info_rx and info_rx.search(r.label)]
    unexplained = [r for r in unexplained if not (info_rx and info_rx.search(r.label))]
    subtotals = [k for k in {cls.find(r) for r in amounts} if ex.breakdowns(k)]
    failed = [r for r in amounts if r.role == "unbalanced" and r.gap is not None and abs(r.gap.value) >= ZERO]
    agree = [cls.rec[k] for k in subtotals if len(ex.breakdowns(k)) >= 2]
    lines = [{"label": leaf.label, "amount": leaf.amount, "extra": leaf.extra, "src": leaf.src,
              # the group's own total (the document total) is not context: the path starts below it
              "path": label_path(path[1:], cls, ex.members), "path_src": path_sources(path[1:], cls, ex.members),
              "section": _title(sections, leaf.section),
              "x": leaf.x, "amounts": leaf.amounts, "cells": leaf.cells, "columns": leaf.columns}
             for leaf, path in pairs]
    total = round(sum(leaf.value for leaf, _ in pairs), 2)
    evidence = {"reconcile": _reconcile(groups, amounts, total),
        "records": len(amounts), "lines": len(pairs), "sum": total, "currency": currency, "groups": groups,
        "top_total": None if main is None else {"label": main.label, "amount": main.amount, "src": main.src},
        "subtotals_ok": len(subtotals) - len(failed), "subtotals_failed": [_brief(r) for r in failed][:20],
        # the totals above the lines (each line's path: a service's total, a section's total) that their lines prove
        "subtotals_used": _used_totals(pairs, main)[:2000],
        "gap_amount": round(sum(abs(r.gap.value) for r in failed), 2),
        "views_agree": [_brief(r) for r in agree][:20],
        "unexplained": [_brief(r) for r in unexplained][:30], "unexplained_count": len(unexplained),
        "informational": [_brief(r) for r in informational][:10],
        "unexplained_amount": round(sum(abs(r.value) for r in unexplained), 2),
        # rows that print an amount in the main currency and in another one ("USD 2,291.58 CAD 3,237.70")
        "converted": [{"label": r.label[:80], "amount": r.amount, "other": v, "other_currency": cur, "src": r.src}
                      for r in amounts for v, cur in r.other[:1] if len(r.amounts) == 1][:50],
        # a printed row that is a line more than once is money counted twice
        "duplicate_lines": [_brief(leaf) for leaf in {id(leaf): leaf for leaf, _ in pairs}.values()
                            if sum(1 for x, _ in pairs if x is leaf) > 1][:20],
    }
    return Tree(recs, sections, lines, evidence, currency, facts_from(regions))


def _reconcile(groups: list[dict], amounts: list[Record], total: float) -> dict | None:
    """The proof of the lines: the printed total, the lines, and the rounding at each printed total between them.
    printed = lines + rounding to the cent when the lines are exactly the leaves of the totals; any other difference is
    money counted twice or not at all ("unexplained")."""
    if not groups:
        return None
    parts = sum(g["amount_used"] for g in groups)
    rounding = sum(g["rounding"] for g in groups)
    at = sorted((x for g in groups for x in g["rounding_at"]), key=lambda x: -abs(x["rounding"]))
    over = [x for g in groups for x in g["over_bound"]]
    label, amount, src = groups[0]["label_used"], groups[0]["amount_used"], groups[0]["src_used"]
    if len(groups) > 1:                                 # several groups: the printed amount that is their sum
        hit = min((a for a in amounts if abs(a.value) >= 1.0), key=lambda a: abs(a.value - parts), default=None)
        if hit is not None and abs(hit.value - parts) <= bound(len(groups)):
            label, amount, src = hit.label[:80], hit.value, hit.src
            rounding += hit.value - parts
        else:
            label, amount, src = "the groups together (no printed total)", parts, None
    unexplained = round(amount - total - rounding, 2)
    return {"label": label, "amount": round(amount, 2), "src": src, "lines": total, "rounding": round(rounding, 2),
            "unexplained": unexplained, "rounding_at": at[:10], "over_bound": over[:20],
            "proved": abs(unexplained) < ZERO and not over and src is not None}


def _rounding(sub: list[tuple[Record, list[Record]]], cls: Classes, ex: Expander) -> dict:
    """Account for every cent between a group's printed total and its lines. Each printed total above the lines
    differs from the sum of its direct parts by its rounding; added up over the tree, the roundings are exactly the
    printed total minus the lines. A total printed more than once with different cents (a total by account and a
    grand total) is one total: it is tested with the printing nearest its parts, and that printing is also its value
    as a part of the total above it. A rounding above bound() is not rounding."""
    kids: dict[int, dict[tuple, Record]] = defaultdict(dict)   # class -> its parts: ("node", class) or ("leaf", id)
    rep: dict[int, Record] = {}
    for leaf, path in sub:
        keys = [cls.find(a) for a in path]
        for k, a in zip(keys, path):
            rep.setdefault(k, a)
        for k, nxt in zip(keys, keys[1:]):
            kids[k][("node", nxt)] = rep[nxt]
        if keys:
            kids[keys[-1]][("leaf", id(leaf))] = leaf
    chosen: dict[int, Record] = {}

    def value(key: tuple, r: Record) -> float:
        return printed(key[1]).value if key[0] == "node" else r.value

    def printed(k: int) -> Record:
        if k not in chosen:
            chosen[k] = rep[k]                          # (a cycle cannot happen: expand never repeats a class)
            s = sum(value(key, r) for key, r in kids[k].items())
            chosen[k] = min(ex.members.get(k, [rep[k]]), key=lambda m: abs(m.value - s))
        return chosen[k]

    top_key = cls.find(sub[0][1][0]) if sub and sub[0][1] else None
    at, over = [], []
    for k in kids:
        parts = kids[k]
        s = sum(value(key, r) for key, r in parts.items())
        p = printed(k)
        d = round(p.value - s, 2)
        if abs(d) >= ZERO:
            item = {"label": p.label[:80], "amount": p.amount, "src": p.src, "parts": len(parts),
                    "children": round(s, 2), "rounding": d}
            at.append(item)
            if abs(d) > bound(len(parts)):
                over.append(item)
    at.sort(key=lambda x: -abs(x["rounding"]))
    used = printed(top_key) if top_key is not None else (sub[0][0] if sub else None)
    return {"amount_used": used.value if used else 0.0, "label_used": used.label[:80] if used else "",
            "src_used": used.src if used else None, "rounding": round(sum(x["rounding"] for x in at), 2),
            "rounding_at": at[:20], "over_bound": over[:20]}


def _closure(k: int, ex: Expander, cls: Classes) -> set[int]:
    """Every class inside a class's breakdowns, all of them (explained by the line items)."""
    out, todo = set(), [k]
    while todo:
        c = todo.pop()
        if c in out:
            continue
        out.add(c)
        for b in ex.breakdowns(c):
            todo += [cls.find(x) for x in b]
        for m in ex.members.get(c, []):
            todo += [cls.find(x) for x in m.children]
    return out


def _within(k: int, ex: Expander, covered: set[int], cls: Classes) -> bool:
    """A class all of whose parts are already covered adds no money."""
    for b in ex.breakdowns(k):
        if all(cls.find(x) in covered for x in b):
            return True
    return False


def _equal_to_known(r: Record, known: set[int]) -> bool:
    """A summary that repeats an amount we have (a "highest service spend" line)."""
    return round(r.value * 100) in known


def _used_totals(pairs: list, main: Record | None) -> list[dict]:
    """The printed totals the lines were read under (not the document total), each once, with what its lines add up to."""
    sums: dict[int, float] = defaultdict(float)
    recs: dict[int, Record] = {}
    for leaf, path in pairs:
        for r in path:
            if r is main or r.amount is None:
                continue
            recs[id(r)] = r
            sums[id(r)] += leaf.value
    return [{"label": r.label[:80], "amount": r.amount, "src": r.src, "children": round(sums[i], 2)}
            for i, r in sorted(recs.items(), key=lambda kv: kv[1].i)]


def _brief(r: Record) -> dict:
    return {"label": r.label[:80], "amount": r.amount, "src": r.src,
            **({"children": round(r.acc, 2)} if r.children else {})}


def _title(sections: list[Section], i: int) -> str:
    return next((s.title for s in sections if s.i == i), "")
