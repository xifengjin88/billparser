"""Rule-based spec generation, tried before any LLM call.

Scores every (column, canonical field) pair from two kinds of evidence:
header words, and what the values look like (currency, dates, codes, text).
Also recognizes month-pivot sheets (services x months) and list-style PDF lines.

The header -> field scorer is the first thing to replace with a trained model;
every accepted spec adds labeled (header, sample values, field) examples.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

from .docmodel import Document, Region, is_numberish
from .layout import build_columns, find_header, norm_h, _row_to_cols
from .transforms import parse_number

LEGACY_RULES: dict[str, list[tuple[str, float]]] = {
    "billed_cost": [(r"\b(extended|line total|net amount|billed cost|total cost|cost|amount|amt|charges?|subtotal|total)\b", 2.5),
                    (r"\b(monthly|mtd|month to date|current month)\b", 2.0),
                    (r"\b(value|spend|price total)\b", 1.5)],
    "instance_type": [(r"\b(instance type|vm size|machine type|instance size|size|sku|flavor|profile)\b", 2.5)],
    "operating_system": [(r"\b(operating system|os|platform)\b", 3.0)],
    "database_engine": [(r"\b(database engine|db engine|engine)\b", 3.0)],
    "unit_price": [(r"\b(unit price|unit cost|unit rate|effective price|price per|rate)\b", 3.0), (r"^price$", 2.5)],
    "quantity": [(r"\b(qty|quantity|usage quantity|units used|hours|consumed)\b", 3.0), (r"\busage\b", 1.5)],
    "unit": [(r"\b(unit of measure|uom|usage unit|unit)\b", 2.5)],
    "service_raw": [(r"\b(service name|service|svc|product name|meter category|category|offering)\b", 3.0),
                    (r"\b(product|item)\b", 2.0)],
    "description": [(r"\b(description|desc|details?|line item|item|sku name|sku|meter name|meter|name)\b", 2.5)],
    "product_code": [(r"\b(product code|product id|productid|sku id|part number|service code|meter id|code)\b", 3.0)],
    "charge_start": [(r"\b(usage date|start date|from|date|period|month|billing period)\b", 2.0)],
    "charge_end": [(r"\b(end date|to date|end|through)\b", 2.5)],
    "region": [(r"\b(region|location)\b", 3.0)],
    "account_id": [(r"\b(account id|subscription id|project id|account number|subscription|project)\b", 2.5)],
    "account_name": [(r"\b(account name|subscription name|project name|customer|account)\b", 2.0)],
    "currency": [(r"\b(currency|ccy)\b", 3.0)],
    "charge_type": [(r"\b(charge type|line item type|cost type|type)\b", 2.5)],
    "tax": [(r"\b(tax|vat|gst)\b", 3.0)],
    "invoice_id": [(r"\b(invoice no|invoice number|invoice id|invoice)\b", 2.5)],
    "pricing_model": [(r"\b(pricing model|purchase option|pricing plan|commitment type|purchase type)\b", 3.5)],
    "usage_type": [(r"\b(usage type)\b", 3.5)],
    "deployment_option": [(r"\b(deployment option|deployment type|availability)\b", 3.0)],
    "commitment_id": [(r"\b(reservation id|savings plan id|commitment id|benefit id|reservation arn|savings plan arn)\b", 3.5)],
}
LEGACY_NEGATIVE = {
    "billed_cost": r"\b(unit|rate|list|tax|qty|quantity|per|code|id|date|currency|type)\b",
    "quantity": r"\b(price|cost|amount|rate)\b",
    "description": r"\b(id|code|date|price|cost|amount)\b",
    "service_raw": r"\b(id|code|price|cost|amount)\b",
    "unit": r"\b(price|cost|rate)\b",
}
HEADER_RULES: dict[str, list[tuple[str, float]]] = {
    "billed_cost": [(r"\b(extended|line total|net amount|billed cost|total cost|cost|amount|amt|charges?|subtotal|total)\b", 2.5),
                    (r"\b(monthly|mtd|month to date|current month)\b", 2.0),
                    (r"\b(value|spend|price total)\b", 1.5)],
    "instance_type": [(r"\b(instance type|vm size|machine type|instance size|size|flavor|profile)\b", 2.5)],
    "operating_system": [(r"\b(operating system|os|platform)\b", 3.0)],
    "database_engine": [(r"\b(database engine|db engine|engine)\b", 3.0)],
    "unit_price": [(r"\b(unit price|unit cost|unit rate|effective price|price per|rate)\b", 3.0), (r"^price$", 2.5)],
    "quantity": [(r"\b(qty|quantity|usage quantity|units used|hours|consumed)\b", 3.0), (r"\busage\b", 1.5)],
    "unit": [(r"\b(unit of measure|uom|usage unit|unit)\b", 2.5)],
    "service_raw": [(r"\b(service name|service|svc|product name|meter category|category|offering)\b", 3.0),
                    (r"\b(product|item)\b", 2.0)],
    "description": [(r"\b(description|desc|details?|line item|item|sku name|meter name|meter|name)\b", 2.5)],
    "product_code": [(r"\b(product code|product id|productid|service code|code)\b", 3.0)],
    "charge_start": [(r"\b(usage date|start date|from|date|period|month|billing period)\b", 2.0)],
    "charge_end": [(r"\b(end date|to date|end|through)\b", 2.5)],
    "region": [(r"\b(region|location)\b", 3.0)],
    "account_id": [(r"\b(account id|subscription id|project id|account number|subscription|project)\b", 2.5)],
    "account_name": [(r"\b(account name|subscription name|project name|customer|account)\b", 2.0)],
    "currency": [(r"\b(currency|ccy)\b", 3.0)],
    "charge_type": [(r"\b(charge type|line item type|cost type|type)\b", 2.5)],
    "tax": [(r"\b(tax|vat|gst)\b", 3.0)],
    "invoice_id": [(r"\b(invoice no|invoice number|invoice id|invoice)\b", 2.5)],
    # purchase option and commitment type have fields of their own (below)
    "pricing_model": [(r"\b(pricing model|pricing plan|purchase type)\b", 3.5)],
    "usage_type": [(r"\b(usage type)\b", 3.5)],
    "deployment_option": [(r"\b(deployment option|deployment type|availability)\b", 3.0)],
    "commitment_id": [(r"\b(reservation id|savings plan id|commitment id|benefit id|reservation arn|savings plan arn)\b", 3.5)],
    # task 10: every other field a file can print (coverage is derived, never read)
    "list_cost": [(r"\b(list cost|on ?demand cost|public cost|cost at list|retail cost)\b", 3.5)],
    "effective_cost": [(r"\b(effective cost|amorti[sz]ed cost|net amorti[sz]ed)\b", 3.5)],
    "contracted_cost": [(r"\b(contracted cost|negotiated cost)\b", 3.5)],
    "list_unit_price": [(r"\b(list unit price|list price|public price|on ?demand rate|on ?demand price|retail price|"
                         r"payg price)\b", 3.5)],
    "contracted_unit_price": [(r"\b(contracted unit price|contracted price|negotiated price|ea price)\b", 3.5)],
    "credits": [(r"\b(credits?|promotions?|promo)\b", 3.0)],
    "resource_id": [(r"\b(resource id|instance id|resource uri|arn)\b", 3.0), (r"\bresource\b", 2.0)],
    "sku_id": [(r"\b(sku id|product sku|part number)\b", 3.5), (r"^sku$", 3.0)],
    "sku_price_id": [(r"\b(sku price id|rate code|price id|offer term rate code)\b", 3.5)],
    "meter_id": [(r"\b(meter id|meter guid)\b", 3.5)],
    "operation": [(r"\boperation\b", 3.0)],
    "tenancy": [(r"\btenancy\b", 3.0)],
    "license_model": [(r"\b(license model|licence model|license type|ahb|azure hybrid benefit)\b", 3.0)],
    "pre_installed_sw": [(r"\b(pre ?installed sw|pre ?installed software)\b", 3.0), (r"^software$", 2.0)],
    "purchase_option": [(r"\b(purchase option|payment option|upfront)\b", 3.5)],
    "pricing_term": [(r"\b(pricing term|term type)\b", 3.5)],
    "commitment_type": [(r"\b(commitment type|commitment discount type|reservation type|benefit type)\b", 3.5)],
    "commitment_term": [(r"\b(commitment term|term length|lease length)\b", 3.5), (r"^term$", 2.0)],
    "commitment_status": [(r"\b(commitment status|utili[sz]ation status)\b", 3.5)],
    "provider": [(r"\b(cloud provider|provider|vendor)\b", 3.0), (r"^cloud$", 2.0)],
    "line_type_raw": [(r"\b(line item type|record type|charge category)\b", 3.0)],
}
_COST_NOT = r"\b(unit|rate|price|qty|quantity|per|code|id|date|currency|type)\b"
NEGATIVE = {
    "billed_cost": r"\b(unit|rate|list|tax|qty|quantity|per|code|id|date|currency|type|on ?demand|public|retail|"
                   r"amorti[sz]ed|effective|contracted|negotiated|credits?|promo)\b",
    "quantity": r"\b(price|cost|amount|rate)\b",
    "description": r"\b(id|code|date|price|cost|amount)\b",
    "service_raw": r"\b(id|code|price|cost|amount|charge)\b",     # ChargeCategory is the line type
    "unit": r"\b(price|cost|rate)\b",
    "unit_price": r"\b(list|public|on ?demand|retail|payg|contracted|negotiated)\b",
    "list_cost": _COST_NOT, "effective_cost": _COST_NOT, "contracted_cost": _COST_NOT, "credits": _COST_NOT,
    "resource_id": r"\b(group|type|name|tags?)\b",
    "commitment_term": r"\b(pricing|type)\b",
    "charge_type": r"\b(record|category|commitment|discount)\b",
}
COST_FIELDS = {"billed_cost", "list_cost", "effective_cost", "contracted_cost", "credits", "tax"}
PRICE_FIELDS = ("unit_price", "list_unit_price", "contracted_unit_price")
NUMERIC_FIELDS = {"billed_cost", "unit_price", "quantity", "tax", "list_cost", "effective_cost", "contracted_cost",
                  "credits", "list_unit_price", "contracted_unit_price"}
TEXT_FIELDS = {"service_raw", "description", "unit", "region", "account_name", "charge_type", "currency",
               "instance_type", "operating_system", "database_engine", "pricing_model", "usage_type",
               "deployment_option", "commitment_id", "tenancy", "license_model", "pre_installed_sw",
               "purchase_option", "pricing_term", "commitment_type", "commitment_status", "provider",
               "line_type_raw"}
CODE_FIELDS = {"resource_id", "sku_id", "sku_price_id", "meter_id", "operation"}
SETTLED = 3.0                                       # a column mapped with this score or more is settled
SHARED = (("charge_type", "line_type_raw"),)        # one column may feed both (the CUR line type does)
INVENTORY_FIELDS = {"instance_type", "operating_system", "database_engine"}
MONTH_RX = re.compile(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?(\s*['-]?\s*\d{2,4})?$|"
                      r"^\d{4}[-/]\d{1,2}$|^\d{1,2}[-/]\d{4}$", re.I)
CODE_RX = re.compile(r"^[A-Z0-9][A-Z0-9_\-:/]{5,}$")
CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
TOTALISH = re.compile(r"\b(sub\s*total|subtotal|total|amount due|balance due)\b", re.I)


@dataclass
class Profile:
    numeric: float
    currency_sym: float
    date: float
    code: float
    distinct: float
    avg_len: float
    letters: float = 0.0                              # share of values with a letter ("1 Hour" is a unit)


def profile(values: list) -> Profile:
    vals = [str(v).strip() for v in values if v is not None and str(v).strip() not in ("", "nan", "None")]
    if not vals:
        return Profile(0, 0, 0, 0, 0, 0)
    n = len(vals)
    num = sum(1 for v in vals if is_numberish(v)) / n
    cur = sum(1 for v in vals if re.search(r"[$€£¥]|\.\d{2}$", v)) / n
    dates = pd.to_datetime(pd.Series(vals), errors="coerce", format="mixed")
    date = float(dates.notna().mean()) if num < 0.9 or any("-" in v or "/" in v for v in vals) else 0.0
    code = sum(1 for v in vals if CODE_RX.match(v) and not is_numberish(v)) / n
    letters = sum(1 for v in vals if re.search(r"[A-Za-z]", v)) / n
    return Profile(num, cur, date, code, len(set(vals)) / n, sum(map(len, vals)) / n, letters)


def header_score(field: str, header: str, rules: dict | None = None, negative: dict | None = None) -> float:
    rules, negative = rules or HEADER_RULES, negative if negative is not None else NEGATIVE
    h = norm_h(CAMEL.sub(" ", str(header)))           # "ChargeType" reads as "charge type"
    best = 0.0
    for rx, w in rules.get(field, ()):
        if re.search(rx, h):
            best = max(best, w)
    if best and field in negative and re.search(negative[field], h):
        best -= 2.0
    return best


def value_score(field: str, p: Profile) -> float:
    if field in NUMERIC_FIELDS:
        if p.numeric < 0.6 or p.date > 0.8:
            return -5.0
        bonus = 1.0 if field in ("billed_cost", "unit_price") and p.currency_sym > 0.5 else 0.0
        return 1.0 + bonus
    if field in ("charge_start", "charge_end"):
        return 1.5 if p.date > 0.7 else -5.0
    if field == "product_code":
        return 1.0 if p.code > 0.6 else -2.0
    if field in CODE_FIELDS:                          # identifiers: anything but dates
        return -5.0 if p.date > 0.7 else 0.5
    if field == "unit":                               # "1 Hour", "1 GB/Month", "10K": numbers with a letter
        return 0.5 if p.letters > 0.8 else -5.0     # (pandas reads "1 Hour" as a date: no date test)
    if field in TEXT_FIELDS:
        if p.numeric > 0.5 or p.date > 0.7:
            return -5.0
        if field == "description":
            return (1.0 if p.avg_len > 6 else 0.0) + (0.5 if p.distinct > 0.8 else 0.0)
        if field == "service_raw":
            return 1.0 if p.distinct < 0.7 else 0.3
        return 0.5
    return 0.0


@dataclass
class MapContext:
    """What one parse's mapper may use beyond the headers: the header dictionary (headers.py) and the price catalog
    (the detectors of instance types and SKUs). known: the catalog's answers, kept for the parse."""
    dictionary: object | None = None
    catalog: object | None = None
    known: dict = field(default_factory=dict)


