"""billparse's result as invoice-parser's output contract (schema.py): a Document of charges, sections, stated totals,
commitments and gaps, every value with its cite (where it is printed in the file).

build() runs after each parse, from the parse summary, its lines and the file read (docmodel). It also returns the
meta the web app shows (lane, flags, notes, timings) and the source bundle (each page's lines with their boxes, each
table's first rows) that the source viewer reads. rereconcile() checks the totals again after a person's edits.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from decimal import Decimal, InvalidOperation

import pandas as pd

from .docmodel import Document as FileDoc
from .docmodel import Region
from .schema import Charge, Cite, Commitment, Confidence, Context, Document, Gap, Section, StatedFact, StatedTotal, cv
from .transforms import parse_number

MAX_CHARGES = int(os.environ.get("CONTRACT_MAX_CHARGES", "50000"))  # more lines are rolled up (see _rollup)
MAX_TABLE_ROWS = 5000                  # rows of a table kept for the source viewer (more are read from the file)
MAX_SERVICE_GAPS = 500                 # one gap per charge with no service code, up to this many
TOLERANCE = Decimal("0.05")
ROUNDING = "rounding"           # the stated fact with the rounding between the printed total and its lines

KIND_OF_CHARGE_TYPE = {"Tax": "tax", "Credit": "discount_or_credit", "Refund": "discount_or_credit",
                       "Support": "support_fee", "Fee": "other", "Adjustment": "other"}
MARKETPLACE = re.compile(r"\bmarketplace\b|^saas$", re.I)
TAX = re.compile(r"\b(tax|vat|gst|mwst|ust)\b", re.I)
STATUS = {"reconciled": "auto_accepted", "unreconciled": "auto_accepted", "auxiliary": "auto_accepted",
          "needs_review": "needs_review", "unsupported": "needs_review", "approved": "approved"}
DOC_TYPE = {"price_sheet": "estimate", "recommendations": "other", "inventory": "inventory"}
TEXT_FIELDS = ("pricing_model", "commitment_id", "instance_type", "operating_system", "database_engine",
               "deployment_option", "usage_type", "unit")
# the dimensions a roll-up keeps: lines that differ in none of them are one charge
ROLLUP_KEYS = ["service_code", "service_name", "service_raw", "region", "account_id", "charge_type", "pricing_model",
               "instance_type", "operating_system", "database_engine", "deployment_option", "usage_type", "unit",
               "commitment_id", "currency", "provider"]


# ---------------------------------------------------------------- cites
class Locator:
    """A line's source reference ("page3:12", "Sheet1:15", "a.zip/b.xlsx/Sheet1:15") as a cite."""

    def __init__(self, doc: FileDoc | None):
        self.kind: dict[str, str] = {}
        self.page: dict[str, int] = {}
        self.text: dict[tuple[str, int], str] = {}
        for r in (doc.regions if doc else []):
            self.kind[r.name] = r.kind
            if r.kind == "page":
                self.page[r.name] = len(self.page) + 1
                for row in r.rows:
                    self.text[(r.name, row.index)] = row.text

    def cite(self, src: object, text: str | None = None) -> Cite:
        s = "" if src is None or (isinstance(src, float) and src != src) else str(src)
        name, _, num = s.rpartition(":")
        if not name or not num.isdigit():
            return Cite(text=text or (s or None))
        n = int(num)
        if name in self.page:
            return Cite(page=self.page[name], line=n, text=self.text.get((name, n), text))
        return Cite(table=name, row=n, text=text)


# ---------------------------------------------------------------- values
def _dec(v) -> Decimal | None:
    if v is None or (isinstance(v, float) and v != v) or v is pd.NA:
        return None
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return None
    return None if d.is_nan() else d


def _str(v) -> str | None:
    if v is None or v is pd.NA or (isinstance(v, float) and v != v):
        return None
    s = str(v).strip()
    return s or None


def _date(v) -> str | None:
    s = _str(v)
    return s[:10] if s else None


def kind_of(charge_type: str | None, text: str) -> str:
    market = bool(MARKETPLACE.search(text or ""))
    if charge_type == "Purchase":
        return "marketplace_fee" if market else "commitment_fee"
    if charge_type in KIND_OF_CHARGE_TYPE:
        return KIND_OF_CHARGE_TYPE[charge_type]
    return "marketplace_usage" if market else "usage"


def lane_of(spec: dict | None) -> str:
    spec = spec or {}
    route, sid = spec.get("route") or "", spec.get("id")
    if route == "builtin":
        return f"export:{sid}"
    if route == "learned":
        return f"spec:{sid}"
    if route.startswith("llm"):
        return "llm"
    return "rules" if route == "mapper" else (route or "none")


def status_of(status: str | None) -> str:
    return STATUS.get(status or "", status or "")


# ---------------------------------------------------------------- build
def build(summary: dict, rows: pd.DataFrame | None, file_doc: FileDoc | None) -> tuple[Document, dict, dict]:
    """(document, meta, bundle) for one parse."""
    loc = Locator(file_doc)
    rows = rows if rows is not None else pd.DataFrame()
    rolled = len(rows) > MAX_CHARGES and "billed_cost" in rows
    lines = len(rows)
    if rolled:
        rows = _rollup(rows)
    doc = Document(source_name=summary.get("file") or "", sha256=summary.get("sha256") or "",
                   page_count=max(1, len(loc.page)))
    head = Cite(text="the file as a whole")
    kind = summary.get("kind") or "cost_lines"
    doc.doc_type = cv(DOC_TYPE.get(kind) or ("invoice" if summary.get("file_type") in ("pdf", "docx", "txt") else
                                             "cost_report"), head, "inferred", None, "rule")
    sections = _sections(doc, rows)
    doc.charges = [_charge(i, r, loc, sections) for i, r in enumerate(rows.to_dict("records"))]
    for top in ("provider", "currency"):
        if top in rows and rows[top].notna().any():
            value = Counter(str(v) for v in rows[top].dropna()).most_common(1)[0][0]
            setattr(doc, top, cv(value, head, "inferred", None, "rule"))
    found = (summary.get("analysis") or {}).get("currency") or (summary.get("evidence") or {}).get("currency")
    if doc.currency is None and found:                 # the lines carry none: the document's (a printed $ sign)
        doc.currency = cv(str(found), head, "inferred", None, "rule")
    if "charge_start" in rows and rows["charge_start"].notna().any():
        doc.billing_period_start = cv(min(_date(v) for v in rows["charge_start"].dropna()), head, "inferred", None, "rule")
    end_col = "charge_end" if "charge_end" in rows and rows["charge_end"].notna().any() else "charge_start"
    if end_col in rows and rows[end_col].notna().any():
        doc.billing_period_end = cv(max(_date(v) for v in rows[end_col].dropna()), head, "inferred", None, "rule")
    if doc.billing_period_start is None:              # a printed billing period ("Billing period: Apr 1 - Apr 30, 2025")
        _period(doc, summary, loc)
    _currency(doc, summary, loc)
    doc.commitments = _commitments(doc.charges)
    if kind == "cost_lines":                           # an inventory or a price sheet prints counts, not money
        _totals(doc, summary, loc)
    _gaps(doc, summary, loc)
    rereconcile(doc, summary.get("completeness"))
    failed = [f"{c['name']}: {c.get('detail')}" for c in summary.get("checks") or [] if c.get("status") == "fail"]
    if summary.get("completeness") == "contradicted" and doc.completeness != "contradicted":
        doc.completeness = "contradicted"                 # billparse's other checks failed: never a better verdict
        doc.confidence = Confidence(level="low", reasons=[*failed, *(doc.confidence.reasons if doc.confidence else [])])
    flags = [{"message": w} for w in summary.get("warnings") or []]
    if rolled:
        flags.append({"message": f"{lines:,} lines were rolled up into {len(doc.charges):,} charges (lines alike in "
                                 f"service, region, account, charge type and the other fields are one charge; its "
                                 f"'lines' attribute counts them)."})
    if summary.get("status") == "unsupported":
        flags.append({"message": "; ".join(f for f in (_feedback(summary) or ["The file could not be read."]))})
    spec = summary.get("spec") or {}
    meta = {
        "lane": lane_of(spec), "decider": None, "version": "billparse",
        "specs_used": [f"{spec['id']}@v{spec.get('version') or 1}"] if spec.get("id") else [],
        "timings_ms": {"parse": int((summary.get("seconds") or 0) * 1000)},
        "flags": flags, "notes": [{"message": f} for f in _feedback(summary)],
        "llm_calls": [], "decider_errors": [], "tables": [],
        "cost_usd": {"total": 0.0},
        "attempts": summary.get("attempts") or [],
        "lines": lines,
    }
    return doc, meta, bundle_of(file_doc)