@dataclass
class Mapping:
    chosen: dict[str, str]                          # field -> header
    confidence: float
    scores: dict[str, float]
    how: dict[str, str] = field(default_factory=dict)       # field -> rule | dictionary | detector
    warnings: list[str] = field(default_factory=list)

    @property
    def settled(self) -> dict[str, str]:
        return {f: h for f, h in self.chosen.items() if self.scores.get(f, 0) >= SETTLED}


def map_columns(columns: dict[str, list], ctx: MapContext | None = None,
                legacy: bool = False) -> tuple[dict[str, str], float, dict]:
    """columns: header -> sample values. Returns (field -> header, confidence, scores). legacy: the rules of before
    task 10 only, no dictionary and no detectors: spec.py guesses with them the columns a spec leaves out, so the
    results of built-in and learned specs never change."""
    m = mapping(columns, ctx, legacy)
    return m.chosen, m.confidence, m.scores


def mapping(columns: dict[str, list], ctx: MapContext | None = None, legacy: bool = False) -> Mapping:
    """Scores every (column, field) pair: a header rule, a header dictionary hit (headers.py; even when no rule
    matches), a detector of the values (detect: never a cost or a quantity); each plus value_score. Pairs are taken
    greedily by score, one field per column; a rejected pair is never taken. Two unit-price columns are told apart
    by line math (unit_prices)."""
    from . import detect
    rules, negative = (LEGACY_RULES, LEGACY_NEGATIVE) if legacy else (HEADER_RULES, NEGATIVE)
    ctx = ctx or MapContext()
    d = None if legacy else ctx.dictionary
    profiles = {h: profile(v) for h, v in columns.items()}
    pairs = []                                       # (score, source rank: rule 2 > dictionary 1 > detector 0, field, header)
    guids: list[str] = []
    for h, p in profiles.items():
        ruled = set()
        for f in rules:
            hs = header_score(f, h, rules, negative)
            if hs <= 0:
                continue
            ruled.add(f)
            pairs.append((hs + value_score(f, p), 2, f, h))
        if d is not None and (hit := d.lookup(h)):
            pairs.append((hit[1] + value_score(hit[0], p), 1, hit[0], h))
        if not legacy:
            for f in detect.fields(h, columns[h], ctx, ruled):
                if f == "meter_id":                  # after the others (below): a GUID is an account first
                    guids.append(h)
                    continue
                pairs.append((detect.SCORE + value_score(f, p), 0, f, h))
    if d is not None:
        pairs = [x for x in pairs if not d.rejected(x[3], x[2])]
    pairs.sort(key=lambda x: (x[0], x[1], x[2], x[3]), reverse=True)    # equal scores: as before task 10
    chosen: dict[str, str] = {}
    how: dict[str, str] = {}
    used: set[str] = set()
    scores: dict[str, float] = {}
    for s, rank, f, h in pairs:
        if s < 2.0 or f in chosen or h in used:
            continue
        chosen[f], scores[f], how[f] = h, s, ("detector", "dictionary", "rule")[rank]
        used.add(h)
    m = Mapping(chosen, 0.0, scores, how)
    if guids and "meter_id" not in chosen and _azure(columns):
        h = next((g for g in guids if g not in used and not (d is not None and d.rejected(g, "meter_id"))), None)
        if h is not None:
            chosen["meter_id"], scores["meter_id"], how["meter_id"] = h, detect.SCORE, "detector"
            used.add(h)
    if not legacy:
        for a, b in SHARED:                          # one column, two fields
            for x, y in ((a, b), (b, a)):
                if x in chosen and y not in chosen and header_score(y, chosen[x]) > 0 \
                        and not (d is not None and d.rejected(chosen[x], y)):
                    chosen[y], scores[y], how[y] = chosen[x], scores[x], how[x]
        unit_prices(m, columns, profiles, d)
    if "billed_cost" not in chosen:                            # rightmost money-looking column
        money = [h for h, p in profiles.items() if h not in used and p.numeric > 0.8 and p.currency_sym > 0.5
                 and not (d is not None and d.rejected(h, "billed_cost"))]
        if money:
            chosen["billed_cost"], scores["billed_cost"], how["billed_cost"] = money[-1], 2.0, "rule"
    conf = 0.0
    if "billed_cost" in chosen:
        conf = 0.5 + 0.25 * bool({"service_raw", "description"} & set(chosen))
        conf += 0.25 * min(1.0, sum(scores.values()) / (4.5 * max(1, len(scores))))
    m.confidence = round(conf, 2)
    return m