def _feedback(summary: dict) -> list[str]:
    out = []
    for c in summary.get("checks") or []:
        if c.get("status") in ("fail", "warn") and c.get("detail"):
            out.append(f"{c['name']}: {c['detail']}")
    return out


def _rollup(rows: pd.DataFrame) -> pd.DataFrame:
    """Lines alike in every dimension (ROLLUP_KEYS) as one line: their cost and quantity added, how many counted."""
    keys = [k for k in ROLLUP_KEYS if k in rows]
    df = rows.copy()
    for k in keys:
        df[k] = df[k].astype("string").fillna("")
    df["billed_cost"] = pd.to_numeric(df["billed_cost"], errors="coerce").fillna(0.0)
    agg: dict = {"billed_cost": "sum", "_src": "first"} if "_src" in df else {"billed_cost": "sum"}
    if "quantity" in df:
        df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")
        agg["quantity"] = "sum"
    for c in ("charge_start",):
        if c in df:
            agg[c] = "min"
    if "charge_end" in df:
        agg["charge_end"] = "max"
    if "description" in df:
        agg["description"] = "first"
    out = df.groupby(keys, dropna=False, sort=False).agg(agg)
    out["_lines"] = df.groupby(keys, dropna=False, sort=False).size()
    out = out.reset_index()
    for k in keys:
        out[k] = out[k].replace("", None)
    return out.sort_values("billed_cost", key=lambda s: -s.abs()).reset_index(drop=True)


def _sections(doc: Document, rows: pd.DataFrame) -> dict[str, str]:
    """The headings above the lines (their path "A > B > C") as nested sections; path -> the deepest section id."""
    out: dict[str, str] = {}
    if "_path" not in rows:
        return out
    by_key: dict[tuple, str] = {}
    for path in rows["_path"].dropna().unique():
        parts = [p.strip() for p in str(path).split(" > ") if p.strip()]
        parent = None
        for depth in range(len(parts)):
            key = tuple(parts[:depth + 1])
            if key not in by_key:
                sid = f"s{len(by_key) + 1}"
                by_key[key] = sid
                doc.sections.append(Section(id=sid, name=cv(parts[depth], Cite(text=parts[depth]), "printed"),
                                            parent_id=parent))
            parent = by_key[key]
        if parent:
            out[str(path)] = parent
    return out


def _charge(i: int, r: dict, loc: Locator, sections: dict[str, str]) -> Charge:
    desc = _str(r.get("description")) or _str(r.get("service_raw")) or "—"
    cite = loc.cite(r.get("_src"), desc)
    heading = _str(r.get("service_raw"))
    if _str(r.get("_path")) and heading and heading.lower() not in desc.lower():
        desc = f"{heading} - {desc}"                   # "Charges" under its service: "AWS Lambda - Charges"
    cost = _dec(r.get("billed_cost"))
    text = " ".join(str(x) for x in (r.get("service_raw"), r.get("description"), r.get("_extra")) if _str(x))
    c = Charge(id=f"c{i + 1}", section_id=sections.get(str(r.get("_path"))) if _str(r.get("_path")) else None,
               cite=cite, description=cv(desc, cite), kind=cv(kind_of(_str(r.get("charge_type")), text), cite,
                                                             "inferred", None, "rule"))
    if cost is not None:
        c.cost, c.basis = cv(cost, cite), "billed"
    if (cur := _str(r.get("currency"))):
        c.currency = cv(cur, cite, "inferred")
    service = _str(r.get("service_name")) or _str(r.get("service_raw"))
    at_service = loc.cite(r["_service_src"], heading) if _str(r.get("_service_src")) else cite
    at_account = loc.cite(r["_account_src"]) if _str(r.get("_account_src")) else cite
    how = _str(r.get("resolved_by"))
    c.context = Context(
        service=cv(service, at_service, "printed" if service == heading else "inferred", None,
                   f"mapping:{how}" if how and service != heading else None) if service else None,
        region=_region(r, loc, cite),
        account_id=cv(_str(r.get("account_id")) or _str(r.get("account_name")), at_account)
        if (_str(r.get("account_id")) or _str(r.get("account_name"))) else None)
    if (code := _str(r.get("service_code"))):
        c.service_code = cv(code, at_service, "inferred", None, f"mapping:{how}" if how else "mapping")
    for f in TEXT_FIELDS:
        if (v := _str(r.get(f))):
            setattr(c, f, cv(v, cite, "printed"))
    for f in ("quantity", "unit_price"):
        if (d := _dec(r.get(f))) is not None:
            setattr(c, f, cv(d, cite))
    if (d := _date(r.get("charge_start"))):
        c.period_start = cv(d, cite)
    if (d := _date(r.get("charge_end"))):
        c.period_end = cv(d, cite)
    if (p := _str(r.get("product_code"))):
        c.sku_id = cv(p, cite)
    attrs: dict = {}
    for k in ("service_raw", "category", "resolved_by", "charge_type", "account_name", "region_name", "effective_cost",
              "list_cost", "credits", "tax", "invoice_id", "price_per", "resource_type", "storage_gb", "_lines"):
        v = r.get(k)
        if _str(v) is not None:
            attrs[k.lstrip("_")] = v if isinstance(v, (int, float)) else str(v)
    if (extra := _str(r.get("_extra"))):
        for part in extra.split(" | "):
            k, sep, v = part.partition(": ")
            attrs[k if sep else f"text{len(attrs)}"] = v if sep else part
    if _str(r.get("_path")):
        attrs["under"] = str(r["_path"])
    c.attributes = attrs
    return c


PERIOD_NAME = re.compile(r"\b(billing|statement|invoice|service)?\s*period\b", re.I)
DATE_SPAN = re.compile(r"^\s*(?P<a>[A-Za-z]{3,9}\.? \d{1,2})(?:,? (?P<ya>\d{4}))?\s*(?:-|–|to)\s*"
                       r"(?P<b>[A-Za-z]{3,9}\.? \d{1,2}),? (?P<yb>\d{4})\s*$|"
                       r"^\s*(?P<c>\d{4}[-/]\d{2}[-/]\d{2})\s*(?:-|–|to)\s*(?P<d>\d{4}[-/]\d{2}[-/]\d{2})\s*$")


RATE = re.compile(r"1\s*([A-Z]{3})\s*=\s*(\d+(?:\.\d+)?)\s*([A-Z]{3})")


def _currency(doc: Document, summary: dict, loc: Locator) -> None:
    """A printed exchange rate ("1 USD = 1.41287168557 CAD") and the amounts printed in the other currency, as
    stated facts beside the main-currency charges."""
    srcs = summary.get("header_field_src") or {}
    for name, value in (summary.get("header_fields") or {}).items():
        if (m := RATE.search(str(value))):
            doc.exchange_rate = cv(Decimal(m.group(2)), loc.cite(srcs.get(name), str(value)))
            break
    for i, x in enumerate((summary.get("evidence") or {}).get("converted") or []):
        cite = loc.cite(x.get("src"), x.get("label"))
        doc.stated_facts.append(StatedFact(id=f"fx{i + 1}", label=str(x.get("label") or ""), currency=x.get("other_currency"),
                                           amount=cv(_dec(x.get("other")), cite), cite=cite))  # type: ignore[arg-type]


def _period(doc: Document, summary: dict, loc: Locator) -> None:
    """The billing period from a header field that names a period and prints a date range."""
    srcs = summary.get("header_field_src") or {}
    for name, value in (summary.get("header_fields") or {}).items():
        m = DATE_SPAN.match(str(value)) if PERIOD_NAME.search(str(name)) else None
        if not m:
            continue
        if m["c"]:
            start, end = m["c"].replace("/", "-"), m["d"].replace("/", "-")
        else:
            start = pd.to_datetime(f"{m['a']} {m['ya'] or m['yb']}", errors="coerce")
            end = pd.to_datetime(f"{m['b']} {m['yb']}", errors="coerce")
            if pd.isna(start) or pd.isna(end):
                continue
            start, end = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
        cite = loc.cite(srcs.get(name), str(value))
        doc.billing_period_start, doc.billing_period_end = cv(start, cite), cv(end, cite)
        return