def _azure(columns: dict[str, list]) -> bool:
    """An Azure file: a subscription or meter header, or Azure resource paths in a column."""
    if any(re.search(r"\b(subscription|meter)\b", norm_h(CAMEL.sub(" ", str(h)))) for h in columns):
        return True
    return any(str(v).lower().startswith("/subscriptions/") for vals in columns.values() for v in vals[:20])


LIST_WORDS = re.compile(r"\b(list|public|on ?demand|retail|payg|pay as you go)\b")


def unit_prices(m: Mapping, columns: dict[str, list], profiles: dict, d=None) -> None:
    """Two or more unit-price columns (Azure's UnitPrice and EffectivePrice): the one whose quantity x price is the
    line's cost (within 1%, on the sample rows) is unit_price; another is list_unit_price when its header says list,
    public or on demand, else contracted_unit_price. None fits: the mapping stays, with a warning."""
    chosen = m.chosen
    taken = {h for f, h in chosen.items() if f not in PRICE_FIELDS}
    cands = [h for h in columns if h not in taken and profiles[h].numeric > 0.6
             and any(header_score(f, h) > 0 for f in PRICE_FIELDS)]
    if len(cands) < 2 or "quantity" not in chosen or "billed_cost" not in chosen:
        return
    qty, cost = amounts_aligned(columns[chosen["quantity"]]), amounts_aligned(columns[chosen["billed_cost"]])

    def fit(h: str) -> float:
        price = amounts_aligned(columns[h])
        rows = [(q, p, c) for q, p, c in zip(qty, price, cost) if None not in (q, p, c) and c != 0]
        if not rows:
            return 0.0
        return sum(abs(q * p - c) <= 0.01 * abs(c) for q, p, c in rows) / len(rows)
    fits = {h: fit(h) for h in cands}
    best = max(cands, key=lambda h: fits[h])
    if fits[best] < 0.8:
        m.warnings.append(f"Two or more unit-price columns ({', '.join(cands)}) and none is the cost divided by the "
                          "quantity: the mapping by header stands")
        return
    for f in PRICE_FIELDS:
        if chosen.get(f) in cands:
            del chosen[f], m.scores[f]
    out = {"unit_price": best}
    for h in cands:
        if h != best:
            f = "list_unit_price" if LIST_WORDS.search(norm_h(CAMEL.sub(" ", h))) else "contracted_unit_price"
            if f not in out and not (d is not None and d.rejected(h, f)):
                out[f] = h
    for f, h in out.items():
        chosen[f], m.scores[f], m.how[f] = h, max(SETTLED, m.scores.get(f, 0.0)), "line math"