def _region(r: dict, loc: Locator, cite: Cite):
    """The region code; when it comes from a printed name ("Canada (Central)"), it is inferred and cited where the
    name is printed."""
    code, name = _str(r.get("region")), _str(r.get("region_name"))
    if not code:
        return None
    at = loc.cite(r["_region_src"], name) if _str(r.get("_region_src")) else cite
    if name and name != code:
        return cv(code, at, "inferred", None, "mapping:region")
    return cv(code, at)


def _commitments(charges: list[Charge]) -> list[Commitment]:
    groups: dict[str, list[Charge]] = {}
    for c in charges:
        if c.commitment_id:
            groups.setdefault(str(c.commitment_id.value), []).append(c)
    out = []
    for i, (cid, cs) in enumerate(groups.items(), 1):
        text = " ".join(str(c.pricing_model.value) if c.pricing_model else str(c.description.value) for c in cs).lower()
        kind = "savings_plan" if "saving" in text else "reserved_instance" if "reserv" in text else "commitment"
        amount = sum((c.cost.value for c in cs if c.cost and c.kind.value == "commitment_fee"), Decimal(0))
        out.append(Commitment(id=f"k{i}", commitment_type=cv(kind, cs[0].cite, "inferred", None, "rule"),
                              commitment_id=cv(cid, cs[0].cite), amount=cv(amount, cs[0].cite, "inferred") if amount else None,
                              charge_ids=[c.id for c in cs], cite=cs[0].cite))
    return out


def _totals(doc: Document, summary: dict, loc: Locator) -> None:
    """The printed totals: each one billparse found (a grand total is tested in rereconcile), and the tree's subtotals
    that do or do not add up."""
    seen: set[tuple] = set()

    def add(label: str, amount, src, level: str, status: str = "untested", computed=None) -> None:
        d = _dec(parse_number(amount)) if not isinstance(amount, Decimal) else amount
        if d is None or (label, d) in seen:
            return
        seen.add((label, d))
        cite = loc.cite(src, label)
        doc.stated_totals.append(StatedTotal(
            id=f"t{len(doc.stated_totals) + 1}", label=cv(label, cite), amount=cv(d, cite), level=level,  # type: ignore[arg-type]
            status=status, computed=None if computed is None else _dec(parse_number(computed))))  # type: ignore[arg-type]
    for t in summary.get("stated_totals") or []:
        if t.get("kind") in ("document_total", "llm_claim"):   # another route's reading, or the LLM's own sum
            continue
        add(str(t.get("label") or "Total"), t.get("amount"), t.get("src"),
            "subtotal" if t.get("kind") == "subtotal" else "grand")
        if t.get("rounding") is not None and not any(f.id == ROUNDING for f in doc.stated_facts):
            # the tree proved the lines against this total: lines + rounding = printed, to the cent
            cite = loc.cite(t.get("src"), str(t.get("label") or ""))
            doc.stated_facts.append(StatedFact(
                id=ROUNDING, label=f"Rounding between {t.get('label') or 'the total'} and its lines",
                amount=cv(_dec(t["rounding"]), cite, "inferred", None, "rule"), cite=cite))  # type: ignore[arg-type]
    ev = summary.get("evidence") or {}
    for r in ev.get("subtotals_failed") or []:
        add(r.get("label") or "Subtotal", r.get("amount"), r.get("src"), "subtotal", "mismatch", r.get("children"))
    for r in ev.get("views_agree") or []:
        add(r.get("label") or "Subtotal", r.get("amount"), r.get("src"), "subtotal", "match", r.get("children"))
    for r in ev.get("subtotals_used") or []:          # a service's total (tax included) over its lines
        printed, lines = _dec(parse_number(r.get("amount"))), _dec(r.get("children"))
        ok = printed is not None and lines is not None and abs(printed - lines) <= TOLERANCE
        add(r.get("label") or "Subtotal", r.get("amount"), r.get("src"), "subtotal", "match" if ok else "mismatch",
            r.get("children"))


def _gaps(doc: Document, summary: dict, loc: Locator) -> None:
    ev = summary.get("evidence") or {}
    currency = str(doc.currency.value) if doc.currency else None
    for r in ev.get("unexplained") or []:
        doc.gaps.append(Gap(kind="unclaimed_amount", amount=_dec(r.get("amount")), currency=currency,
                            cite=loc.cite(r.get("src"), r.get("label")),
                            message=f"{r.get('label') or 'An amount'} ({r.get('amount')}) is printed but is in no "
                                    f"line or total"))
    for t in doc.stated_totals:
        if t.level == "subtotal" and t.status == "mismatch":
            doc.gaps.append(Gap(kind="unreconciled_total", ref=t.id, cite=t.amount.cite, currency=currency,
                                amount=None if t.computed is None else t.amount.value - t.computed,
                                message=f"{t.label.value}: printed {t.amount.value}, its lines add up to {t.computed}"))
    # a line of $0.00 puts no money at risk: no gap for its service
    missing = [c for c in doc.charges if c.kind.value == "usage" and c.service_code is None and c.cost is not None
               and c.cost.value != 0]
    for c in missing[:MAX_SERVICE_GAPS]:
        label = str(c.context.service.value) if c.context.service else str(c.description.value)
        doc.gaps.append(Gap(kind="missing_match_field", field="service_code", ref=c.id, cite=c.cite,
                            message=f"No service code for “{label[:80]}”"))


# ---------------------------------------------------------------- checks after edits
def counted_total(doc: Document) -> Decimal:
    return doc.counted_total()


def rereconcile(doc: Document, completeness: str | None = None) -> Document:
    """Test the printed totals against the counted charges again (as billparse's stated_total check: one printed total
    equal to the sum, the sum before tax or the sum plus the printed tax; else subtotals that add up to the lines), then
    set completeness and confidence."""
    counted = [c for c in doc.charges if c.counted and c.cost]
    total = sum((c.cost.value for c in counted), Decimal(0))  # type: ignore[union-attr]
    non_tax = sum((c.cost.value for c in counted if c.kind.value != "tax"), Decimal(0))  # type: ignore[union-attr]
    tax = sum((t.amount.value for t in doc.stated_totals if TAX.search(str(t.label.value))), Decimal(0))
    rounding = next((f.amount.value for f in doc.stated_facts if f.id == ROUNDING), None)
    if rounding is not None:                            # proved by the tree: lines + its rounding, to the cent
        targets, tol = [total + rounding], Decimal("0.005")
    else:                                               # n lines printed to the cent: half a cent each at most
        targets = [total, non_tax, total + tax]
        tol = min(max(TOLERANCE, abs(total) * Decimal("0.0005")), Decimal("0.005") * (len(counted) + 1))
    doc.gaps = [g for g in doc.gaps if not (g.kind == "unreconciled_total" and
                                            any(g.ref == t.id and t.level == "grand" for t in doc.stated_totals))]
    grand = [t for t in doc.stated_totals if t.level == "grand"]
    proved = False
    for t in grand:
        t.status, t.computed, t.covers = "untested", None, []
    for t in sorted(grand, key=lambda t: -abs(t.amount.value)):
        hit = next((v for v in targets if abs(t.amount.value - v) <= tol), None)
        if hit is not None:
            t.status, t.covers, proved = "match", [c.id for c in counted], True
            t.computed = total if rounding is not None else hit   # the charges; the rounding is a stated fact
            break
    subs = [t for t in doc.stated_totals if t.level == "subtotal" and t.computed is None]
    if not grand and subs and abs(sum(t.amount.value for t in subs) - non_tax) <= tol:
        for t in subs:
            t.status = "match"
        proved = True
    if grand and not proved:
        t = max(grand, key=lambda t: abs(t.amount.value))
        t.status, t.computed, t.covers = "mismatch", total, [c.id for c in counted]
        doc.gaps.append(Gap(kind="unreconciled_total", ref=t.id, cite=t.amount.cite, amount=t.amount.value - total,
                            message=f"{t.label.value}: printed {t.amount.value}, the counted charges add up to {total}"))
    failed = any(t.status == "mismatch" for t in doc.stated_totals)
    if failed:
        doc.completeness = "contradicted"
    elif proved:
        doc.completeness = "reconciled"
    elif completeness in ("consistent", "unchecked", "contradicted"):
        doc.completeness = completeness  # type: ignore[assignment]
    else:
        doc.completeness = "consistent" if doc.charges else "unchecked"
    doc.confidence = confidence_for(doc)
    return doc