def amounts_aligned(values: list) -> list[float | None]:
    return [parse_number(v) for v in values]


# ---------------------------------------------------------------- spec builders
TREE_SPEC = {"source": "tree", "meta": {"kind": "cost_lines", "cost_basis": "billed", "layout": "tree"}, "tree": {}}


def propose_all(doc: Document, ctx: MapContext | None = None) -> list[tuple[dict, float, Mapping | None]]:
    """Every rule-based spec worth trying, best first, each with the column mapping it came from (None: the tree,
    a pivot, list lines). Documents of pages and text lines (PDF, text) first try the tree of sums (tree.py), which
    reads totals, subtotals and lines from their amounts."""
    out: list[tuple[dict, float, Mapping | None]] = []
    if any(r.kind == "page" and r.rows for r in doc.regions):
        out.append((dict(TREE_SPEC, tree={}), 0.7, None))
    spec, conf, m = propose_mapped(doc, ctx)
    if spec:
        out.append((spec, conf, m))
    return out


def propose(doc: Document, ctx: MapContext | None = None, legacy: bool = False) -> tuple[dict | None, float]:
    """Best rule-based spec for a document, with confidence 0..1. legacy: the rules of before task 10 (the second,
    independent reading that checks a learned or LLM spec: its verdicts do not change)."""
    spec, conf, _ = propose_mapped(doc, ctx, legacy)
    return spec, conf


def propose_mapped(doc: Document, ctx: MapContext | None = None,
                   legacy: bool = False) -> tuple[dict | None, float, Mapping | None]:
    best: tuple[dict | None, float, Mapping | None] = (None, 0.0, None)
    best_size = (0, 0)
    for region in doc.regions:
        if region.scanned:
            continue
        cand = _propose_frame(region, ctx, legacy) if region.is_frame else _propose_grid(region, ctx, legacy)
        if not cand:
            continue
        # near-equal confidence: the table that fills more fields, then has more rows (the detail, not a pivot of it)
        size = (len(cand[0].get("columns") or {}), len(region.df) if region.is_frame else len(region.rows))
        if cand[1] > best[1] + 0.05 or (cand[1] >= best[1] - 0.05 and size > best_size):
            best, best_size = cand, size
    if best[1] < 0.6 and doc.file_type == "pdf":
        lines = propose_lines(doc)
        if lines and lines[1] > best[1]:
            best = (*lines, None)
    return best


def _samples_frame(region: Region, n: int = 200) -> dict[str, list]:
    df = region.df.head(n)
    return {str(c): df[c].tolist() for c in df.columns}


def _propose_frame(region: Region, ctx: MapContext | None = None,
                   legacy: bool = False) -> tuple[dict, float, Mapping | None] | None:
    cols = _samples_frame(region)
    pivot = _propose_pivot(cols, source="frame")
    if pivot:
        return (*pivot, None)
    m = mapping(cols, ctx, legacy)
    if "billed_cost" not in m.chosen:
        inv = _inventory(m.chosen, "frame", list(cols))
        return (*inv, m) if inv else None
    return _spec_from_mapping(m.chosen, source="frame", headers=list(cols)), m.confidence, m