def confidence_for(doc: Document) -> Confidence:
    reasons = []
    for t in doc.stated_totals:
        if t.status == "mismatch":
            reasons.append(f"{t.label.value}: printed {t.amount.value}, computed {t.computed}")
    unclaimed = [g for g in doc.gaps if g.kind == "unclaimed_amount"]
    if unclaimed:
        reasons.append(f"{len(unclaimed)} printed amount(s) are in no charge or total")
    no_code = sum(1 for g in doc.gaps if g.kind == "missing_match_field")
    if no_code:
        reasons.append(f"{no_code} charge(s) have no service code")
    if not doc.charges:
        reasons.append("No charge was read")
    if doc.completeness == "reconciled" and not reasons:
        return Confidence(level="high", reasons=["The charges add up to the printed total(s)"])
    if doc.completeness == "consistent" and not [r for r in reasons if "service code" not in r]:
        return Confidence(level="medium", reasons=["The file prints no total; every amount was read", *reasons])
    return Confidence(level="low", reasons=reasons or [f"Completeness: {doc.completeness}"])


def money_at_risk(doc: Document) -> Decimal:
    """Money tied to open gaps: their amounts plus every counted charge a gap points at (as invoice-parser)."""
    risk = Decimal(0)
    refs = {g.ref for g in doc.gaps if g.ref}
    for g in doc.gaps:
        if g.amount is not None:
            risk += abs(g.amount)
    for c in doc.charges:
        if c.cost and c.counted and c.id in refs:
            risk += abs(c.cost.value)
    return risk


# ---------------------------------------------------------------- source bundle
def bundle_of(file_doc: FileDoc | None) -> dict:
    """What the source viewer shows: each page's lines (with boxes) and each table's first rows."""
    if file_doc is None:
        return {}
    lines, tables, page = [], [], 0
    for r in file_doc.regions:
        if r.kind == "page":
            page += 1
            for row in r.rows:
                cells = [c for c in row.cells if c.text]
                bbox = [min(c.x0 for c in cells), row.top, max(c.x1 for c in cells), row.top + 9.0] if cells else None
                lines.append({"page": page, "line": row.index, "text": row.text, "bbox": bbox})
        elif r.is_frame or r.rows:
            tables.append(bundle_of_region(r))
    return {"fmt": file_doc.file_type, "page_count": max(1, page), "lines": lines, "tables": tables}


def bundle_of_region(r: Region, max_rows: int | None = MAX_TABLE_ROWS) -> dict:
    """One table of the source viewer: its first max_rows rows (None: all of them)."""
    return _frame_table(r, max_rows) if r.is_frame else _sheet_table(r, max_rows)


def _frame_table(r: Region, max_rows: int | None) -> dict:
    df = r.df
    assert df is not None
    head = [str(c) for c in df.columns]
    part = df if max_rows is None else df.head(max_rows - 1)
    body = part.astype(object).where(part.notna(), None).values.tolist()
    return {"name": r.name, "row_offset": (r.header_row or 0) + 1, "col_letters": False,
            "truncated": max_rows is not None and len(df) + 1 > max_rows,
            "rows": [head, *[[_cell(v) for v in row] for row in body]]}


def _sheet_table(r: Region, max_rows: int | None) -> dict:
    first = r.rows[0].index
    rows_by_index = {row.index: row for row in r.rows}
    last = r.rows[-1].index if max_rows is None else min(r.rows[-1].index, first + max_rows - 1)
    base = 0 if r.name.endswith("json-meta") else 1   # spreadsheet cells keep their column number (A = 1) in x0
    out = []
    for i in range(first, last + 1):
        row = rows_by_index.get(i)
        cells: list[str] = []
        for c in (row.cells if row else []):
            col = int(round(c.x0)) - base
            if 0 <= col < 200:
                cells.extend([""] * (col + 1 - len(cells)))
                cells[col] = c.text
        out.append(cells)
    return {"name": r.name, "row_offset": first, "col_letters": True,
            "truncated": r.rows[-1].index > last, "rows": out}


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v)) if abs(v) < 1e15 else str(v)
    return str(v)