def _propose_grid(region: Region, ctx: MapContext | None = None,
                  legacy: bool = False) -> tuple[dict, float, Mapping | None] | None:
    hdr = find_header(region)
    if hdr is None:
        return None
    cols = build_columns(region, hdr)
    samples: dict[str, list] = {c.name: [] for c in cols}
    for row in region.rows[hdr + 1: hdr + 80]:
        if TOTALISH.search(row.text) or (row.bold and not any(is_numberish(c.text) for c in row.cells)):
            continue                                           # subtotal and group rows would skew profiles
        for k, v in _row_to_cols(row, cols, region.kind).items():
            samples[k].append(v)
    pivot = _propose_pivot(samples, source="grid")
    if pivot:
        return (*pivot, None)
    m = mapping(samples, ctx, legacy)
    chosen, conf = m.chosen, m.confidence
    if "billed_cost" not in chosen:
        inv = _inventory(chosen, "grid", [c.name for c in cols])
        return (*inv, m) if inv else None
    has_bold_groups = any(r.bold and not any(is_numberish(c.text) for c in r.cells)
                          for r in region.rows[hdr + 1: hdr + 80])
    spec = _spec_from_mapping(chosen, source="grid", headers=[c.name for c in cols], groups=True)
    if has_bold_groups:
        spec["notes"] = "Group headers detected; carried into service_raw when no service column exists."
    return spec, conf, m


def _inventory(chosen: dict[str, str], source: str, headers: list[str]) -> tuple[dict, float] | None:
    """A table of resources with no cost (VM sizes, OS, regions): read as an inventory, not dropped."""
    if not (INVENTORY_FIELDS & set(chosen)):
        return None
    key = next(f for f in ("instance_type", "database_engine", "operating_system") if f in chosen)
    spec = {"source": source, "detect": {"required": [chosen[key]]},
            "meta": {"kind": "inventory", "cost_basis": "none"},
            "columns": {f: {"from": [h]} for f, h in chosen.items()},
            "filters": [{"drop_if_blank": key}]}
    if source == "grid":
        spec["table"] = {"continuation": "drop", "multi_page": True}
    return spec, 0.6


def _spec_from_mapping(chosen: dict[str, str], source: str, headers: list[str], groups: bool = False) -> dict:
    columns = {f: {"from": [h]} for f, h in chosen.items()}
    if groups and "service_raw" not in chosen:
        # hierarchical invoices: the bold group row is the service, lines below are SKUs
        fallback = [chosen["description"]] if "description" in chosen else []
        columns["service_raw"] = {"from": ["_group", *fallback], "coalesce": True}
    elif groups:
        columns["service_raw"] = {"from": [chosen["service_raw"], "_group"], "coalesce": True}
    required = [chosen["billed_cost"]] + ([chosen[k] for k in ("service_raw", "description") if k in chosen][:1])
    spec = {"source": source, "detect": {"required": required},
            "meta": {"kind": "cost_lines", "cost_basis": "billed"},
            "columns": columns, "filters": [{"drop_if_blank": "billed_cost"}]}
    if source == "grid":
        spec["table"] = {"group_header": {"carry_to": "_group"}, "continuation": "merge", "multi_page": True}
    return spec


def _propose_pivot(cols: dict[str, list], source: str) -> tuple[dict, float] | None:
    months = [h for h in cols if MONTH_RX.match(str(h).strip())]
    if len(months) < 3:
        return None
    label = next((h for h in cols if h not in months and profile(cols[h]).numeric < 0.3), None)
    if label is None:
        return None
    spec = {"source": source, "detect": {"required": [label, *months[:2]]},
            "meta": {"kind": "cost_lines", "cost_basis": "billed", "layout": "month_pivot"},
            "reshape": {"unpivot": {"id": label, "exclude": r"(^|\b)total\b|^col_\d+$",
                                    "total_row": r"^\s*(grand\s+)?total\b"}},
            "columns": {"service_raw": {"from": ["_id"]}, "billed_cost": {"from": ["_value"]},
                        "charge_start": {"from": ["_var"]}},
            "filters": [{"drop_if_blank": "billed_cost"},
                        {"drop_if_match": {"field": "service_raw", "regex": r"^\s*(sub)?total\b"}}]}
    if source == "grid":
        spec["table"] = {"group_header": {"carry_to": "_group"}, "continuation": "drop", "stop_at_total": False}
    return spec, 0.8


# ---------------------------------------------------------------- list-style PDFs
LINE_PATTERNS = [
    r"^(?P<service_raw>[A-Za-z][^$]*?[A-Za-z0-9)])\s*\.{2,}\s*[$€£]?\s*(?P<billed_cost>\(?-?[\d,]+\.\d{2}\)?)$",
    r"^(?P<service_raw>[A-Za-z][^$]*?)\s+(?P<quantity>[\d,]+(?:\.\d+)?)\s+(?P<unit>[A-Za-z][A-Za-z\-]*(?:\s[A-Za-z]+)?)\s+"
    r"[$€£]?\s*(?P<unit_price>[\d,]+\.\d{2,6})\s+[$€£]?\s*(?P<billed_cost>\(?-?[\d,]+\.\d{2}\)?)$",
    r"^(?P<service_raw>[A-Za-z][^$]*?[A-Za-z0-9)])\s+[$€£]\s?(?P<billed_cost>\(?-?[\d,]+\.\d{2}\)?)$",
    r"^(?P<service_raw>[A-Za-z][^$]*?[A-Za-z)])\s+(?P<billed_cost>\(?-?[\d,]+\.\d{2}\)?)\s*(USD|EUR|GBP)?$",
]
TOTAL_FIELD_ANCHOR = r"(total\s+due|amount\s+due|balance\s+due|grand\s+total|invoice\s+total|total\s+charges)"


def propose_lines(doc: Document) -> tuple[dict, float] | None:
    texts = [r.text for reg in doc.regions if not reg.is_frame for r in reg.rows]
    money_lines = [t for t in texts if re.search(r"\d\.\d{2}\)?\s*(USD|EUR|GBP)?$", t)]
    if len(money_lines) < 2:
        return None
    best, best_n = None, 0
    for pat in LINE_PATTERNS:
        rx = re.compile(pat)
        n = sum(1 for t in money_lines if rx.search(t))
        if n > best_n:
            best, best_n = pat, n
    if not best:
        return None
    coverage = best_n / len(money_lines)
    spec = {"source": "lines", "lines": [{"pattern": best}],
            "detect": {}, "meta": {"kind": "cost_lines", "cost_basis": "billed", "layout": "lines"},
            "columns": {"service_raw": {"from": ["service_raw"]}, "billed_cost": {"from": ["billed_cost"]},
                        **({"quantity": {"from": ["quantity"]}, "unit": {"from": ["unit"]},
                            "unit_price": {"from": ["unit_price"]}} if "quantity" in best else {})},
            "fields": {"invoice_total": {"anchor": TOTAL_FIELD_ANCHOR, "take": "right_of",
                                         "regex": r"([\d,]+\.\d{2})"}},
            "totals": {"from_fields": ["invoice_total"]},
            "filters": [{"drop_if_match": {"field": "service_raw", "regex": TOTAL_FIELD_ANCHOR + r"|^\s*(sub)?total\b"}}]}
    return spec, round(min(0.85, 0.45 + 0.4 * coverage), 2)


def amounts_in(values: list) -> list[float]:
    return [x for x in (parse_number(v) for v in values) if x is not None]
